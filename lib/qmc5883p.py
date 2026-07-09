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

# COLE OS VALORES DA CALIBRAÇÃO AQUI ↓↓↓
OFFSET_X = -886
OFFSET_Y = -870
OFFSET_Z = -743
# -------------------------------------

class QMC5883P:
    def __init__(self, i2c):
        self.i2c = i2c
        self.init()

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

        x -= OFFSET_X
        y -= OFFSET_Y
        z -= OFFSET_Z

        return x, y, z

    def heading(self):
        x, y, _ = self.read_calibrated()

        ang = math.degrees(math.atan2(y, x))
        if ang < 0:
            ang += 360

        return ang
