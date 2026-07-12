# Estrutura do projeto — EX-VISION DIY

Projeto MicroPython para Raspberry Pi Pico W. São **dois firmwares/aplicações**
que compartilham o mesmo código-base, um por Pico:

| Pico | Entrada (boot) | O que faz |
|------|----------------|-----------|
| **Bússola/GPS** (tela + encoder conectados) | `main.py` | Menu com submenus na tela redonda: bússola, data/hora por GPS, calibração interativa, testes e configurações (declinação, brilho) |
| **Só Wi-Fi** (rádio funcionando, sem tela/encoder) | `captive_portal.py` | Portal cativo: sobe Access Point e serve a galeria de imagens |

Cada Pico roda o `main.py` do MicroPython no boot. No Pico "só Wi-Fi", salve o
`captive_portal.py` **como** `main.py` (ou crie um `main.py` de uma linha:
`import captive_portal; captive_portal.start()`).

## Árvore de arquivos

```
exvision-diy/
├── main.py                 # ENTRADA 1 — app da bússola (tela + encoder + GPS)
├── captive_portal.py       # ENTRADA 2 — app do Pico só-Wi-Fi (portal cativo)
├── main2.py                # TESTE — boot mínimo (só display) p/ isolar ruído/tearing
│
├── lib/                    # bibliotecas e drivers (MicroPython adiciona /lib ao path)
│   ├── phew/               # micro-framework web (AP, rotas, DNS catch-all)
│   ├── micropython_phew-0.0.3.dist-info/
│   ├── tft_config.py       # configuração do display redondo GC9A01 (pinos, clock SPI)
│   ├── ssd1306.py          # driver de display OLED (alternativo)
│   ├── qmc5883p.py         # driver do magnetômetro + calibração/declinação persistentes
│   ├── raspy_qmc5883l/     # driver alternativo do magnetômetro
│   ├── rotary.py           # base do encoder rotativo
│   └── rotary_irq_rp2.py   # encoder rotativo por interrupção (usado no menu)
│
├── www/                    # arquivos servidos por HTTP pelo portal
│   ├── index.html          # página da galeria (upload + lista de imagens)
│   ├── style.css           # estilo da galeria
│   ├── croppermin.js       # biblioteca Cropper.js (recorte de imagem) — reservada
│   └── portal.html         # página-stub antiga (não usada atualmente)
│
├── download/               # DADOS: imagens no Pico (fora do Git — ver .gitignore)
│
├── ESTRUTURA.md            # este arquivo
└── .gitignore

# Gerados no próprio Pico (não versionados):
#   calib.txt   → offsets da calibração da bússola
#   decl.txt    → declinação magnética escolhida
#   bright.txt  → último brilho da tela
```

### Por que essa divisão

- **`lib/`** — o MicroPython já coloca `/lib` no `sys.path` (é de lá que o phew
  já era carregado). Drivers de terceiros ficam separados do seu código.
- **`www/`** — assets web separados da lógica. O `captive_portal.py` lê daqui
  (`www/index.html`, `www/style.css`).
- **`download/`** — são **dados** (imagens), não código. Ficam na raiz porque os
  dois apps usam: a bússola desenha `download/0.jpg`…`download/89.jpg` conforme
  a direção, e o portal lista/recebe imagens dessa pasta. Está no `.gitignore`.
- **Duas entradas na raiz** (`main.py` e `captive_portal.py`) deixam claro, de
  cara, que existem dois modos de uso.

## Tela principal (Pico da bússola)

O display é **redondo** (GC9A01), então **todo texto é centralizado** horizontal
e verticalmente — os cantos ficam cortados. Isso é feito pelo helper
`draw_center()` em `main.py`. As **imagens não são centralizadas** por código:
elas já foram preparadas para preencher a tela redonda e são desenhadas em
`(0, 0)` ocupando a tela inteira.

O menu é controlado pelo **encoder rotativo** (girar = mover, apertar =
selecionar). Os menus e submenus ficam no dict `MENUS` (`main.py`); cada submenu
tem um item **"Voltar"**. Ao trocar de menu, a faixa do encoder é reconfigurada
(`_open_menu`).

### Árvore de menus

```
MENU PRINCIPAL
├─ Calibration   → calibração interativa da bússola (barras X/Y)
├─ Date/Time     → data/hora via GPS
├─ Compass       → bússola (imagem + rumo na tela)
├─ Tests ─────►  ├─ WiFi          → Access Point + servidor HTTP de teste
│                ├─ Magnetometro  → X/Y/Z + rumo ao vivo
│                └─ Voltar
└─ Settings ──►  ├─ Parnaiba PI   → declinação -21.4°
                 ├─ Albufeira PT  → declinação -1.7°
                 ├─ Sem offset    → 0° (Norte magnético)
                 ├─ Custom        → ajusta a declinação com o encoder
                 ├─ Brilho        → ajusta o brilho da tela
                 └─ Voltar
```

