"""Compatibilidade com o PC de qualquer pessoa: endereço do Ollama,
atalhos de apps (Snap, Flatpak, nomes em português, KDE sem
gtk-launch), GPU AMD, PC sem GPU, sessão Wayland, sem display e a
confirmação de comando perigoso na interface gráfica."""

import os
import sys
import tempfile
from pathlib import Path

from _util import carregar_tars

tars = carregar_tars()


# ------------------------------------------------------------------
# OLLAMA_HOST em todos os formatos que o próprio Ollama aceita
# ------------------------------------------------------------------
casos_host = {
    None: "http://127.0.0.1:11434",
    "": "http://127.0.0.1:11434",
    "0.0.0.0": "http://127.0.0.1:11434",
    "0.0.0.0:11434": "http://127.0.0.1:11434",
    ":11434": "http://127.0.0.1:11434",
    "192.168.0.10": "http://192.168.0.10:11434",
    "192.168.0.10:8080": "http://192.168.0.10:8080",
    "http://127.0.0.1:11434": "http://127.0.0.1:11434",
    "http://127.0.0.1:11434/": "http://127.0.0.1:11434",
    "https://ollama.exemplo.com": "https://ollama.exemplo.com:443",
    "[::1]:11434": "http://[::1]:11434",
}
for entrada, esperado in casos_host.items():
    obtido = tars._normalizar_ollama_host(entrada)
    assert obtido == esperado, (entrada, obtido, esperado)
print("OK: OLLAMA_HOST em", len(casos_host), "formatos")


# ------------------------------------------------------------------
# Atalhos .desktop
# ------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
apps = tmp / "applications"
apps.mkdir()

(apps / "firefox_firefox.desktop").write_text(
    "[Desktop Entry]\nType=Application\nName=Firefox Web Browser\n"
    "Name[pt_BR]=Navegador Firefox\nExec=/snap/bin/firefox %u\n"
    "\n[Desktop Action new-private-window]\nName=Nova janela anônima\n"
    "Exec=/snap/bin/firefox --private-window %u\n"
)
(apps / "org.gnome.Calculator.desktop").write_text(
    "[Desktop Entry]\nType=Application\nName=Calculator\n"
    "Name[pt_BR]=Calculadora\nExec=gnome-calculator\n"
)
(apps / "escondido.desktop").write_text(
    "[Desktop Entry]\nType=Application\nName=Escondido\nHidden=true\nExec=x\n"
)
(apps / "link.desktop").write_text(
    "[Desktop Entry]\nType=Link\nName=Um link\nURL=https://exemplo.com\n"
)

# Ubuntu/Zorin: tradução num catálogo gettext, não no arquivo
(apps / "org.gnome.Calculator.desktop").write_text(
    "[Desktop Entry]\nType=Application\nName=Calculator\n"
    "Exec=gnome-calculator\nX-Ubuntu-Gettext-Domain=gnome-calculator\n"
)


class _CatalogoFalso:
    def gettext(self, texto):
        return {"Calculator": "Calculadora"}.get(texto, texto)


_catalogos_reais = tars._catalogos_gettext
tars._catalogos_gettext = lambda dominio, grupos=(): [_CatalogoFalso()] if dominio == "gnome-calculator" else []
calc = tars._ler_desktop_entry(apps / "org.gnome.Calculator.desktop")
assert "Calculadora" in calc["outros_nomes"], calc
print("OK: nome traduzido via gettext (Ubuntu/Zorin) entra na busca")

ff = tars._ler_desktop_entry(apps / "firefox_firefox.desktop")
assert ff["exec"] == "/snap/bin/firefox %u", ff["exec"]  # não o da "janela anônima"
assert "Navegador Firefox" in ff["outros_nomes"]
assert tars._ler_desktop_entry(apps / "escondido.desktop") is None
assert tars._ler_desktop_entry(apps / "link.desktop") is None
print("OK: .desktop lê só a seção principal e ignora Hidden/Link")

os.environ["XDG_DATA_DIRS"] = "/usr/share"
pastas = [str(p) for p in tars._pastas_de_aplicativos()]
assert "/var/lib/snapd/desktop/applications" in pastas, pastas
assert "/var/lib/flatpak/exports/share/applications" in pastas, pastas
print("OK: procura atalhos de Snap e Flatpak")

tars._pastas_de_aplicativos = lambda: [apps]
tars._cache_desktop["dados"] = None
tars.flatpak_apps = lambda forcar=False: []
tars.perguntar_candidatos_app = lambda alvo: []

achado = tars._buscar_app_no_sistema("calculadora")
assert achado and achado["nome"] == "Calculator", achado
print("OK: 'calculadora' acha o app 'Calculator' pelo nome em português")

# Qual lançador usar, conforme o que existe no PC
existentes = set()
tars.shutil.which = lambda nome: f"/usr/bin/{nome}" if nome in existentes else None

os.environ["XDG_DATA_DIRS"] = str(tmp)
existentes = {"gtk-launch", "gio"}
cmd = tars.comando_para_desktop_entry(apps / "org.gnome.Calculator.desktop", "gnome-calculator")
assert cmd == ["gtk-launch", "org.gnome.Calculator"], cmd

os.environ["XDG_DATA_DIRS"] = "/usr/share"  # atalho fora do XDG: gtk-launch não acharia
cmd = tars.comando_para_desktop_entry(apps / "firefox_firefox.desktop", "/snap/bin/firefox %u")
assert cmd[:2] == ["gio", "launch"], cmd

existentes = {"kioclient"}  # KDE sem GTK/GLib
cmd = tars.comando_para_desktop_entry(apps / "org.gnome.Calculator.desktop", "gnome-calculator")
assert cmd[:2] == ["kioclient", "exec"], cmd

