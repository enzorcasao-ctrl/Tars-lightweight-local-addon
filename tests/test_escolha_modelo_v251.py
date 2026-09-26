"""2.5.1: modelo de código só pra código, e a IA não trava abrindo app.

Log real: "abra X" ia pro qwen2.5-coder, que não sabe usar as ferramentas
(recusa, ou escreve a chamada como texto)."""

from _util import carregar_tars

tars = carregar_tars()


def m(tag, p, caps, v, fam=""):
    return {"tag": tag, "familia": fam, "parametros_b": p, "vram_estimado_gb": v, "capacidades": set(caps),
            "capacidades_conhecidas": True, "categoria": tars._categorizar_modelo(tag, set(caps), p, fam)}


catalogo = {x["tag"]: x for x in [
    m("qwen2.5-coder:7b", 7.6, {"tools"}, 5.2, "qwen2"),
    m("qwen3:8b", 8.2, {"tools", "thinking"}, 5.5, "qwen3"),
    m("qwen3:4b", 4.0, {"tools", "thinking"}, 3.0, "qwen3"),
    m("gemma3:12b", 12.2, {"vision"}, 7.9, "gemma3"),
    m("deepseek-coder-v2:16b", 16, {"tools"}, 10.0, "deepseek2"),
    m("mistral-small3.2:24b", 24, {"tools", "vision"}, 15.0, "mistral3"),
    m("qwen2.5:0.5b", 0.5, {"tools"}, 0.9, "qwen2"),
]}
assert catalogo["qwen2.5-coder:7b"]["categoria"] == "codigo" and catalogo["deepseek-coder-v2:16b"]["categoria"] == "codigo"
assert catalogo["mistral-small3.2:24b"]["categoria"] == "visao" and catalogo["qwen3:8b"]["categoria"] == "geral"
print("OK: só especialistas (coder, codestral...) são 'código'; mistral e deepseek-r1 não")

tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 6.0, "vram_livre_gb": 5.0}
codigos = {"qwen2.5-coder:7b", "deepseek-coder-v2:16b"}
for tarefa in ("acao", "busca", "visao", "tecnico", "geral", "simples"):
    cadeia = tars.construir_cadeia_fallback(tarefa, catalogo)
    assert set(cadeia[-2:]) == codigos and not codigos & set(cadeia[:-2]), (tarefa, cadeia)
assert tars.construir_cadeia_fallback("codigo", catalogo)[0] == "qwen2.5-coder:7b"  # o de 16b não cabe em 6 GB
assert tars.construir_cadeia_fallback("acao", catalogo)[0] == "qwen3:4b"
assert tars.construir_cadeia_fallback("tecnico", catalogo)[0] == "qwen3:8b"
print("OK: ação/busca/visão/técnico nunca começam por um modelo de código; código começa")

for frase, esperada in (("abra o terminal", "acao"), ("abra o vs code", "acao"), ("open vs code", "acao"),
                        ("escreva uma função em python que some listas", "codigo"),
                        ("meu driver nvidia não carrega", "tecnico"), ("instale o vlc", "tecnico")):
    assert tars.tarefa_por_palavras(frase) == esperada, (frase, tars.tarefa_por_palavras(frase))
assert tars.deve_pensar("codigo", "x")
print("OK: 'abra o terminal' é ação; escrever código é 'código'; driver é técnico")

# Chamada de ferramenta escrita como texto vira chamada de verdade
for texto in ('<tool_call>\n{"name": "open_application", "arguments": {"app": "firefox"}}\n</tool_call>',
              'Vou abrir: {"name": "open_application", "parameters": {"app": "firefox"}}',
              '```json\n{"function": {"name": "open_application", "arguments": "{\\"app\\": \\"firefox\\"}"}}\n```'):
    assert tars._ferramentas_no_texto(texto) == [{"function": {"name": "open_application", "arguments": {"app": "firefox"}}}], texto
assert tars._ferramentas_no_texto('um JSON qualquer {"name": "x"}') == []
assert tars._ferramentas_no_texto("Abri o Firefox.") == []
print("OK: <tool_call>{...}</tool_call> ou JSON solto no texto viram chamada de ferramenta")

# Recusou mesmo lembrado: passa pro próximo modelo
tars.catalogar_modelos = lambda forcar=False: catalogo
tars.carregar_modelo = lambda mod, silencioso=False: True
tars.modo_modelo = "AUTO"
atendeu = []


def turno(modelo, msgs, tools, **kw):
    atendeu.append(modelo)
    if modelo == "qwen3:4b":
        return {"content": "Desculpe, não consigo abrir aplicativos.", "tool_calls": []}, None
    if not any(x["role"] == "tool" for x in msgs[-3:]):
        return {"content": "", "tool_calls": [{"function": {"name": "open_application", "arguments": {"app": "firefox"}}}]}, None
    return {"content": "Abri o Firefox.", "tool_calls": []}, None


tars._executar_turno_streaming = turno
tars._DISPATCH_FERRAMENTAS["open_application"] = lambda a: {"sucesso": True, "mensagem": "SUCCESS"}
resposta = tars.perguntar_modelo("qwen3:4b", "abra o firefox", tarefa="acao")
assert resposta == "Abri o Firefox." and atendeu[:2] == ["qwen3:4b", "qwen3:4b"] and atendeu[2] == "qwen3:8b", atendeu
assert not any("não consigo" in (x.get("content") or "") for x in tars.conversa)
print("OK: recusou duas vezes -> o pedido passa pro próximo modelo, que abre o app")

print("\nTUDO OK.")
