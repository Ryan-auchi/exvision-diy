"""config.py — configuração do usuário.

Coordenada-alvo da bússola. A bússola calcula o rumo (bearing) e a distância
da sua posição atual (GPS) até este ponto e desenha uma seta apontando pra ele.

Preencha com a coordenada do seu ponto de interesse (graus decimais):
    TARGET_LAT = -2.9055     # Sul é negativo
    TARGET_LON = -41.7767    # Oeste é negativo

Enquanto estiver None, a bússola mostra "Sem alvo" em vez da seta.
Pegue lat/lon em graus decimais no Google Maps (clique no local -> coordenadas).
"""

TARGET_LAT = None
TARGET_LON = None
