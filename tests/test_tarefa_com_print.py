"""Tarefa que depende de print de tela ("abra a calculadora e clique nos
números"), reproduzindo o que um modelo de visão real fez:

1. O print era anexado como "(print da tela anexado acima — analise a
   imagem)" numa mensagem de usuário: o modelo achava que o usuário
   tinha mandado uma imagem nova, esquecia a tarefa e só descrevia a tela.
2. Depois disso ele "pensava" e não respondia nada — e o openTARS
   encerrava a tarefa pela metade.
3. O print vai reduzido pra IA (ex: 2560 -> 1280 de largura), mas o
   clique usava a coordenada da imagem direto na tela real: caía no
   lugar errado.

A parte 3 roda de verdade num display (Xvfb) se houver; senão é pulada.
"""

import os
import sys

from _util import carregar_tars

tars = carregar_tars()

PEDIDO = "abra a calculadora e clique em 7, +, 2 e ="

# ------------------------------------------------------------------
# 1 e 2: conversa com um "modelo" roteirizado
# ------------------------------------------------------------------
vistos = []
roteiro = iter([
    {"content": "", "tool_calls": [{"function": {"name": "take_screenshot", "arguments": {}}}]},
    {"content": "", "tool_calls": []},  # pensa e não responde
    {"content": "", "tool_calls": [{"function": {"name": "click_mouse", "arguments": {"x": 10, "y": 20}}}]},
    {"content": "", "tool_calls": [{"function": {"name": "take_screenshot", "arguments": {}}}]},
    {"content": "Pronto: 7 + 2 = 9.", "tool_calls": []},
])


def turno_falso(modelo, mensagens, tools, **kw):
    vistos.append([dict(m) for m in mensagens])
    return next(roteiro), None


cliques = []
tars._executar_turno_streaming = turno_falso
tars.carregar_modelo = lambda m: True
tars._DISPATCH_FERRAMENTAS["take_screenshot"] = lambda a: {
    "sucesso": True, "mensagem": "print ok", "_imagem_b64": "AAAA"}
tars._DISPATCH_FERRAMENTAS["click_mouse"] = lambda a: cliques.append(a) or {
    "sucesso": True, "mensagem": "clicou"}
tars.SESSAO_GRAFICA = "x11"
tars.modo_modelo = "MANUAL"

resposta = tars.perguntar_modelo("modelo-visao", PEDIDO)

msg_print = vistos[1][-1]
assert msg_print["role"] == "user" and msg_print.get("images") == ["AAAA"], msg_print
assert PEDIDO in msg_print["content"] and "not from the user" in msg_print["content"], msg_print["content"]
print("OK: o print chega junto com o pedido original, marcado como mensagem automática")

assert PEDIDO in vistos[2][-1]["content"] and not vistos[2][-1].get("images"), vistos[2][-1]
assert cliques == [{"x": 10, "y": 20}], cliques
assert resposta == "Pronto: 7 + 2 = 9.", resposta
com_imagem = [m for m in vistos[-1] if m.get("images")]
assert len(com_imagem) == 1 and com_imagem[0] is not None, len(com_imagem)
assert vistos[-1][-1].get("images"), "o print mais novo tem que ficar"
print("OK: só o print mais recente fica na conversa (economiza contexto)")

payloads = []
import importlib.util
spec = importlib.util.spec_from_file_location("tars2", tars.__file__)
t2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(t2)
t2._chamar_ollama_stream = lambda p: (payloads.append(p), (None, "sem ollama"))[1]
t2.detectar_gpu = lambda forcar=False: {"vram_total_gb": 24.0, "vram_livre_gb": 20.0}
t2._executar_turno_streaming("m", [{"role": "user", "content": "oi"}], None)
assert payloads[-1]["options"]["num_ctx"] == 16384, payloads[-1]["options"]
t2.detectar_gpu = lambda forcar=False: None
t2._executar_turno_streaming("m", [{"role": "user", "content": "oi"}], None)
assert payloads[-1]["options"]["num_ctx"] == 8192, payloads[-1]["options"]
print("OK: contexto pedido ao Ollama cabe ferramentas + print (16k com GPU >=16GB, senão 8k)")
print("OK: quando a IA para sem responder, o openTARS pede pra continuar e a tarefa termina")

# Resposta vazia na PRIMEIRA mensagem (sem ferramenta antes) não cutuca.
roteiro = iter([{"content": "", "tool_calls": []}])
vistos.clear()
tars.perguntar_modelo("modelo-visao", "oi")
assert len(vistos) == 1
print("OK: resposta vazia sem tarefa em andamento não gera insistência")


# ------------------------------------------------------------------
# 3: clique de verdade, tela 2560x1440, print reduzido pra 1280x720
# ------------------------------------------------------------------
if not os.environ.get("DISPLAY"):
    print("PULADO (parte real): sem DISPLAY")
    print("\nTUDO OK.")
    sys.exit(0)

os.environ.setdefault("XAUTHORITY", "/dev/null")
for nome in ("pyautogui", "psutil", "tars"):
    sys.modules.pop(nome, None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tkinter as tk  # noqa: E402

import tars as real  # noqa: E402

if real.ERRO_PYAUTOGUI:
    print(f"PULADO (parte real): {real.ERRO_PYAUTOGUI}")
    sys.exit(0)

largura_tela, altura_tela = real.pyautogui.size()
if largura_tela <= real.LARGURA_MAXIMA_SCREENSHOT:
    print(f"PULADO (parte real): tela {largura_tela}px não é reduzida no print")
    sys.exit(0)

janela = tk.Tk()
janela.geometry("+900+500")
visor = tk.StringVar(value="")
botoes = {}
for i, rotulo in enumerate(["7", "+", "2", "="]):
    b = tk.Button(janela, text=rotulo, width=6, height=3,
                  command=lambda r=rotulo: visor.set(visor.get() + r))
    b.grid(row=0, column=i)
    botoes[rotulo] = b
tk.Label(janela, textvariable=visor).grid(row=1, columnspan=4)
for _ in range(20):
    janela.update()

r = real.tirar_print()
assert r["sucesso"] and "1280x720" in r["mensagem"], r["mensagem"]
fator = largura_tela / 1280

for rotulo in ["7", "+", "2", "="]:
    b = botoes[rotulo]
    # Onde o botão aparece NO PRINT (o que a IA vê e responde)
    x_img = (b.winfo_rootx() + b.winfo_width() / 2) / fator
    y_img = (b.winfo_rooty() + b.winfo_height() / 2) / fator
    real.clicar_mouse("left", x_img, y_img)
    for _ in range(10):
        janela.update()

assert visor.get() == "7+2=", f"visor: {visor.get()!r}"
tam = real.tamanho_tela()
assert (tam["largura"], tam["altura"]) == (1280, 720), tam
janela.destroy()
print(f"OK: tela real {largura_tela}x{altura_tela}, cliques com coordenadas do print 1280x720 acertaram os 4 botões")

print("\nTUDO OK.")
