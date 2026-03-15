from machine import Pin, SPI
import gc9a01

from machine import Pin, SPI
import gc9a01

TFA = 0
BFA = 0
WIDE = 1
TALL = 0


def config(rotation=0, buffer_size=0, options=0):
    """Configure the display and return an instance of gc9a01.GC9A01."""

    spi = SPI(0, baudrate=60000000, sck=Pin(18), mosi=Pin(19))
    return gc9a01.GC9A01(
        spi,
        240,
        240,
        reset=Pin(28, Pin.OUT),
        cs=Pin(2, Pin.OUT),
        dc=Pin(13, Pin.OUT),
        backlight=Pin(1, Pin.OUT),
        rotation=rotation,
        options=options,
        buffer_size=buffer_size,
    )


