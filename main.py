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
from machine import I2C, Pin, UART, PWM
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
GREEN = gc9a01.color565(0, 220, 0)
BLUE = gc9a01.color565(0, 120, 255)

# --- Calibração da bússola (barras que enchem conforme cada eixo é explorado) ---
# faixa (max-min) por eixo para considerar o eixo "explorado". Se as barras
# encherem rápido demais ou nunca completarem, ajuste este valor ao seu sensor.
CAL_TARGET_RANGE = 1500
CAL_AXES = ("X", "Y", "Z")
CAL_LABEL_X = 24         # posição do rótulo (X/Y/Z)
CAL_BAR_X = 44           # início da barra
CAL_BAR_W = 104          # largura total da barra
CAL_BAR_H = 16           # altura da barra
CAL_BAR_YS = (78, 118, 158)  # linha vertical de cada barra (X, Y, Z), centradas
CAL_VAL_X = 154          # posição do número (valor bruto ao vivo)
CAL_VAL_W = 66           # largura a limpar antes de redesenhar o número

# pontos cardeais (8 direções) para o readout de teste da bússola
CARDINALS = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")

# Declinação magnética (graus; some ao rumo p/ apontar ao Norte verdadeiro).
# Valores APROXIMADOS — confira o exato da sua localidade em magnetic-declination.com
DECL_PARNAIBA = -21.4    # Parnaíba, PI (Brasil)
DECL_ALBUFEIRA = -1.7    # Albufeira (Portugal)

# Controle de brilho da tela (MOSFET no gate, PWM)
BACKLIGHT_PIN = 15       # GPIO do gate do MOSFET
BACKLIGHT_FREQ = 1000    # Hz do PWM
BACKLIGHT_INVERT = False  # True se o MOSFET acende com nível BAIXO (gate ativo-baixo)
BRIGHT_FILE = "bright.txt"
BRIGHT_MIN = 10          # piso p/ não apagar a tela por engano

# Menus e submenus (só os rótulos; a ação de cada um está em _on_button)
MENUS = {
    "main":     ["Calibration", "Date/Time", "Compass", "Tests", "Settings"],
    "tests":    ["WiFi", "Magnetometro", "Voltar"],
    "settings": ["Parnaiba PI", "Albufeira PT", "Sem offset", "Custom", "Brilho", "Voltar"],
}


