"""Utilitário compartilhado pelos testes: carrega tars.py isolado, sem
precisar dos pacotes de sistema (pyautogui, psutil) instalados — eles
são só usados pra controlar mouse/teclado e ler CPU/RAM, nada disso é
necessário pra testar a lógica (seleção de modelo, detecção de tarefa,
resolução de app/site, etc).

Uso, em qualquer test_*.py desta pasta:

    from _util import carregar_tars
    tars = carregar_tars()
"""

import importlib.util
import os
import sys
import tempfile
import types

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CAMINHO_TARS = os.path.join(RAIZ, "tars.py")

# Os módulos irmãos (tars_i18n, tars_acessibilidade...) são importados pelo
# tars.py; os testes rodam de dentro de tests/.
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

# Testes previsíveis: textos em português (a menos que o teste peça outro
# idioma) e nada gravado na configuração real do usuário.
os.environ.setdefault("TARS_IDIOMA", "pt_BR")
os.environ.setdefault("TARS_ARQUIVO_CONFIG", os.path.join(tempfile.mkdtemp(prefix="opentars-teste-"), "config.json"))
os.environ.setdefault("TARS_SEM_ATALHO", "1")
# Histórico de modelos, embeddings em cache...: nada no ~/.cache de verdade.
os.environ.setdefault("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="opentars-cache-"))


def carregar_tars():
    if "pyautogui" not in sys.modules:
        sys.modules["pyautogui"] = types.ModuleType("pyautogui")

    if "psutil" not in sys.modules:
        stub = types.ModuleType("psutil")
        stub.process_iter = lambda *a, **k: []
        sys.modules["psutil"] = stub

    spec = importlib.util.spec_from_file_location("tars", CAMINHO_TARS)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo
