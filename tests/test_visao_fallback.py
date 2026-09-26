"""2.5.2: print de tela com um modelo que não enxerga.

Log real: qwen3:8b tirou um print; o openTARS mandou a imagem, o Ollama
respondeu 400 "model does not support multimodal", e o reenvio "só texto"
AINDA levava a imagem (erro de novo, pedido perdido)."""

from _util import carregar_tars

tars = carregar_tars()

ERRO_400 = ('Ollama error (400): {"error":"{\\"error\\":{\\"code\\":400,\\"message\\":\\"Multimodal data '
            'provided, but model does not support multimodal requests.\\"}}"}')


def catalogo(conhecidas):
    return {
        "qwen3:8b": {"tag": "qwen3:8b", "capacidades": {"tools", "thinking"}, "capacidades_conhecidas": conhecidas,
                     "categoria": "geral", "parametros_b": 8.2, "vram_estimado_gb": 5.5},
        "gemma3:12b": {"tag": "gemma3:12b", "capacidades": {"vision"}, "capacidades_conhecidas": True,
                       "categoria": "visao", "parametros_b": 12.2, "vram_estimado_gb": 7.9},
    }


descricoes = []


def chat_unico(modelo, prompt, imagens=None, **kw):
    descricoes.append((modelo, bool(imagens)))
    return "Calculator window. Display: 0. Button '1' at (40, 300), '=' at (200, 380)."


tars._chat_unico = chat_unico
tars.carregar_modelo = lambda m, silencioso=False: True
tars.contexto_ia = lambda: 8192
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 12.0, "vram_livre_gb": 11.0}
tars.modo_modelo = "MANUAL"
tars._DISPATCH_FERRAMENTAS["take_screenshot"] = lambda a: {
    "sucesso": True, "mensagem": "SUCCESS: screenshot taken.", "_imagem_b64": "iVBORw0KGgo="}

enviados = []


def turno(modelo, msgs, tools, **kw):
    com_imagem = any(m.get("images") for m in msgs)
    enviados.append((modelo, com_imagem, bool(tools)))
    if com_imagem and modelo == "qwen3:8b":
        return None, ERRO_400
    if not any(m["role"] == "tool" for m in msgs[-4:]) and not any("described the screenshot" in (m.get("content") or "") for m in msgs):
        return {"content": "", "tool_calls": [{"function": {"name": "take_screenshot", "arguments": {"window": "calculator"}}}]}, None
    return {"content": "The calculator shows 0.", "tool_calls": []}, None


tars._executar_turno_streaming = turno

# 1. O catálogo já diz que o qwen3 não enxerga: a imagem nem vai
tars.catalogar_modelos = lambda forcar=False: catalogo(True)
tars.conversa = []
tars._sessoes_por_modelo = {} if hasattr(tars, "_sessoes_por_modelo") else None
r = tars.perguntar_modelo("qwen3:8b", "what does the calculator show?", tarefa="acao")
assert r == "The calculator shows 0.", r
assert not any(img for m, img, _ in enviados), enviados
assert descricoes == [("gemma3:12b", True)], descricoes
print("OK: modelo sem visão -> o print não vai; o gemma3 descreve a tela em texto e a tarefa continua")

# 2. Catálogo sem capacidades conhecidas: o Ollama recusa a imagem ->
#    TODA imagem sai da conversa e o turno é refeito COM as ferramentas
enviados.clear(); descricoes.clear()
tars._MODELOS_SEM_VISAO.clear()
tars.catalogar_modelos = lambda forcar=False: catalogo(False)
tars.conversa = []
r = tars.perguntar_modelo("qwen3:8b", "what does the calculator show now?", tarefa="acao")
assert r == "The calculator shows 0.", (r, enviados)
com_imagem = [e for e in enviados if e[1]]
assert len(com_imagem) == 1 and enviados[enviados.index(com_imagem[0]) + 1] == ("qwen3:8b", False, True), enviados
assert "qwen3:8b" in tars._MODELOS_SEM_VISAO and not tars.modelo_enxerga("qwen3:8b")
print("OK: 400 'does not support multimodal' -> imagem trocada por descrição, repete com ferramentas, e lembra")

# 3. Sem nenhum modelo com visão: orienta a usar list_elements
enviados.clear(); descricoes.clear()
tars.catalogar_modelos = lambda forcar=False: {"qwen3:8b": catalogo(True)["qwen3:8b"]}
msg = tars._print_em_texto("iVBORw0KGgo=", "x", "qwen3:8b")
assert "list_elements" in msg and not descricoes
print("OK: sem modelo de visão instalado -> a IA é mandada pro list_elements")
print("\nTUDO OK.")
