"""captive_portal.py

Ponto de entrada standalone para o Pico W que roda SOMENTE o portal cativo
de imagens (Access Point + DNS catch-all + servidor web). Não importa nada de
display, encoder, GPS ou bússola, então roda no Pico onde só o Wi-Fi está
conectado.

Para usar como app de boot nesse Pico: salve este arquivo como `main.py`
(ou crie um main.py com `import captive_portal; captive_portal.start()`).

Ao conectar no Wi-Fi "EX-VISION", o celular abre a página sozinho graças ao
servidor DNS catch-all (responde qualquer domínio com o IP do Pico) somado ao
handler catchall que redireciona qualquer rota desconhecida para o portal.
"""

import os
import gc

from phew import access_point, dns, server


# ---------------------------------------------------------------------------
# configuração
# ---------------------------------------------------------------------------

AP_SSID = "EX-VISION"
# Rede aberta (sem senha) faz o popup do portal cativo aparecer sozinho na
# maioria dos celulares. Para exigir senha, defina AP_PASSWORD = "88776688".
AP_PASSWORD = None

DOWNLOAD_DIR = "download"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif")

# preenchido em start() com o IP do Access Point (ex.: "192.168.4.1")
ap_ip = "192.168.4.1"


# ---------------------------------------------------------------------------
# utilitários
# ---------------------------------------------------------------------------

def _ensure_download_dir():
    if DOWNLOAD_DIR not in os.listdir("."):
        os.mkdir(DOWNLOAD_DIR)


def _safe_name(filename):
    """Remove qualquer componente de diretório, deixando só o nome do arquivo."""
    return filename.replace("\\", "/").split("/")[-1]


def _extract(header, token):
    """Extrai o valor entre `token` e a próxima aspa dupla. Ex.: name=\"x\"."""
    if token not in header:
        return None
    return header.split(token, 1)[1].split('"', 1)[0]


def _free_space():
    fs = os.statvfs("/")
    free_kb = (fs[0] * fs[3]) / 1024
    return "{:.1f} KB".format(free_kb)


def _gallery_html():
    _ensure_download_dir()
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.lower().endswith(IMAGE_EXT)]
    if not files:
        return "<p>Nenhuma imagem.</p>"

    tiles = []
    for name in sorted(files):
        size_kb = os.stat(DOWNLOAD_DIR + "/" + name)[6] / 1024
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
# upload multipart em streaming (substitui o parser do phew)
#
# O _parse_form_data original do phew acumula o corpo inteiro como string
# (line.decode().strip()), o que corrompe dados binários de JPEG e estoura a
# RAM do Pico. Aqui gravamos o arquivo direto no disco em blocos e devolvemos
# apenas o nome salvo em request.form. Reatribuir o atributo do módulo basta:
# _handle_request chama _parse_form_data pelo nome no namespace do módulo.
# ---------------------------------------------------------------------------

async def _parse_form_data_streaming(reader, headers):
    boundary = b"--" + headers["content-type"].split("boundary=")[1].strip().encode()
    delimiter = b"\r\n" + boundary  # o corpo termina em \r\n--boundary--
    tail = len(delimiter)
    form = {}

    _ensure_download_dir()

    # descarta a linha inicial "--boundary"
    await reader.readline()

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
            with open(DOWNLOAD_DIR + "/" + saved, "wb") as f:
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
                    # grava tudo menos uma cauda que poderia conter o boundary
                    if len(buf) > tail:
                        f.write(buf[:-tail])
                        buf = buf[-tail:]
            form[name] = saved
            gc.collect()
            break  # tratamos apenas um arquivo por envio
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
    with open("index.html", "r") as f:
        html = f.read()
    html = html.replace("{{IMAGES}}", _gallery_html())
    html = html.replace("{{FREE_SPACE}}", _free_space())
    # devolve bytes p/ o Content-Length bater com o UTF-8 (acentos) e evitar
    # que o navegador trunque a página
    return html.encode("utf-8"), 200, "text/html; charset=utf-8"


@server.route("/style.css", methods=["GET"])
def style(request):
    return server.serve_file("style.css")


@server.route("/download/<name>", methods=["GET"])
def image(request, name):
    return server.serve_file(DOWNLOAD_DIR + "/" + _safe_name(name))


@server.route("/upload", methods=["POST"])
def upload(request):
    # o arquivo já foi gravado por _parse_form_data_streaming
    saved = request.form.get("file") if request.form else None
    print("Upload concluído:", saved)
    return b"OK", 200, "text/plain"


@server.route("/delete", methods=["GET"])
def delete(request):
    name = request.query.get("name")
    if name:
        try:
            os.remove(DOWNLOAD_DIR + "/" + _safe_name(name))
        except OSError as e:
            print("Erro ao excluir:", e)
    return server.redirect("/", 302)


@server.catchall()
def catchall(request):
    # qualquer domínio/rota desconhecida -> manda pro portal (dispara o popup)
    return server.redirect("http://" + ap_ip + "/", 302)


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------

def start():
    global ap_ip

    _ensure_download_dir()

    wlan = access_point(AP_SSID, AP_PASSWORD)
    ap_ip = wlan.ifconfig()[0]
    print("Access Point '{}' ativo em http://{}".format(AP_SSID, ap_ip))

    dns.run_catchall(ap_ip)   # resolve todo domínio para o Pico
    server.run()              # bloqueia rodando o event loop


if __name__ == "__main__":
    start()
