import network
import time
import socket
import os
import gc

AP_SSID = "Teste-Pico"
AP_PASS = "88776688"

def get_free_space():
    fs = os.statvfs('/')
    free_kb = (fs[0] * fs[3]) / 1024
    return f"{free_kb:.1f} KB"

def list_download_images():
    try:
        if "download" not in os.listdir("."): os.mkdir("download")
        files = [f for f in os.listdir("download") if f.lower().endswith(('.jpg','.png','.jpeg','.gif'))]
    except: return "Erro ao ler pasta"

    if not files: return "<p>Nenhuma imagem.</p>"
    
    html = []
    for name in sorted(files):
        f_size = os.stat("download/" + name)[6] / 1024
        html.append(f"""
        <div class='tile'>
            <img data-src='/download/{name}' src=''>
            <div class='tile-info'>
                <strong>{name}</strong><br>
                <small>{f_size:.1f} KB</small><br>
                <a href='/delete?name={name}' class='delete-btn'>Excluir</a>
            </div>
        </div>""")
    return "\n".join(html)

def start_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password=AP_PASS)
    print("IP:", ap.ifconfig()[0])
    return ap

def run_server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 80))
    s.listen(1) # 1 conexão por vez na fila

    while True:
        cl = None
        try:
            cl, addr = s.accept()
            cl.settimeout(10)
            req = cl.recv(1024)
            if not req: 
                cl.close()
                continue
            
            method = req.decode().split(' ')[0]
            path = req.decode().split(' ')[1]

            # ROTA INDEX
            if method == "GET" and path in ["/", "/index.html"]:
                with open("index.html", "r") as f:
                    content = f.read()
                content = content.replace("{{IMAGES}}", list_download_images())
                content = content.replace("{{FREE_SPACE}}", get_free_space())
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                cl.sendall(content)

            # ROTA CSS
            elif method == "GET" and path == "/style.css":
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: text/css\r\n\r\n")
                with open("style.css", "r") as f:
                    cl.sendall(f.read())

            # ROTA envio da imagem
            elif method == "GET" and path.startswith("/download/"):
                fname = path.split("/")[-1]
                try:
                    cl.send("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nConnection: close\r\n\r\n")
                    with open("download/" + fname, "rb") as f:
                        while True:
                            chunk = f.read(512)
                            if not chunk: break
                            cl.sendall(chunk)
                    time.sleep_ms(20)
                except: pass

            # ROTA UPLOAD
            elif method == "POST" and path == "/upload":
                try:
                    cl.settimeout(20)
                    body_buffer = req
                    
                    while b"\r\n\r\n" not in body_buffer:
                        chunk = cl.recv(512)
                        if not chunk: break
                        body_buffer += chunk
                    
                    headers_part, body_start = body_buffer.split(b"\r\n\r\n", 1)
                    
                    boundary = None
                    for line in headers_part.decode().lower().split("\r\n"):
                        if "boundary=" in line:
                            boundary = line.split("boundary=")[1].strip().encode()

                    if boundary:
                        while b"\r\n\r\n" not in body_start and len(body_start) < 3000:
                            chunk = cl.recv(512)
                            if not chunk: break
                            body_start += chunk
                        
                        if b"\r\n\r\n" in body_start:
                            file_info, file_data = body_start.split(b"\r\n\r\n", 1)
                            
                            filename = "imagem_upload.jpg"
                            if b'filename="' in file_info:
                                filename = file_info.split(b'filename="')[1].split(b'"')[0].decode()
                            
                            full_boundary = b"--" + boundary
                            
                            # Gravação em streaming para não estourar a RAM
                            with open("download/" + filename, "wb") as f:
                                f.write(file_data)
                                try:
                                    while True:
                                        chunk = cl.recv(1024)
                                        if not chunk: break
                                        if full_boundary in chunk:
                                            final_part = chunk.split(full_boundary)[0]
                                            if final_part.endswith(b"\r\n"): 
                                                final_part = final_part[:-2]
                                            f.write(final_part)
                                            break
                                        f.write(chunk)
                                except OSError:
                                    pass # Timeout no final é comum, ignoramos
                            
                            print("Upload concluído:", filename)
                    
                    cl.send("HTTP/1.1 302 Found\r\nLocation: /\r\n\r\n")
                    
                except Exception as e:
                    print("Erro no upload:", e)
                    try: cl.send("HTTP/1.1 302 Found\r\nLocation: /\r\n\r\n")
                    except: pass
            # ROTA DELETE
            elif method == "GET" and path.startswith("/delete"):
                fname = path.split("name=")[1]
                os.remove("download/" + fname)
                cl.send("HTTP/1.1 302 Found\r\nLocation: /\r\n\r\n")

            cl.close()
            gc.collect()
        except Exception as e:
            if cl: cl.close()
            gc.collect()


start_ap()
run_server()