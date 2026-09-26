"""Escolha da tarefa em camadas (2.9).

Cada camada olha o pedido de um jeito e dá VOTOS (categoria -> peso):

  1. palavras-chave  verbo/alvo explícito ("feche", "pesquise")     peso 3
  2. contexto        continuação do pedido anterior ("de novo")      peso 2,5
  3. estrutura       bloco de código, erro, comando, link, pergunta  peso 1–6
  4. apps            cita um app instalado ("spotify", "gimp")       peso 1
  5. embeddings      sentido parecido com os exemplos                peso 2,5 / 1
  6. ajudante        só se as camadas acima empatarem, e escolhendo
                     APENAS entre as finalistas (bem mais fácil pra um
                     modelo de 0,5B do que escolher entre 7)
  7. coerência       corrige resultados sem sentido ("simples" pra um
                     pedido de 20 palavras)

Antes de responder, mais duas análises que mudam COMO a IA trabalha:

  - várias etapas ("abre o claude E faz uma pergunta"): a IA pensa antes,
    ganha um lembrete de fazer tudo e um modelo maior;
  - histórico: cada modelo tem, por tarefa, os acertos e falhas das
    últimas vezes NESTE PC. Quem falha muito numa tarefa vai pro fim da
    fila dela (e volta se melhorar).

Este arquivo tem as partes que não dependem do Ollama nem do sistema
(regras de texto e a combinação dos votos), pra poder testar isoladas."""

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

PESO_PALAVRA = 3.0
PESO_CONTEXTO = 2.5
PESO_EMBEDDING_CERTO = 2.5
PESO_EMBEDDING_DUVIDA = 1.0
PESO_APP = 1.0

# Decide sem o ajudante: a 1ª tarefa soma pelo menos isso E fica pelo menos
# MARGEM_DECISAO na frente da 2ª.
MINIMO_DECISAO = 2.5
MARGEM_DECISAO = 1.5
MAX_FINALISTAS = 3

# Continuação curta do pedido anterior ("agora clica no =", "de novo",
# "and then close it"). Normalizado (sem acento, minúsculo).
_RE_CONTINUACAO = re.compile(
    r"^(e |agora|depois|entao|de novo|denovo|mais uma vez|tambem|outra vez|isso|faz isso|"
    r"and |now|then|again|also|do it|do that|same |"
    r"y |ahora|despues|otra vez|tambien|"
    r"et |maintenant|puis|encore|aussi|"
    r"und |jetzt|dann|nochmal|auch)\b"
)
LIMITE_PALAVRAS_CONTINUACAO = 7
VALIDADE_CONTEXTO_SEG = 300

_RE_BLOCO_CODIGO = re.compile(r"```|^\s*(def |class |import |from \S+ import |function |const |let |#include|public static)", re.M)
_RE_ERRO_CODIGO = re.compile(r"(traceback \(most recent call last\)|syntaxerror|typeerror|nameerror|"
                             r"indentationerror|undefined is not|segmentation fault|nullpointer)", re.I)
_RE_ARQUIVO_CODIGO = re.compile(r"\b\w+\.(py|js|ts|jsx|tsx|java|cpp|c|h|rs|go|rb|php|kt|swift)\b", re.I)
_RE_COMANDO = re.compile(r"(^|\s)(sudo |apt |apt-get |dpkg |systemctl |journalctl |flatpak |snap |pip |"
                         r"chmod |chown |grep |ls -|cd /|/etc/|/usr/|/var/|\.sh\b|\.deb\b|nvidia-smi|"
                         r"e: |error: |erro: )", re.I)
_RE_URL = re.compile(r"(https?://|www\.)\S+|\b[a-z0-9-]+\.(com|com\.br|org|net|io|dev|br|gov)\b", re.I)
_RE_PERGUNTA = re.compile(r"^(o que|oque|qual|quais|quem|quando|por que|porque|como funciona|"
                          r"what|who|when|why|how does|which|que es|quien|cuando|por que|"
                          r"qu'est|qui |quand|pourquoi|was ist|wer |wann|warum)\b")


