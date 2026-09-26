"""2.5.2: clicar num app fechado abre o app antes.

Log real: "Open the calculator and do 12 × 8" -> o qwen3 pulou o
open_application e mandou os cinco click_element direto; todos falharam."""

import os
import shutil
import subprocess
import sys
import time

from _util import carregar_tars

if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    print("PULADO: precisa de DISPLAY e D-Bus de sessão")
    sys.exit(0)
if not shutil.which("gnome-calculator"):
    print("PULADO: gnome-calculator não instalado")
    sys.exit(0)

os.environ.update(GSK_RENDERER="cairo", LANGUAGE="en", LC_ALL="C.UTF-8")
tars = carregar_tars()
if tars.acess.indisponivel():
    print(f"PULADO: acessibilidade indisponível ({tars.acess.indisponivel()})")
    sys.exit(0)
subprocess.run(["pkill", "-x", "gnome-calculato"])
time.sleep(0.5)

try:
    inicio = time.time()
    resultados = [tars.executar_ferramenta("click_element", {"name": n, "window": "Calculator"})
                  for n in ("1", "2", "×", "8", "=")]
    assert all(r["sucesso"] for r in resultados), resultados
    assert "opened it first" in resultados[0]["mensagem"], resultados[0]
    assert "opened it first" not in resultados[1]["mensagem"]
    assert "96" in resultados[-1]["textos"], resultados[-1]
    abertas = [j for j in tars.listar_janelas_x() or [] if "calc" in (j["classe"] + j["titulo"]).lower()]
    assert len(abertas) == 1, abertas
    print(f"OK: calculadora fechada -> abriu uma vez e fez 12 × 8 = 96 pelos botões ({time.time() - inicio:.1f}s)")
finally:
    subprocess.run(["pkill", "-x", "gnome-calculato"])
print("\nTUDO OK.")