class Backlight:
    """Controle de brilho da tela via PWM no MOSFET (0–100%)."""

    def __init__(self, pin_num=BACKLIGHT_PIN, freq=BACKLIGHT_FREQ):
        self.pwm = PWM(Pin(pin_num))
        self.pwm.freq(freq)
        self.level = 100
        self._load()
        self.set(self.level, save=False)

    def set(self, percent, save=True):
        """Define o brilho em 0–100% (aplica na hora; opcionalmente salva)."""
        percent = int(percent)
        if percent < 0:
            percent = 0
        elif percent > 100:
            percent = 100
        self.level = percent
        duty = 100 - percent if BACKLIGHT_INVERT else percent
        # teto em 65534: duty_u16(65535) às vezes "vira" 0 no RP2 (apaga a tela)
        self.pwm.duty_u16(min(duty * 65535 // 100, 65534))
        if save:
            try:
                with open(BRIGHT_FILE, "w") as f:
                    f.write(str(percent))
            except OSError:
                pass

    def _load(self):
        try:
            with open(BRIGHT_FILE) as f:
                self.level = int(f.read().strip())
        except (OSError, ValueError):
            self.level = 100


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

    backlight = Backlight()

    return tft, uart, sensor, backlight


def display_intro(tft):
    """Desenha texto inicial na tela."""
    draw_center(tft, "ola ola ola", BLACK, WHITE)


class App:
    """Estado da aplicação com menu, submenus e modos de exibição."""

    def __init__(self, tft, uart, sensor, backlight):
        self.tft = tft
        self.uart = uart
        self.sensor = sensor
        self.backlight = backlight
        self.mode = "menu"
        self.menu = "main"        # menu atual (chave de MENUS)
        self.menu_idx = 0
        self.uart_buffer = b""
        self.latest_datetime = None

        # estado da calibração da bússola
        self.cal_min = [32767, 32767, 32767]
        self.cal_max = [-32768, -32768, -32768]
        self.cal_complete = False
        self.cal_setup_done = False   # moldura/rótulos já desenhados?
        self.cal_apply = False        # pedido de salvar (processado fora da IRQ)

        # edição da declinação custom
        self.decl_val = 0             # valor sendo editado (graus)
        self.pending_declination = None  # declinação a salvar (fora da IRQ)

        # edição do brilho da tela
        self.bright_val = backlight.level  # valor sendo editado (%)
        self.pending_brightness = None     # brilho a salvar (fora da IRQ)

        # cache da bússola: só redesenha quando muda (evita tearing/reescrita)
        self.compass_last_img = None
        self.compass_last_rot = None
        self.compass_last_txt = None

        # inicializa encoder rotativo (pinos CLK/DT) e usa o botão de pressão para confirmar
        self.encoder = RotaryIRQ(
            pin_num_clk=10,
            pin_num_dt=11,
            min_val=0,
            max_val=len(MENUS["main"]) - 1,
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

    # Navegação de menus ------------------------------------------------------
    def _open_menu(self, name):
        """Troca o menu ativo, reajusta a faixa do encoder e redesenha."""
        self.menu = name
        self.menu_idx = 0
        self.mode = "menu"
        self.encoder.set(
            value=0,
            min_val=0,
            max_val=len(MENUS[name]) - 1,
            range_mode=RotaryIRQ.RANGE_WRAP,
        )
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
        if self.encoder is None:
            return
        if self.mode == "menu":
            self.menu_idx = self.encoder.value()
            self.draw_menu()
        elif self.mode == "decl_edit":
            self.decl_val = self.encoder.value()
            self._draw_decl_edit()
        elif self.mode == "bright_edit":
            self.bright_val = self.encoder.value()
            self.backlight.set(self.bright_val, save=False)  # preview ao vivo
            self._draw_bright_edit()

    def _on_button(self):
        if self.mode == "menu":
            self._select_menu_item()
        elif self.mode == "decl_edit":
            # confirma a declinação custom (salvamento fora da IRQ) e volta
            self.pending_declination = self.decl_val
            self._open_menu("settings")
        elif self.mode == "bright_edit":
            # confirma o brilho (gravação de arquivo fora da IRQ) e volta
            self.pending_brightness = self.bright_val
            self._open_menu("settings")
        else:
            # saindo de uma view (compass/datetime/calibration/magtest/test)
            if self.mode == "test":
                self.stop_wifi()
            # calibração: se completa, salva (fora da IRQ); se não, cancela e sai
            if self.mode == "calibration" and self.cal_complete:
                self.cal_apply = True
            self._open_menu(self.menu)   # volta pro menu de origem

    def _select_menu_item(self):
        """Executa a ação do item selecionado no menu atual."""
        sel = MENUS[self.menu][self.menu_idx]

        if self.menu == "main":
            if sel == "Compass":
                self.disable_gps()
                self.compass_last_img = None   # força redesenho ao entrar
                self.compass_last_rot = None
                self.compass_last_txt = None
                self.mode = "compass"
            elif sel == "Date/Time":
                self.enable_gps()
                self.mode = "datetime"
            elif sel == "Calibration":
                self.disable_gps()
                self._reset_calibration()
                self.mode = "calibration"
            elif sel == "Tests":
                self._open_menu("tests")
                return
            elif sel == "Settings":
                self._open_menu("settings")
                return
            self.tft.fill(0)

        elif self.menu == "tests":
            if sel == "WiFi":
                self.disable_gps()
                self.mode = "test"
                self.start_wifi()
            elif sel == "Magnetometro":
                self.disable_gps()
                self.mode = "magtest"
                self.tft.fill(0)
            elif sel == "Voltar":
                self._open_menu("main")

        elif self.menu == "settings":
            if sel == "Parnaiba PI":
                self.pending_declination = DECL_PARNAIBA
                self.draw_menu()
            elif sel == "Albufeira PT":
                self.pending_declination = DECL_ALBUFEIRA
                self.draw_menu()
            elif sel == "Sem offset":
                self.pending_declination = 0
                self.draw_menu()
            elif sel == "Custom":
                self.decl_val = int(self.sensor.declination)
                self.mode = "decl_edit"
                self.encoder.set(
                    value=self.decl_val, min_val=-90, max_val=90,
                    range_mode=RotaryIRQ.RANGE_BOUNDED,
                )
                self._draw_decl_edit()
            elif sel == "Brilho":
                self.bright_val = self.backlight.level
                self.mode = "bright_edit"
                self.encoder.set(
                    value=self.bright_val, min_val=BRIGHT_MIN, max_val=100,
                    incr=5, range_mode=RotaryIRQ.RANGE_BOUNDED,
                )
                self._draw_bright_edit()
            elif sel == "Voltar":
                self._open_menu("main")

    def draw_menu(self):
        """Desenha o menu atual centralizado (o display é redondo)."""
        self.tft.fill(0)
        items = MENUS[self.menu]
        row_h = font.HEIGHT + 4
        screen_w = self.tft.width()
        total_h = len(items) * row_h
        start_y = (self.tft.height() - total_h) // 2

        # cabeçalho do menu Settings: mostra a declinação ativa
        if self.menu == "settings":
            decl = self.pending_declination
            if decl is None:
                decl = self.sensor.declination
            header = "Decl: {:.0f}".format(decl)
            self.tft.text(font, header, (screen_w - font.WIDTH * len(header)) // 2, 34, BLUE)

        for idx, text in enumerate(items):
            x = (screen_w - font.WIDTH * len(text)) // 2
            y = start_y + idx * row_h
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

    def _update_compass(self, heading):
        """Desenha a bússola só quando a imagem/rotação muda (reduz tearing)."""
        h = (heading - 10) % 360
        if h < 90:
            rot = 3
        elif h < 180:
            rot = 0
        elif h < 270:
            rot = 1
        else:
            rot = 2
        img = int(h % 90)

        if rot != self.compass_last_rot or img != self.compass_last_img:
            self.tft.rotation(rot)
            self.tft.jpg("download/{}.jpg".format(img), 0, 0, gc9a01.FAST)
            self.compass_last_rot = rot
            self.compass_last_img = img
            self.compass_last_txt = None   # readout precisa ser redesenhado sobre a nova imagem

        self._draw_heading_readout(heading)

    def _draw_heading_readout(self, heading):
        """Overlay de teste: graus + ponto cardeal (só redesenha quando muda)."""
        idx = int((heading + 22.5) // 45) % 8
        txt = "{:03d} {}".format(int(heading), CARDINALS[idx])
        if txt == self.compass_last_txt:
            return
        self.compass_last_txt = txt
        w = font.WIDTH * len(txt)
        x = (self.tft.width() - w) // 2
        # caixa preta atrás para ler sobre a imagem
        self.tft.fill_rect(x - 3, 16, w + 6, font.HEIGHT + 6, BLACK)
        self.tft.text(font, txt, x, 19, WHITE)

    def _draw_magtest(self):
        """Teste do magnetômetro: X, Y, Z ao vivo e o rumo em graus + cardeal."""
        x, y, z = self.sensor.read_raw()
        h = self.sensor.heading()
        card = CARDINALS[int((h + 22.5) // 45) % 8]
        # largura fixa (com bg preto) evita fantasma quando o número muda de tamanho
        lines = [
            "MAGNETOMETRO",
            "X:{:>7}".format(x),
            "Y:{:>7}".format(y),
            "Z:{:>7}".format(z),
            "Rumo:{:>4} {:>2}".format(int(h), card),
        ]
        draw_center(self.tft, lines, WHITE, BLACK)

    def _draw_decl_edit(self):
        """Editor da declinação custom."""
        self.tft.fill(0)
        draw_center(
            self.tft,
            ["Declinacao", "{:+d} graus".format(self.decl_val), "", "girar = ajustar", "apertar = ok"],
            WHITE,
        )

    def _draw_bright_edit(self):
        """Editor do brilho da tela."""
        self.tft.fill(0)
        draw_center(
            self.tft,
            ["Brilho", "{}%".format(self.bright_val), "", "girar = ajustar", "apertar = ok"],
            WHITE,
        )

    # Calibração da bússola ---------------------------------------------------
    def _reset_calibration(self):
        """Zera min/max e marca a tela para ser redesenhada do zero."""
        self.cal_min = [32767, 32767, 32767]
        self.cal_max = [-32768, -32768, -32768]
        self.cal_complete = False
        self.cal_setup_done = False

    def _draw_calibration_frame(self):
        """Desenha a parte fixa da tela de calibração (título, rótulos, molduras)."""
        self.tft.fill(0)
        title = "CALIBRAR"
        self.tft.text(font, title, (self.tft.width() - font.WIDTH * len(title)) // 2, 34, WHITE)
        for i, axis in enumerate(CAL_AXES):
            y = CAL_BAR_YS[i]
            self.tft.text(font, axis, CAL_LABEL_X, y + (CAL_BAR_H - font.HEIGHT) // 2, WHITE)
            self.tft.rect(CAL_BAR_X, y, CAL_BAR_W, CAL_BAR_H, GREY)
        self.cal_setup_done = True

    def update_calibration(self):
        """Lê o sensor, atualiza a exploração de cada eixo e desenha as barras."""
        vals = self.sensor.read_raw()

        for i in range(3):
            if vals[i] < self.cal_min[i]:
                self.cal_min[i] = vals[i]
            if vals[i] > self.cal_max[i]:
                self.cal_max[i] = vals[i]

        if not self.cal_setup_done:
            self._draw_calibration_frame()

        all_full = True
        for i in range(3):
            rng = self.cal_max[i] - self.cal_min[i]
            frac = rng / CAL_TARGET_RANGE
            if frac > 1.0:
                frac = 1.0
            # só X e Y decidem a conclusão; o rumo não usa Z (o Z fica só como
            # indicador visual e não trava a calibração)
            if i < 2 and frac < 1.0:
                all_full = False

            filled = int((CAL_BAR_W - 2) * frac)
            y = CAL_BAR_YS[i] + 1
            color = GREEN if frac >= 1.0 else BLUE
            # interior da barra: parte cheia colorida + restante preto
            if filled > 0:
                self.tft.fill_rect(CAL_BAR_X + 1, y, filled, CAL_BAR_H - 2, color)
            if filled < CAL_BAR_W - 2:
                self.tft.fill_rect(CAL_BAR_X + 1 + filled, y, (CAL_BAR_W - 2) - filled, CAL_BAR_H - 2, BLACK)

            # valor bruto ao vivo do eixo, à direita da barra
            vy = CAL_BAR_YS[i] + (CAL_BAR_H - font.HEIGHT) // 2
            self.tft.fill_rect(CAL_VAL_X, vy, CAL_VAL_W, font.HEIGHT, BLACK)
            self.tft.text(font, str(vals[i]), CAL_VAL_X, vy, WHITE)

        # ao completar, mostra a mensagem uma única vez
        if all_full and not self.cal_complete:
            self.cal_complete = True
            msg1 = "CONCLUIDO"
            msg2 = "aperte o botao"
            self.tft.text(font, msg1, (self.tft.width() - font.WIDTH * len(msg1)) // 2, 196, GREEN)
            self.tft.text(font, msg2, (self.tft.width() - font.WIDTH * len(msg2)) // 2, 208, WHITE)

    def update(self):
        """Atualiza estado dependendo do modo corrente."""
        # salvamento da calibração pedido pela IRQ do botão (feito aqui, fora dela)
        if self.cal_apply:
            self.cal_apply = False
            ox = (self.cal_min[0] + self.cal_max[0]) // 2
            oy = (self.cal_min[1] + self.cal_max[1]) // 2
            oz = (self.cal_min[2] + self.cal_max[2]) // 2
            self.sensor.set_calibration(ox, oy, oz)

        # aplicação da declinação pedida pela IRQ (escrita de arquivo fora dela)
        if self.pending_declination is not None:
            self.sensor.set_declination(self.pending_declination)
            self.pending_declination = None

        # gravação do brilho pedida pela IRQ (o PWM já foi aplicado ao vivo)
        if self.pending_brightness is not None:
            self.backlight.set(self.pending_brightness, save=True)
            self.pending_brightness = None

        self.process_uart()

        if self.mode == "menu":
            # nada a fazer; espera interações
            pass
        elif self.mode == "compass":
            self._update_compass(self.sensor.heading())
        elif self.mode == "datetime":
            if self.latest_datetime:
                draw_center(self.tft, self.latest_datetime, WHITE, BLACK)
        elif self.mode == "calibration":
            self.update_calibration()
        elif self.mode == "magtest":
            self._draw_magtest()
        elif self.mode == "decl_edit":
            # tela estática; atualizada nos eventos do encoder
            pass
        elif self.mode == "bright_edit":
            # tela estática; atualizada nos eventos do encoder
            pass
        elif self.mode == "test":
            # nothing to update every cycle; server runs in background
            pass


def main():
    tft, uart, sensor, backlight = init_peripherals()
    app = App(tft, uart, sensor, backlight)

    while True:
        app.update()
        sleep(0.05)


if __name__ == "__main__":
    main()







