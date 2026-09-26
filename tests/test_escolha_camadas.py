"""2.9: escolha da tarefa em camadas (tars_escolha.py + decidir_tarefa)."""

import os
import time

os.environ["TARS_IDIOMA"] = "pt_BR"
from _util import carregar_tars  # noqa: E402

tars = carregar_tars()
M = "granite-embedding:278m"
tars.listar_modelos_instalados = lambda forcar=False: {"qwen3:8b", "qwen2.5:0.5b", M}
tars.desktop_entries = lambda forcar=False: [{"nome": "Spotify"}, {"nome": "GIMP"}, {"nome": "Files"}]
tars._cache_desktop["ts"] = time.time()

# Embeddings e ajudante controlados pelo teste
notas_falsas = {}
embutidos = []


class EmbFalso:
    MODELO_PADRAO = M

    @staticmethod
    def escolher_modelo(instalados):
        return M

    @staticmethod
    def pronto(m):
        return True

    @staticmethod
    def preparar_em_segundo_plano(m):
        pass

    @staticmethod
    def info(m):
        return {"margem": 0.05, "exemplos": 100, "precisao_loo": 0.9}

    @staticmethod
    def notas(texto, m):
        embutidos.append(texto)
        return notas_falsas.get(texto)


tars.embeddings = EmbFalso
ajudante = []


def ajudante_falso(texto, finalistas=None, anterior=None, **kw):
    ajudante.append((texto, finalistas, anterior))
    pistas_recebidas.append(kw.get("pistas"))
    return resposta_ajudante.get(texto)


resposta_ajudante = {}
pistas_recebidas = []
tars._classificar_com_ia = ajudante_falso


def decidir(texto):
    return tars.decidir_tarefa(texto)


# 1. Palavra-chave sem nada contra: decide na hora, sem embeddings nem ajudante
d = decidir("feche o firefox agora mesmo")
assert d.categoria == "acao" and d.via == "palavras" and not embutidos and not ajudante, d
print("OK: palavra-chave clara decide sozinha (nem calcula embeddings)")

# 2. Contexto: "de novo" logo depois de uma ação
tars.lembrar_tarefa("acao")
d = decidir("de novo")
assert d.categoria == "acao" and "contexto" in d.via, d
tars._ultima_tarefa["ts"] = time.time() - 3600
d = decidir("de novo")
assert d.categoria == "simples", d
print("OK: continuação curta ('de novo') herda a tarefa anterior (por 5 minutos)")

# 3. Estrutura: traceback é código, mesmo sem palavra-chave
d = decidir('Traceback (most recent call last):\n  File "a.py", line 3\nNameError: x')
assert d.categoria == "codigo" and "estrutura" in d.via, d
d = decidir("E: Unable to locate package foo quando rodo sudo apt install foo")
assert d.categoria == "tecnico", d
print("OK: traceback -> código; 'E: ... sudo apt' -> técnico (pelo formato)")

# 4. Embeddings decididos: sem ajudante
notas_falsas["quanto está o bitcoin agora"] = [(0.8, "busca"), (0.6, "geral")]
ajudante.clear()
d = decidir("quanto está o bitcoin agora")
assert d.categoria == "busca" and d.via == "embeddings" and not ajudante, d
print("OK: embeddings com folga decidem sem o ajudante")

# 5. Empate: o ajudante escolhe SÓ entre as finalistas
notas_falsas["me fala do novo iphone lançado"] = [(0.61, "busca"), (0.60, "geral"), (0.3, "acao")]
resposta_ajudante["me fala do novo iphone lançado"] = "busca"
d = decidir("me fala do novo iphone lançado")
texto, finalistas, _ = ajudante[-1]
assert d.categoria == "busca" and d.via == "ajudante" and finalistas == ["busca", "geral"], (d, ajudante[-1])
print("OK: em dúvida, o ajudante desempata só entre as finalistas (busca x geral)")

# 6. App instalado citado soma voto de ação
notas_falsas["spotify pausa essa música aí"] = [(0.5, "geral"), (0.49, "acao")]
resposta_ajudante["spotify pausa essa música aí"] = "acao"
d = decidir("spotify pausa essa música aí")
assert d.categoria == "acao" and d.votos.get("acao", 0) > d.votos.get("geral", 0), d
print("OK: citar um app instalado (Spotify) puxa pra ação")

# 7. Coerência: 'simples' pra um pedido longo não passa
longo = "queria entender melhor como organizar meus estudos e minha rotina ao longo das próximas semanas"
notas_falsas[longo] = [(0.5, "simples"), (0.49, "geral")]
resposta_ajudante[longo] = "simples"
d = decidir(longo)
assert d.categoria == "geral" and "coerencia" in d.via, d
print("OK: 'simples' num pedido de 15 palavras é corrigido (camada de coerência)")

# 8. Tudo em silêncio (sem embeddings, sem ajudante): cai no melhor voto / padrão
tars.embeddings = type("Sem", (), {"escolher_modelo": staticmethod(lambda i: None), "MODELO_PADRAO": M})
tars._classificar_com_ia = lambda texto, **kw: None
assert decidir("hmm talvez quem sabe aquilo lá").categoria == "simples"
assert decidir("o que você acha de viajar pra praia ou pra montanha nas férias").categoria == "geral"
print("OK: sem embeddings nem ajudante, segue pelos votos e pelo tamanho do pedido")
print("\nTUDO OK.")
