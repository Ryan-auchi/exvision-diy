"""main.py — EX-VISION (porta-retrato + bússola) — versão reformulada.

Aparelho ÚNICO (Pico W com Wi-Fi funcionando). Modos:

  * FOTO (boot)  : mostra uma foto do SD (/sd/fotos); encoder = anterior/próxima.
  * BÚSSOLA      : seta desenhada apontando para a coordenada-alvo (config.py)
                   + distância no centro. Usa GPS (posição) + magnetômetro (rumo).
  * PORTAL       : captive portal (Wi-Fi) para enviar fotos já cortadas 240x320.
  * CONFIG       : offset (declinação), brilho, calibração.

Menu (botão do encoder a partir de foto/bússola). A % de bateria aparece SÓ no
menu (no topo).

Gestão de energia:
  * Wi-Fi ligado só no modo Portal.
  * GPS + magnetômetro ligados só na Bússola (magnetômetro também na Calibração).
  * Fora disso, desligados (deinit + pinos baixos antes de cortar energia).

Hardware assumido nesta branch (ver lib/tft_config.py e MONTAGEM.md):
  * Display ILI9341 240x320 (driver st7789_mpy no .uf2).
  * Leitor micro SD no MESMO SPI0, CS = GP22 (precisa de lib/sdcard.py).
  * Bateria via divisor 2x100k no ADC0 (GP26).
"""

import gc
import os
import math
import time
import uasyncio as asyncio
from machine import I2C, Pin, UART, PWM, ADC

import vga1_8x8 as font
import tft_config
from qmc5883p import QMC5883P
from rotary_irq_rp2 import RotaryIRQ
import portal

# coordenada-alvo da bússola (preenchida pelo usuário em config.py)
try:
    from config import TARGET_LAT, TARGET_LON
except ImportError:
    TARGET_LAT = None
    TARGET_LON = None


# --- Cores (via tft_config para não acoplar o nome do driver) ----------------
WHITE = tft_config.color565(255, 255, 255)
BLACK = tft_config.color565(0, 0, 0)
GREY = tft_config.color565(100, 100, 100)
RED = tft_config.color565(255, 0, 0)
GREEN = tft_config.color565(0, 220, 0)
BLUE = tft_config.color565(0, 120, 255)

# --- Pinos --------------------------------------------------------------------
ENC_CLK = 10
ENC_DT = 11
ENC_BTN = 12
SD_CS = 22
GPS_TX = 8
GPS_RX = 9
I2C_SCL = 7
I2C_SDA = 6
BAT_ADC_PIN = 26          # ADC0

# Pinos de energia opcionais (MOSFET). None = sem chave de energia por hardware
# (só liga/desliga por software). Defina o GPIO se você montar os MOSFETs.
GPS_EN_PIN = None
MAG_EN_PIN = None

# --- Bateria (LiPo) -----------------------------------------------------------
BAT_DIVIDER = 2.0         # divisor 2x100k -> tensão real = leitura * 2
BAT_CURVE = (
    (4.20, 100), (4.10, 90), (4.00, 80), (3.90, 65), (3.80, 50),
    (3.70, 35), (3.60, 20), (3.50, 10), (3.40, 5), (3.30, 0),
)

# --- Brilho (MOSFET no gate, PWM) --------------------------------------------
BACKLIGHT_PIN = 15
BACKLIGHT_FREQ = 1000
BACKLIGHT_INVERT = False
BRIGHT_FILE = "bright.txt"
BRIGHT_MIN = 10

# --- Calibração da bússola ----------------------------------------------------
CAL_TARGET_RANGE = 1500
CAL_AXES = ("X", "Y", "Z")
CAL_LABEL_X = 40
CAL_BAR_X = 60
CAL_BAR_W = 120
CAL_BAR_H = 16
CAL_BAR_YS = (130, 170, 210)
CAL_VAL_X = 186
CAL_VAL_W = 50

CARDINALS = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")

# Declinação magnética (graus; some ao rumo p/ apontar ao Norte verdadeiro).
DECL_PARNAIBA = -21.4
DECL_ALBUFEIRA = -1.7

