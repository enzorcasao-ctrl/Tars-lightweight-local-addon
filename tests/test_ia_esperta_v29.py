"""2.9: IAs mais espertas e menos propensas a falhas.

- o ajudante recebe as PISTAS das outras camadas e escreve o motivo antes
  de escolher (e isso aparece na trilha do --explicar);
- pedido com várias etapas: pensa antes, recebe lembrete e vai pro maior
  modelo que roda bem;
- "Pronto!" sem ter chamado ferramenta nenhuma é cobrado;
- histórico: modelo que falha muito numa tarefa desce na fila dela;
- modelo que quebra (erro do Ollama) ou não consegue nada passa o pedido
  pro próximo da fila, que continua a mesma conversa;
- a IA sabe quais janelas estão abertas."""

import os

os.environ["TARS_IDIOMA"] = "pt_BR"
from _util import carregar_tars  # noqa: E402

tars = carregar_tars()


def fez_ferramenta_neste_pedido(msgs, com_sucesso=False):
    """Algum resultado de ferramenta DEPOIS do último pedido do usuário?"""
    ultimo = max(i for i, x in enumerate(msgs) if x["role"] == "user" and not x["content"].startswith("["))
    return any(x["role"] == "tool" and (not com_sucesso or "SUCCESS" in x["content"]) for x in msgs[ultimo:])


def m(tag, p, caps, v, fam="qwen3"):
    return {"tag": tag, "familia": fam, "parametros_b": p, "vram_estimado_gb": v, "capacidades": set(caps),
            "capacidades_conhecidas": True, "categoria": tars._categorizar_modelo(tag, set(caps), p, fam)}


catalogo = {x["tag"]: x for x in [
    m("qwen3:4b", 4.0, {"tools", "thinking"}, 3.0),
    m("qwen3:8b", 8.2, {"tools", "thinking"}, 5.5),
    m("llama3.1:8b", 8.0, {"tools"}, 5.3, "llama"),
    m("qwen2.5-coder:7b", 7.6, {"tools"}, 5.2, "qwen2"),
]}
tars.catalogar_modelos = lambda forcar=False: catalogo
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 8.0, "vram_livre_gb": 7.5}
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo)
tars.carregar_modelo = lambda mod, silencioso=False: True
tars.contexto_ia = lambda: 8192
tars.modo_modelo = "AUTO"
tars.listar_janelas_x = lambda: None  # sem X nos testes de lógica (a parte 7 simula)

# ------------------------------------------------------------
# 1. O ajudante recebe as pistas e o motivo vai pra trilha
# ------------------------------------------------------------
recebido = {}


def chat_ajudante(modelo, prompt, formato=None, **kw):
    recebido["prompt"], recebido["formato"] = prompt, formato
    return '{"motivo": "quer ver videos de receita", "categoria": "busca"}'


tars._chat_unico = chat_ajudante
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo) | {tars.MODELO_AJUDANTE}
categoria = tars._classificar_com_ia("quero umas receitas de lasanha pra assistir", finalistas=["busca", "geral"],
                                     pistas=["its meaning is closest to: busca (then geral)"])
assert categoria == "busca", categoria
assert "Clues from faster checks" in recebido["prompt"] and "closest to: busca" in recebido["prompt"]
assert list(recebido["formato"]["properties"]) == ["motivo", "categoria"], "pensa (motivo) ANTES de escolher"
assert recebido["formato"]["properties"]["categoria"]["enum"] == ["busca", "geral"]
assert tars._ultimo_motivo_ajudante["motivo"] == "quer ver videos de receita"
# Ollama antigo (texto livre): vale a ÚLTIMA categoria citada
tars._chat_unico = lambda *a, **k: "not geral, it is busca"
assert tars._classificar_com_ia("x y z w", finalistas=["busca", "geral"]) == "busca"
print("OK: o ajudante escolhe só entre as finalistas, com as pistas das camadas e um motivo antes")

