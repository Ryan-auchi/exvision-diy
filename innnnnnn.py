import network
import time
import machine
import socket
import os

AP_SSID = "Teste-Pico"
AP_PASS = "88776688" 


def load_html_template():
    return """<html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>Pico W Portal</title>
    <style>
        body{font-family:Arial; margin:10px; background:#f0f0f0;}
        .grid{display:grid;grid-template-columns:repeat(auto-fill, minmax(150px, 1fr));gap:10px;}
        .tile{border:1px solid #ccc;padding:5px;border-radius:8px;background:#fff;text-align:center;}
        .tile img{max-width:100%; max-height:150px; object-fit:cover; display:block; margin:0 auto 5px; border-radius:5px;}
        .controls{margin-bottom:20px; padding:15px; background:#fff; border-radius:8px;}
    </style></head>
    <body><div class='controls'><h1>Portal Pico W</h1>
    <form method='POST' action='/upload' enctype='multipart/form-data'>
    <input type='file' name='file'><button type='submit'>Upload</button></form>
    </div><div class='grid'>{{IMAGES}}</div></body></html>"""

def list_download_images():
    files = []
    try:
        if "download" not in os.listdir("."):
            os.mkdir("download")
        all_names = sorted(os.listdir("download"))
        print("download/ contém:", all_names)
        for name in all_names:
            lname = name.lower()
            if lname.endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")):
                files.append(name)
    except Exception as e:
        print("Erro lendo download/:", e)
    if not files:
        return "<div class='tile'>(Nenhuma imagem em download/)</div>"
    html = []
    for name in files:
        html.append("<div class='tile'><img src='/download/" + name + "'><div><strong>" + name + "</strong></div><div><a href='/delete?name=" + name + "'>Excluir</a></div></div>")
    return "\n".join(html)


def start_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(False)
    time.sleep(1)
    ap.active(True)
    if AP_PASS:
        ap.config(essid=AP_SSID, password=AP_PASS)
    else:
        ap.config(essid=AP_SSID)
    time.sleep(2)
    print("AP criado:", ap.ifconfig())
    print("SSID:", AP_SSID)
    return ap


def run_captive_server():
    template = load_html_template()
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    print("Servidor captive portal rodando: http://192.168.4.1")

    while True:
        try:
            cl, remote = s.accept()
            cl.settimeout(5)
            req = cl.recv(1024)
            if not req:
                cl.close()
                continue
            first_line = req.split(b"\r\n")[0]
            print("Req de", remote, first_line)
            
            try:
                line = first_line.decode()
            except Exception:
                line = ""
            parts = line.split(" ")
            if len(parts) < 2:
                cl.close()
                continue
            method, path = parts[0], parts[1]
            if method == "GET" and path in ["/", "/index", "/index.html"]:
                body = template.replace("{{IMAGES}}", list_download_images())
                resp = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + body
                cl.send(resp.encode())
            elif method == "GET" and path.startswith("/download/"):
                img_name = path[len("/download/"):]
                try:
                    filepath = "download/" + img_name
                    
                    ctype = "image/jpeg"
                    if img_name.lower().endswith(".png"): ctype = "image/png"
                    elif img_name.lower().endswith(".gif"): ctype = "image/gif"
                    
                    
                    cl.send(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nConnection: close\r\n\r\n".encode())
                    
                    # Enviar arquivo em pedaços (Streaming) para não ocupar RAM
                    with open(filepath, "rb") as f:
                        while True:
                            content = f.read(2048) # Lê apenas 2KB por vez
                            if not content:
                                break
                            cl.sendall(content) # Envia o pedaço
                except Exception as e:
                    print("Erro ao servir imagem:", e)
                    cl.send("HTTP/1.1 404 Not Found\r\n\r\n".encode())
            elif method == "GET" and path.startswith("/delete?"):
                try:
                    params = path.split("?", 1)[1]
                    kv = params.split("=", 1)
                    if kv[0] == "name":
                        name = kv[1]
                        if ".." in name or name.startswith("/"):
                            raise Exception("invalid")
                        os.remove("download/" + name)
                except Exception as e:
                    print("Erro excluir:", e)
                cl.send("HTTP/1.1 302 Found\r\nLocation: http://192.168.4.1/\r\nConnection: close\r\n\r\n".encode())
            elif method == "POST" and path == "/upload":
                try:
                    # 1. Aumentar timeout para arquivos grandes
                    cl.settimeout(15) 
                    
                    # 2. Ler o cabeçalho do POST para achar o Boundary e Content-Length
                    # Já temos parte disso no 'req' inicial
                    header_data = req
                    while b"\r\n\r\n" not in header_data:
                        chunk = cl.recv(512)
                        if not chunk: break
                        header_data += chunk
                    
                    headers_raw, body_start = header_data.split(b"\r\n\r\n", 1)
                    headers_str = headers_raw.decode().lower()
                    
                    # Extrair Boundary
                    boundary = None
                    for line in headers_str.split("\r\n"):
                        if "content-type" in line and "boundary=" in line:
                            boundary = line.split("boundary=")[1].strip().encode()
                    
                    if boundary:
                        # 3. Procurar o nome do arquivo e o início real dos dados binários
                        # O corpo começa com o boundary, seguido de headers do arquivo
                        while b"\r\n\r\n" not in body_start:
                            chunk = cl.recv(512)
                            if not chunk: break
                            body_start += chunk
                        
                        file_headers, file_content_start = body_start.split(b"\r\n\r\n", 1)
                        
                        # Tentar pegar o nome do arquivo
                        filename = "upload.jpg" # fallback
                        if b'filename="' in file_headers:
                            filename = file_headers.split(b'filename="')[1].split(b'"')[0].decode()
                        
                        # 4. Gravação em Streaming (O coração da solução)
                        # Removemos o boundary final da contagem se ele já estiver no buffer
                        full_boundary = b"--" + boundary
                        
                        with open("download/" + filename, "wb") as f:
                            # Escreve o que já foi lido após os cabeçalhos do arquivo
                            f.write(file_content_start)
                            
                            # Continua lendo do socket e gravando
                            while True:
                                chunk = cl.recv(1024)
                                if not chunk or full_boundary in chunk:
                                    # Se achar o boundary final, grava apenas o que vem antes dele
                                    if chunk and full_boundary in chunk:
                                        final_part = chunk.split(full_boundary)[0]
                                        # Remove os últimos \r\n que o protocolo HTTP adiciona antes do boundary
                                        if final_part.endswith(b"\r\n"):
                                            final_part = final_part[:-2]
                                        f.write(final_part)
                                    break
                                f.write(chunk)
                        
                    print("Upload concluído com sucesso:", filename)
                except Exception as e:
                    print("Erro no upload streaming:", e)
                
                # Redireciona de volta para a Home para ver a nova imagem
                cl.send("HTTP/1.1 302 Found\r\nLocation: /\r\nConnection: close\r\n\r\n".encode())
            elif method == "GET" and path == "/favicon.ico":
                cl.send("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n".encode())
            else:
                cl.send("HTTP/1.1 302 Found\r\nLocation: http://192.168.4.1/\r\nConnection: close\r\n\r\n".encode())

            cl.close()
        except Exception as e:
            print("Erro socket:", e)
            try:
                cl.close()
            except:
                pass


def main():
    print("=== Iniciando Captive Portal Pico W ===")
    ap = start_ap()
    if not ap.active():
        print("Erro: AP não ativado.")
        return
    print("Aguardando conexão...")
    run_captive_server()


if __name__ == "__main__":
    main()