# Menus e submenus
MENUS = {
    "main":     ["Foto", "Bussola", "Portal", "Config"],
    "settings": ["Offset", "Brilho", "Calibracao", "Voltar"],
    "offset":   ["Parnaiba PI", "Albufeira PT", "Sem offset", "Custom", "Voltar"],
}

FOTOS_DIR = "/sd/fotos"
IMAGE_EXT = (".jpg", ".jpeg")


# ---------------------------------------------------------------------------
# utilitários de energia / hardware
# ---------------------------------------------------------------------------

def _power(pin_num, on):
    """Aciona um MOSFET de energia, se configurado (senão, no-op)."""
    if pin_num is None:
        return
    Pin(pin_num, Pin.OUT).value(1 if on else 0)


def mount_sd():
    """Monta o cartão SD em /sd (compartilha o SPI0 do display). True se ok."""
    try:
        import sdcard
        cs = Pin(SD_CS, Pin.OUT)
        sd = sdcard.SDCard(tft_config.spi_bus(), cs)
        os.mount(sd, "/sd")
        try:
            os.stat(FOTOS_DIR)
        except OSError:
            os.mkdir(FOTOS_DIR)
        return True
    except Exception as e:
        print("SD nao montou:", e)
        return False


# ---------------------------------------------------------------------------
# geometria: rumo, distância e desenho da seta
# ---------------------------------------------------------------------------

def _nmea_to_deg(value, hemi):
    if not value or "." not in value:
        return None
    try:
        dot = value.index(".")
        deg = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
        dec = deg + minutes / 60.0
        if hemi in ("S", "W"):
            dec = -dec
        return dec
    except (ValueError, IndexError):
        return None


def parse_rmc(sentence):
    """Interpreta NMEA RMC -> {dt, fix, lat, lon} (ou None)."""
    parts = sentence.split(',')
    if len(parts) < 10:
        return None
    fix = parts[2] == "A"
    dt = None
    if parts[1] and parts[9]:
        try:
            t, d = parts[1], parts[9]
            hour, minute, second = int(t[0:2]), int(t[2:4]), int(t[4:6])
            day, month, year = int(d[0:2]), int(d[2:4]), 2000 + int(d[4:6])
            if hour >= 24:
                hour -= 24
                day += 1
            dt = "{:02d}/{:02d}/{:04d} {:02d}:{:02d}:{:02d}".format(
                day, month, year, hour, minute, second)
        except (ValueError, IndexError):
            dt = None
    lat = lon = None
    if fix and parts[3] and parts[5]:
        lat = _nmea_to_deg(parts[3], parts[4])
        lon = _nmea_to_deg(parts[5], parts[6])
    return {"dt": dt, "fix": fix, "lat": lat, "lon": lon}


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fmt_distance(km):
    if km < 1.0:
        return "{:d} m".format(int(km * 1000))
    if km < 10.0:
        return "{:.1f} km".format(km)
    return "{:d} km".format(int(km))


