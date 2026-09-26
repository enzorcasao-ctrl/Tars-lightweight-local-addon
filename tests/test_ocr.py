"""2.9: clique e leitura por OCR em app sem acessibilidade.

O xmessage (X puro, sem árvore de acessibilidade, como um app Electron)
mostra um texto e um botão "okay". click_element("okay") tem que ACHAR o
botão lendo a tela e clicar nele (o xmessage fecha quando o botão é
apertado)."""

import os
import shutil
import subprocess
import sys
import time

if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    print("PULADO: precisa de DISPLAY e D-Bus")
    sys.exit(0)
if not shutil.which("tesseract") or not shutil.which("xmessage"):
    print("PULADO: precisa de tesseract e xmessage")
    sys.exit(0)
try:
    import pyautogui  # noqa: F401  (o de verdade: o clique precisa acontecer)
except Exception as e:
    print(f"PULADO: pyautogui indisponível ({e})")
    sys.exit(0)

from _util import carregar_tars  # noqa: E402

tars = carregar_tars()
if tars.acess.indisponivel() or tars.ERRO_PYAUTOGUI:
    print("PULADO: acessibilidade ou mouse indisponível")
    sys.exit(0)

# 1. O módulo OCR sozinho: lê e acha texto numa imagem
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

img = Image.new("RGB", (700, 200), "white")
d = ImageDraw.Draw(img)
try:
    fonte = ImageFont.truetype("DejaVuSans.ttf", 22)
except Exception:
    fonte = ImageFont.load_default()
d.text((30, 30), "New chat", fill="black", font=fonte)
d.text((300, 120), "Enviar mensagem", fill="black", font=fonte)
palavras = tars.ocr.ler(img, tars.ocr.idiomas_para(["pt_BR"]))
achado = tars.ocr.achar(palavras, "enviar mensagem")
assert achado and 300 < achado["x"] < 520 and 110 < achado["y"] < 150, (achado, palavras)
assert tars.ocr.achar(palavras, "New Chat") and not tars.ocr.achar(palavras, "Configurações")
print(f"OK: OCR acha 'Enviar mensagem' no lugar certo ({achado['x']}, {achado['y']}) e não inventa o que não existe")

# 2. Na janela de verdade
proc = subprocess.Popen(["xmessage", "-title", "Discordo", "-buttons", "Enviar agora", "Mensagem de teste do openTARS"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    limite = time.time() + 10
    while time.time() < limite and not tars.encontrar_janela_por_nome("Discordo"):
        time.sleep(0.2)
    time.sleep(0.5)
    r = tars.executar_ferramenta("list_elements", {"window": "Discordo"})
    assert r["sucesso"] and any("Mensagem de teste" in l for l in r["textos"]), r
    print(f"OK: list_elements num app sem acessibilidade lê a janela por OCR: {r['textos'][:2]}")

    inicio = time.time()
    r = tars.executar_ferramenta("click_element", {"name": "Enviar agora", "window": "Discordo"})
    assert r["sucesso"] and "reading the screen" in r["mensagem"], r
    limite = time.time() + 3
    while proc.poll() is None and time.time() < limite:
        time.sleep(0.1)
    assert proc.poll() is not None, "o clique tinha que apertar o botão (e fechar o xmessage)"
    print(f"OK: click_element('Enviar agora') achou o botão lendo a tela e clicou ({time.time() - inicio:.1f}s)")
finally:
    if proc.poll() is None:
        proc.kill()
print("\nTUDO OK.")
