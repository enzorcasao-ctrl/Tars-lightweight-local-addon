from _util import carregar_tars
import io, json
tars = carregar_tars()

# Force AUTO mode, single candidate model, no fallback chain needed
tars.modo_modelo = "MANUAL"
tars.modelo_manual = "qwen3.8:27b"

tars.carregar_modelo = lambda *a, **k: True

calls = []

def fake_stream(modelo, mensagens, tools, **kw):
    calls.append(tools)
    imagem_no_turno = bool(mensagens) and mensagens[-1].get("images")
    if imagem_no_turno and tools:
        # simulate failure: model doesn't support tools+images together
        return None, "Erro do Ollama (400): tools not supported with images"
    if imagem_no_turno and not tools:
        return {"content": "Vejo uma tela com um editor de texto aberto.", "thinking": "", "tool_calls": []}, None
    # first turn: request a screenshot
    return {"content": "", "thinking": "", "tool_calls": [
        {"function": {"name": "take_screenshot", "arguments": {}}}
    ]}, None

tars._executar_turno_streaming = fake_stream
tars.executar_ferramenta = lambda nome, args: {"sucesso": True, "mensagem": "print tirado", "_imagem_b64": "AAAA"}
tars.obter_sessao = lambda modelo: [{"role": "system", "content": "sys"}]

resultado = tars.perguntar_modelo("qwen3.8:27b", "veja minha tela e faça uma pergunta simples", tarefa="visao")
print("RESULTADO FINAL:", repr(resultado))
assert resultado == "Vejo uma tela com um editor de texto aberto."
print("Numero de chamadas a _executar_turno_streaming:", len(calls))
assert len(calls) == 3  # 1) tool-call turn, 2) failed image+tools turn, 3) fallback image-only turn
print("OK: fallback funcionou e resposta final foi retornada corretamente.")