def _polar(cx, cy, r, angle_deg):
    """Ponto no raio `r` e ângulo `angle_deg` (0 = cima, sentido horário)."""
    a = math.radians(angle_deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def thick_line(tft, x0, y0, x1, y1, color, thick=3):
    """Linha com espessura (várias linhas paralelas deslocadas na normal)."""
    dx, dy = x1 - x0, y1 - y0
    length = math.sqrt(dx * dx + dy * dy) or 1.0
    nx, ny = -dy / length, dx / length     # normal unitária
    for off in range(-thick, thick + 1):
        tft.line(int(x0 + nx * off), int(y0 + ny * off),
                 int(x1 + nx * off), int(y1 + ny * off), color)


def draw_compass_ring(tft, cx, cy, angle_deg, color, r_ring):
    """Bússola em anel com ponteiro em CHEVRON DUPLO apontando para `angle_deg`.

    angle 0 = frente do aparelho (topo), sentido horário. Sem marcador fixo de
    direção: o mostrador é fixo, então só o chevron indica o alvo. O miolo fica
    livre para o texto da distância.
    """
    cx = int(cx)
    cy = int(cy)
    # aro + marcas simétricas a cada 45 graus (só moldura, não indicam direção)
    tft.circle(cx, cy, r_ring, GREY)
    for k in range(8):
        ox, oy = _polar(cx, cy, r_ring, k * 45)
        ix, iy = _polar(cx, cy, r_ring - 8, k * 45)
        tft.line(int(ox), int(oy), int(ix), int(iy), GREY)

    # chevron duplo: dois "V" apontando para fora, na direção do alvo
    a = math.radians(angle_deg)
    ux, uy = math.sin(a), -math.cos(a)     # radial (para fora, rumo ao alvo)
    px, py = math.cos(a), math.sin(a)      # perpendicular (abertura do "V")
    hw = 16                                # meia-abertura de cada chevron
    for apex_r, arm_r in ((r_ring - 4, r_ring - 18), (r_ring - 18, r_ring - 32)):
        ax = cx + ux * apex_r
        ay = cy + uy * apex_r
        lx = cx + ux * arm_r + px * hw
        ly = cy + uy * arm_r + py * hw
        rx = cx + ux * arm_r - px * hw
        ry = cy + uy * arm_r - py * hw
        thick_line(tft, lx, ly, ax, ay, color)
        thick_line(tft, rx, ry, ax, ay, color)


def draw_center(tft, lines, fg, bg=None):
    """Desenha linha(s) de texto centralizadas."""
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


# ---------------------------------------------------------------------------
# brilho e fotos
# ---------------------------------------------------------------------------

class Backlight:
    def __init__(self, pin_num=BACKLIGHT_PIN, freq=BACKLIGHT_FREQ):
        self.pwm = PWM(Pin(pin_num))
        self.pwm.freq(freq)
        self.level = 100
        self._load()
        self.set(self.level, save=False)

    def set(self, percent, save=True):
        percent = int(percent)
        percent = 0 if percent < 0 else (100 if percent > 100 else percent)
        self.level = percent
        duty = 100 - percent if BACKLIGHT_INVERT else percent
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


class Photos:
    """Lista e navega as fotos em /sd/fotos."""

    def __init__(self):
        self.files = []
        self.idx = 0
        self.refresh()

    def refresh(self):
        try:
            self.files = sorted(
                f for f in os.listdir(FOTOS_DIR) if f.lower().endswith(IMAGE_EXT))
        except OSError:
            self.files = []
        if self.idx >= len(self.files):
            self.idx = 0

    def count(self):
        return len(self.files)

    def current(self):
        if not self.files:
            return None
        return FOTOS_DIR + "/" + self.files[self.idx]


# ---------------------------------------------------------------------------
# aplicação
# ---------------------------------------------------------------------------

class App:
    def __init__(self, tft, backlight):
        self.tft = tft
        self.backlight = backlight
        self.i2c = I2C(1, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=400000)
        self.bat_adc = ADC(Pin(BAT_ADC_PIN))

        self.sensor = None        # magnetômetro (ligado só quando necessário)
        self.uart = None          # GPS (ligado só na bússola)
        self.uart_buffer = b""

        self.photos = Photos()

        self.mode = "photo"
        self.menu = "main"
        self.menu_idx = 0

        # GPS
        self.latest_datetime = None
        self.latest_lat = None
        self.latest_lon = None
        self.gps_fix = False

        # bússola (cache p/ só redesenhar quando muda)
        self.compass_key = None
        self.compass_msg = None

        # foto (cache)
        self.photo_last = -2
        self.photo_msg = None

        # calibração
        self.cal_min = [32767, 32767, 32767]
        self.cal_max = [-32768, -32768, -32768]
        self.cal_complete = False
        self.cal_setup_done = False
        self.cal_apply = False

        # edições
        self.decl_val = 0
        self.pending_declination = None
        self.bright_val = backlight.level
        self.pending_brightness = None

        # eventos (setados nas IRQ, consumidos no loop async)
        self.button_event = False
        self.rotary_event = False
        self._last_btn_ms = 0

        # pedidos assíncronos (portal sobe/desce fora da IRQ)
        self.portal_start_request = False
        self.portal_stop_request = False

        # encoder + botão
        self.encoder = RotaryIRQ(
            pin_num_clk=ENC_CLK, pin_num_dt=ENC_DT,
            min_val=0, max_val=0, range_mode=RotaryIRQ.RANGE_WRAP, pull_up=True,
        )
        self.encoder.add_listener(self._on_rotary_irq)
        self.button = Pin(ENC_BTN, Pin.IN, Pin.PULL_UP)
        self.button.irq(trigger=Pin.IRQ_FALLING, handler=self._on_button_irq)

        self._enter_photo()

    # --- IRQ (leves: só marcam flags) ---------------------------------------
    def _on_rotary_irq(self):
        self.rotary_event = True

    def _on_button_irq(self, pin):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_btn_ms) < 250:
            return                 # debounce
        self._last_btn_ms = now
        self.button_event = True

    # --- energia de periféricos ---------------------------------------------
    def enable_gps(self):
        _power(GPS_EN_PIN, True)
        if self.uart is None:
            self.uart = UART(1, baudrate=9600, tx=Pin(GPS_TX), rx=Pin(GPS_RX))
        self.uart_buffer = b""

    def disable_gps(self):
        try:
            if self.uart is not None:
                self.uart.deinit()
        except Exception:
            pass
        self.uart = None
        # pinos em nível baixo antes de cortar a energia (evita back-powering)
        Pin(GPS_TX, Pin.OUT).value(0)
        Pin(GPS_RX, Pin.OUT).value(0)
        _power(GPS_EN_PIN, False)

    def ensure_mag(self):
        if self.sensor is None:
            _power(MAG_EN_PIN, True)
            time.sleep_ms(50)
            self.sensor = QMC5883P(self.i2c)

    def release_mag(self):
        self.sensor = None
        _power(MAG_EN_PIN, False)

    # --- bateria ------------------------------------------------------------
    def battery_voltage(self):
        raw = 0
        for _ in range(8):
            raw += self.bat_adc.read_u16()
        raw //= 8
        return (raw / 65535) * 3.3 * BAT_DIVIDER

    def battery_pct(self):
        v = self.battery_voltage()
        if v < 2.5 or v > 5.0:
            return None            # sem divisor / leitura inválida
        if v >= BAT_CURVE[0][0]:
            return 100
        if v <= BAT_CURVE[-1][0]:
            return 0
        for i in range(len(BAT_CURVE) - 1):
            v1, p1 = BAT_CURVE[i]
            v2, p2 = BAT_CURVE[i + 1]
            if v2 <= v <= v1:
                return int(p2 + (p1 - p2) * (v - v2) / (v1 - v2))
        return None

    # --- transições de modo -------------------------------------------------
    def _enter_photo(self):
        self.disable_gps()
        self.release_mag()
        self.mode = "photo"
        self.photo_last = -2
        self.photo_msg = None
        self.photos.refresh()
        n = self.photos.count()
        self.encoder.set(
            value=self.photos.idx if n else 0,
            min_val=0, max_val=max(n - 1, 0),
            range_mode=RotaryIRQ.RANGE_WRAP,
        )
        self.tft.fill(0)

    def _enter_compass(self):
        self.ensure_mag()
        self.enable_gps()
        self.mode = "compass"
        self.compass_key = None
        self.compass_msg = None
        self.tft.fill(0)

    def _enter_calibration(self):
        self.ensure_mag()
        self.disable_gps()
        self._reset_calibration()
        self.mode = "calibration"

    def _open_menu(self, name):
        # sair de qualquer modo desliga GPS/magnetômetro (economia)
        self.disable_gps()
        self.release_mag()
        self.menu = name
        self.menu_idx = 0
        self.mode = "menu"
        self.encoder.set(
            value=0, min_val=0, max_val=len(MENUS[name]) - 1,
            range_mode=RotaryIRQ.RANGE_WRAP,
        )
        self.draw_menu()

    # --- eventos (consumidos no loop, fora da IRQ) --------------------------
    def _on_rotary(self):
        v = self.encoder.value()
        if self.mode == "photo":
            self.photos.idx = v
        elif self.mode == "menu":
            self.menu_idx = v
            self.draw_menu()
        elif self.mode == "decl_edit":
            self.decl_val = v
            self._draw_decl_edit()
        elif self.mode == "bright_edit":
            self.bright_val = v
            self.backlight.set(self.bright_val, save=False)   # preview ao vivo
            self._draw_bright_edit()

    def _on_button(self):
        if self.mode == "photo":
            self._open_menu("main")
        elif self.mode == "menu":
            self._select_menu_item()
        elif self.mode == "compass":
            self._open_menu("main")
        elif self.mode == "portal":
            self.portal_stop_request = True
        elif self.mode == "calibration":
            if self.cal_complete:
                self.cal_apply = True
            self._open_menu("settings")
        elif self.mode == "decl_edit":
            self.pending_declination = self.decl_val
            self._open_menu("offset")
        elif self.mode == "bright_edit":
            self.pending_brightness = self.bright_val
            self._open_menu("settings")

    def _select_menu_item(self):
        sel = MENUS[self.menu][self.menu_idx]

        if self.menu == "main":
            if sel == "Foto":
                self._enter_photo()
            elif sel == "Bussola":
                self._enter_compass()
            elif sel == "Portal":
                self.portal_start_request = True
            elif sel == "Config":
                self._open_menu("settings")

        elif self.menu == "settings":
            if sel == "Offset":
                self._open_menu("offset")
            elif sel == "Brilho":
                self.bright_val = self.backlight.level
                self.mode = "bright_edit"
                self.encoder.set(
                    value=self.bright_val, min_val=BRIGHT_MIN, max_val=100,
                    incr=5, range_mode=RotaryIRQ.RANGE_BOUNDED)
                self._draw_bright_edit()
            elif sel == "Calibracao":
                self._enter_calibration()
            elif sel == "Voltar":
                self._open_menu("main")

        elif self.menu == "offset":
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
                self.ensure_mag()
                self.decl_val = int(self.sensor.declination)
                self.mode = "decl_edit"
                self.encoder.set(
                    value=self.decl_val, min_val=-90, max_val=90,
                    range_mode=RotaryIRQ.RANGE_BOUNDED)
                self._draw_decl_edit()
            elif sel == "Voltar":
                self._open_menu("settings")

    # --- desenho dos menus / telas ------------------------------------------
    def draw_menu(self):
        tft = self.tft
        tft.fill(0)
        screen_w = tft.width()

        # bateria no topo (só no menu)
        bat = self.battery_pct()
        btxt = "Bat: {}%".format(bat) if bat is not None else "Bat: --"
        bcolor = RED if (bat is not None and bat <= 20) else GREEN
        tft.text(font, btxt, (screen_w - font.WIDTH * len(btxt)) // 2, 24, bcolor)

        # cabeçalho de declinação no submenu de offset
        if self.menu == "offset":
            decl = self.pending_declination
            if decl is None and self.sensor is not None:
                decl = self.sensor.declination
            if decl is not None:
                header = "Decl: {:.0f}".format(decl)
                tft.text(font, header, (screen_w - font.WIDTH * len(header)) // 2, 48, BLUE)

        items = MENUS[self.menu]
        row_h = font.HEIGHT + 8
        total_h = len(items) * row_h
        start_y = (tft.height() - total_h) // 2
        for idx, text in enumerate(items):
            x = (screen_w - font.WIDTH * len(text)) // 2
            y = start_y + idx * row_h
            color = WHITE if idx == self.menu_idx else GREY
            tft.text(font, text, x, y, color)

    def _draw_decl_edit(self):
        self.tft.fill(0)
        draw_center(self.tft, ["Declinacao", "{:+d} graus".format(self.decl_val),
                               "", "girar = ajustar", "apertar = ok"], WHITE)

    def _draw_bright_edit(self):
        self.tft.fill(0)
        draw_center(self.tft, ["Brilho", "{}%".format(self.bright_val),
                               "", "girar = ajustar", "apertar = ok"], WHITE)

    # --- foto ---------------------------------------------------------------
    def _show_photo(self):
        if self.photos.count() == 0:
            if self.photo_msg != "empty":
                self.photo_msg = "empty"
                self.tft.fill(0)
                draw_center(self.tft, ["Sem fotos", "", "use o Portal",
                                       "para enviar"], WHITE)
            return
        if self.photos.idx == self.photo_last:
            return
        self.photo_last = self.photos.idx
        self.photo_msg = None
        path = self.photos.current()
        try:
            self.tft.jpg(path, 0, 0)
        except Exception as e:
            self.tft.fill(0)
            draw_center(self.tft, ["Erro na foto", path.split("/")[-1]], RED)
            print("Erro jpg:", e)

    # --- GPS ----------------------------------------------------------------
    def process_uart(self):
        if self.uart is None or not self.uart.any():
            return
        self.uart_buffer += self.uart.read(self.uart.any())
        if b"\n" not in self.uart_buffer:
            return
        lines = self.uart_buffer.split(b"\n")
        for l in lines[:-1]:
            if l.startswith(b"$GPRMC") or l.startswith(b"$GNRMC"):
                try:
                    rmc = parse_rmc(l.decode().strip())
                    if rmc:
                        if rmc["dt"]:
                            self.latest_datetime = rmc["dt"]
                        self.gps_fix = rmc["fix"]
                        if rmc["lat"] is not None:
                            self.latest_lat = rmc["lat"]
                            self.latest_lon = rmc["lon"]
                except Exception:
                    pass
        self.uart_buffer = lines[-1]

    # --- bússola ------------------------------------------------------------
    def _compass_message(self, lines):
        if self.compass_msg == lines:
            return
        self.compass_msg = lines
        self.compass_key = None
        self.tft.fill(0)
        draw_center(self.tft, lines, WHITE)

    def _update_compass(self):
        if TARGET_LAT is None or TARGET_LON is None:
            self._compass_message(["Sem alvo", "", "defina em", "config.py"])
            return
        if not self.gps_fix or self.latest_lat is None:
            self._compass_message(["Bussola", "", "buscando", "GPS..."])
            return

        heading = self.sensor.heading()
        brg = bearing_deg(self.latest_lat, self.latest_lon, TARGET_LAT, TARGET_LON)
        rel = (brg - heading) % 360
        dist_txt = fmt_distance(
            haversine_km(self.latest_lat, self.latest_lon, TARGET_LAT, TARGET_LON))

        key = (int(rel) // 2, dist_txt)
        if key == self.compass_key:
            return
        self.compass_key = key
        self.compass_msg = None

        tft = self.tft
        cx, cy = tft.width() // 2, tft.height() // 2
        tft.fill(0)
        draw_compass_ring(tft, cx, cy, rel, GREEN, min(cx, cy) - 8)
        w = font.WIDTH * len(dist_txt)
        tft.text(font, dist_txt, cx - w // 2, cy - font.HEIGHT // 2, WHITE, BLACK)

    # --- calibração ---------------------------------------------------------
    def _reset_calibration(self):
        self.cal_min = [32767, 32767, 32767]
        self.cal_max = [-32768, -32768, -32768]
        self.cal_complete = False
        self.cal_setup_done = False

    def _draw_calibration_frame(self):
        tft = self.tft
        tft.fill(0)
        title = "CALIBRAR"
        tft.text(font, title, (tft.width() - font.WIDTH * len(title)) // 2, 80, WHITE)
        for i, axis in enumerate(CAL_AXES):
            y = CAL_BAR_YS[i]
            tft.text(font, axis, CAL_LABEL_X, y + (CAL_BAR_H - font.HEIGHT) // 2, WHITE)
            tft.rect(CAL_BAR_X, y, CAL_BAR_W, CAL_BAR_H, GREY)
        self.cal_setup_done = True

    def update_calibration(self):
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
            frac = min(rng / CAL_TARGET_RANGE, 1.0)
            if i < 2 and frac < 1.0:      # só X e Y decidem a conclusão
                all_full = False
            filled = int((CAL_BAR_W - 2) * frac)
            y = CAL_BAR_YS[i] + 1
            color = GREEN if frac >= 1.0 else BLUE
            if filled > 0:
                self.tft.fill_rect(CAL_BAR_X + 1, y, filled, CAL_BAR_H - 2, color)
            if filled < CAL_BAR_W - 2:
                self.tft.fill_rect(CAL_BAR_X + 1 + filled, y,
                                   (CAL_BAR_W - 2) - filled, CAL_BAR_H - 2, BLACK)
            vy = CAL_BAR_YS[i] + (CAL_BAR_H - font.HEIGHT) // 2
            self.tft.fill_rect(CAL_VAL_X, vy, CAL_VAL_W, font.HEIGHT, BLACK)
            self.tft.text(font, str(vals[i]), CAL_VAL_X, vy, WHITE)

        if all_full and not self.cal_complete:
            self.cal_complete = True
            msg1, msg2 = "CONCLUIDO", "aperte o botao"
            self.tft.text(font, msg1, (self.tft.width() - font.WIDTH * len(msg1)) // 2, 250, GREEN)
            self.tft.text(font, msg2, (self.tft.width() - font.WIDTH * len(msg2)) // 2, 262, WHITE)

    # --- portal (async: sobe/desce fora da IRQ) -----------------------------
    async def _enter_portal(self):
        self.disable_gps()
        self.release_mag()
        self.mode = "portal"
        self.tft.fill(0)
        draw_center(self.tft, ["Portal", "", "iniciando..."], WHITE)
        try:
            ip = await portal.start()
            self.tft.fill(0)
            draw_center(self.tft, ["Portal ativo", "", portal.AP_SSID, ip,
                                   "", "apertar = sair"], WHITE)
        except Exception as e:
            print("Erro portal:", e)
            self.tft.fill(0)
            draw_center(self.tft, ["Erro", "Portal/WiFi"], RED)

    async def _exit_portal(self):
        try:
            await portal.stop()
        except Exception as e:
            print("Erro ao parar portal:", e)
        self.photos.refresh()      # pega as fotos recém-enviadas
        self._open_menu("main")

    # --- pedidos assíncronos + tick por modo --------------------------------
    def _apply_pending(self):
        if self.cal_apply:
            self.cal_apply = False
            ox = (self.cal_min[0] + self.cal_max[0]) // 2
            oy = (self.cal_min[1] + self.cal_max[1]) // 2
            oz = (self.cal_min[2] + self.cal_max[2]) // 2
            if self.sensor is not None:
                self.sensor.set_calibration(ox, oy, oz)
        if self.pending_declination is not None:
            self.ensure_mag()
            self.sensor.set_declination(self.pending_declination)
            self.pending_declination = None
        if self.pending_brightness is not None:
            self.backlight.set(self.pending_brightness, save=True)
            self.pending_brightness = None

    async def _tick(self):
        if self.mode == "photo":
            self._show_photo()
        elif self.mode == "compass":
            self.process_uart()
            self._update_compass()
        elif self.mode == "calibration":
            self.update_calibration()
        # menu / edições / portal: telas estáticas ou tratadas por tarefas

    async def run(self):
        while True:
            if self.button_event:
                self.button_event = False
                self._on_button()
            if self.rotary_event:
                self.rotary_event = False
                self._on_rotary()
            if self.portal_start_request:
                self.portal_start_request = False
                await self._enter_portal()
            if self.portal_stop_request:
                self.portal_stop_request = False
                await self._exit_portal()
            self._apply_pending()
            await self._tick()
            await asyncio.sleep_ms(40)


def main():
    gc.collect()
    tft = tft_config.config(rotation=0)
    tft.init()
    tft.fill(0)

    backlight = Backlight()

    if not mount_sd():
        draw_center(tft, ["Sem cartao SD", "", "verifique o", "leitor micro SD"], RED)
        time.sleep(2)

    app = App(tft, backlight)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
