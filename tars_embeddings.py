"""Classificação do pedido por embeddings.

Um modelo de embeddings transforma um texto num vetor que representa o
SIGNIFICADO dele: frases com o mesmo sentido viram vetores próximos, mesmo
com palavras (ou idiomas) diferentes. Aqui:

1. as frases de exemplo de cada tarefa (tars_exemplos.py) viram vetores
   uma vez só e ficam guardadas em ~/.cache/opentars;
2. cada pedido vira UM vetor (~10–30 ms) e é comparado com os exemplos;
   a tarefa com as frases mais parecidas ganha.

Bem mais rápido que perguntar ao modelo ajudante (150–500 ms), e entende o
sentido sem depender de palavra-chave.

A confiança mínima não é um número fixo (cada modelo de embeddings tem a
sua escala): ao montar o índice, cada exemplo é classificado pelos outros
(leave-one-out) e escolhemos a menor diferença entre a 1ª e a 2ª tarefa
com que o índice ainda acerta ≥ ALVO_PRECISAO. Pedido ambíguo (abaixo
disso) não é decidido aqui: quem chamou pergunta ao ajudante.

Sem dependências: usa numpy se estiver instalado, senão Python puro."""

import hashlib
import json
import math
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from tars_exemplos import EXEMPLOS

try:
    import numpy as _np
except Exception:  # pragma: no cover - depende da máquina
    _np = None

# Em ordem de preferência. Os primeiros são multilíngues (o openTARS
# fala 5 idiomas); os só-inglês ficam de reserva. Sem ":" = qualquer tag.
PREFERIDOS = (
    "granite-embedding:278m",
    "embeddinggemma",
    "paraphrase-multilingual",
    "bge-m3",
    "snowflake-arctic-embed2",
    "nomic-embed-text",
    "mxbai-embed-large",
    "snowflake-arctic-embed",
    "all-minilm",
    "granite-embedding",
)
MODELO_PADRAO = "granite-embedding:278m"  # o que o instalador baixa (563 MB)

VERSAO_INDICE = 1
K_VIZINHOS = 3          # nota de uma tarefa = média das K frases mais parecidas
ALVO_PRECISAO = 0.95    # a margem escolhida acerta pelo menos isso no leave-one-out
MARGEM_MINIMA = 0.005
MARGEM_MAXIMA = 0.25
LIMITE_CACHE_PEDIDOS = 256
LIMITE_FALHAS_SEGUIDAS = 3

_embutir = None          # função(modelo, [textos]) -> [[floats]] | None
_pasta_cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "opentars"
_indices = {}            # modelo -> _Indice
_trava = threading.Lock()
_preparando = set()
_falhou = set()          # modelos que não geraram embeddings nesta sessão
_cache_pedidos = OrderedDict()
_falhas_seguidas = {}


def configurar(embutir, pasta_cache=None):
    """tars.py passa a função que fala com o Ollama (/api/embed)."""
    global _embutir, _pasta_cache
    _embutir = embutir
    if pasta_cache:
        _pasta_cache = Path(pasta_cache)


def desligado():
    return (os.environ.get("TARS_MODELO_EMBEDDING") or "").strip().lower() in ("off", "0", "nao", "não", "no", "none")


def escolher_modelo(instalados):
    """O modelo de embeddings a usar, dentre os instalados, ou None.
    TARS_MODELO_EMBEDDING força um (ou 'off' desliga)."""
    if desligado() or not instalados:
        return None
    instalados = [m for m in instalados if m not in _falhou]
    forcado = (os.environ.get("TARS_MODELO_EMBEDDING") or "").strip()
    for preferido in ((forcado,) if forcado else ()) + PREFERIDOS:
        for tag in instalados:
            if _mesmo_modelo(tag, preferido):
                return tag
    return None


def eh_modelo_de_embedding(tag):
    """Pelo nome: é um dos modelos de embeddings conhecidos?"""
    return any(_mesmo_modelo(tag, p) for p in PREFERIDOS)


