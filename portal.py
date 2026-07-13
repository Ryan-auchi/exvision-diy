"""portal.py — captive portal como MODO (não bloqueante).

Reaproveita o phew (access point + DNS catch-all + servidor web) para receber
fotos já cortadas em 240x320 pelo navegador e gravá-las em /sd/fotos.

Diferente do script standalone: aqui o portal sobe como TAREFAS asyncio via
`start()` e é derrubado por `stop()` (que também desliga o Wi-Fi). Assim o
loop principal continua vigiando o encoder para sair do modo portal.

O corte/redimensionamento acontece no navegador (www/index.html); o Pico só
grava o JPEG recebido — ele não redimensiona.
"""

import os
import gc
import network
import uasyncio as asyncio
import usocket

from phew import dns, server


AP_SSID = "EX-VISION"
AP_PASSWORD = None            # rede aberta: dispara o popup do portal cativo
FOTOS_DIR = "/sd/fotos"       # fotos ficam no cartão SD
WWW_DIR = "www"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif")

ap_ip = "192.168.4.1"

_ap = None
_server = None
_dns_sock = None
_dns_task = None


# ---------------------------------------------------------------------------
# utilitários
# ---------------------------------------------------------------------------

def _ensure_dir():
    # cria /sd/fotos (assume o SD já montado em /sd pelo main.py)
    try:
        os.stat(FOTOS_DIR)
    except OSError:
        try:
            os.mkdir(FOTOS_DIR)
        except OSError:
            pass


def _safe_name(filename):
    return filename.replace("\\", "/").split("/")[-1]


def _extract(header, token):
    if token not in header:
        return None
    return header.split(token, 1)[1].split('"', 1)[0]


def _free_space():
    try:
        fs = os.statvfs(FOTOS_DIR)
        return "{:.1f} KB".format((fs[0] * fs[3]) / 1024)
    except OSError:
        return "?"


def _gallery_html():
    _ensure_dir()
    try:
        files = [f for f in os.listdir(FOTOS_DIR) if f.lower().endswith(IMAGE_EXT)]
    except OSError:
        files = []
    if not files:
        return "<p>Nenhuma imagem.</p>"
    tiles = []
    for name in sorted(files):
        size_kb = os.stat(FOTOS_DIR + "/" + name)[6] / 1024
        tiles.append(
            "<div class='tile'>"
            "<img data-src='/download/{name}' src=''>"
            "<div class='tile-info'>"
            "<strong>{name}</strong><br>"
            "<small>{size:.1f} KB</small><br>"
            "<a href='/delete?name={name}' class='delete-btn'>Excluir</a>"
            "</div></div>".format(name=name, size=size_kb)
        )
    return "\n".join(tiles)


# ---------------------------------------------------------------------------
# upload multipart em streaming (grava direto no SD, seguro p/ binário)
# ---------------------------------------------------------------------------

async def _parse_form_data_streaming(reader, headers):
    boundary = b"--" + headers["content-type"].split("boundary=")[1].strip().encode()
    delimiter = b"\r\n" + boundary
    tail = len(delimiter)
    form = {}

    _ensure_dir()
    await reader.readline()   # descarta "--boundary" inicial

    while True:
        field_headers = await server._parse_headers(reader)
        if not field_headers:
            break

        disposition = field_headers.get("content-disposition", "")
        name = _extract(disposition, 'name="') or "campo"
        filename = _extract(disposition, 'filename="')

        if filename:
            saved = _safe_name(filename)
            buf = b""
            with open(FOTOS_DIR + "/" + saved, "wb") as f:
                while True:
                    chunk = await reader.read(256)
                    if not chunk:
                        f.write(buf)
                        break
                    buf += chunk
                    idx = buf.find(delimiter)
                    if idx != -1:
                        f.write(buf[:idx])
                        break
                    if len(buf) > tail:
                        f.write(buf[:-tail])
                        buf = buf[-tail:]
            form[name] = saved
            gc.collect()
            break
        else:
            value = b""
            while True:
                line = await reader.readline()
                stripped = line.rstrip()
                if stripped == boundary or stripped == boundary + b"--":
                    break
                value += line
            form[name] = value.decode().strip()

    return form


server._parse_form_data = _parse_form_data_streaming


# ---------------------------------------------------------------------------
# rotas
# ---------------------------------------------------------------------------

@server.route("/", methods=["GET"])
def index(request):
    with open(WWW_DIR + "/index.html", "r") as f:
        html = f.read()
    html = html.replace("{{IMAGES}}", _gallery_html())
    html = html.replace("{{FREE_SPACE}}", _free_space())
    return html.encode("utf-8"), 200, "text/html; charset=utf-8"


@server.route("/style.css", methods=["GET"])
def style(request):
    return server.serve_file(WWW_DIR + "/style.css")


@server.route("/download/<name>", methods=["GET"])
def image(request, name):
    return server.serve_file(FOTOS_DIR + "/" + _safe_name(name))


@server.route("/upload", methods=["POST"])
def upload(request):
    saved = request.form.get("file") if request.form else None
    print("Upload concluido:", saved)
    return b"OK", 200, "text/plain"


@server.route("/delete", methods=["GET"])
def delete(request):
    name = request.query.get("name")
    if name:
        try:
            os.remove(FOTOS_DIR + "/" + _safe_name(name))
        except OSError as e:
            print("Erro ao excluir:", e)
    return server.redirect("/", 302)


@server.catchall()
def catchall(request):
    return server.redirect("http://" + ap_ip + "/", 302)


# ---------------------------------------------------------------------------
# controle do modo (start/stop) — Wi-Fi só fica ligado enquanto o portal roda
# ---------------------------------------------------------------------------

def _access_point():
    ap = network.WLAN(network.AP_IF)
    ap.active(False)
    if AP_PASSWORD:
        ap.config(essid=AP_SSID, password=AP_PASSWORD)
    else:
        ap.config(essid=AP_SSID)
    ap.active(True)
    retry = 0
    while not ap.active() and retry < 20:
        import time
        time.sleep_ms(100)
        retry += 1
    return ap


async def start():
    """Liga o Wi-Fi e sobe DNS + servidor web como tarefas. Devolve o IP."""
    global _ap, _server, _dns_sock, _dns_task, ap_ip
    _ensure_dir()
    gc.collect()

    _ap = _access_point()
    ap_ip = _ap.ifconfig()[0]

    # DNS catch-all (replica o phew, mas guardando socket/tarefa p/ cancelar)
    _dns_sock = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM)
    _dns_sock.setblocking(False)
    _dns_sock.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
    _dns_sock.bind(usocket.getaddrinfo(ap_ip, 53, 0, usocket.SOCK_DGRAM)[0][-1])
    _dns_task = asyncio.create_task(dns._handler(_dns_sock, ap_ip))

    _server = await asyncio.start_server(server._handle_request, "0.0.0.0", 80)
    return ap_ip


async def stop():
    """Derruba servidor, DNS e desliga o Wi-Fi (economia de energia)."""
    global _ap, _server, _dns_sock, _dns_task
    if _server is not None:
        try:
            _server.close()
            await _server.wait_closed()
        except Exception:
            pass
        _server = None
    if _dns_task is not None:
        try:
            _dns_task.cancel()
        except Exception:
            pass
        _dns_task = None
    if _dns_sock is not None:
        try:
            _dns_sock.close()
        except Exception:
            pass
        _dns_sock = None
    if _ap is not None:
        try:
            _ap.active(False)
        except Exception:
            pass
        _ap = None
    gc.collect()
