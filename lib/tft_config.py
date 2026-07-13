"""tft_config.py — display ILI9341 240x320 (driver st7789_mpy).

Esta branch assume a troca do GC9A01 (redondo) pelo ILI9341 (retrato 240x320)
e um leitor micro SD no MESMO barramento SPI0 (compartilhado).

Barramento SPI0 (compartilhado display + SD):
    SCK  = GP18
    MOSI = GP19
    MISO = GP16   (usado pelo SD; o display não lê)

Display ILI9341:
    CS   = GP2
    DC   = GP13
    RST  = GP28
    (backlight é controlado por PWM/MOSFET no GP15, ver Backlight no main.py)

SD (mesmo SPI0):
    CS   = GP22   (ver SD_CS no main.py)

O driver é compilado no .uf2 (módulo C). O nome/força do init dependem do seu
build: o repo russhughes st7789_mpy cobre o ILI9341. Se o seu módulo tiver
outro nome ou construtor, ajuste APENAS este arquivo — o resto do código usa a
API comum (init/fill/text/line/jpg/fill_rect/width/height/rotation).
"""

from machine import Pin, SPI
import st7789 as display

# exposto para o resto do código (evita acoplar o nome do módulo do driver)
color565 = display.color565

# largura x altura em retrato
WIDTH = 240
HEIGHT = 320

# pinos do barramento (o mesmo objeto SPI é reaproveitado pelo SD, ver main.py)
SCK = 18
MOSI = 19
MISO = 16

_spi = None


def spi_bus():
    """Cria (uma vez) e devolve o SPI0 compartilhado por display e SD.

    Inclui MISO porque o SD precisa ler. 24 MHz é um meio-termo seguro no
    barramento compartilhado (o SD costuma não gostar de 40 MHz + fiação de
    protoboard). Suba depois se a leitura do cartão aguentar.
    """
    global _spi
    if _spi is None:
        _spi = SPI(0, baudrate=24000000, sck=Pin(SCK), mosi=Pin(MOSI), miso=Pin(MISO))
    return _spi


def config(rotation=0, buffer_size=0, options=0):
    """Configura o ILI9341 e devolve a instância do display."""
    return display.ST7789(
        spi_bus(),
        WIDTH,
        HEIGHT,
        reset=Pin(28, Pin.OUT),
        cs=Pin(2, Pin.OUT),
        dc=Pin(13, Pin.OUT),
        rotation=rotation,
        options=options,
        buffer_size=buffer_size,
    )
