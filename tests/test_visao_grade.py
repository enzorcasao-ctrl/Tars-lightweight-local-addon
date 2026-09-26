"""2.9: apps sem acessibilidade — ícone sem texto (visão em grade) e campo
achado pelo texto que ele mostra (OCR).

O "modelo de visão" aqui é falso mas ENXERGA de verdade: acha os pixels
verdes do ícone na imagem com a grade que recebeu e responde a célula.
Assim o teste confere toda a geometria (grade, zoom, recorte, volta pras
coordenadas da janela) sem precisar de um modelo instalado."""

import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw

if os.environ.get("DISPLAY"):
    try:
        import pyautogui  # noqa: F401  (o de verdade, antes do falso do _util)
    except Exception:
        pass

from _util import carregar_tars  # noqa: E402

tars = carregar_tars()
ocr = tars.ocr


def enxergar_verde(img, prompt, opcoes):
    """Responde a célula onde estão os pixels verde-puros (o ícone)."""
    letras = sorted({o[0] for o in opcoes if o != "none"})
    numeros = sorted({int(o[1:]) for o in opcoes if o != "none"})
    rgb = img.convert("RGB")
    px = rgb.load()
    xs, ys = [], []
    for y in range(0, rgb.height, 2):
        for x in range(0, rgb.width, 2):
            r, g, b = px[x, y]
            if g > 200 and r < 60 and b < 60:
                xs.append(x)
                ys.append(y)
    if not xs:
        return "none"
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    coluna = min(int(cx / (rgb.width / len(letras))), len(letras) - 1)
    linha = min(int(cy / (rgb.height / len(numeros))), len(numeros) - 1)
    return f"{letras[coluna]}{numeros[linha]}"


# ------------------------------------------------------------
# 1. Geometria: 3 rodadas de grade acham um ícone de 30 px
# ------------------------------------------------------------
tela = Image.new("RGB", (1280, 800), (40, 44, 52))
d = ImageDraw.Draw(tela)
alvos = [(1010, 655), (37, 41), (640, 400), (1255, 785)]
for ax, ay in alvos:
    tela_teste = tela.copy()
    ImageDraw.Draw(tela_teste).rectangle((ax - 15, ay - 15, ax + 15, ay + 15), fill=(0, 255, 0))
    perguntas = []

    def perguntar(img, prompt, opcoes):
        perguntas.append(img.size)
        return enxergar_verde(img, prompt, opcoes)

    ponto = ocr.localizar_por_grade(tela_teste, "send icon", perguntar, "Chat")
    assert ponto and abs(ponto[0] - ax) <= 4 and abs(ponto[1] - ay) <= 4, (ax, ay, ponto)
    assert len(perguntas) == len(ocr.NIVEIS_GRADE) and all(w >= ocr.LARGURA_MINIMA_VISAO for w, _ in perguntas)
assert ocr.localizar_por_grade(tela, "send icon", enxergar_verde, "Chat") is None, "não vê: não inventa"
grade = ocr.desenhar_grade(tela, 4, 3)
assert grade.size == tela.size and grade is not tela
print(f"OK: a grade em 3 rodadas acha um ícone de 30 px (no centro, ±4 px) em {len(alvos)} posições, "
      "em cima das linhas da grade e nos cantos; sem o ícone, responde que não achou")

# ------------------------------------------------------------
# 2. Num app de verdade sem acessibilidade (Tk não tem)
# ------------------------------------------------------------
if not (os.environ.get("DISPLAY") and os.environ.get("DBUS_SESSION_BUS_ADDRESS") and shutil.which("tesseract")):
    print("PULADO (parte 2): precisa de DISPLAY, D-Bus e tesseract")
    print("\nTUDO OK.")
    sys.exit(0)
if not hasattr(sys.modules.get("pyautogui"), "click"):
    print("PULADO (parte 2): pyautogui indisponível\n\nTUDO OK.")
    sys.exit(0)
if tars.ERRO_PYAUTOGUI or tars.acess.indisponivel():
    print("PULADO (parte 2): mouse ou acessibilidade indisponível\n\nTUDO OK.")
    sys.exit(0)

saida = tempfile.mktemp(prefix="opentars-app-")
APP = r'''
import sys, tkinter as tk
saida = sys.argv[1]
def anotar(t):
    open(saida, "a").write(t + "\n")
r = tk.Tk(); r.title("Chatzinho"); r.geometry("700x420+60+60"); r.configure(bg="white")
c = tk.Canvas(r, width=700, height=300, bg="white", highlightthickness=0); c.pack()
c.create_text(120, 40, text="Conversas recentes", font=("DejaVu Sans", 16), fill="black")
icone = c.create_rectangle(600, 230, 632, 262, fill="#00ff00", outline="")
c.tag_bind(icone, "<Button-1>", lambda e: anotar("ICONE"))
e = tk.Entry(r, font=("DejaVu Sans", 16), width=40, fg="gray"); e.insert(0, "Pergunte algo ao assistente"); e.pack(pady=20)
e.bind("<Return>", lambda ev: anotar("ENVIADO:" + e.get()))
r.after(60000, r.destroy); r.mainloop()
'''
proc = subprocess.Popen([sys.executable, "-c", APP, saida], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def anotado():
    try:
        return open(saida).read()
    except FileNotFoundError:
        return ""


try:
    limite = time.time() + 10
    while time.time() < limite and not tars.encontrar_janela_por_nome("Chatzinho"):
        time.sleep(0.2)
    time.sleep(0.8)

    # 2a. Ícone sem texto: o OCR não acha, a visão (falsa) aponta, clica
    tars._modelo_de_visao = lambda excluir=(): "visao-falsa:1b"

    def chat_visao(modelo, prompt, formato=None, imagens=None, **kw):
        img = Image.open(io.BytesIO(base64.b64decode(imagens[0])))
        return '{"celula": "%s"}' % enxergar_verde(img, prompt, formato["properties"]["celula"]["enum"])

    tars._chat_unico = chat_visao
    r = tars.executar_ferramenta("click_element", {"name": "send icon", "window": "Chatzinho"})
    assert r["sucesso"] and "vision model" in r["mensagem"], r
    limite = time.time() + 3
    while "ICONE" not in anotado() and time.time() < limite:
        time.sleep(0.1)
    assert "ICONE" in anotado(), (r, anotado())
    print("OK: click_element num ícone sem texto, num app sem acessibilidade: a visão aponta na grade e o clique acerta")

    # 2b. Campo achado pelo texto que ele mostra, digita e envia
    r = tars.executar_ferramenta("type_in_element", {"name": "Pergunte algo", "text": " qual a capital do Chile",
                                                     "window": "Chatzinho", "submit": True})
    assert r["sucesso"] and "reading the screen" in r["mensagem"], r
    limite = time.time() + 3
    while "ENVIADO" not in anotado() and time.time() < limite:
        time.sleep(0.1)
    assert "qual a capital do Chile" in anotado(), (r, anotado())
    print("OK: type_in_element(name='Pergunte algo') acha o campo lendo a tela, clica, digita e envia")
finally:
    proc.kill()
print("\nTUDO OK.")
