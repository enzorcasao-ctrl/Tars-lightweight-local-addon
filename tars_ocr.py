"""Leitura do texto da tela (OCR, com o tesseract) pra apps que não
expõem os botões pela acessibilidade: Electron (Claude, Discord, VS Code),
jogos, apps Java, janelas remotas...

Com a imagem de uma janela: acha onde está um texto ("Enviar", "New chat",
"OK") e devolve o centro dele, pra clicar ali. Sem modelo de visão: roda
na CPU em ~0,3–1 s por janela.

Usa o programa tesseract pela linha de comando (sem dependência Python)."""

import difflib
import functools
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

CONFIANCA_MINIMA = 35      # 0–100, por palavra (o tesseract dá -1 pra linhas vazias)
SEMELHANCA_MINIMA = 0.78   # entre o texto pedido e o lido
LARGURA_MINIMA_AMPLIAR = 1600  # janelas menores são ampliadas 2x (fonte de interface é pequena)
TIMEOUT_OCR = 20
BORDA = 20
MODOS = (3, 11)

# Idioma da interface -> dados do tesseract
_IDIOMAS = {"pt_BR": "por", "pt": "por", "en": "eng", "es": "spa", "fr": "fra", "de": "deu"}


def disponivel():
    return shutil.which("tesseract") is not None


@functools.lru_cache(maxsize=1)
def idiomas_instalados():
    try:
        saida = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return frozenset()
    return frozenset(linha.strip() for linha in saida.splitlines()[1:] if linha.strip())


def idiomas_para(codigos_interface):
    """'por+eng' com o que estiver instalado (inglês sempre, se houver)."""
    instalados = idiomas_instalados()
    escolhidos = []
    for codigo in list(codigos_interface) + ["en"]:
        t = _IDIOMAS.get(codigo) or _IDIOMAS.get(codigo.split("_")[0])
        if t and t in instalados and t not in escolhidos:
            escolhidos.append(t)
    return "+".join(escolhidos) or "eng"


def _normalizar(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9+=*/%×÷-]+", " ", t).strip()


def ler(imagem, idiomas="eng"):
    """Palavras da imagem (PIL): [{'texto', 'x', 'y', 'largura', 'altura',
    'conf', 'linha'}], em pixels da imagem ORIGINAL."""
    escala = 2 if imagem.width < LARGURA_MINIMA_AMPLIAR else 1
    img = imagem.convert("L")
    if escala != 1:
        img = img.resize((img.width * escala, img.height * escala), Image.LANCZOS)
    # Tema escuro (texto claro em fundo escuro): o tesseract lê bem melhor
    # texto escuro em fundo claro.
    if ImageStat.Stat(img).mean[0] < 110:
        img = ImageOps.invert(img)
    # Borda: texto encostado na beirada da janela some no OCR.
    img = ImageOps.expand(img, border=BORDA, fill=255)
    with tempfile.TemporaryDirectory(prefix="opentars-ocr-") as pasta:
        arquivo = Path(pasta) / "janela.png"
        img.save(arquivo)
        # Dois jeitos de ler, juntos: o "página" (psm 3) pega texto dentro
        # de botões com borda; o "texto solto" (psm 11) pega rótulos
        # espalhados pela interface.
        palavras = []
        for modo in MODOS:
            for p in _ler_tsv(arquivo, idiomas, modo, escala):
                if not any(_mesma_palavra(p, q) for q in palavras):
                    palavras.append(p)
    return palavras