# ------------------------------------------------------------
# 2. Várias etapas
# ------------------------------------------------------------
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo)
d = tars.decidir_tarefa("abre a calculadora e faz 12 vezes 8")
assert d.categoria == "acao" and d.multi_etapas and ("etapas", "abre a calculadora | faz 12 vezes 8") in d.trilha
assert not tars.decidir_tarefa("pesquise preço e qualidade do ssd").multi_etapas
assert tars.escolher_modelo("x", tarefa="acao") == "qwen3:4b", "ação simples: o menor capaz"
assert tars.escolher_modelo("x", tarefa="acao", complexa=True) == "qwen3:8b", "várias etapas: o maior que roda bem"
assert tars.deve_pensar("acao", "abre a calculadora e faz 12 vezes 8", multi_etapas=True)
assert not tars.deve_pensar("acao", "abre a calculadora")

vistos = []


def turno_ok(modelo, msgs, tools, pensar=True, **kw):
    vistos.append((modelo, pensar, [x["content"] for x in msgs if x["role"] == "user"]))
    if not fez_ferramenta_neste_pedido(msgs):
        return {"content": "", "tool_calls": [{"function": {"name": "open_application",
                                                             "arguments": {"app": "calculadora"}}}]}, None
    return {"content": "Abri a calculadora e fiz a conta: 96.", "tool_calls": []}, None


tars._executar_turno_streaming = turno_ok
tars._DISPATCH_FERRAMENTAS["open_application"] = lambda a: {"sucesso": True, "mensagem": "SUCCESS: opened."}
tars.conversa = []
tars.perguntar_modelo("qwen3:8b", "abre a calculadora e faz 12 vezes 8", tarefa="acao", multi_etapas=True)
assert vistos[0][1] is True, "pensa antes num pedido de várias etapas"
assert any("This request has 2 steps" in u for u in vistos[0][2]), vistos[0][2]
print("OK: várias etapas -> modelo maior, raciocínio ligado e lembrete de fazer todas")

# ------------------------------------------------------------
# 3. "Pronto!" sem ferramenta nenhuma é cobrado
# ------------------------------------------------------------
roteiro = iter([
    {"content": "Pronto, abri o Firefox pra você!", "tool_calls": []},
    {"content": "", "tool_calls": [{"function": {"name": "open_application", "arguments": {"app": "firefox"}}}]},
    {"content": "Abri o Firefox.", "tool_calls": []},
])
cobrancas = []


def turno_mentiroso(modelo, msgs, tools, **kw):
    cobrancas.append(msgs[-1]["content"])
    return next(roteiro), None


tars._executar_turno_streaming = turno_mentiroso
resposta = tars.perguntar_modelo("qwen3:4b", "abre o firefox", tarefa="acao")
assert resposta == "Abri o Firefox." and "did NOT call any tool" in cobrancas[1], (resposta, cobrancas)
assert not any("Pronto, abri" in (x.get("content") or "") for x in tars.obter_sessao("qwen3:4b"))
print("OK: 'Pronto, abri!' sem ter chamado ferramenta é barrado; a IA faz de verdade")

# ------------------------------------------------------------
# 4. Histórico: quem falha muito numa tarefa desce na fila dela
# ------------------------------------------------------------
h = tars.historico_modelos
assert h.placar("qwen3:4b", "acao") == (0, 1), "dizer que fez sem ter feito conta como falha no placar"
h.dados = {}
assert tars.construir_cadeia_fallback("acao")[0] == "qwen3:4b"
for _ in range(3):
    h.registrar("qwen3:4b", "acao", False)
cadeia = tars.construir_cadeia_fallback("acao")
assert cadeia[0] != "qwen3:4b" and cadeia.index("qwen3:4b") < cadeia.index("qwen2.5-coder:7b"), cadeia
assert tars.construir_cadeia_fallback("geral")[-1] == "qwen2.5-coder:7b", "outras tarefas não mudam"
for _ in range(5):
    h.registrar("qwen3:4b", "acao", True)
assert tars.construir_cadeia_fallback("acao")[0] == "qwen3:4b", "voltou a acertar: volta pra frente"
assert h.caminho.exists() and h.placar("qwen3:4b", "acao") == (5, 8)
print("OK: modelo que falha a maioria das vezes numa tarefa vai pro fim da fila dela (e volta se melhorar)")

