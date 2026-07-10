# Estrutura do projeto — EX-VISION DIY

Projeto MicroPython para Raspberry Pi Pico W. São **dois firmwares/aplicações**
que compartilham o mesmo código-base, um por Pico:

| Pico | Entrada (boot) | O que faz |
|------|----------------|-----------|
| **Bússola/GPS** (tela + encoder conectados) | `main.py` | Menu na tela redonda, bússola, data/hora por GPS, calibração e modo de teste |
| **Só Wi-Fi** (rádio funcionando, sem tela/encoder) | `captive_portal.py` | Portal cativo: sobe Access Point e serve a galeria de imagens |

Cada Pico roda o `main.py` do MicroPython no boot. No Pico "só Wi-Fi", salve o
`captive_portal.py` **como** `main.py` (ou crie um `main.py` de uma linha:
`import captive_portal; captive_portal.start()`).

## Árvore de arquivos

```
exvision-diy/
├── main.py                 # ENTRADA 1 — app da bússola (tela + encoder + GPS)
├── captive_portal.py       # ENTRADA 2 — app do Pico só-Wi-Fi (portal cativo)
│
├── lib/                    # bibliotecas e drivers (MicroPython adiciona /lib ao path)
│   ├── phew/               # micro-framework web (AP, rotas, DNS catch-all)
│   ├── micropython_phew-0.0.3.dist-info/
│   ├── tft_config.py       # configuração do display redondo GC9A01
│   ├── ssd1306.py          # driver de display OLED (alternativo)
│   ├── qmc5883p.py         # driver do magnetômetro (bússola)
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
selecionar). Os itens estão em `App.MENU_ITEMS`:

| Opção do menu | Modo interno | Onde está no código | O que aparece na tela |
|---------------|--------------|---------------------|-----------------------|
| **Calibration** | `"calibration"` | `App.update()` → lê `sensor.read_raw()` | Valores brutos **X / Y / Z** do magnetômetro, centralizados (3 linhas) |
| **Date/Time** | `"datetime"` | `App.enable_gps()` + `process_uart()` + `App.update()` | Liga o GPS, lê sentenças NMEA `RMC` (`parse_datetime_from_rmc`) e mostra **dd/mm/aaaa HH:MM:SS** centralizado |
| **Compass** | `"compass"` | `App.update()` → `update_display()` | Lê o rumo (`sensor.heading()`), **rotaciona a tela** conforme a direção e desenha a imagem `download/<n>.jpg` correspondente (tela cheia) |
| **Test** | `"test"` | `App.start_wifi()` + `App._webserver()` | Sobe um Access Point `PicoConfig` e um servidor HTTP simples; mostra **"Portal ativo / PicoConfig / <IP>"** centralizado. Em falha de rádio: tela vermelha com **"Erro Hardware / WiFi"** |

Fluxo de navegação (`App._on_button`): dentro de um modo, apertar o botão volta
ao menu; ao sair do modo *Test*, o Wi-Fi é desligado (`stop_wifi`). Ao entrar em
*Compass* ou *Calibration*, o GPS é desligado para economizar energia
(`disable_gps`).

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
