"""2.6.3: Ollama desligado (ex: parado pra usar o vLLM) e busca num site.

Log real: "Find lasagna recipe videos on YouTube" -> tarefa "simples" com o
qwen3:0.6b, e o erro era um HTTPConnectionPool cru seguido de "não consegui
carregar nenhum modelo" em vez de dizer que o Ollama estava desligado."""

import os

os.environ["TARS_IDIOMA"] = "en"
from _util import carregar_tars  # noqa: E402

tars = carregar_tars()
tars.OLLAMA_HOST = "http://127.0.0.1:9"  # porta sem ninguém: conexão recusada
tars.catalogar_modelos = lambda forcar=False: {}
tars.listar_modelos_instalados = lambda forcar=False: {"qwen3:0.6b", "qwen3:8b"}
tars.construir_cadeia_fallback = lambda tarefa, catalogo=None: ["qwen3:0.6b", "qwen3:8b"]
tars._modelo_carregado_atual = None if hasattr(tars, "_modelo_carregado_atual") else None

resposta = tars.perguntar_modelo("qwen3:0.6b", "Find lasagna recipe videos on YouTube", tarefa="busca")
assert "not responding" in resposta and "systemctl start ollama" in resposta, resposta
print("OK: Ollama desligado -> diz que o Ollama não está respondendo e como ligar")

for frase, esperada in (("Find lasagna recipe videos on YouTube", "busca"), ("open youtube", "simples"),
                        ("open the youtube website", "simples"), ("find me a cheap ssd", "busca")):
    assert tars.tarefa_por_palavras(frase) == esperada, (frase, tars.tarefa_por_palavras(frase))
print("OK: 'vídeos de X no YouTube' é busca; 'abra o youtube' continua simples")
print("\nTUDO OK.")
