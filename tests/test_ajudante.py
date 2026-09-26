"""Modelo ajudante (qwen2.5:0.5b): é quem classifica, resolve nomes e o
/modelo; responde num formato forçado; fica fora da conversa; não é
descarregado ao trocar de modelo; e o --avaliar-classificador funciona."""

import io
import json as _json
import contextlib

from _util import carregar_tars

tars = carregar_tars()

assert tars.MODELO_AJUDANTE == "qwen2.5:0.5b", tars.MODELO_AJUDANTE
print("OK: ajudante padrão é o qwen2.5:0.5b")


def modelo(tag, params, caps=("completion", "tools"), cat=None):
    return {"tag": tag, "parametros_b": params, "familia": tag.split(":")[0],
            "categoria": cat or ("simples" if params <= 1.5 else "geral"),
            "vram_estimado_gb": round(params * 0.6 + 0.6, 1), "capacidades": set(caps),
            "capacidades_conhecidas": True, "emoji": ""}


catalogo = {m["tag"]: m for m in (
    modelo("qwen2.5:0.5b", 0.49),
    modelo("gemma3:270m", 0.27, caps=("completion",)),
    modelo("qwen3:0.6b", 0.6, caps=("completion", "tools", "thinking")),
    modelo("qwen3:8b", 8.2, caps=("completion", "tools", "thinking")),
)}
tars.catalogar_modelos = lambda forcar=False: catalogo
tars._cache_catalogo["dados"] = catalogo
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo)
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 12.0, "vram_livre_gb": 10.0}

assert tars.modelo_ajudante(catalogo) == "qwen2.5:0.5b"
for tarefa in ("simples", "geral", "acao", "busca"):
    cadeia = tars.construir_cadeia_fallback(tarefa, catalogo)
    assert "qwen2.5:0.5b" not in cadeia and "gemma3:270m" not in cadeia, (tarefa, cadeia)
assert tars.construir_cadeia_fallback("simples", catalogo)[0] == "qwen3:0.6b"
so_ajudante = {"qwen2.5:0.5b": catalogo["qwen2.5:0.5b"]}
assert tars.construir_cadeia_fallback("geral", so_ajudante) == ["qwen2.5:0.5b"]
print("OK: ajudantes (novo e antigo) ficam fora da conversa, a não ser que sejam o único modelo")


# ------------------------------------------------------------------
# Chamadas ao ajudante: formato forçado, sem raciocínio
# ------------------------------------------------------------------
class Resp:
    def __init__(self, conteudo, status=200):
        self.status_code = status
        self._c = conteudo

    def json(self):
        return {"message": {"content": self._c}}


enviados = []


def responder(conteudo):
    def post(url, json, timeout):
        enviados.append(json)
        return Resp(conteudo)
    return post


tars._SESSION.post = responder('{"categoria": "tecnico"}')
assert tars.detectar_tarefa("como eu descubro meu endereço ip") == "tecnico"  # 6 palavras, nenhuma palavra-chave
corpo = enviados[-1]
assert corpo["model"] == "qwen2.5:0.5b"
assert corpo["format"]["properties"]["categoria"]["enum"] == list(tars._CATEGORIAS_VALIDAS)
assert "Examples:" in corpo["messages"][0]["content"] and "think" not in corpo
print("OK: classificação com exemplos e resposta forçada (só pode dizer uma categoria)")

enviados.clear()
assert tars.detectar_tarefa("valeu, obrigado") == "simples" and not enviados
print("OK: pedido de até 3 palavras sem palavra-chave não gasta chamada")

# /modelo: só pode responder um modelo instalado ou "nenhum"
tars._carregar_em_segundo_plano = lambda m: None
tars._SESSION.post = responder('{"modelo": "qwen3:8b"}')
assert tars.interpretar_comando_modelo("/modelo aquele que é bom de conversa") is True
assert tars.modelo_manual == "qwen3:8b"
assert "none" in enviados[-1]["format"]["properties"]["modelo"]["enum"]
print("OK: /modelo <descrição> só aceita um modelo que existe")

# Nome de app: lista forçada, descarta frases
tars._SESSION.post = responder('{"nomes": ["gnome-calculator", "o app da calculadora", "kcalc"]}')
assert tars.perguntar_candidatos_app("calculadora") == ["gnome-calculator", "kcalc"]
print("OK: nomes de programa vêm como lista, e frases soltas são descartadas")

# Ollama antigo que não entende "format": tenta de novo sem ele
respostas = iter([Resp("", status=400), Resp("acao")])
enviados.clear()


def post_antigo(url, json, timeout):
    enviados.append(dict(json))
    return next(respostas)


tars._SESSION.post = post_antigo
assert tars._classificar_com_ia("preciso organizar umas coisas no meu computador") == "acao"
assert "format" in enviados[0] and "format" not in enviados[1]
print("OK: Ollama antigo sem resposta forçada continua funcionando")


# ------------------------------------------------------------------
# Trocar de modelo não descarrega o ajudante
# ------------------------------------------------------------------
descarregados = []
tars.listar_modelos_rodando = lambda: {"qwen2.5:0.5b", "qwen3:0.6b"}
tars.descarregar_modelo = lambda m: descarregados.append(m) or True
tars.contexto_ia = lambda: 8192
tars._SESSION.post = lambda *a, **k: Resp("")
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 4.0, "vram_livre_gb": 3.0}  # 8b não cabe junto
tars.modelo_atual = "qwen3:0.6b"
with contextlib.redirect_stdout(io.StringIO()):
    tars.carregar_modelo("qwen3:8b")
assert descarregados == ["qwen3:0.6b"], descarregados
print("OK: ao trocar de modelo, o ajudante continua carregado")


# ------------------------------------------------------------------
# --avaliar-classificador
# ------------------------------------------------------------------
frases = [tuple(par) for par in tars.i18n.lista("avaliacao")]
assert len(frases) >= 30, "as frases de avaliação vêm do arquivo de idioma"
gabarito = dict(frases)


def post_gabarito(url, json, timeout):
    pedido = json["messages"][0]["content"].rsplit('"', 2)[-2]
    return Resp(_json.dumps({"categoria": gabarito.get(pedido, "simples")}))


tars._SESSION.post = post_gabarito
tars.ollama_online = lambda: True
saida = io.StringIO()
with contextlib.redirect_stdout(saida):
    codigo = tars.avaliar_classificador()
texto = saida.getvalue()
n = len(frases)
assert codigo == 0 and f"Só o ajudante:                {n}/{n} (100%)" in texto, texto
print(f"OK: --avaliar-classificador mede o acerto ({n} frases)")

print("\nTUDO OK.")
