"""main.py

Programa principal da bússola/GPS.
Responsabilidades:
  * inicializar periféricos (display, I2C, UART)
  * ler sentenças RMC do GPS
  * mostrar data/hora e orientação no display

A lógica não foi alterada; apenas extraída em funções para melhorar a
legibilidade e facilitar futuras modificações.
"""

import gc
from time import sleep
from machine import I2C, Pin, UART
import vga1_8x8 as font
import gc9a01
import tft_config
from qmc5883p import QMC5883P

# network/http for test mode
import network
import socket
import _thread

# use local rotary driver for menu input
from rotary_irq_rp2 import RotaryIRQ


# cores usadas na tela (o display é redondo: sempre centralizar o texto)
WHITE = gc9a01.color565(255, 255, 255)
BLACK = gc9a01.color565(0, 0, 0)
GREY = gc9a01.color565(100, 100, 100)
RED = gc9a01.color565(255, 0, 0)


# ---------------------------------------------------------------------------
# utilitários
# ---------------------------------------------------------------------------

def draw_center(tft, lines, fg, bg=None):
    """Desenha uma ou mais linhas de texto centralizadas no display redondo.

    Como a tela é circular, os cantos ficam cortados; por isso todo texto é
    centralizado horizontal e verticalmente. `lines` pode ser uma string ou
    uma lista de strings (uma por linha).
    """
    if isinstance(lines, str):
        lines = [lines]
    total_h = len(lines) * font.HEIGHT
    start_y = (tft.height() - total_h) // 2
    for i, line in enumerate(lines):
        x = (tft.width() - font.WIDTH * len(line)) // 2
        y = start_y + i * font.HEIGHT
        if bg is None:
            tft.text(font, line, x, y, fg)
        else:
            tft.text(font, line, x, y, fg, bg)


def parse_datetime_from_rmc(sentence: str) -> str | None:
    """Extrai data/hora de uma sentença NMEA RMC.

    Retorna string formatada "dd/mm/yyyy HH:MM:SS" ou None se inválida.
    """
    parts = sentence.split(',')
    if len(parts) > 9 and parts[1] and parts[9]:
        time_str = parts[1]
        hour = int(time_str[0:2])
        minute = int(time_str[2:4])
        second = int(time_str[4:6])

        date_str = parts[9]
        day = int(date_str[0:2])
        month = int(date_str[2:4])
        year = 2000 + int(date_str[4:6])

        # ajuste simples se estiver fora de 0‑23
        if hour >= 24:
            hour -= 24
            day += 1

        return f"{day:02d}/{month:02d}/{year} {hour:02d}:{minute:02d}:{second:02d}"
    return None


def init_peripherals():
    """Inicializa e retorna instâncias de tft, uart e sensor."""
    gc.collect()

    tft = tft_config.config(tft_config.TALL)
    tft.init()
    tft.rotation(3)
    tft.fill(0)

    uart = UART(1, baudrate=9600, tx=Pin(8), rx=Pin(9))

    i2c = I2C(1, scl=Pin(7), sda=Pin(6), freq=400000)
    sensor = QMC5883P(i2c)

    return tft, uart, sensor


def display_intro(tft):
    """Desenha texto inicial na tela."""
    draw_center(tft, "ola ola ola", BLACK, WHITE)


def update_display(tft, heading: float, datetime_str: str | None):
    """Ajusta a rotação e mostra imagem/data-hora na tela."""
    h = (heading - 10) % 360
    if h < 90:
        tft.rotation(3)
    elif h < 180:
        tft.rotation(0)
    elif h < 270:
        tft.rotation(1)
    else:
        tft.rotation(2)

    tft.jpg(f"download/{int(h % 90)}.jpg", 0, 0, gc9a01.FAST)

    if datetime_str:
        draw_center(tft, datetime_str, WHITE, BLACK)