def _ler_tsv(arquivo, idiomas, modo, escala):
    try:
        saida = subprocess.run(
            ["tesseract", str(arquivo), "stdout", "-l", idiomas, "--psm", str(modo), "tsv"],
            capture_output=True, text=True, timeout=TIMEOUT_OCR,
        ).stdout
    except Exception:
        return []

    palavras = []
    for linha in saida.splitlines()[1:]:
        campos = linha.split("\t")
        if len(campos) < 12 or not campos[11].strip():
            continue
        try:
            conf = float(campos[10])
        except ValueError:
            continue
        if conf < CONFIANCA_MINIMA or not re.search(r"\w|[=+*/%×÷-]", campos[11]):
            continue
        x, y = (int(campos[6]) - BORDA) // escala, (int(campos[7]) - BORDA) // escala
        w, h = int(campos[8]) // escala, int(campos[9]) // escala
        palavras.append({
            "texto": campos[11].strip(), "x": x, "y": y, "largura": w, "altura": h, "conf": conf,
            "linha": (modo, int(campos[2]), int(campos[3]), int(campos[4])),  # modo, bloco, parágrafo, linha
        })
    return palavras


def _mesma_palavra(a, b):
    """A mesma palavra lida pelos dois modos (mesmo texto, caixas sobrepostas)."""
    if _normalizar(a["texto"]) != _normalizar(b["texto"]):
        return False
    ix = min(a["x"] + a["largura"], b["x"] + b["largura"]) - max(a["x"], b["x"])
    iy = min(a["y"] + a["altura"], b["y"] + b["altura"]) - max(a["y"], b["y"])
    return ix > 0 and iy > 0


def linhas(palavras):
    """Texto de cada linha, na ordem de leitura (pra IA ler a janela)."""
    grupos = {}
    for p in palavras:
        grupos.setdefault(p["linha"], []).append(p)
    ordenadas = sorted(grupos.values(), key=lambda g: (min(p["y"] for p in g), min(p["x"] for p in g)))
    return [" ".join(p["texto"] for p in sorted(g, key=lambda p: p["x"])) for g in ordenadas]


def achar(palavras, alvo):
    """Onde está o texto 'alvo' (uma ou mais palavras seguidas numa linha).
    Devolve {'x', 'y' (centro), 'texto', 'semelhanca'} ou None."""
    alvo_n = _normalizar(alvo)
    if not alvo_n:
        return None
    n = len(alvo_n.split())
    grupos = {}
    for p in palavras:
        grupos.setdefault(p["linha"], []).append(p)

    melhor = None
    for grupo in grupos.values():
        grupo = sorted(grupo, key=lambda p: p["x"])
        for tamanho in {max(1, n - 1), n, n + 1}:
            for i in range(0, len(grupo) - tamanho + 1):
                trecho = grupo[i:i + tamanho]
                lido = _normalizar(" ".join(p["texto"] for p in trecho))
                if not lido:
                    continue
                nota = 1.0 if lido == alvo_n else difflib.SequenceMatcher(None, lido, alvo_n).ratio()
                # Texto de 1–2 letras ("=", "7", "OK") só vale exato.
                if len(alvo_n) <= 2 and lido != alvo_n:
                    continue
                if nota >= SEMELHANCA_MINIMA and (melhor is None or nota > melhor["semelhanca"]):
                    x0 = min(p["x"] for p in trecho)
                    y0 = min(p["y"] for p in trecho)
                    x1 = max(p["x"] + p["largura"] for p in trecho)
                    y1 = max(p["y"] + p["altura"] for p in trecho)
                    melhor = {"x": (x0 + x1) // 2, "y": (y0 + y1) // 2, "texto": " ".join(p["texto"] for p in trecho),
                              "semelhanca": nota}
    return melhor


def parecidos(palavras, alvo, n=6):
    """Textos da janela mais parecidos com o pedido (pra sugerir à IA)."""
    candidatos = list(dict.fromkeys(linhas(palavras)))
    alvo_n = _normalizar(alvo)
    return sorted(candidatos, key=lambda c: -difflib.SequenceMatcher(None, _normalizar(c), alvo_n).ratio())[:n]