### O que cada opção faz

| Opção | Modo interno | O que aparece / faz |
|-------|--------------|---------------------|
| **Calibration** | `calibration` | Barras **X/Y** que enchem conforme você gira o dispositivo (faixa `máx−mín` de cada eixo), com os valores brutos ao vivo ao lado. Conclui só com X e Y (o rumo não usa Z). Ao apertar em **CONCLUIDO**, salva os offsets em `calib.txt`. |
| **Date/Time** | `datetime` | Liga o GPS, lê NMEA `RMC` (`parse_datetime_from_rmc`) e mostra **dd/mm/aaaa HH:MM:SS** |
| **Compass** | `compass` | Rumo já com declinação; **rotaciona a tela** e desenha `download/<n>.jpg`. Overlay de teste com **graus + cardeal**. Redesenha só quando a imagem/rumo muda (anti-tearing). |
| **Tests → WiFi** | `test` | Sobe AP `PicoConfig` + servidor HTTP simples; mostra o IP centralizado (ou tela vermelha em falha de rádio) |
| **Tests → Magnetometro** | `magtest` | **X, Y, Z** brutos ao vivo + **Rumo** em graus/cardeal |
| **Settings → declinação** | — | Aplica a declinação (preset ou `Custom` via encoder) e salva em `decl.txt` |
| **Settings → Brilho** | `bright_edit` | Ajusta o brilho por PWM, **preview ao vivo**, salva em `bright.txt` |

### Detalhes de hardware / implementação

- **Brilho da tela:** PWM no **GPIO15** (gate de um MOSFET), classe `Backlight`.
  A flag `BACKLIGHT_INVERT` acerta a polaridade do MOSFET; `BRIGHT_MIN` é o piso
  para não apagar a tela por engano.
- **Declinação:** somada em `sensor.heading()` para apontar ao **Norte
  verdadeiro**. Os valores dos presets são **aproximados** — confira o exato em
  magnetic-declination.com.
- **Persistência:** calibração (`calib.txt`), declinação (`decl.txt`) e brilho
  (`bright.txt`) são gravados no próprio Pico. As **escritas de arquivo acontecem
  no loop `update()`, fora da IRQ do botão** (via flags `cal_apply`,
  `pending_declination`, `pending_brightness`), porque I/O dentro de IRQ é
  arriscado no MicroPython.
- **Navegação (`_on_button`):** dentro de uma view, apertar volta ao menu de
  origem; ao sair do WiFi, o rádio é desligado (`stop_wifi`). Entrar em
  *Compass*/*Calibration* desliga o GPS para economizar energia (`disable_gps`).
- **Anti-tearing:** a bússola guarda a última imagem/rotação/texto desenhados e
  só reescreve a tela quando mudam; o clock do SPI está em 40 MHz
  ([tft_config.py](lib/tft_config.py)).

## Portal cativo (`captive_portal.py`)

Fluxo em `start()`:

1. `access_point("EX-VISION")` — sobe o AP (rede **aberta** por padrão, para o
   popup do portal aparecer sozinho no celular; defina `AP_PASSWORD` para exigir
   senha).
2. `dns.run_catchall(ip)` — servidor DNS que resolve **qualquer domínio** para o
   IP do Pico; combinado com o handler `@server.catchall()` (redireciona rota
   desconhecida para `/`), faz o celular abrir a página automaticamente.
3. `server.run()` — event loop das rotas.

### Rotas

| Rota | Método | Função |
|------|--------|--------|
| `/` | GET | Galeria: injeta lista de imagens e espaço livre no `www/index.html` |
| `/style.css` | GET | Serve `www/style.css` |
| `/download/<nome>` | GET | Serve a imagem de `download/` (streaming) |
| `/upload` | POST | Recebe a imagem (upload com barra de progresso) |
| `/delete?name=` | GET | Apaga a imagem e redireciona para `/` |
| catch-all | — | Redireciona para `/` (dispara o popup do portal) |

### Detalhe importante — upload

O parser multipart embutido do phew lê o corpo inteiro como *string*, o que
**corrompe JPEG binário e estoura a RAM** do Pico. Por isso o `captive_portal.py`
substitui `phew.server._parse_form_data` por `_parse_form_data_streaming`, que
grava o arquivo direto no disco em blocos (binary-safe, memória constante). O
override é feito no próprio `captive_portal.py`, **sem editar os arquivos do
phew** em `lib/`.