def _mesmo_modelo(tag, preferido):
    if ":" in preferido:
        return tag == preferido or (preferido.endswith(":latest") and tag == preferido[:-7])
    return tag.split(":", 1)[0] == preferido


# ------------------------------------------------------------
# Vetores
# ------------------------------------------------------------

def _normalizar(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _hash_exemplos():
    bruto = json.dumps([VERSAO_INDICE, EXEMPLOS], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(bruto.encode()).hexdigest()[:12]


def _nome_seguro(modelo):
    return re.sub(r"[^A-Za-z0-9._]", "_", modelo)


def _arquivo_cache(modelo):
    return _pasta_cache / f"tarefas-{_nome_seguro(modelo)}-{_hash_exemplos()}.json"


class _Indice:
    def __init__(self, categorias, vetores, margem, precisao_loo):
        self.categorias = categorias            # categoria de cada exemplo
        self.nomes = sorted(set(categorias))
        self.margem = margem
        self.precisao_loo = precisao_loo
        self.vetores = vetores
        if _np is not None:
            self.matriz = _np.asarray(vetores, dtype=_np.float32)
            self.cat_idx = _np.asarray([self.nomes.index(c) for c in categorias])

    def semelhancas(self, q):
        if _np is not None:
            return self.matriz @ _np.asarray(q, dtype=_np.float32)
        return [sum(a * b for a, b in zip(v, q)) for v in self.vetores]

    def pontuar(self, sims, ignorar=None):
        """[(nota, categoria)] do maior pro menor. Nota = média das K
        frases mais parecidas daquela categoria."""
        por_cat = {c: [] for c in self.nomes}
        for i, (s, c) in enumerate(zip(sims, self.categorias)):
            if i != ignorar:
                por_cat[c].append(float(s))
        notas = []
        for c, lista in por_cat.items():
            if lista:
                melhores = sorted(lista, reverse=True)[:K_VIZINHOS]
                notas.append((sum(melhores) / len(melhores), c))
        return sorted(notas, reverse=True)


def _calibrar(categorias, vetores):
    """Leave-one-out: cada exemplo classificado pelos outros. Devolve a
    menor margem (1ª − 2ª tarefa) com precisão ≥ ALVO_PRECISAO, e a
    precisão geral do índice."""
    indice = _Indice(categorias, vetores, 0.0, 0.0)
    resultados = []  # (margem, acertou)
    for i, v in enumerate(vetores):
        notas = indice.pontuar(indice.semelhancas(v), ignorar=i)
        if len(notas) < 2:
            continue
        margem = notas[0][0] - notas[1][0]
        resultados.append((margem, notas[0][1] == categorias[i]))
    if not resultados:
        return MARGEM_MAXIMA, 0.0

    precisao_geral = sum(ok for _, ok in resultados) / len(resultados)
    # Da maior margem pra menor: a menor margem em que a precisão de
    # todos os casos com margem >= ela ainda bate o alvo.
    resultados.sort(reverse=True)
    escolhida, acertos = MARGEM_MAXIMA, 0
    for n, (margem, ok) in enumerate(resultados, 1):
        acertos += ok
        if acertos / n >= ALVO_PRECISAO:
            escolhida = margem
    return min(MARGEM_MAXIMA, max(MARGEM_MINIMA, escolhida)), precisao_geral


def _montar(modelo):
    """Carrega o índice do cache ou gera os vetores dos exemplos."""
    arquivo = _arquivo_cache(modelo)
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        if dados.get("modelo") == modelo and dados.get("hash") == _hash_exemplos():
            return _Indice(dados["categorias"], dados["vetores"], dados["margem"], dados.get("precisao_loo", 0.0))
    except Exception:
        pass

    categorias, frases = [], []
    for categoria, lista in EXEMPLOS.items():
        for frase in lista:
            categorias.append(categoria)
            frases.append(frase)

    brutos = _embutir(modelo, frases) if _embutir else None
    if not brutos or len(brutos) != len(frases) or not all(brutos):
        return None
    vetores = [_normalizar(v) for v in brutos]
    margem, precisao = _calibrar(categorias, vetores)

    try:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        for antigo in arquivo.parent.glob(f"tarefas-{_nome_seguro(modelo)}-*.json"):
            if antigo != arquivo:
                antigo.unlink(missing_ok=True)
        temporario = arquivo.with_suffix(".tmp")
        temporario.write_text(json.dumps({
            "modelo": modelo, "hash": _hash_exemplos(), "categorias": categorias,
            "vetores": [[round(x, 5) for x in v] for v in vetores],
            "margem": margem, "precisao_loo": precisao,
        }), encoding="utf-8")
        temporario.replace(arquivo)
    except Exception:
        pass  # sem cache: monta de novo na próxima vez
    return _Indice(categorias, vetores, margem, precisao)


def preparar(modelo):
    """Deixa o índice pronto (bloqueia). True se deu certo."""
    if not modelo or modelo in _falhou:
        return False
    if modelo in _indices:
        return True
    with _trava:
        if modelo in _indices:
            return True
        indice = _montar(modelo)
        if indice is None:
            _falhou.add(modelo)
            return False
        _indices[modelo] = indice
        return True


def preparar_em_segundo_plano(modelo):
    if not modelo or modelo in _indices or modelo in _falhou or modelo in _preparando:
        return
    _preparando.add(modelo)

    def rodar():
        try:
            preparar(modelo)
        finally:
            _preparando.discard(modelo)

    threading.Thread(target=rodar, daemon=True).start()


def pronto(modelo):
    return modelo in _indices


def info(modelo):
    indice = _indices.get(modelo)
    if not indice:
        return None
    return {"exemplos": len(indice.categorias), "margem": indice.margem, "precisao_loo": indice.precisao_loo}


def notas(texto, modelo):
    """[(nota, categoria)] do pedido, ou None (índice não pronto/falha)."""
    indice = _indices.get(modelo)
    if indice is None or not texto or not texto.strip():
        return None
    chave = (modelo, texto.strip().lower())
    q = _cache_pedidos.get(chave)
    if q is None:
        brutos = _embutir(modelo, [texto.strip()]) if _embutir else None
        if not brutos or not brutos[0]:
            # Modelo apagado, Ollama travado: depois de algumas falhas
            # seguidas, para de tentar (cada tentativa custaria o timeout).
            _falhas_seguidas[modelo] = _falhas_seguidas.get(modelo, 0) + 1
            if _falhas_seguidas[modelo] >= LIMITE_FALHAS_SEGUIDAS:
                _falhou.add(modelo)
                _indices.pop(modelo, None)
            return None
        _falhas_seguidas.pop(modelo, None)
        q = _normalizar(brutos[0])
        _cache_pedidos[chave] = q
        if len(_cache_pedidos) > LIMITE_CACHE_PEDIDOS:
            _cache_pedidos.popitem(last=False)
    else:
        _cache_pedidos.move_to_end(chave)
    return indice.pontuar(indice.semelhancas(q))


def classificar(texto, modelo):
    """A tarefa do pedido, ou None se o índice não está pronto ou se o
    pedido é ambíguo (margem abaixo da calibrada): aí o ajudante decide."""
    resultado = notas(texto, modelo)
    if not resultado:
        return None
    if len(resultado) == 1:
        return resultado[0][1]
    (nota1, cat1), (nota2, _) = resultado[0], resultado[1]
    if nota1 - nota2 < _indices[modelo].margem:
        return None
    return cat1


def esquecer():
    """Só pros testes: volta ao estado inicial."""
    _indices.clear()
    _falhou.clear()
    _cache_pedidos.clear()
    _falhas_seguidas.clear()
