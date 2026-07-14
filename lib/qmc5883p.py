from machine import I2C
import math
import time

QMC5883P_ADDR = 0x2C

MODE_REG   = 0x0A
CONFIG_REG = 0x0B

X_LSB_REG = 0x01
X_MSB_REG = 0x02
Y_LSB_REG = 0x03
Y_MSB_REG = 0x04
Z_LSB_REG = 0x05
Z_MSB_REG = 0x06

# Offsets padrão (fallback). A calibração pela tela salva em CALIB_FILE e
# sobrescreve estes valores no boot seguinte.
OFFSET_X = -886
OFFSET_Y = -870
OFFSET_Z = -743

CALIB_FILE = "calib.txt"
DECL_FILE = "decl.txt"

# Orientação dos eixos do magnetômetro em relação ao aparelho.
# Os eixos X/Y do chip podem estar trocados e/ou invertidos -> a bússola aponta
# torta/espelhada. As 8 combinações possíveis estão cobertas por este modo
# (0..7): bit0 = troca X<->Y, bit1 = nega X, bit2 = nega Y.
#
# Como acertar (tela Tests -> Magnetometro):
#   1. Aponte a FRENTE do aparelho para o Norte real -> Rumo deve dar ~0.
#   2. Aponte a FRENTE para o Leste -> Rumo deve dar ~90 (girar p/ direita
#      aumenta os graus). S = 180, O = 270.
#   Se não bater, incremente HEADING_MODE (0,1,2,...,7) e teste de novo.
#   Uma das 8 combinações deixa o rumo correto.
HEADING_MODE = 4

# Ajuste fino do zero (graus), somado ao rumo. Corrige um deslocamento CONSTANTE
# depois que o sentido de rotação (HEADING_MODE) já está certo. Ex.: a tela
# montada deitada gira o desenho 90 graus. Se a seta ficar 90 graus torta, use
# 90/180/270 até acertar; se já estiver certa, deixe 0.
HEADING_OFFSET = 90

class QMC5883P:
    def __init__(self, i2c):
        self.i2c = i2c
        self.offset_x = OFFSET_X
        self.offset_y = OFFSET_Y
        self.offset_z = OFFSET_Z
        self.declination = 0.0   # graus somados ao rumo (Norte verdadeiro)
        self.load_calibration()
        self.load_declination()
        self.init()

    def load_declination(self):
        """Carrega a declinação salva; ignora se não existir."""
        try:
            with open(DECL_FILE) as f:
                self.declination = float(f.read().strip())
        except (OSError, ValueError):
            pass

    def set_declination(self, degrees, save=True):
        """Define a declinação (efeito imediato) e opcionalmente persiste."""
        self.declination = float(degrees)
        if save:
            try:
                with open(DECL_FILE, "w") as f:
                    f.write(str(self.declination))
            except OSError:
                pass

    def load_calibration(self):
        """Carrega offsets salvos por set_calibration(); ignora se não existir."""
        try:
            with open(CALIB_FILE) as f:
                ox, oy, oz = f.read().strip().split(",")
                self.offset_x = int(ox)
                self.offset_y = int(oy)
                self.offset_z = int(oz)
        except (OSError, ValueError):
            pass  # sem arquivo válido: mantém os offsets padrão

    def set_calibration(self, ox, oy, oz, save=True):
        """Aplica novos offsets (efeito imediato) e opcionalmente persiste."""
        self.offset_x, self.offset_y, self.offset_z = ox, oy, oz
        if save:
            try:
                with open(CALIB_FILE, "w") as f:
                    f.write("{},{},{}".format(ox, oy, oz))
            except OSError:
                pass

    def init(self):
        self.i2c.writeto_mem(QMC5883P_ADDR, MODE_REG, bytes([0xCF]))   # Continuous mode
        time.sleep_ms(10)

        self.i2c.writeto_mem(QMC5883P_ADDR, CONFIG_REG, bytes([0x08])) # Set/Reset + 8G
        time.sleep_ms(10)

    def read_raw(self):
        data = self.i2c.readfrom_mem(QMC5883P_ADDR, X_LSB_REG, 6)

        x = (data[1] << 8) | data[0]
        y = (data[3] << 8) | data[2]
        z = (data[5] << 8) | data[4]

        if x >= 32768: x -= 65536
        if y >= 32768: y -= 65536
        if z >= 32768: z -= 65536

        return x, y, z

    def read_calibrated(self):
        x, y, z = self.read_raw()

        x -= self.offset_x
        y -= self.offset_y
        z -= self.offset_z

        return x, y, z

    def heading(self):
        x, y, _ = self.read_calibrated()

        # aplica a orientação dos eixos escolhida em HEADING_MODE
        if HEADING_MODE & 1:
            x, y = y, x
        if HEADING_MODE & 2:
            x = -x
        if HEADING_MODE & 4:
            y = -y

        ang = math.degrees(math.atan2(y, x)) + self.declination + HEADING_OFFSET
        ang %= 360

        return ang
