"""main2.py — TESTE de isolamento do glitch de 1 Hz no display.

Faz o MÍNIMO possível: inicializa só o display, desenha o menu uma vez e
fica em loop parado. NÃO cria o magnetômetro, NÃO abre UART/GPS e NÃO importa
`network` (Wi-Fi). Serve para descobrir se o glitch vem de algum periférico ou
da alimentação/fiação do próprio display.

Como usar: salve como `main.py` no Pico (ou rode manualmente) e observe a tela.
  - Glitch SUMIU  -> a causa é um periférico (sensor/Wi-Fi). Reintroduza um
                     de cada vez até o glitch voltar.
  - Glitch CONTINUA -> é elétrico: alimentação/decoupling ou fiação do SPI.
"""

import gc
from time import sleep

import vga1_8x8 as font
import gc9a01
import tft_config


WHITE = gc9a01.color565(255, 255, 255)
GREY = gc9a01.color565(100, 100, 100)

MENU_ITEMS = ["Calibration", "Date/Time", "Compass", "Test"]
SELECTED = 0  # item destacado (fixo, só para desenhar algo parecido com o menu)


def init_display():
    gc.collect()
    tft = tft_config.config(tft_config.TALL)
    tft.init()
    tft.rotation(3)
    tft.fill(0)
    return tft


def draw_menu(tft):
    """Menu centralizado na tela redonda (igual ao main.py, sem interação)."""
    tft.fill(0)
    screen_w = tft.width()
    screen_h = tft.height()
    total_h = len(MENU_ITEMS) * font.HEIGHT
    start_y = (screen_h - total_h) // 2
    for idx, text in enumerate(MENU_ITEMS):
        x = (screen_w - font.WIDTH * len(text)) // 2
        y = start_y + idx * font.HEIGHT
        color = WHITE if idx == SELECTED else GREY
        tft.text(font, text, x, y, color)


def main():
    tft = init_display()
    draw_menu(tft)
    # tela estática: se algo piscar/glitchar aqui, NÃO é o software desenhando
    while True:
        sleep(0.05)


if __name__ == "__main__":
    main()