existentes = set()
cmd = tars.comando_para_desktop_entry(apps / "firefox_firefox.desktop", "/snap/bin/firefox %u")
assert cmd == ["/snap/bin/firefox"], cmd
print("OK: abre atalhos com gtk-launch, gio, kioclient ou o próprio Exec=")

import shutil as _shutil
tars.shutil.which = _shutil.which


# ------------------------------------------------------------------
# GPU AMD (sysfs) e vídeo integrado ignorado
# ------------------------------------------------------------------
drm = Path(tempfile.mkdtemp())
for card, vram, usado in (("card0", 512 * 1024**2, 100 * 1024**2), ("card1", 8 * 1024**3, 2 * 1024**3)):
    d = drm / card / "device"
    d.mkdir(parents=True)
    (d / "vendor").write_text("0x1002\n")
    (d / "mem_info_vram_total").write_text(f"{vram}\n")
    (d / "mem_info_vram_used").write_text(f"{usado}\n")

gpu = tars._detectar_gpu_amd(str(drm))
assert gpu == {"vram_total_gb": 8.0, "vram_livre_gb": 6.0, "fabricante": "AMD"}, gpu
assert tars._detectar_gpu_amd(str(drm / "nada")) is None
print("OK: GPU AMD detectada; vídeo integrado de 512MB ignorado")


# ------------------------------------------------------------------
# PC sem GPU: AUTO não escolhe o modelo gigante
# ------------------------------------------------------------------
def modelo(tag, params, cat="geral"):
    return {
        "tag": tag, "parametros_b": params, "categoria": cat,
        "vram_estimado_gb": tars._estimar_vram_gb(params, "Q4_K_M"),
        "capacidades": {"completion", "tools"}, "capacidades_conhecidas": True,
    }

catalogo = {m["tag"]: m for m in (modelo("grande:24b", 24), modelo("medio:8b", 8.2), modelo("pequeno:4b", 4))}
tars.detectar_gpu = lambda forcar=False: None

tars.ram_total_gb = lambda: 16.0
cadeia = tars.construir_cadeia_fallback("geral", catalogo)
assert cadeia[0] == "medio:8b", cadeia
print("OK: sem GPU (16GB RAM), conversa geral usa o 8B, não o 24B:", cadeia)

tars.ram_total_gb = lambda: None  # sem como saber: não penaliza
assert tars.construir_cadeia_fallback("geral", catalogo)[0] == "grande:24b"

assert tars.formatar_parametros(0.75163) == "752M"
assert tars.formatar_parametros(8.19) == "8.2B"
assert tars.formatar_parametros(24.0) == "24B"
print("OK: parâmetros formatados (752M, 8.2B, 24B)")


# ------------------------------------------------------------------
# Wayland: captura por programa; preto do XWayland é rejeitado
# ------------------------------------------------------------------
from PIL import Image

tars.SESSAO_GRAFICA = "wayland"
chamados = []


def falso_programa(cmd):
    chamados.append(cmd[0])
    return Image.new("RGB", (100, 50), (40, 80, 120))


tars._capturar_com_programa = falso_programa
tars.shutil.which = lambda nome: "/usr/bin/spectacle" if nome == "spectacle" else None
img = tars._capturar_tela()
assert chamados == ["spectacle"] and img.size == (100, 50), chamados
print("OK: Wayland usa o capturador do desktop (spectacle no KDE)")

import PIL.ImageGrab
tars.shutil.which = lambda nome: None
PIL.ImageGrab.grab = lambda *a, **k: Image.new("RGB", (100, 50), (0, 0, 0))
tars.pyautogui = tars._PyautoguiIndisponivel("teste")
try:
    tars._capturar_tela()
    raise AssertionError("tela preta no Wayland deveria virar erro")
except RuntimeError as e:
    assert "gnome-screenshot" in str(e), e
print("OK: Wayland sem capturador dá erro claro dizendo o que instalar")
tars.shutil.which = _shutil.which


# Mouse/teclado no Wayland: resultado avisa a IA
tars._DISPATCH_FERRAMENTAS["press_key"] = lambda a: {"sucesso": True, "mensagem": "Tecla pressionada."}
r = tars.executar_ferramenta("press_key", {"key": "enter"})
assert "Xorg" in r["mensagem"], r
tars.SESSAO_GRAFICA = "x11"
r = tars.executar_ferramenta("press_key", {"key": "enter"})
assert "Xorg" not in r["mensagem"], r
print("OK: no Wayland, ferramentas de mouse/teclado avisam a IA")


# ------------------------------------------------------------------
# Confirmação de comando perigoso pela interface gráfica
# ------------------------------------------------------------------
perguntados = []
tars.confirmar_comando_hook = lambda cmd: perguntados.append(cmd) or False
r = tars.executar_terminal("rm -rf /tmp/qualquer")
assert perguntados == ["rm -rf /tmp/qualquer"] and not r["sucesso"], (perguntados, r)
tars.confirmar_comando_hook = None
print("OK: comando perigoso pergunta pela janela quando há interface")


# ------------------------------------------------------------------
# Sem display: pyautogui não importa, mas o openTARS carrega
# ------------------------------------------------------------------
sys.modules["pyautogui"] = None  # faz "import pyautogui" falhar
sem_display = carregar_tars()
assert sem_display.ERRO_PYAUTOGUI, "deveria registrar o motivo"
r = sem_display.mover_mouse(10, 10)
assert not r["sucesso"] and "unavailable" in r["mensagem"], r
print("OK: sem display, o openTARS abre e mouse/teclado respondem com o motivo")

print("\nTUDO OK.")