@dataclass
class Decisao:
    categoria: str
    via: str                                   # camadas que decidiram
    votos: dict = field(default_factory=dict)  # categoria -> soma
    finalistas: list = field(default_factory=list)
    multi_etapas: bool = False                 # "faz X e depois Y"
    trilha: list = field(default_factory=list)  # (camada, o que concluiu): o raciocínio

    def resumo(self):
        votos = ", ".join(f"{c}={v:.1f}" for c, v in sorted(self.votos.items(), key=lambda x: -x[1]))
        return f"{self.categoria} via {self.via} [{votos}]"


class Votos:
    def __init__(self):
        self.soma = {}
        self.origem = {}   # categoria -> [camadas]

    def dar(self, categoria, peso, camada):
        if not categoria or peso <= 0:
            return
        self.soma[categoria] = self.soma.get(categoria, 0.0) + peso
        self.origem.setdefault(categoria, []).append(camada)

    def ranking(self):
        return sorted(self.soma.items(), key=lambda x: -x[1])

    def decisao_clara(self):
        """(categoria, via) se a 1ª ganha com folga, senão None."""
        r = self.ranking()
        if not r:
            return None
        primeira, nota = r[0]
        segunda = r[1][1] if len(r) > 1 else 0.0
        if nota >= MINIMO_DECISAO and nota - segunda >= MARGEM_DECISAO:
            return primeira, "+".join(self.origem[primeira])
        return None

    def finalistas(self, validas):
        r = [c for c, v in self.ranking() if v > 0 and c in validas][:MAX_FINALISTAS]
        return r


def eh_continuacao(texto_normalizado):
    palavras = texto_normalizado.split()
    return 0 < len(palavras) <= LIMITE_PALAVRAS_CONTINUACAO and bool(_RE_CONTINUACAO.match(texto_normalizado))


def votos_estruturais(texto, texto_normalizado, votos):
    """Camada 3: o FORMATO do pedido diz muito, sem IA nenhuma."""
    if _RE_BLOCO_CODIGO.search(texto) or _RE_ERRO_CODIGO.search(texto):
        # Código colado ou um erro de programa: inconfundível, vence as
        # palavras soltas que aparecem dentro dele ("File", "open"...).
        votos.dar("codigo", 6.0, "estrutura")
    elif _RE_ARQUIVO_CODIGO.search(texto):
        votos.dar("codigo", 1.5, "estrutura")
    if _RE_COMANDO.search(texto):
        votos.dar("tecnico", 2.0, "estrutura")
    if _RE_URL.search(texto):
        # Link sozinho ("abre github.com") é abrir site; link + texto, busca.
        votos.dar("simples" if len(texto_normalizado.split()) <= 4 else "busca", 1.5, "estrutura")
    if _RE_PERGUNTA.match(texto_normalizado) and len(texto_normalizado.split()) >= 4:
        votos.dar("geral", 1.0, "estrutura")


def coerencia(categoria, texto_normalizado, votos):
    """Camada 7: resultado sem sentido vira o mais provável que faça sentido."""
    palavras = len(texto_normalizado.split())
    if categoria == "simples" and palavras > 12:
        alternativa = next((c for c, _ in votos.ranking() if c != "simples"), "geral")
        return alternativa, "coerencia"
    return categoria, None


# ------------------------------------------------------------
# Várias etapas
# ------------------------------------------------------------