# ------------------------------------------------------------
# 5. Modelo que quebra: o próximo da fila continua a conversa
# ------------------------------------------------------------
h.caminho.unlink()
h.dados = None
atendeu = []


def turno_quebra(modelo, msgs, tools, **kw):
    atendeu.append(modelo)
    if modelo == "qwen3:4b":
        return None, 'Ollama error (500): {"error":"model requires more system memory (9 GiB) than is available"}'
    if not fez_ferramenta_neste_pedido(msgs):
        return {"content": "", "tool_calls": [{"function": {"name": "open_application", "arguments": {"app": "gimp"}}}]}, None
    return {"content": "Abri o GIMP.", "tool_calls": []}, None


tars._executar_turno_streaming = turno_quebra
tars.conversa = []
resposta = tars.perguntar_modelo("qwen3:4b", "abre o gimp", tarefa="acao")
assert resposta == "Abri o GIMP." and atendeu[0] == "qwen3:4b" and atendeu[1] != "qwen3:4b", (resposta, atendeu)
assert h.placar("qwen3:4b", "acao") == (0, 1) and h.placar(atendeu[1], "acao") == (1, 1)
print(f"OK: erro do Ollama no qwen3:4b -> {atendeu[1]} assume a mesma conversa (e o placar registra os dois)")

# ------------------------------------------------------------
# 6. Nada funcionou: outro modelo tenta, sem repetir o que falhou
# ------------------------------------------------------------
tentativas = []


def turno_circulos(modelo, msgs, tools, **kw):
    if not tools:
        return {"content": "Não consegui: o botão não aparece.", "tool_calls": []}, None
    if modelo == "qwen3:4b":
        n = len(tentativas)
        tentativas.append(modelo)
        return {"content": "", "tool_calls": [{"function": {"name": "click_element",
                                                             "arguments": {"name": f"Botão {n}", "window": "App"}}}]}, None
    if not fez_ferramenta_neste_pedido(msgs, com_sucesso=True):
        tentativas.append(modelo)
        return {"content": "", "tool_calls": [{"function": {"name": "type_in_element",
                                                             "arguments": {"text": "oi", "window": "App", "submit": True}}}]}, None
    return {"content": "Enviei a mensagem.", "tool_calls": []}, None


tars._executar_turno_streaming = turno_circulos
tars._DISPATCH_FERRAMENTAS["click_element"] = lambda a: {"sucesso": False, "mensagem": "No element."}
tars._DISPATCH_FERRAMENTAS["type_in_element"] = lambda a: {"sucesso": True, "mensagem": "SUCCESS: typed and sent."}
tars.conversa = []
resposta = tars.perguntar_modelo("qwen3:4b", "manda oi no app", tarefa="acao")
assert resposta == "Enviei a mensagem.", resposta
assert tentativas.count("qwen3:4b") == tars.LIMITE_FALHAS_SEGUIDAS and tentativas[-1] != "qwen3:4b", tentativas
assert any("attempts above (by another model) all failed" in (x.get("content") or "")
           for x in tars.obter_sessao(tentativas[-1]))
print(f"OK: {tars.LIMITE_FALHAS_SEGUIDAS} falhas sem nenhum sucesso -> {tentativas[-1]} assume com outra estratégia e resolve")

# ------------------------------------------------------------
# 7. Janelas abertas como contexto (sem as do próprio openTARS)
# ------------------------------------------------------------
tars.listar_janelas_x = lambda: [
    {"id": 1, "pid": 111, "titulo": "Claude"},
    {"id": 2, "pid": os.getpid(), "titulo": "openTARS"},
    {"id": 3, "pid": 333, "titulo": "Firefox — " + "x" * 80},
]
tars.janela_ativa_x = lambda: 1
ctx = tars._contexto_do_pedido("digita oi no claude", "acao", False)
assert "'Claude' (focused)" in ctx and "'openTARS'" not in ctx and "..." in ctx, ctx
assert tars._contexto_do_pedido("qual a capital da frança", "geral", False) is None
print("OK: numa ação, a IA recebe as janelas abertas (qual tem o foco), sem as do openTARS")

print("\nTUDO OK.")
