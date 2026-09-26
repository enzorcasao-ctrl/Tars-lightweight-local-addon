"""2.9: IAs menos propensas a falhas."""

from _util import carregar_tars

tars = carregar_tars()

# 1. Conserto de chamadas
n, a, nota = tars.consertar_chamada("open_app", {"app_name": "firefox"})
assert (n, a) == ("open_application", {"app": "firefox"}) and "open_app" in nota, (n, a, nota)
n, a, _ = tars.consertar_chamada("click_mouse", {"x": "120", "y": 80.6})
assert a == {"x": 120, "y": 81}, a
n, a, _ = tars.consertar_chamada("hotkey", {"keys": "ctrl+s"})
assert a == {"keys": ["ctrl", "s"]}, a
n, a, _ = tars.consertar_chamada("type_in_element", {"text": "oi", "window": "Claude", "submit": "true"})
assert a["submit"] is True
n, a, _ = tars.consertar_chamada("serch_web", {"search_query": "lasanha"})
assert (n, a) == ("search_web", {"query": "lasanha"}), (n, a)
n, a, nota = tars.consertar_chamada("search_web", {"query": "x"})
assert nota is None
print("OK: nomes e argumentos inventados são corrigidos (open_app/app_name, '120', 'ctrl+s', 'true', erro de digitação)")

# 2. Ferramentas por tarefa
nomes = lambda tarefa: {f["function"]["name"] for f in tars.ferramentas_para(tarefa)}  # noqa: E731
assert nomes("simples") == {"open_website", "search_web", "open_application", "close_application"}
assert "execute_terminal" in nomes("tecnico") and "click_mouse" not in nomes("tecnico")
assert len(nomes("acao")) == len(tars.TOOLS) and len(nomes(None)) == len(tars.TOOLS)
print(f"OK: cada tarefa recebe só as ferramentas que fazem sentido (simples: 4, ação: {len(tars.TOOLS)})")

# 3. "Pronto!" depois de uma ferramenta que falhou -> conserta ou conta a verdade
tars.carregar_modelo = lambda m, silencioso=False: True
tars.contexto_ia = lambda: 8192
tars.modo_modelo = "MANUAL"
tars.catalogar_modelos = lambda forcar=False: {}
roteiro = iter([
    ("tool", "open_application", {"app": "photoshop"}),
    ("texto", "Pronto! Abri o Photoshop pra você."),
    ("texto", "Não consegui abrir: o Photoshop não está instalado neste PC."),
])
recebidas, ofertas = [], []


def turno(modelo, msgs, tools, **kw):
    recebidas.append(msgs[-1]["content"])
    ofertas.append(len(tools or []))
    tipo, *resto = next(roteiro)
    if tipo == "texto":
        return {"content": resto[0], "tool_calls": []}, None
    return {"content": "", "tool_calls": [{"function": {"name": resto[0], "arguments": resto[1]}}]}, None


tars._executar_turno_streaming = turno
tars._DISPATCH_FERRAMENTAS["open_application"] = lambda a: {"sucesso": False, "mensagem": "APP NOT FOUND: 'photoshop'."}
tars.conversa = []
resposta = tars.perguntar_modelo("qwen3:8b", "abre o photoshop", tarefa="simples")
assert resposta.startswith("Não consegui"), resposta
assert any("LAST tool call FAILED" in r for r in recebidas)
assert ofertas[0] == 4, ofertas
print("OK: 'Pronto, abri!' logo depois de uma falha é barrado; a IA conta a verdade")
print("\nTUDO OK.")
