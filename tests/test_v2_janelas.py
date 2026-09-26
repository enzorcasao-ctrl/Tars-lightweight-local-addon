"""Controle de janelas (2.0), de verdade: precisa de um display X com
gerenciador de janelas (ex: Xvfb + openbox). Sem isso, é pulado.

Um app de teste roda em OUTRO processo (como um app real) e o openTARS:
abre e espera a janela, lista, traz pra frente, tira print só dela,
clica nos botões com as coordenadas DO PRINT RECORTADO e fecha a
janela pelo X (sem matar o processo)."""

import json
import os
import subprocess
import sys
import tempfile
import time

if not os.environ.get("DISPLAY"):
    print("PULADO: sem DISPLAY")
    sys.exit(0)

os.environ.setdefault("XAUTHORITY", "/dev/null")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tars  # noqa: E402

if tars.ERRO_PYAUTOGUI:
    print(f"PULADO: {tars.ERRO_PYAUTOGUI}")
    sys.exit(0)

try:
    from Xlib import display as _d
    _x = _d.Display()
    tem_wm = _x.screen().root.get_full_property(_x.intern_atom("_NET_CLIENT_LIST"), 0) is not None
    _x.close()
except Exception:
    tem_wm = False
if not tem_wm:
    print("PULADO: sem gerenciador de janelas (EWMH)")
    sys.exit(0)

pasta = tempfile.mkdtemp()
estado = os.path.join(pasta, "estado.json")
app = os.path.join(pasta, "calcteste.py")
open(app, "w").write(f'''
import json, tkinter as tk
r = tk.Tk(className="calcteste"); r.title("Calc Teste")
visor = tk.StringVar(value="")
def salvar(extra=None):
    json.dump({{"visor": visor.get(), "fechada": extra == "fechada",
               "botoes": {{b: [w.winfo_rootx() - r.winfo_rootx() + w.winfo_width() // 2,
                              w.winfo_rooty() - r.winfo_rooty() + w.winfo_height() // 2]
                          for b, w in botoes.items()}}}}, open({estado!r}, "w"))
botoes = {{}}
for i, b in enumerate(["7", "+", "2", "="]):
    w = tk.Button(r, text=b, width=5, height=3, command=lambda b=b: (visor.set(visor.get() + b), salvar()))
    w.grid(row=0, column=i); botoes[b] = w
tk.Label(r, textvariable=visor).grid(row=1, columnspan=4)
r.protocol("WM_DELETE_WINDOW", lambda: (salvar("fechada"), r.destroy()))
r.after(500, salvar)
r.mainloop()
''')

tars.encontrar_app = lambda nome: {
    "tipo": "executavel", "nome": "calcteste",
    "comando": [sys.executable, app], "origem": app,
}

t0 = time.time()
r = tars.abrir_aplicativo("calcteste")
tempo = time.time() - t0
assert r["sucesso"] and r.get("janela", {}).get("titulo") == "Calc Teste", r
print(f"OK: abriu e esperou a janela ({tempo:.1f}s): {r['janela']}")

lista = tars.listar_janelas()
assert any(j["titulo"] == "Calc Teste" and j["em_foco"] for j in lista["janelas"]), lista
print("OK: list_windows mostra a janela, em foco")

time.sleep(0.6)
botoes = json.load(open(estado))["botoes"]

r = tars.tirar_print("calcteste")
assert r["sucesso"] and "Calc Teste" in r["mensagem"] and tars._escala_print["ox"] > 0, r["mensagem"]
print("OK: print só da janela:", r["mensagem"][:70])

for b in ["7", "+", "2", "="]:
    x, y = botoes[b]  # posição do botão DENTRO do print recortado
    tars.clicar_mouse("left", x, y)
    time.sleep(0.2)
time.sleep(0.3)
assert json.load(open(estado))["visor"] == "7+2=", json.load(open(estado))
print("OK: cliques com coordenadas do print recortado acertaram 7, +, 2, =")

assert tars.focar_janela("calc teste")["sucesso"]
r = tars.fechar_aplicativo("calcteste")
time.sleep(0.3)
assert r["sucesso"] and json.load(open(estado))["fechada"], r
print("OK: close_application fechou pela janela (o app recebeu o pedido de fechar)")

print("\nTUDO OK.")
