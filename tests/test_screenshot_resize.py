import sys, types

sys.modules['psutil'] = types.ModuleType('psutil')
sys.modules['psutil'].process_iter = lambda *a, **k: []

# pyautogui de verdade precisa de um display (Xorg/Wayland), que não
# existe neste ambiente de teste — stub só o screenshot(), preservando
# o resto do módulo como stub também (move/clique/tecla não são
# exercitados por este teste).
pyautogui_stub = types.ModuleType('pyautogui')
pyautogui_stub.FAILSAFE = True
pyautogui_stub.PAUSE = 0.1


def _fake_screenshot():
    from PIL import Image
    return Image.new("RGB", (3840, 2160), color=(10, 20, 30))


pyautogui_stub.screenshot = _fake_screenshot
sys.modules['pyautogui'] = pyautogui_stub

from _util import carregar_tars
tars = carregar_tars()

assert tars.pyautogui.PAUSE == tars.PYAUTOGUI_PAUSE_SEG, tars.pyautogui.PAUSE
print("OK: pyautogui.PAUSE reduzido:", tars.pyautogui.PAUSE)

# A captura real depende do display; aqui simula uma tela 4K.
tars._capturar_tela = _fake_screenshot

r = tars.tirar_print()
assert r["sucesso"]
print("mensagem:", r["mensagem"])
assert "3840x2160" in r["mensagem"]
assert f"{tars.LARGURA_MAXIMA_SCREENSHOT}x" in r["mensagem"]

import base64
import io
from PIL import Image

img = Image.open(io.BytesIO(base64.b64decode(r["_imagem_b64"])))
assert img.size[0] == tars.LARGURA_MAXIMA_SCREENSHOT, img.size
print("OK: imagem redimensionada de fato para", img.size)

print("\nTUDO OK.")
