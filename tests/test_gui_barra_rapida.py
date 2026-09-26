"""Barra rápida e menu de idioma, na janela de verdade (Tk + Xvfb).

- aberta pelo atalho (--rapido), a janela principal fica escondida e só a
  barra aparece; a resposta chega nela (e na conversa da janela);
- Esc fecha a barra; "mostrar" (outra abertura) traz a janela principal;
- trocar o idioma no menu troca todos os textos na hora.

Precisa de tkinter e de um display; sem isso, é pulado."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="opentars-runtime-")

try:
    import tkinter
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

from _util import carregar_tars  # noqa: E402

core = carregar_tars()
sys.modules["tars"] = core
core.iniciar_conversa = lambda: 0
core.ollama_online = lambda: True
core.catalogar_modelos = lambda forcar=False: {}
core.listar_modelos_instalados = lambda forcar=False: set()
core.avisos_de_ambiente = lambda: []
core.aquecer_ajudante = lambda: None
core.detectar_gpu = lambda forcar=False: None
core.salvar_sessoes = lambda: None
core.encerrar_todas_ias = lambda: None


def processar(texto):
    core.emitir("tarefa", modo="AUTO", tarefa="acao", modelo="qwen3:8b")
    core.emitir("ferramenta", nome="open_application", resumo="calculadora")
    core.emitir("ferramenta_fim", nome="open_application", sucesso=True, segundos=0.4)
    core.emitir("pensamento", texto="vou abrir", inicio=True)
    core.emitir("resposta", texto="Abri a **calculadora**.", inicio=True, apos_pensamento=True)
    core.emitir("fim_stream")
    core.emitir("tempo", modo="AUTO", modelo="qwen3:8b", segundos=1.2)


core.processar_mensagem = processar

import tars_gui  # noqa: E402

saida = sys.__stdout__
j = tars_gui.JanelaTars(escondida=True)
resultado = {"ok": False, "erro": None}


def passos():
    j.update()
    assert not j.winfo_viewable(), "aberta pelo atalho: a janela principal começa escondida"

    j.mostrar_barra()
    yield 300
    b = j.barra
    assert b.winfo_viewable(), "a barra aparece"
    assert b.caixa.entrada.cget("fg") == tars_gui.TEXTO_3 or b.caixa.texto() == ""
    print("OK: --rapido abre só a barra flutuante", file=saida)

    b.caixa.definir("abra a calculadora")
    b._enviar()
    limite = time.time() + 5
    while j.ocupado and time.time() < limite:
        yield 50
    yield 300
    na_barra = b.area.get("1.0", "end")
    na_janela = j.texto.get("1.0", "end")
    assert "abrindo app" in na_barra and "Abri a calculadora." in na_barra, na_barra
    assert "vou abrir" not in na_barra, "o raciocínio não polui a barra"
    assert "abra a calculadora" in na_janela and "Abri a calculadora." in na_janela, na_janela
    assert b.area.winfo_ismapped() and int(b.area.cget("height")) >= 2, (b.area.winfo_ismapped(), b.area.cget("height"), b.area.count("1.0", "end", "displaylines"), b.area.winfo_width(), b.winfo_geometry())
    print("OK: a resposta aparece na barra (sem o raciocínio) e também na conversa da janela", file=saida)

    b._esc()
    yield 200
    assert not b.winfo_viewable(), "Esc fecha a barra"
    j.pedido_externo("mostrar", None)
    yield 400
    assert j.winfo_viewable(), "outra abertura traz a janela principal"
    print("OK: Esc fecha a barra; abrir de novo traz a janela principal", file=saida)

    j.trocar_idioma("en")
    yield 200
    assert j.botao_limpar._texto == "New chat" and j.botao_enviar._texto == "Send"
    assert tuple(j.combo_ia["values"])[0] == "Automatic" and j.var_ia.get() == "Automatic"
    assert j.botao_idioma._texto.startswith("EN") and j.rotulo_ia.cget("text") == "AI"
    assert "open window" in b.link.cget("text") and j.caixa.dica.startswith("Ask openTARS")
    j._limpar_conversa()
    yield 100
    assert j.texto.get("1.0", "2.0").startswith("What can I do for you?")
    assert core.i18n.idioma_atual() == "en" and "reply in English" in core.montar_system_prompt()
    print("OK: trocar para English muda botões, seletor, dicas, boas-vindas e o idioma da IA", file=saida)

    j.trocar_idioma("de")
    yield 100
    assert j.botao_limpar._texto == "Neuer Chat" and j.texto.get("1.0", "2.0").startswith("Was kann ich")
    j.trocar_idioma("pt_BR")
    yield 100
    assert j.botao_limpar._texto == "Nova conversa" and j.texto.get("1.0", "2.0").startswith("O que eu faço")
    print("OK: Deutsch e de volta pro português", file=saida)

    # Código na resposta vira janelinha com botão Copiar
    j._escrever(tars_gui.formatar_evento("resposta", {"texto": "Use isto:\n```python\nprint('oi')\nx = 1\n```\nPronto.", "inicio": True}))
    j._depois_da_resposta()
    yield 200
    blocos = j.conversa.blocos
    assert len(blocos) == 1 and blocos[0].codigo == "print('oi')\nx = 1", [b.codigo for b in blocos]
    assert "```" not in j.texto.get("1.0", "end") and "Pronto." in j.texto.get("1.0", "end")
    blocos[0].copiar()
    assert j.clipboard_get() == "print('oi')\nx = 1" and blocos[0].botao._texto.startswith("Copiado")
    print("OK: bloco de código com botão Copiar (copia só o código)", file=saida)
    resultado["ok"] = True


gerador = passos()


def proximo():
    try:
        espera = next(gerador)
        j.after(espera, proximo)
    except StopIteration:
        j.quit()
    except Exception as e:  # noqa: BLE001
        resultado["erro"] = e
        j.quit()


j.after(800, proximo)
j.mainloop()
sys.stdout = sys.__stdout__

if resultado["ok"]:
    print("\nTUDO OK.")
    sys.exit(0)
import traceback  # noqa: E402

traceback.print_exception(resultado["erro"])
print(f"FALHOU: {resultado['erro']!r}")
sys.exit(1)