# Onde um pedido se divide em partes (já normalizado: sem acento, minúsculo).
_RE_CONECTORES = re.compile(
    r"\s*(?:;|,?\s+e depois\s+|,?\s+depois\s+|,?\s+e entao\s+|,?\s+em seguida\s+|,\s*e\s+|\s+e\s+|"
    r",?\s+and then\s+|,?\s+then\s+|,?\s+after that\s+|,\s*and\s+|\s+and\s+|"
    r",?\s+y luego\s+|,?\s+y despues\s+|,?\s+luego\s+|\s+y\s+|"
    r",?\s+et ensuite\s+|,?\s+puis\s+|\s+et\s+|"
    r",?\s+und dann\s+|,?\s+dann\s+|,?\s+danach\s+|\s+und\s+|,)\s*"
)


def partes_do_pedido(texto_normalizado, verbos):
    """As partes do pedido que pedem uma AÇÃO (têm um verbo de ação)."""
    partes = [p.strip() for p in _RE_CONECTORES.split(texto_normalizado) if p and p.strip()]
    return [p for p in partes if set(p.split()) & verbos]


def eh_multi_etapas(texto_normalizado, verbos):
    """'abre a calculadora e faz 12 x 8', 'fecha o spotify e depois abre o
    discord': duas ou mais partes com verbo de ação. 'pesquise preço e
    qualidade' não conta (só um verbo)."""
    return len(partes_do_pedido(texto_normalizado, verbos)) >= 2


# ------------------------------------------------------------
# Histórico de acertos por modelo e tarefa
# ------------------------------------------------------------

class Historico:
    """Últimos resultados (1 = deu certo, 0 = falhou) de cada modelo em cada
    tarefa, guardados em ~/.cache/opentars. Um modelo que falha a maioria
    das vezes numa tarefa desce na fila DAQUELA tarefa; como só as últimas
    vezes contam, ele volta a subir se passar a acertar (ex: modelo novo
    do Ollama, openTARS atualizado)."""

    MAX_REGISTROS = 20
    MIN_AMOSTRAS = 3
    TAXA_RUIM = 0.6

    def __init__(self, caminho):
        self.caminho = Path(caminho)
        self.dados = None
        self.versao = 0
        self._trava = threading.Lock()

    def _carregar(self):
        if self.dados is None:
            try:
                dados = json.loads(self.caminho.read_text(encoding="utf-8"))
                self.dados = {k: [1 if x else 0 for x in v][-self.MAX_REGISTROS:]
                              for k, v in dados.items() if isinstance(v, list)}
            except Exception:
                self.dados = {}
        return self.dados

    @staticmethod
    def _chave(modelo, tarefa):
        return f"{tarefa}|{modelo}"

    def registrar(self, modelo, tarefa, ok):
        if not modelo or not tarefa or ok is None:
            return
        with self._trava:
            dados = self._carregar()
            chave = self._chave(modelo, tarefa)
            dados[chave] = (dados.get(chave, []) + [1 if ok else 0])[-self.MAX_REGISTROS:]
            self.versao += 1
            try:
                self.caminho.parent.mkdir(parents=True, exist_ok=True)
                temporario = self.caminho.with_suffix(".tmp")
                temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
                os.replace(temporario, self.caminho)
            except Exception:
                pass  # sem disco: vale só nesta sessão

    def placar(self, modelo, tarefa):
        """(acertos, total) das últimas vezes."""
        lista = self._carregar().get(self._chave(modelo, tarefa), [])
        return sum(lista), len(lista)

    def ruim(self, modelo, tarefa):
        acertos, total = self.placar(modelo, tarefa)
        return total >= self.MIN_AMOSTRAS and (total - acertos) / total >= self.TAXA_RUIM

    def reordenar(self, modelos, tarefa, fixos_no_fim=()):
        """Os que falham muito nessa tarefa vão pro fim (antes dos fixos,
        ex: modelos de programação numa tarefa que não é código)."""
        fixos = [m for m in modelos if m in fixos_no_fim]
        resto = [m for m in modelos if m not in fixos_no_fim]
        bons = [m for m in resto if not self.ruim(m, tarefa)]
        ruins = [m for m in resto if self.ruim(m, tarefa)]
        return bons + ruins + fixos
