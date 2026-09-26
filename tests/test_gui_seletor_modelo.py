"""Seletor de modelo da interface gráfica.

Bug: listar_modelos_instalados() devolve um set, e o ttk.Combobox não
entende set — virava UM item só com o texto do set inteiro
("'mistral-small3.2:24b', 'gemma3:1b'..."), e escolher esse item fixava
um "modelo" inexistente.

Precisa de tkinter e de um display (ex: Xvfb); sem isso, é pulado.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402,F401  (idioma pt_BR e config temporária)

try:
    import tkinter  # noqa: F401
except ImportError:
    print("PULADO: tkinter não disponível")
    sys.exit(0)

if not os.environ.get("DISPLAY"):
    print("PULADO: sem DISPLAY")
    sys.exit(0)

os.environ.setdefault("XAUTHORITY", "/dev/null")

try:
    tkinter.Tk().destroy()
except tkinter.TclError as e:
    print(f"PULADO: display inacessível ({e})")
    sys.exit(0)

import tars as core  # noqa: E402

INSTALADOS = {"mistral-small3.2:24b", "gemma3:1b", "qwen3:0.6b", "gemma3:270m", "qwen2.5:0.5b"}
ESCOLHIVEIS = sorted(INSTALADOS - {"gemma3:270m", "qwen2.5:0.5b"})  # ajudantes não aparecem
core.listar_modelos_instalados = lambda forcar=False: set(INSTALADOS)
core.iniciar_conversa = lambda: 0
core.ollama_online = lambda: True
CATALOGO = {t: {"tag": t, "familia": t.split(":")[0], "categoria": "geral", "parametros_b": 1.0,
                "capacidades": {"tools"}, "capacidades_conhecidas": True} for t in INSTALADOS}
core.catalogar_modelos = lambda forcar=False: CATALOGO
core.avisos_de_ambiente = lambda: []
core.aquecer_ajudante = lambda: None
carregados = []
core._carregar_em_segundo_plano = lambda m: carregados.append(m)

import tars_gui  # noqa: E402

out = sys.__stdout__
resultado = {"ok": False, "erro": None}
j = tars_gui.JanelaTars()


def teste():
    try:
        v = tuple(j.combo_ia["values"])
        assert v == (tars_gui.automatico(), *ESCOLHIVEIS), f"valores do seletor: {v!r}"
        assert j.var_ia.get() == tars_gui.automatico()

        # escolher um modelo fixa ele (e já começa a carregar)
        j.var_ia.set("mistral-small3.2:24b")
        j._ao_escolher_ia()
        assert core.modo_modelo == "MANUAL" and core.modelo_manual == "mistral-small3.2:24b"
        assert core.escolher_modelo("oi") == "mistral-small3.2:24b"
        assert "mistral-small3.2:24b" in carregados

        # voltar pro automático
        j.var_ia.set(tars_gui.automatico())
        j._ao_escolher_ia()
        assert core.modo_modelo == "AUTO" and core.modelo_manual is None

        # "/modelo <nome>" digitado sincroniza o seletor
        core.interpretar_comando_modelo("/modelo qwen3:0.6b")
        j._popular_modelos()
        assert j.var_ia.get() == "qwen3:0.6b"
        core.interpretar_comando_modelo("/modelo auto")
        j._popular_modelos()
        assert j.var_ia.get() == tars_gui.automatico()

        # clicar num exemplo com o foco FORA da caixa de texto ainda envia
        enviados = []
        core.processar_mensagem = lambda texto: enviados.append(texto)
        j.texto.focus_set()
        j.update()
        j._usar_exemplo(tars_gui.exemplos()[1])
        j._trabalho.join(2)
        assert enviados == [tars_gui.exemplos()[1]], enviados

        # tela de boas-vindas com os exemplos, e "Nova conversa" volta pra ela
        j._limpar_conversa()
        assert j.texto.get("1.0", "2.0").startswith("O que eu faço")
        j._escrever([("\nVocê\n", "rotulo_voce"), ("oi\n", "voce")])
        j._limpar_conversa()
        assert j.texto.get("1.0", "2.0").startswith("O que eu faço")

        resultado["ok"] = True
    except Exception as e:
        resultado["erro"] = e
    finally:
        j.quit()


j.after(1500, teste)
j.mainloop()
sys.stdout = sys.__stdout__

if resultado["ok"]:
    print("OK: seletor de IA e tela de boas-vindas da janela", file=out)
    sys.exit(0)

print(f"FALHOU: {resultado['erro']!r}", file=out)
sys.exit(1)