# ------------------------------------------------------------
# Localizar pela VISÃO, em grade (ícones sem texto)
# ------------------------------------------------------------
#
# O OCR só acha o que está ESCRITO. Pra um ícone (lixeira, enviar, ⚙) o
# openTARS pergunta a um modelo com visão, mas não "em que pixel está?"
# (cada modelo responde num formato, e a maioria erra coordenadas): a
# janela ganha uma grade com rótulos (A1, B2...) e o modelo só diz a
# CÉLULA. A célula escolhida é ampliada e ganha outra grade, e de novo:
# em 3 rodadas o erro cai de ~300 px pra ~25 px, com qualquer modelo com
# visão.

from PIL import ImageDraw, ImageFont  # noqa: E402

NIVEIS_GRADE = ((4, 3), (4, 3), (4, 4))   # (colunas, linhas) de cada rodada
FOLGA_RECORTE = 0.25                      # o alvo pode estar na borda da célula
LARGURA_MINIMA_VISAO = 640                # recorte pequeno é ampliado pro modelo enxergar
_LETRAS = "ABCDEFGH"


def rotulos(colunas, linhas):
    return [f"{_LETRAS[c]}{l + 1}" for l in range(linhas) for c in range(colunas)]


def celulas(largura, altura, colunas, linhas):
    """rótulo -> (x0, y0, x1, y1) em pixels de uma imagem largura x altura."""
    saida = {}
    for l in range(linhas):
        for c in range(colunas):
            saida[f"{_LETRAS[c]}{l + 1}"] = (c * largura / colunas, l * altura / linhas,
                                             (c + 1) * largura / colunas, (l + 1) * altura / linhas)
    return saida


@functools.lru_cache(maxsize=8)
def _fonte(tamanho):
    for nome in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(nome, tamanho)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=tamanho)
    except TypeError:  # Pillow antigo
        return ImageFont.load_default()


