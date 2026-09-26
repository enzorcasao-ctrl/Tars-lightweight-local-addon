"""IA recusando abrir/fechar apps (log real, PC com 6GB de VRAM):
- "ação" ia pro qwen2.5-coder, que responde "não consigo abrir aplicativos";
- a recusa ficava no histórico e o qwen3:8b recusava também;
- "counter strike" não achava "Counter-Strike 2";
- pedir pra fechar o próprio openTARS."""

import json
import os
import tempfile

os.environ["TARS_ARQUIVO_SESSOES"] = tempfile.mktemp(suffix=".json")

from _util import carregar_tars

tars = carregar_tars()


def m(tag, p, caps, cat, v):
    return {"tag": tag, "parametros_b": p, "categoria": cat, "vram_estimado_gb": v,
            "capacidades": set(caps), "capacidades_conhecidas": True}


# O catálogo do log do usuário
catalogo = {x["tag"]: x for x in [
    m("qwen3.8:27b", 27.3, {"thinking", "tools", "vision"}, "visao", 17.0),
    m("mistral-small3.2:24b", 24, {"tools", "vision"}, "visao", 15.0),
    m("gemma3:12b", 12.2, {"vision"}, "visao", 7.9),
    m("gemma4:e4b", 8, {"thinking", "tools", "vision"}, "visao", 5.4),
    m("qwen2.5-coder:7b", 7.6, {"tools"}, "tecnico", 5.2),
    m("qwen3:8b", 8.2, {"thinking", "tools"}, "geral", 5.5),
    m("qwen3:0.6b", 0.75, {"thinking", "tools"}, "simples", 1.1),
    m("qwen2.5:0.5b", 0.49, {"tools"}, "simples", 0.9),
    m("gemma3:270m", 0.27, set(), "simples", 0.8),
]}
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 6.0, "vram_livre_gb": 5.1}
for tarefa in ("acao", "busca"):
    cadeia = tars.construir_cadeia_fallback(tarefa, catalogo)
    assert cadeia[0] == "qwen3:8b" and cadeia.index("qwen2.5-coder:7b") > cadeia.index("gemma4:e4b"), cadeia
assert tars.construir_cadeia_fallback("codigo", catalogo)[0] == "qwen2.5-coder:7b"
for tarefa in ("acao", "busca", "visao", "tecnico", "geral", "simples"):
    cadeia = tars.construir_cadeia_fallback(tarefa, catalogo)
    assert cadeia[-1] == "qwen2.5-coder:7b", (tarefa, cadeia)
assert tars.construir_cadeia_fallback("tecnico", catalogo)[0] == "qwen3:8b"
assert tars.detectar_tarefa("abra o terminal") == "acao" and tars.detectar_tarefa("abra o vs code") == "acao"
print("OK: modelo de código só em pedido de código (nas outras tarefas, só se todo o resto falhar)")

# Nomes com hífen
tars.desktop_entries = lambda forcar=False: [{
    "nome": "Counter-Strike 2", "outros_nomes": [], "arquivo": "/x/cs2.desktop",
    "exec": "steam steam://rungameid/730",
}]
tars.flatpak_apps = lambda forcar=False: []
tars.comando_para_desktop_entry = lambda arq, ex: ["steam", "steam://rungameid/730"]
tars.shutil.which = lambda n: None
achado = tars._buscar_app_no_sistema("counter strike")
assert achado and achado["nome"] == "Counter-Strike 2", achado
print("OK: 'counter strike' acha 'Counter-Strike 2'")
import shutil  # noqa: E402
tars.shutil.which = shutil.which

r = tars.fechar_aplicativo("openTARS")
assert not r["sucesso"] and "sair" in r["mensagem"], r
print("OK: pedir pra fechar o openTARS explica como sair, em vez de tentar se matar")

# Recusa -> lembrete -> ferramenta, e a recusa não fica no histórico
roteiro = iter([
    {"content": "Desculpe, mas não consigo fechar aplicativos diretamente.", "tool_calls": []},
    {"content": "", "tool_calls": [{"function": {"name": "close_application", "arguments": {"app": "claude"}}}]},
    {"content": "Fechei o Claude.", "tool_calls": []},
])
vistos = []
tars._executar_turno_streaming = lambda mod, msgs, tools, **kw: (vistos.append([dict(x) for x in msgs]), (next(roteiro), None))[1]
tars.carregar_modelo = lambda mod: True
tars.modo_modelo = "MANUAL"
fechados = []
tars._DISPATCH_FERRAMENTAS["close_application"] = lambda a: fechados.append(a["app"]) or {"sucesso": True, "mensagem": "SUCESSO"}
resposta = tars.perguntar_modelo("qwen3:8b", "fecha o claude")
assert resposta == "Fechei o Claude." and fechados == ["claude"], (resposta, fechados)
assert "DO have tools" in vistos[1][-1]["content"]
assert not any("não consigo" in (x.get("content") or "") for x in tars.conversa)
print("OK: quando a IA diz 'não consigo', o openTARS lembra das ferramentas e ela fecha o app")

# Recusa DEPOIS de tentar uma ferramenta é legítima: não insiste
roteiro = iter([
    {"content": "", "tool_calls": [{"function": {"name": "close_application", "arguments": {"app": "xyz"}}}]},
    {"content": "Não consigo fechar o xyz: ele não está aberto.", "tool_calls": []},
])
vistos.clear()
tars.perguntar_modelo("qwen3:8b", "fecha o xyz")
assert len(vistos) == 2
print("OK: 'não consigo' depois de uma ferramenta falhar é resposta válida, sem insistência")

# Histórico antigo com recusas é limpo ao carregar
antigo = [
    {"role": "user", "content": "abra counter strike"},
    {"role": "assistant", "content": "Desculpe, mas não consigo abrir aplicativos."},
    {"role": "user", "content": "abra o firefox"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "open_application"}}]},
    {"role": "tool", "content": "{}"},
    {"role": "assistant", "content": "Abri o Firefox."},
    {"role": "user", "content": "fecha o claude"},
    {"role": "assistant", "content": "I can't close applications directly."},
]
json.dump({"versao": 2, "conversa": antigo}, open(os.environ["TARS_ARQUIVO_SESSOES"], "w"))
carregada = tars.carregar_sessoes()
textos = [x.get("content") for x in carregada[1:]]
assert textos == ["abra o firefox", "", "{}", "Abri o Firefox."], textos
print("OK: recusas antigas saem do histórico ao abrir (e levam o pedido junto)")

assert not tars._parece_recusa("Abri a calculadora.")
assert not tars._parece_recusa("O comando terminou com erro: permissão negada.")
print("OK: respostas normais não são confundidas com recusa")

os.remove(os.environ["TARS_ARQUIVO_SESSOES"])
print("\nTUDO OK.")