class App:
    """Estado da aplicação com menu e modos de exibição."""

    MENU_ITEMS = ["Calibration", "Date/Time", "Compass", "Test"]

    def __init__(self, tft, uart, sensor):
        self.tft = tft
        self.uart = uart
        self.sensor = sensor
        self.mode = "menu"
        self.menu_idx = 0
        self.uart_buffer = b""
        self.latest_datetime = None

        # inicializa encoder rotativo (pinos CLK/DT) e usa o botão de pressão para confirmar
        self.encoder = RotaryIRQ(
            pin_num_clk=10,
            pin_num_dt=11,
            min_val=0,
            max_val=len(self.MENU_ITEMS) - 1,
            range_mode=RotaryIRQ.RANGE_WRAP,
            pull_up=True,
        )
        self.encoder.add_listener(self._on_rotary)

        # o interrupt veio do pino de push do encoder (por exemplo, pino 16)
        self.button = Pin(12, Pin.IN, Pin.PULL_UP)
        self.button.irq(trigger=Pin.IRQ_FALLING, handler=lambda p: self._on_button())

        # desenha menu inicial
        # desliga o GPS (economia) até o usuário pedir
        self.disable_gps()
        self.draw_menu()

    # GPS power/uart control -------------------------------------------------
    def enable_gps(self):
        """(Re)ativa a UART do GPS e limpa buffers."""
        if self.uart is None:
            self.uart = UART(1, baudrate=9600, tx=Pin(8), rx=Pin(9))
        self.uart_buffer = b""

    def disable_gps(self):
        """Desliga a UART do GPS para economizar energia (se suportado)."""
        try:
            if self.uart is not None:
                # deinit libera a UART no MicroPython
                self.uart.deinit()
        except Exception:
            pass
        self.uart = None

    # Wi‑Fi / web‑server support for test mode ------------------------------
    def start_wifi(self):
        """Tenta iniciar o Wi-Fi com tratamento de erro melhorado."""
        self.stop_wifi()
        gc.collect() # Libera memória antes de iniciar o rádio
        
        try:
            self.ap = network.WLAN(network.AP_IF)
            # Tenta desativar explicitamente antes de ativar
            self.ap.active(False) 
            sleep(0.5) 
            
            self.ap.config(essid="PicoConfig")
            self.ap.active(True) # O erro acontece aqui
            
            # Aguarda até que o rádio esteja realmente pronto
            retry = 0
            while not self.ap.active() and retry < 10:
                sleep(0.1)
                retry += 1
                
            ip = self.ap.ifconfig()[0]
            # mostra os dados de conexão centralizados na tela redonda
            self.tft.fill(0)
            draw_center(self.tft, ["Portal ativo", "PicoConfig", ip], WHITE)
            return ip
        except Exception as e:
            print(f"Erro detalhado: {e}")
            self.tft.fill(RED)
            draw_center(self.tft, ["Erro Hardware", "WiFi"], WHITE, RED)
            return None

    def stop_wifi(self):
        """Disable AP and close server socket if running."""
        try:
            if hasattr(self, "s"):
                self.s.close()
        except Exception:
            pass
        try:
            if hasattr(self, "ap"):
                self.ap.active(False)
        except Exception:
            pass

    def _webserver(self):
        # simple blocking HTTP server that responds with Hello World
        addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
        self.s = socket.socket()
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind(addr)
        self.s.listen(1)
        while True:
            try:
                cl, addr = self.s.accept()
                cl.settimeout(5)
                _ = cl.recv(1024)  # read request (ignore content)
                response = (
                    "HTTP/1.0 200 OK\r\nContent-type: text/html\r\n\r\n"
                    "<html><body><h1>Hello world</h1></body></html>"
                )
                cl.send(response)
                cl.close()
            except Exception:
                # loop again in case of server errors
                pass

    def _on_rotary(self):
        if self.mode == "menu" and self.encoder is not None:
            self.menu_idx = self.encoder.value()
            self.draw_menu()

    def _on_button(self):
        if self.mode == "menu":
            sel = self.MENU_ITEMS[self.menu_idx]
            if sel == "Compass":
                # Ao entrar em Bússola, desligar GPS para economizar
                self.disable_gps()
                self.mode = "compass"
            elif sel == "Date/Time":
                # Ativar GPS para receber RMC
                self.enable_gps()
                self.mode = "datetime"
            elif sel == "Calibration":
                self.disable_gps()
                self.mode = "calibration"
            elif sel == "Test":
                self.disable_gps()
                self.mode = "test"
                # start Wi-Fi access point and web server
                self.start_wifi()
            self.tft.fill(0)
        else:
            # leaving whatever view we were in
            if self.mode == "test":
                self.stop_wifi()
            self.mode = "menu"
            self.tft.fill(0)
            self.draw_menu()

    def draw_menu(self):
        """Centraliza verticalmente e horizontalmente os itens do menu."""
        self.tft.fill(0)
        # calculos de alinhamento
        screen_w = self.tft.width()
        screen_h = self.tft.height()
        total_h = len(self.MENU_ITEMS) * font.HEIGHT
        start_y = (screen_h - total_h) // 2

        for idx, text in enumerate(self.MENU_ITEMS):
            text_w = font.WIDTH * len(text)
            x = (screen_w - text_w) // 2
            y = start_y + idx * font.HEIGHT
            color = WHITE if idx == self.menu_idx else GREY
            self.tft.text(font, text, x, y, color)

    def process_uart(self):
        # só processa se a UART estiver ativa
        if self.uart is None:
            return

        if self.uart.any():
            self.uart_buffer += self.uart.read(self.uart.any())
            if b"\n" in self.uart_buffer:
                lines = self.uart_buffer.split(b"\n")
                for l in lines[:-1]:
                    if l.startswith(b"$GPRMC") or l.startswith(b"$GNRMC"):
                        try:
                            s = l.decode().strip()
                            dt = parse_datetime_from_rmc(s)
                            if dt:
                                self.latest_datetime = dt
                        except Exception:
                            pass
                self.uart_buffer = lines[-1]

    def update(self):
        """Atualiza estado dependendo do modo corrente."""
        self.process_uart()

        if self.mode == "menu":
            # nada a fazer; espera interações
            pass
        elif self.mode == "compass":
            heading = self.sensor.heading()
            if heading is not None:
                # na view da bússola não mostrar data/hora
                update_display(self.tft, heading, None)
        elif self.mode == "datetime":
            if self.latest_datetime:
                draw_center(self.tft, self.latest_datetime, WHITE, BLACK)
        elif self.mode == "calibration":
            x, y, z = self.sensor.read_raw()
            self.tft.fill(0)
            draw_center(self.tft, [f"X:{x}", f"Y:{y}", f"Z:{z}"], WHITE)
        elif self.mode == "test":
            # nothing to update every cycle; server runs in background
            pass


def main():
    tft, uart, sensor = init_peripherals()
    app = App(tft, uart, sensor)

    while True:
        app.update()
        sleep(0.05)


if __name__ == "__main__":
    main()







