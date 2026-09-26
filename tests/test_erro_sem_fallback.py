from _util import carregar_tars
tars = carregar_tars()

tars.modo_modelo = "MANUAL"
tars.modelo_manual = "qwen3.8:27b"
tars.carregar_modelo = lambda *a, **k: True
tars._executar_turno_streaming = lambda modelo, mensagens, tools, **kw: (None, "Erro ao comunicar com Ollama: connection refused")
tars.obter_sessao = lambda modelo: [{"role": "system", "content": "sys"}]

resultado = tars.perguntar_modelo("qwen3.8:27b", "oi", tarefa=None)
print("RESULTADO:", repr(resultado))
assert "connection refused" in resultado
print("OK: erro sem fallback também é impresso e retornado.")
