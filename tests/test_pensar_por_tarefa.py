"""Raciocínio ("think") só onde ajuda.

Log real: "fecha o claude" levou 31 s, quase tudo com o qwen3 pensando.
Ação, busca, visão e bate-papo vão sem raciocínio; programação e
perguntas gerais continuam com ele; pedido longo (várias etapas) também."""

import os

from _util import carregar_tars

tars = carregar_tars()

assert not tars.deve_pensar("acao", "fecha o claude")
assert not tars.deve_pensar("busca", "pesquise rtx 5060")
assert not tars.deve_pensar("simples", "oi")
assert not tars.deve_pensar("visao", "clique no botão ok")
assert tars.deve_pensar("tecnico", "por que meu script python dá erro")
assert tars.deve_pensar("geral", "me explica buraco negro")
longo = "abra o firefox, entre no github, procure o repositório do ollama, abra a aba de releases e baixe o pacote mais recente pra linux"
assert tars.deve_pensar("acao", longo)
print("OK: ação/busca/visão/simples sem raciocínio; técnico, geral e pedido longo com")

# Modo manual (sem categoria): decide pelas palavras-chave, sem chamar a IA
assert not tars.deve_pensar(None, "feche o spotify")
assert tars.deve_pensar(None, "me conta uma história")
print("OK: no modo manual, decide pelas palavras-chave (sem gastar uma chamada)")

os.environ["TARS_PENSAR"] = "sempre"
assert tars.deve_pensar("acao", "fecha o claude")
os.environ["TARS_PENSAR"] = "nunca"
assert not tars.deve_pensar("tecnico", "erro no python")
del os.environ["TARS_PENSAR"]
print("OK: TARS_PENSAR=sempre|nunca força")

# O "think" vai no pedido ao Ollama de acordo
enviados = []


class Resposta:
    status_code = 200

    def iter_lines(self):
        # Resposta neutra: "Feito." sem ferramenta numa ação seria cobrado
        # (2.9, ver test_ia_esperta_v29.py) e mudaria a contagem aqui.
        yield b'{"message": {"content": "Ok."}, "done": true}'

    def close(self):
        pass


tars.ollama_request = lambda payload, stream=False, timeout=0: (enviados.append(payload), Resposta())[1]
tars.catalogar_modelos = lambda forcar=False: {"qwen3:8b": {"tag": "qwen3:8b", "capacidades": {"tools", "thinking"},
                                                            "capacidades_conhecidas": True, "categoria": "geral"}}
tars.carregar_modelo = lambda m, silencioso=False: True
tars.contexto_ia = lambda: 8192
tars.modo_modelo = "MANUAL"  # sem cadeia de alternativas: só o think importa aqui
tars.perguntar_modelo("qwen3:8b", "fecha o claude", tarefa="acao")
tars.perguntar_modelo("qwen3:8b", "como funciona o kernel linux", tarefa="tecnico")
assert [p["think"] for p in enviados] == [False, True], [p.get("think") for p in enviados]
print("OK: o pedido ao Ollama leva think=false numa ação e think=true numa pergunta técnica")

# Modelo que não pensa (sem 'thinking'): o campo nem vai
enviados.clear()
tars._MODELOS_SEM_THINKING.add("llama3:8b")
tars.perguntar_modelo("llama3:8b", "como funciona o kernel linux", tarefa="tecnico")
assert "think" not in enviados[0]
print("OK: modelo sem raciocínio não recebe o parâmetro")

print("\nTUDO OK.")
