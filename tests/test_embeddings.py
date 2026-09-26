"""2.6: classificação do pedido por embeddings.

Com um "modelo" de embeddings falso e determinístico (palavras -> vetor),
confere a mecânica: escolha do modelo instalado, índice guardado em cache,
calibração, pedido ambíguo passa pro ajudante, e a ordem palavras-chave ->
embeddings -> ajudante no detectar_tarefa."""

import hashlib
import os
import re
import tempfile
import time

os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="opentars-cache-")

from _util import carregar_tars  # noqa: E402

tars = carregar_tars()
emb = tars.embeddings
emb.configurar(None, os.path.join(os.environ["XDG_CACHE_HOME"], "opentars"))

DIM = 96


def vetor(texto):
    v = [0.0] * DIM
    for palavra in re.findall(r"\w+", texto.lower()):
        h = int(hashlib.md5(palavra.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
        v[(h >> 8) % DIM] += 0.5
    return v


chamadas = []


def embutir(modelo, textos):
    chamadas.append((modelo, len(textos)))
    return [vetor(x) for x in textos]


# 1. Escolha do modelo: multilíngue primeiro; sem ":" = qualquer tag
assert emb.escolher_modelo(["qwen3:8b", "all-minilm:latest", "granite-embedding:278m"]) == "granite-embedding:278m"
assert emb.escolher_modelo(["qwen3:8b", "embeddinggemma:latest", "all-minilm:22m"]) == "embeddinggemma:latest"
assert emb.escolher_modelo(["qwen3:8b", "granite-embedding:30m"]) == "granite-embedding:30m"  # só-inglês, reserva
assert emb.escolher_modelo(["qwen3:8b", "qwen2.5:0.5b"]) is None
os.environ["TARS_MODELO_EMBEDDING"] = "off"
assert emb.escolher_modelo(["granite-embedding:278m"]) is None
os.environ["TARS_MODELO_EMBEDDING"] = "nomic-embed-text"
assert emb.escolher_modelo(["granite-embedding:278m", "nomic-embed-text:latest"]) == "nomic-embed-text:latest"
del os.environ["TARS_MODELO_EMBEDDING"]
print("OK: escolhe o modelo de embeddings instalado (multilíngue primeiro); TARS_MODELO_EMBEDDING força ou desliga")

# 2. Índice: gera os vetores dos exemplos uma vez, guarda, e reusa
emb.configurar(embutir)
emb.esquecer()
M = "granite-embedding:278m"
assert emb.preparar(M) and chamadas == [(M, sum(len(v) for v in emb.EXEMPLOS.values()))], chamadas
dados = emb.info(M)
assert 0 < dados["margem"] <= emb.MARGEM_MAXIMA and dados["exemplos"] > 100, dados
arquivos = os.listdir(os.path.join(os.environ["XDG_CACHE_HOME"], "opentars"))
assert len(arquivos) == 1 and arquivos[0].startswith("tarefas-granite_embedding_278m-"), arquivos
emb.esquecer(); chamadas.clear()
assert emb.preparar(M) and chamadas == [] and emb.info(M)["margem"] == dados["margem"]
print(f"OK: {dados['exemplos']} exemplos viram vetores uma vez; depois vêm do cache (margem calibrada {dados['margem']:.3f})")

# 3. Classificar: frase quase igual a um exemplo decide; ambígua devolve None
#    (o "modelo" falso é bem mais fraco que um de verdade: margem fixa aqui;
#    a calibração é conferida no item 7)
emb._indices[M].margem = 0.05
assert emb.classificar("abre a pasta de downloads", M) == "acao", emb.notas("abre a pasta de downloads", M)[:2]
assert emb.classificar("escreve uma função que inverte uma string", M) == "codigo"
assert emb.classificar("xyz", M) is None
chamadas.clear()
for _ in range(3):
    emb.classificar("abre a pasta de downloads", M)
assert chamadas == [], "pedido repetido não vai de novo ao Ollama"
print("OK: pedido parecido com os exemplos decide; ambíguo fica pro ajudante; repetido vem do cache")

# 4. No detectar_tarefa: palavras-chave primeiro, embeddings antes do ajudante
tars.listar_modelos_instalados = lambda forcar=False: {"qwen3:8b", "qwen2.5:0.5b", M}
ajudante = []
tars._classificar_com_ia = lambda texto, **kw: ajudante.append(texto) or "geral"
assert tars.detectar_tarefa("feche o firefox") == "acao" and not ajudante          # palavra-chave
assert tars.detectar_tarefa("quanto está o bitcoin agora") == "busca" and not ajudante  # sem palavra-chave
emb._indices[M].margem = 1.0  # qualquer pedido fica "em dúvida"
assert tars.detectar_tarefa("hmm talvez quem sabe xyz aquilo") == "geral" and ajudante  # ambíguo: ajudante
print("OK: palavras-chave -> embeddings -> ajudante (o ajudante só é chamado quando os embeddings ficam em dúvida)")

# 5. Índice ainda não pronto: não trava o pedido, prepara em segundo plano
emb.esquecer()
lento = []


def embutir_lento(modelo, textos):
    if len(textos) > 1:
        time.sleep(0.5)
    lento.append(len(textos))
    return [vetor(x) for x in textos]


emb.configurar(embutir_lento)
ajudante.clear()
inicio = time.time()
r5 = tars.detectar_tarefa("quanto está o bitcoin agora")
assert r5 == "geral" and ajudante, (r5, ajudante, emb.pronto(M))
assert time.time() - inicio < 0.3, "não esperou o índice"
limite = time.time() + 5
while not emb.pronto(M) and time.time() < limite:
    time.sleep(0.05)
assert emb.pronto(M)
print("OK: sem o índice pronto, o ajudante responde na hora e o índice é montado em segundo plano")

# 6. Ollama sem o modelo (erro): marca como falho e segue sem embeddings
# 6a. Índice pronto (do cache), mas o modelo sumiu: 3 falhas e para de tentar
emb.configurar(lambda modelo, textos: None)
for i in range(3):
    assert emb.classificar(f"pedido novo número {i}", M) is None
assert not emb.pronto(M) and emb.escolher_modelo([M]) is None
# 6b. Sem cache e sem vetores: nem monta o índice
emb.esquecer()
for arquivo in os.listdir(os.path.join(os.environ["XDG_CACHE_HOME"], "opentars")):
    os.remove(os.path.join(os.environ["XDG_CACHE_HOME"], "opentars", arquivo))
assert not emb.preparar(M) and tars._classificar_por_embedding("abre a pasta", esperar=True) is None
print("OK: se o Ollama não gerar os vetores (modelo apagado, travado), o openTARS segue só com o ajudante")

# 7. Calibração: exemplos separados (mas com ruído, como num modelo de
#    verdade) -> margem pequena; embaralhados -> margem máxima (cauteloso)
import random  # noqa: E402

rnd = random.Random(7)
cats = [["a", "b", "c", "d"][i % 4] for i in range(80)]
bom = [emb._normalizar([(1.0 if j == i % 4 else 0.0) + rnd.gauss(0, 0.35) for j in range(16)]) for i in range(80)]
margem, precisao = emb._calibrar(cats, bom)
assert precisao >= 0.9 and margem < emb.MARGEM_MAXIMA, (margem, precisao)
ruim = [emb._normalizar([rnd.gauss(0, 1) for _ in range(16)]) for _ in range(80)]
margem_ruim, precisao_ruim = emb._calibrar(cats, ruim)
assert precisao_ruim < 0.6 and margem_ruim > margem, (margem_ruim, precisao_ruim)
print(f"OK: modelo bom ({precisao:.0%} interno) decide com margem {margem:.3f}; "
      f"modelo ruim ({precisao_ruim:.0%}) fica cauteloso ({margem_ruim:.3f})")

print("\nTUDO OK.")