def desenhar_grade(imagem, colunas, linhas):
    """Cópia da imagem (ampliada se for pequena) com a grade e os rótulos."""
    img = imagem.convert("RGB")
    if img.width < LARGURA_MINIMA_VISAO:
        fator = LARGURA_MINIMA_VISAO / img.width
        img = img.resize((round(img.width * fator), round(img.height * fator)), Image.LANCZOS)
    else:
        img = img.copy()
    desenho = ImageDraw.Draw(img, "RGBA")
    largura, altura = img.size
    espessura = max(2, largura // 400)
    for c in range(1, colunas):
        x = round(c * largura / colunas)
        desenho.line([(x, 0), (x, altura)], fill=(255, 0, 0, 210), width=espessura)
    for l in range(1, linhas):
        y = round(l * altura / linhas)
        desenho.line([(0, y), (largura, y)], fill=(255, 0, 0, 210), width=espessura)
    fonte = _fonte(max(14, min(32, largura // (colunas * 7))))
    for rotulo, (x0, y0, _, _) in celulas(largura, altura, colunas, linhas).items():
        caixa = desenho.textbbox((x0 + 4, y0 + 3), rotulo, font=fonte)
        desenho.rectangle((caixa[0] - 3, caixa[1] - 2, caixa[2] + 3, caixa[3] + 2), fill=(0, 0, 0, 190))
        desenho.text((x0 + 4, y0 + 3), rotulo, fill=(255, 235, 0), font=fonte)
    return img


def localizar_por_grade(imagem, alvo, perguntar, janela=""):
    """Centro (x, y) do 'alvo' na imagem, ou None se o modelo não o vê.

    perguntar(imagem_com_grade, prompt, opcoes) -> um rótulo das opções
    (ou "none"): quem chama liga isso a um modelo com visão."""
    x_base = y_base = 0.0
    recorte = imagem
    centro = None
    for rodada, (colunas, linhas) in enumerate(NIVEIS_GRADE):
        opcoes = rotulos(colunas, linhas)
        prompt = (
            f"This is {'a screenshot of the window' if rodada == 0 else 'a zoomed part of the window'} "
            f'"{janela}", with a red grid drawn over it. Each cell has its label (A1, B2, ...) in its '
            f'top-left corner. Which cell contains the CENTER of this element: "{alvo}"? '
            'If you cannot see it, answer "none".'
        )
        resposta = perguntar(desenhar_grade(recorte, colunas, linhas), prompt, opcoes + ["none"])
        if resposta not in opcoes:
            # Na 1ª rodada, "não vejo": desiste. Depois, fica com a última
            # célula (o zoom pode ter cortado um pedaço do elemento).
            break
        x0, y0, x1, y1 = celulas(recorte.width, recorte.height, colunas, linhas)[resposta]
        centro = (x_base + (x0 + x1) / 2, y_base + (y0 + y1) / 2)
        fx, fy = (x1 - x0) * FOLGA_RECORTE, (y1 - y0) * FOLGA_RECORTE
        nx0, ny0 = max(0.0, x0 - fx), max(0.0, y0 - fy)
        nx1, ny1 = min(recorte.width, x1 + fx), min(recorte.height, y1 + fy)
        recorte = recorte.crop((round(nx0), round(ny0), round(nx1), round(ny1)))
        x_base, y_base = x_base + round(nx0), y_base + round(ny0)
    if not centro:
        return None
    # A grade erra até meia célula (~25 px). Um ícone costuma se destacar do
    # fundo: mira no centro do que estiver ali.
    return ajustar_ao_elemento(imagem, round(centro[0]), round(centro[1]))


RAIO_AJUSTE = 40          # px em volta do ponto estimado
AREA_MINIMA_ELEMENTO = 12  # px: menos que isso é serrilhado/ruído
DIFERENCA_DO_FUNDO = 60    # soma das diferenças R+G+B pra contar como "não é fundo"


def ajustar_ao_elemento(imagem, x, y, raio=RAIO_AJUSTE):
    """Centro do elemento (o que difere do fundo) mais perto de (x, y),
    dentro de um quadrado de lado 2*raio. Sem nada claro ali, (x, y)."""
    img = imagem.convert("RGB")
    x0, y0 = max(0, x - raio), max(0, y - raio)
    x1, y1 = min(img.width, x + raio + 1), min(img.height, y + raio + 1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        return x, y
    janela = img.crop((x0, y0, x1, y1))
    largura, altura = janela.size
    px = janela.load()
    borda = [px[i, 0] for i in range(largura)] + [px[i, altura - 1] for i in range(largura)] + \
            [px[0, j] for j in range(altura)] + [px[largura - 1, j] for j in range(altura)]
    fundo = max(set(borda), key=borda.count)

    def diferente(c):
        return abs(c[0] - fundo[0]) + abs(c[1] - fundo[1]) + abs(c[2] - fundo[2]) > DIFERENCA_DO_FUNDO

    marca = [[diferente(px[i, j]) for i in range(largura)] for j in range(altura)]
    visto = [[False] * largura for _ in range(altura)]
    px_x, px_y = x - x0, y - y0
    melhor = None  # (distância, cx, cy)
    for j in range(altura):
        for i in range(largura):
            if not marca[j][i] or visto[j][i]:
                continue
            pilha, pontos = [(i, j)], []
            visto[j][i] = True
            while pilha:
                a, b = pilha.pop()
                pontos.append((a, b))
                for na, nb in ((a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)):
                    if 0 <= na < largura and 0 <= nb < altura and marca[nb][na] and not visto[nb][na]:
                        visto[nb][na] = True
                        pilha.append((na, nb))
            if len(pontos) < AREA_MINIMA_ELEMENTO or len(pontos) > 0.7 * largura * altura:
                continue
            xs = [a for a, _ in pontos]
            ys = [b for _, b in pontos]
            bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
            dx = max(bx0 - px_x, 0, px_x - bx1)
            dy = max(by0 - px_y, 0, px_y - by1)
            distancia = dx * dx + dy * dy
            if melhor is None or distancia < melhor[0]:
                melhor = (distancia, (bx0 + bx1) / 2, (by0 + by1) / 2)
    if melhor is None:
        return x, y
    return round(x0 + melhor[1]), round(y0 + melhor[2])
