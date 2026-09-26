#!/usr/bin/env python3

import os
import sys
import difflib
import json
import time
import shlex
import shutil
import subprocess
import webbrowser
import threading
import re
import io
import base64
import gettext
import functools
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor

import socket
import logging
from logging.handlers import RotatingFileHandler

import requests
import psutil
from PIL import Image

import tars_i18n as i18n
from tars_i18n import t
import tars_acessibilidade as acess
import tars_embeddings as embeddings
import tars_escolha as escolha
import tars_ocr as ocr


# ------------------------------------------------------------
# Sessão gráfica. O controle de mouse/teclado (pyautogui) fala com o
# servidor X. Numa sessão Wayland ele só alcança janelas XWayland, e
# sem nenhum display ele nem importa: o import falha e, antes, isso
# derrubava o openTARS inteiro, até o chat que não precisa de mouse.
# ------------------------------------------------------------

def _tipo_sessao():
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "nenhuma"


SESSAO_GRAFICA = _tipo_sessao()


class _PyautoguiIndisponivel:
    """Fica no lugar do pyautogui quando ele não carrega: qualquer uso
    (mover mouse, clicar, digitar) vira um erro com o motivo real, que
    a ferramenta devolve pra IA em vez de quebrar o programa."""

    def __init__(self, motivo):
        self.__dict__["_motivo"] = motivo

    def __getattr__(self, nome):
        raise RuntimeError(f"mouse/keyboard control unavailable: {self._motivo}")


# O pyautogui (via python-xlib) aborta a importação se não houver
# arquivo de autorização do X. Sem ~/.Xauthority e sem $XAUTHORITY não
# existe cookie nenhum mesmo, então apontar pra um arquivo vazio só
# evita o crash (os lançadores fazem o mesmo; isso cobre quem roda o
# tars.py direto).
if not os.environ.get("XAUTHORITY") and not (Path.home() / ".Xauthority").exists():
    os.environ["XAUTHORITY"] = "/dev/null"

try:
    import pyautogui
    ERRO_PYAUTOGUI = None
except Exception as _e:  # sem display, display inacessível, Xlib ausente...
    # Texto técnico (vai pra IA nos resultados das ferramentas); pro
    # usuário, avisos_de_ambiente() explica no idioma dele.
    ERRO_PYAUTOGUI = (
        "no graphical session (DISPLAY is empty)"
        if SESSAO_GRAFICA == "nenhuma"
        else f"{type(_e).__name__}: {_e}"
    )
    pyautogui = _PyautoguiIndisponivel(ERRO_PYAUTOGUI)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

VERSAO = "2.9.0"

# Endereço do servidor Ollama — respeita a variável de ambiente padrão
# do próprio Ollama (OLLAMA_HOST), pra funcionar sem editar o código
# em qualquer máquina onde o Ollama não esteja no endereço padrão
# (outra porta, outro host na rede, WSL, container etc.).
def _normalizar_ollama_host(valor):
    """Interpreta OLLAMA_HOST do mesmo jeito que o próprio Ollama: o
    valor pode vir sem esquema ("0.0.0.0", "192.168.0.10:11434"), sem
    porta, ou só com a porta (":11434"). Muita gente deixa
    OLLAMA_HOST=0.0.0.0 no ambiente pra expor o Ollama na rede — usado
    cru, isso virava a URL "0.0.0.0/api/chat" e nada funcionava."""

    s = (valor or "").strip()
    if not s:
        return "http://127.0.0.1:11434"

    if "://" in s:
        esquema, resto = s.split("://", 1)
        esquema = esquema.lower()
        porta_padrao = {"http": "80", "https": "443"}.get(esquema, "11434")
    else:
        esquema, resto = "http", s
        porta_padrao = "11434"

    hostporta, _, caminho = resto.partition("/")

    m = re.fullmatch(r"\[([^\]]*)\](?::(\d+))?", hostporta)  # IPv6: [::1]:11434
    if m:
        host, porta = m.group(1), m.group(2)
    elif hostporta.count(":") == 1:
        host, porta = hostporta.split(":")
    else:
        host, porta = hostporta, None

    # Endereços "escute em todas as interfaces" servem pro servidor,
    # não pra conectar: do lado do cliente, significam esta máquina.
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"

    url = f"{esquema}://{host}:{porta or porta_padrao}"
    if caminho.strip("/"):
        url += "/" + caminho.strip("/")
    return url


OLLAMA_HOST = _normalizar_ollama_host(os.environ.get("OLLAMA_HOST"))

OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"
OLLAMA_SHOW_URL = f"{OLLAMA_HOST}/api/show"
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST}/api/generate"
OLLAMA_PS_URL = f"{OLLAMA_HOST}/api/ps"
OLLAMA_EMBED_URL = f"{OLLAMA_HOST}/api/embed"
OLLAMA_EMBED_ANTIGO_URL = f"{OLLAMA_HOST}/api/embeddings"  # Ollama < 0.3

KEEP_ALIVE = "30m"
# O ajudante é minúsculo (~400 MB): fica carregado junto com o modelo
# de conversa pra classificar e resolver nomes sem esperar carregamento.
KEEP_ALIVE_AJUDANTE = "30m"
KEEP_ALIVE_CLASSIFICADOR = KEEP_ALIVE_AJUDANTE

# Usado só como último recurso, se o catálogo dinâmico (ver mais
# abaixo) vier vazio por algum motivo (Ollama offline, sem nenhum
# modelo baixado). O TARS não depende mais de uma lista fixa de
# modelos — ele descobre o que está instalado e como cada um se
# comporta consultando o próprio Ollama.
MODELO_PADRAO = os.environ.get("TARS_MODELO_PADRAO", "qwen3:0.6b")

# Modelo AJUDANTE: minúsculo e rápido, faz os trabalhos de bastidor —
# classificar o pedido (pra escolher o modelo de conversa), resolver o
# nome de um app ou site que a busca normal não achou e interpretar o
# "/modelo <descrição>". Não é usado pra conversar. O instalador baixa
# ele sozinho. qwen2.5:0.5b: segue instruções e formato bem pro tamanho,
# e não gasta tempo "pensando" (sem modo de raciocínio).
MODELO_AJUDANTE = (
    os.environ.get("TARS_MODELO_AJUDANTE")
    or os.environ.get("TARS_MODELO_CLASSIFICADOR")  # nome antigo da variável
    or "qwen2.5:0.5b"
)
MODELO_CLASSIFICADOR = MODELO_AJUDANTE  # nome antigo, mantido por compatibilidade

# Ajudante das versões 1.x/2.0 antigas. Se ainda estiver instalado, não
# é mais usado pra nada e fica fora da escolha automática de conversa.
MODELO_AJUDANTE_ANTIGO = "gemma3:270m"

# ------------------------------------------------------------
# Timeouts de rede (segundos) — nomeados por propósito, não por valor,
# pra ficar claro o que cada chamada está esperando e por quê.
# ------------------------------------------------------------
TIMEOUT_HTTP_CURTO = 5          # checagens rápidas: /api/tags, /api/ps
TIMEOUT_HTTP_MODELO_INFO = 6    # /api/show de um modelo
TIMEOUT_HTTP_DESCARREGAR = 30   # pedido de unload (keep_alive=0)
TIMEOUT_HTTP_AJUDANTE = 20      # consulta pontual e curta a um modelo leve
TIMEOUT_HTTP_LONGO = 600        # carregar um modelo grande / resposta de conversa completa
TIMEOUT_TERMINAL_COMANDO = 30   # comando digitado pelo usuário/IA no terminal
TIMEOUT_SUBPROCESSO_PADRAO = 15 # subprocessos internos em geral (flatpak, etc.)
TIMEOUT_CLIPBOARD = 5           # xclip/xsel
TIMEOUT_GPU = 5                 # nvidia-smi
TIMEOUT_FLATPAK_LISTAGEM = 10   # flatpak list
TIMEOUT_DNS = 3                 # checar se um domínio sugerido pela IA existe de fato

# Timeout curto de propósito — é uma chamada pequena e opcional; se o
# Ollama estiver lento/travado, é melhor desistir rápido e cair no
# comportamento padrão do que travar a resposta do usuário esperando.
TIMEOUT_CLASSIFICADOR = 8

# Um pedido vira um vetor em ~10–30 ms; se passar disso, o modelo de
# embeddings está carregando ou o Ollama está ocupado: desiste e o
# ajudante decide. Os exemplos (centenas de frases de uma vez) podem
# demorar mais na primeira vez, em segundo plano.
TIMEOUT_EMBEDDING_PEDIDO = 2
TIMEOUT_EMBEDDING_EXEMPLOS = 120

# ------------------------------------------------------------
# Temperaturas — cada uso tem um propósito diferente: a classificadora
# quer sempre a mesma resposta pro mesmo texto (determinístico), a
# conversa principal quer um pouco de variação natural.
# ------------------------------------------------------------
TEMPERATURA_CONVERSA = 0.1
TEMPERATURA_AJUDANTE = 0.1
TEMPERATURA_CLASSIFICADOR = 0.0

# TTL (segundos) do cache de apps instalados (.desktop / flatpak).
# Evita re-escanear o sistema de arquivos e chamar subprocess a cada comando.
CACHE_APPS_TTL = 30

# ------------------------------------------------------------
# Outros limites/ajustes numéricos usados pelo código — nomeados aqui
# em vez de espalhados como números soltos no meio da lógica.
# ------------------------------------------------------------
LIMITE_CICLOS_FERRAMENTAS = 20      # máximo de idas-e-voltas com tool calls por mensagem (clicar num app leva vários)
LIMITE_HISTORICO_MENSAGENS = 30     # mensagens guardadas por sessão antes de podar
LIMITE_STDOUT_CHARS = 8000          # truncamento do stdout do terminal
LIMITE_STDERR_CHARS = 4000          # truncamento do stderr do terminal
LIMITE_ARQUIVOS_LISTADOS = 200      # itens retornados por list_files
LIMITE_CANDIDATOS_APP = 5           # nomes de executável sugeridos pela IA por pedido
LARGURA_MINIMA_CAIXA = 44           # largura mínima das caixas de status no terminal

# Confirmação de que um app abriu: em vez de dormir um tempo fixo e só
# então checar uma vez (que ou demora demais pra apps rápidos, ou é
# curto demais pra apps pesados tipo navegador), faz polling — checa
# em intervalos curtos até confirmar o processo ativo ou estourar o
# teto. Isso deixa apps leves (calculadora, editor de texto) prontos
# quase na hora, e dá mais chance real de apps pesados estarem de pé
# antes do TARS devolver sucesso — importante pra tarefa composta tipo
# "abra X e digite Y", onde o type_text roda logo em seguida.
INTERVALO_POLL_PROCESSO_SEG = 0.15
TIMEOUT_POLL_PROCESSO_SEG = 2.0

# Automação de mouse/teclado — durações bem curtas de propósito: o
# usuário quer velocidade aqui, não uma animação suave de cursor.
# pyautogui.PAUSE (configurado logo abaixo) é o que mais pesava nisso:
# por padrão ele insere 0.1s de pausa DEPOIS de toda chamada (move,
# clique, tecla...), e numa tarefa com vários passos (abrir, mover,
# clicar, digitar, apertar tecla) isso somava meio segundo só de
# pausas artificiais.
DURACAO_MOVER_MOUSE_SEG = 0.03
DURACAO_MOVER_PARA_SCROLL_SEG = 0.02
INTERVALO_DIGITACAO_SEG = 0.005
PYAUTOGUI_PAUSE_SEG = 0.01

pyautogui.PAUSE = PYAUTOGUI_PAUSE_SEG
# FAILSAFE fica ligado de propósito: jogar o mouse pro canto da tela
# ainda aborta qualquer automação em andamento, e isso não tem custo
# de velocidade nenhum.

INTERVALO_CPU_PERCENT_SEG = 0.2     # amostragem do psutil.cpu_percent
TENTATIVAS_ENCERRAR_IAS = 4
ESPERA_ENTRE_TENTATIVAS_ENCERRAR_SEG = 1.5

# Tempo máximo que a própria IA pode pedir pra esperar entre um passo e
# outro (ver ferramenta wait_seconds) — evita que ela trave o TARS por
# minutos "esperando" alguma coisa carregar.
LIMITE_ESPERA_SEG = 5.0

# Largura máxima (em pixels) do print enviado pro modelo de visão.
# Uma tela 4K sem redimensionar gera um PNG grande o bastante pra
# pesar de verdade na codificação em base64, no envio pro Ollama e no
# processamento da imagem pelo próprio modelo (que internamente já
# reduz a resolução de qualquer jeito) — encolher ANTES de mandar
# corta esse tempo todo sem perder legibilidade prática (texto de UI
# continua lendo bem nessa largura). Configurável por variável de
# ambiente pra quem quiser mais nitidez à custa de velocidade.
LARGURA_MAXIMA_SCREENSHOT = int(os.environ.get("TARS_LARGURA_SCREENSHOT", "1280"))

# Capacidades reconhecidas pelo Ollama que o TARS realmente usa pra
# decidir comportamento — mostradas na listagem de modelos e citadas
# no prompt da IA classificadora.
_CAPACIDADES_RELEVANTES = {"tools", "vision", "thinking"}


# ============================================================
# LOG DE AUDITORIA
# ============================================================

# Toda ferramenta executada (abrir app, rodar comando, fechar
# processo etc.) fica registrada aqui — nome, argumentos e resultado,
# com timestamp. É o que permite, depois de qualquer coisa inesperada
# acontecer, entender o que foi executado e por quê, em vez de
# depender só da memória de quem estava olhando o terminal na hora.
DIRETORIO_LOG = Path(os.environ.get("TARS_DIR_LOG", str(Path.home() / ".tars_log")))
ARQUIVO_LOG = DIRETORIO_LOG / "tars.log"
TAMANHO_MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUPS_LOG = 3
LIMITE_LOG_CAMPO_CHARS = 300


def _configurar_log():
    logger = logging.getLogger("tars")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        DIRETORIO_LOG.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            ARQUIVO_LOG,
            maxBytes=TAMANHO_MAX_LOG_BYTES,
            backupCount=BACKUPS_LOG,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)

    except Exception as e:
        # Auditoria é best-effort: se não der pra escrever o log (disco
        # cheio, permissão negada etc.), o TARS continua funcionando
        # normalmente sem ela, só avisa uma vez. Print puro aqui (sem
        # o helper de cor c()) de propósito: essa função roda antes
        # dele existir na ordem de definição do módulo.
        print(f"[openTARS] {t('msg.log_desativado', erro=e)}")
        logger.addHandler(logging.NullHandler())

    return logger


LOG = _configurar_log()


def _resumir_para_log(valor):
    """Converte um valor qualquer (geralmente os argumentos de uma
    ferramenta) numa string curta o bastante pro log — sem isso, um
    texto digitado enorme ou um resultado de comando de terminal
    grande inflaria o arquivo de log rapidamente."""

    try:
        texto = json.dumps(valor, ensure_ascii=False)
    except Exception:
        texto = str(valor)

    if len(texto) > LIMITE_LOG_CAMPO_CHARS:
        texto = texto[:LIMITE_LOG_CAMPO_CHARS] + "…"

    return texto


# ============================================================
# ESTADO
# ============================================================

modo_modelo = "AUTO"
modelo_manual = None
modelo_atual = None

sessoes = {}

lock_modelo = threading.Lock()

# Sessão HTTP única e reutilizada: evita reabrir conexão TCP/TLS
# a cada chamada ao Ollama (isso importa bastante no loop de tools,
# que pode chamar o Ollama várias vezes por turno).
_SESSION = requests.Session()

# Cache de aplicativos instalados (desktop entries e flatpak).
_cache_desktop = {"dados": None, "ts": 0.0}
_cache_flatpak = {"dados": None, "ts": 0.0}


# ============================================================
# UTILIDADES
# ============================================================

def _tabela_sem_acentos():
    """Letras latinas acentuadas -> sem acento (á->a, ñ->n, ö->o, ß->ss...),
    montada uma vez só: str.translate com ela é bem mais rápido que
    decompor Unicode a cada chamada, e normalizar() roda muito (uma vez
    por processo do sistema, por app instalado, por palavra-chave...)."""
    tabela = {}
    for codigo in range(0xC0, 0x250):
        letra = chr(codigo)
        base = "".join(x for x in unicodedata.normalize("NFKD", letra) if not unicodedata.combining(x))
        if base != letra and base.isascii() and base.isalpha():
            tabela[codigo] = base.lower()
    tabela.update({ord("ß"): "ss", ord("æ"): "ae", ord("œ"): "oe", ord("ø"): "o", ord("đ"): "d", ord("ł"): "l"})
    return tabela


_TABELA_NORMALIZACAO = _tabela_sem_acentos()

# Separa palavras (Unicode: funciona com qualquer alfabeto).
_RE_TOKENS = re.compile(r"[\W_]+")


def normalizar(texto):
    return texto.lower().strip().translate(_TABELA_NORMALIZACAO)


_RE_SEPARADORES_NOME = re.compile(r"[-_.:/]+")


def normalizar_nome_app(texto):
    """Pra comparar nomes de apps: "Counter-Strike 2" vira "counter strike 2",
    e bate com o "counter strike" que o usuário escreveu."""
    return re.sub(r"\s+", " ", _RE_SEPARADORES_NOME.sub(" ", normalizar(texto or ""))).strip()


# ------------------------------------------------------------
# Vocabulário dos idiomas: palavras-chave, comandos, verbos de busca...
# Montado uma vez a partir dos arquivos de idioma (já normalizado e em
# conjuntos, pra busca instantânea) e refeito só quando o idioma muda.
# ------------------------------------------------------------

_CHAVES_COMANDOS = (
    "cmd.modelos", "cmd.modelo", "cmd.encerrar", "cmd.limpar", "cmd.idioma",
    "cmd.auto", "cmd.auto_argumento", "cmd.sair",
)

_vocab_atual = None


def _preparar_expressoes(expressoes):
    """(palavras soltas, trechos com espaço), normalizados."""
    palavras, trechos = set(), []
    for expr in expressoes:
        n = normalizar(str(expr))
        if not n:
            continue
        if " " in n:
            trechos.append(n)
        else:
            palavras.add(n)
    return frozenset(palavras), tuple(dict.fromkeys(trechos))


def _normalizados(itens):
    return tuple(dict.fromkeys(n for n in (normalizar(str(x)) for x in itens) if n))


# Palavras das listas de abrir/fechar/buscar que não são verbos ("feche a
# ABA do youtube"): não contam como uma etapa do pedido.
_NAO_SAO_VERBOS = frozenset({"aba", "abas", "tab", "tabs", "pestana", "pestanas", "onglet", "onglets",
                             "reiter", "tabs", "site", "pagina", "page", "website"})


def _montar_vocabulario():
    entrada = i18n.idiomas_de_entrada()
    v = {
        "detectar": {
            grupo: _preparar_expressoes(list(neutras) + i18n.juntar(f"detectar.{grupo}", entrada))
            for grupo, neutras in _PALAVRAS_NEUTRAS.items()
        },
        "busca_verbos": frozenset(_normalizados(i18n.juntar("busca.verbos", entrada))),
        # Verbos de ação (pra achar pedidos com várias etapas): os da lista
        # própria + os das palavras-chave de abrir/fechar/buscar.
        "verbos_acao": frozenset(
            palavra for frase in _normalizados(
                i18n.juntar("detectar.verbos", entrada) + i18n.juntar("detectar.abrir", entrada)
                + i18n.juntar("detectar.fechar", entrada) + i18n.juntar("detectar.busca", entrada)
                + i18n.juntar("busca.verbos", entrada))
            for palavra in frase.split()[:1]
            if palavra not in _NAO_SAO_VERBOS
        ),
        "busca_conectores": _normalizados(i18n.juntar("busca.conectores", entrada)),
        "busca_preposicoes": _normalizados(i18n.juntar("busca.preposicoes", entrada)),
        "navegadores": frozenset(_MARCAS_NAVEGADORES) | frozenset(_normalizados(i18n.juntar("palavras.navegador", entrada))),
        # Comandos são explícitos: valem em qualquer idioma.
        "comandos": {chave: frozenset(_normalizados(i18n.em_todos(chave))) for chave in _CHAVES_COMANDOS},
        "respostas_sim": frozenset(_normalizados(i18n.em_todos("cmd.sim"))),
        "modelo_ignorar": frozenset(_normalizados(i18n.em_todos("modelo.ignorar"))),
    }
    superlativos = {}
    for chave, resolvedor in _CRITERIOS_MODELO.items():
        for frase in _normalizados(i18n.em_todos(chave)):
            superlativos.setdefault(frase, resolvedor)
    v["superlativos"] = superlativos

    # Nomes traduzidos de apps: Name[pt_BR]= no .desktop e catálogos gettext.
    grupos = [tuple(i18n.juntar("_locale", [codigo]) or [codigo]) for codigo in entrada]
    for local in i18n.locales_do_sistema():
        grupos.append((local, local.split("_")[0]) if "_" in local else (local,))
    grupos = tuple(dict.fromkeys(grupos))
    v["grupos_locale"] = grupos
    v["sufixos_desktop"] = tuple(dict.fromkeys([""] + [f"[{x}]" for grupo in grupos for x in grupo]))
    return v


def _vocab():
    global _vocab_atual
    v = _vocab_atual
    if v is None:
        v = _vocab_atual = _montar_vocabulario()
    return v


def _ao_mudar_idioma(_codigo):
    """Idioma novo: palavras-chave novas e nomes de apps relidos."""
    global _vocab_atual
    _vocab_atual = None
    _cache_arquivos_desktop.clear()
    _cache_desktop["dados"] = None


i18n.ao_mudar_idioma(_ao_mudar_idioma)


# ------------------------------------------------------------
# Interface: cores ANSI (funcionam no terminal do Zorin/qualquer
# terminal Linux moderno). Usadas só no que é impresso pro humano —
# nunca dentro do conteúdo de "mensagem" devolvido nas ferramentas,
# porque esse texto vai para o JSON que a IA lê.
# ------------------------------------------------------------

class Cor:
    RESET = "\033[0m"
    NEGRITO = "\033[1m"
    CINZA = "\033[90m"
    VERMELHO = "\033[31m"
    VERDE = "\033[32m"
    AMARELO = "\033[33m"
    AZUL = "\033[34m"
    MAGENTA = "\033[35m"
    CIANO = "\033[36m"


def c(texto, cor):
    return f"{cor}{texto}{Cor.RESET}"


def imprimir_caixa(titulo, linhas, cor_titulo=Cor.CIANO):
    largura = (
        max(LARGURA_MINIMA_CAIXA, len(titulo) + 4, *(len(l) + 4 for l in linhas))
        if linhas
        else max(LARGURA_MINIMA_CAIXA, len(titulo) + 4)
    )

    print()
    print(c("╭" + "─" * largura + "╮", Cor.CINZA))
    print(c("│ ", Cor.CINZA) + c(titulo.ljust(largura - 2), cor_titulo) + c(" │", Cor.CINZA))

    for linha in linhas:
        print(c("│ ", Cor.CINZA) + linha.ljust(largura - 2)[:largura - 2] + c(" │", Cor.CINZA))

    print(c("╰" + "─" * largura + "╯", Cor.CINZA))


def executar_subprocesso(cmd, timeout=TIMEOUT_SUBPROCESSO_PADRAO):
    try:
        resultado = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return {
            "codigo": resultado.returncode,
            "stdout": resultado.stdout.strip(),
            "stderr": resultado.stderr.strip(),
        }

    except subprocess.TimeoutExpired:
        return {
            "codigo": -1,
            "stdout": "",
            "stderr": "timeout",
        }

    except Exception as e:
        return {
            "codigo": -1,
            "stdout": "",
            "stderr": str(e),
        }


# ============================================================
# SAÍDA PRO USUÁRIO (terminal ou janela)
# ============================================================
#
# O que o openTARS mostra enquanto trabalha (ferramenta rodando, modelo
# carregando, raciocínio e resposta chegando) sai como EVENTO. No
# terminal vira texto colorido; a janela registra um ouvinte e recebe o
# evento já estruturado, em vez de ler e interpretar o texto impresso
# (que muda com o idioma escolhido).

_ouvinte_saida = None


def definir_ouvinte_saida(funcao):
    """A interface gráfica registra aqui uma função(tipo, dados). None
    volta a imprimir no terminal."""
    global _ouvinte_saida
    _ouvinte_saida = funcao


def emitir(tipo, **dados):
    ouvinte = _ouvinte_saida
    if ouvinte is not None:
        try:
            ouvinte(tipo, dados)
        except Exception:
            pass
        return
    renderizar = _TERMINAL.get(tipo)
    if renderizar is not None:
        renderizar(dados)


def nome_ferramenta(nome):
    """"open_application" -> "abrindo app" (no idioma escolhido)."""
    chave = f"ferramenta.{nome}"
    texto = t(chave)
    return nome if texto == chave else texto


def nome_tarefa(tarefa):
    chave = f"tarefa.{tarefa}"
    texto = t(chave)
    return tarefa if texto == chave else texto


def _segundos(valor):
    return f"{valor:.2f}"


def _term_ferramenta(d):
    resumo = f" · {d['resumo']}" if d.get("resumo") else ""
    print(c(f"[openTARS] ▸ {nome_ferramenta(d['nome'])}{resumo}", Cor.CIANO))


def _term_ferramenta_fim(d):
    if d.get("sucesso"):
        print(c(f"[openTARS]   ✓ {_segundos(d['segundos'])}s", Cor.VERDE))
    else:
        print(c(f"[openTARS]   ✗ {t('msg.falhou')} ({_segundos(d['segundos'])}s)", Cor.VERMELHO))


def _term_tarefa(d):
    if d.get("modo") == "AUTO":
        texto = t("msg.tarefa_auto", tarefa=nome_tarefa(d["tarefa"]), modelo=d["modelo"])
        print(c(f"\n[AUTO] {texto}", Cor.AZUL))
    else:
        print(c(f"\n[MANUAL] {t('msg.tarefa_manual', modelo=d['modelo'])}", Cor.AMARELO))


def _term_carregando(d):
    if d.get("segundo_plano"):
        print(c(f"\n[IA] {t('msg.carregando_fundo', modelo=d['modelo'])}", Cor.CINZA))
    else:
        imprimir_caixa(t("msg.carregando_titulo", modelo=d["modelo"]), [t("msg.carregando_demora")])


def _term_carregado(d):
    print(c("\n✓ " + t("msg.carregado", modelo=d["modelo"], segundos=_segundos(d["segundos"])), Cor.VERDE))


def _term_descarregando(d):
    print(c(f"\n[IA] {t('msg.descarregando', modelo=d['modelo'])}", Cor.CINZA))


def _term_pensamento(d):
    if d.get("inicio"):
        print(c(f"\n[{t('msg.pensando')}] ", Cor.CINZA), end="", flush=True)
    print(c(d["texto"], Cor.CINZA), end="", flush=True)


def _term_resposta(d):
    if d.get("inicio"):
        if d.get("apos_pensamento"):
            print()
        print(c("\nopenTARS › ", Cor.NEGRITO + Cor.MAGENTA), end="", flush=True)
    print(d["texto"], end="", flush=True)


def _term_tempo(d):
    cor = Cor.AZUL if d.get("modo") == "AUTO" else Cor.AMARELO
    texto = t("msg.tempo_total", modelo=d["modelo"], segundos=_segundos(d["segundos"]))
    print(c(f"[{d.get('modo')}] {texto}", cor))


_TERMINAL = {
    "ferramenta": _term_ferramenta,
    "ferramenta_fim": _term_ferramenta_fim,
    "tarefa": _term_tarefa,
    "carregando": _term_carregando,
    "carregado": _term_carregado,
    "descarregando": _term_descarregando,
    "pensamento": _term_pensamento,
    "resposta": _term_resposta,
    "fim_stream": lambda d: print(),
    "tempo": _term_tempo,
    "analisando": lambda d: print(c(f"[IA] {t('msg.analisando', modelo=d['modelo'])}", Cor.CINZA)),
}


# ============================================================
# OLLAMA
# ============================================================

def ollama_request(payload, stream=False, timeout=TIMEOUT_HTTP_LONGO):
    return _SESSION.post(
        OLLAMA_URL,
        json=payload,
        timeout=timeout,
        stream=stream,
    )


def _chat_unico(
    modelo,
    prompt,
    temperatura=TEMPERATURA_AJUDANTE,
    timeout=TIMEOUT_HTTP_AJUDANTE,
    keep_alive=KEEP_ALIVE_AJUDANTE,
    formato=None,
    imagens=None,
):
    """Uma pergunta isolada e não-streaming a um modelo, sem tocar no
    histórico de conversa principal nem em qual modelo está 'ativo' —
    usada por toda consulta pontual e de baixo custo que o TARS faz a
    uma IA leve (resolver nome de app/site, classificar tarefa,
    resolver troca de modelo por descrição livre). Centraliza aqui o
    try/except e o parsing da resposta, que antes se repetiam em cada
    uma dessas funções com um timeout diferente (e, num caso, sem
    timeout curto nenhum). Devolve o texto de resposta já sem espaços
    nas pontas, ou None em qualquer falha (Ollama offline, timeout,
    resposta malformada etc.) — quem chamou decide o que fazer na
    ausência de resposta."""

    corpo = {
        "model": modelo,
        "messages": [{"role": "user", "content": prompt, **({"images": imagens} if imagens else {})}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": temperatura},
    }

    # Pergunta curta não precisa de raciocínio: num modelo que "pensa"
    # (qwen3), deixar ligado multiplica o tempo e pode vazar <think> na
    # resposta que o openTARS tenta interpretar.
    info = (_cache_catalogo.get("dados") or {}).get(modelo)
    if info and "thinking" in info.get("capacidades", ()):
        corpo["think"] = False

    # Resposta forçada num formato (JSON schema): um modelo pequeno não
    # consegue "responder qualquer coisa" — só um valor válido.
    if formato:
        corpo["format"] = formato

    try:
        resposta = _SESSION.post(OLLAMA_URL, json=corpo, timeout=timeout)
        # Ollama antigo sem suporte a think/format: tenta sem eles.
        for extra in ("think", "format"):
            if resposta.status_code == 200 or extra not in corpo:
                continue
            corpo.pop(extra)
            resposta = _SESSION.post(OLLAMA_URL, json=corpo, timeout=timeout)
    except Exception:
        return None

    if resposta.status_code != 200:
        return None

    try:
        texto = resposta.json().get("message", {}).get("content") or ""
    except Exception:
        return None

    return re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()


def _json_da_resposta(texto):
    """Lê o JSON devolvido pelo ajudante (ou o primeiro {...} no texto,
    se veio algo em volta). None se não houver JSON."""
    if not texto:
        return None
    try:
        return json.loads(texto)
    except Exception:
        pass
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _embutir(modelo, textos):
    """Vetores dos textos pelo Ollama (/api/embed), ou None."""
    timeout = TIMEOUT_EMBEDDING_PEDIDO if len(textos) == 1 else TIMEOUT_EMBEDDING_EXEMPLOS
    try:
        r = _SESSION.post(OLLAMA_EMBED_URL, json={
            "model": modelo, "input": textos, "keep_alive": KEEP_ALIVE_AJUDANTE, "truncate": True,
        }, timeout=timeout)
        if r.status_code == 200:
            vetores = r.json().get("embeddings")
            return vetores if isinstance(vetores, list) and len(vetores) == len(textos) else None
        if r.status_code != 404:
            return None
        vetores = []  # Ollama antigo: um texto por chamada
        for texto in textos:
            r = _SESSION.post(OLLAMA_EMBED_ANTIGO_URL, json={"model": modelo, "prompt": texto,
                                                            "keep_alive": KEEP_ALIVE_AJUDANTE}, timeout=timeout)
            if r.status_code != 200:
                return None
            vetores.append(r.json().get("embedding"))
        return vetores
    except Exception:
        return None


embeddings.configurar(_embutir)


def modelo_embedding():
    """O modelo de embeddings instalado que classifica os pedidos, ou None."""
    try:
        return embeddings.escolher_modelo(listar_modelos_instalados())
    except Exception:
        return None


def aquecer_ajudante():
    """Carrega o ajudante em segundo plano ao abrir o openTARS, pra
    primeira classificação não esperar o carregamento. Sem opções
    extras de propósito: tem que bater com as chamadas de _chat_unico,
    senão o Ollama recarregaria o modelo na primeira pergunta."""

    def carregar():
        try:
            if MODELO_AJUDANTE in listar_modelos_instalados():
                _SESSION.post(
                    OLLAMA_GENERATE_URL,
                    json={"model": MODELO_AJUDANTE, "prompt": "", "keep_alive": KEEP_ALIVE_AJUDANTE},
                    timeout=TIMEOUT_HTTP_LONGO,
                )
        except Exception:
            pass

    threading.Thread(target=carregar, daemon=True).start()
    # Os vetores dos exemplos de cada tarefa (do cache, ou gerados agora).
    try:
        embeddings.preparar_em_segundo_plano(modelo_embedding())
    except Exception:
        pass


def descarregar_modelo(modelo):
    """Pede ao Ollama para descarregar um modelo (keep_alive=0).
    Retorna True se a chamada foi aceita — isso NÃO garante que o
    modelo já saiu da memória, só que o Ollama recebeu o pedido; a
    confirmação de verdade é feita via /api/ps em encerrar_todas_ias."""

    if not modelo:
        return False

    try:
        r = _SESSION.post(
            OLLAMA_GENERATE_URL,
            json={
                "model": modelo,
                "prompt": "",
                "keep_alive": 0,
            },
            timeout=TIMEOUT_HTTP_DESCARREGAR,
        )
        return r.status_code == 200
    except Exception:
        return False


def ollama_online():
    """O servidor do Ollama responde no endereço configurado?"""
    try:
        return _SESSION.get(f"{OLLAMA_HOST}/api/version", timeout=TIMEOUT_HTTP_CURTO).status_code == 200
    except Exception:
        return False


def avisos_de_ambiente():
    """Problemas do ambiente que o usuário precisa saber logo ao abrir,
    em vez de descobrir quando um comando falhar."""

    avisos = []

    if not ollama_online():
        avisos.append(t("aviso.ollama_offline", host=OLLAMA_HOST))

    if ERRO_PYAUTOGUI:
        motivo = t("aviso.sem_display") if SESSAO_GRAFICA == "nenhuma" else ERRO_PYAUTOGUI
        avisos.append(t("aviso.sem_mouse", motivo=motivo))
    elif SESSAO_GRAFICA == "wayland":
        avisos.append(t("aviso.wayland"))

    return avisos


def listar_modelos_rodando():
    """Consulta o Ollama (endpoint /api/ps) pelos modelos que estão de
    fato carregados na memória agora — não só o que o TARS acha que
    carregou (útil se algo foi carregado fora do script, ex: via
    Open WebUI ou `ollama run` manual).

    Retorna um set com os nomes, ou None se não foi possível checar
    (Ollama offline/inacessível) — distinguir isso de "nenhum modelo
    rodando" é importante pra não confirmar um desligamento à toa."""

    try:
        r = _SESSION.get(OLLAMA_PS_URL, timeout=TIMEOUT_HTTP_CURTO)

        if r.status_code != 200:
            return None

        data = r.json()

        return {
            item.get("name")
            for item in data.get("models", [])
            if item.get("name")
        }

    except Exception:
        return None


def encerrar_todas_ias(
    max_tentativas=TENTATIVAS_ENCERRAR_IAS,
    espera_seg=ESPERA_ENTRE_TENTATIVAS_ENCERRAR_SEG,
):
    """Descarrega da memória (VRAM/RAM) todo modelo Ollama em execução
    — não só o que o TARS tem marcado como atual — e CONFIRMA via
    /api/ps que cada um realmente saiu antes de declarar sucesso,
    tentando de novo algumas vezes se algum ainda aparecer rodando.
    Usado no comando manual 'encerrar todas llms' e sempre que o TARS
    é fechado."""

    global modelo_atual

    with lock_modelo:

        rodando = listar_modelos_rodando()

        if rodando is None:
            print(c(f"\n[openTARS] {t('msg.ollama_sem_contato')}", Cor.AMARELO))
            modelo_atual = None
            return {"sucesso": False, "encerrados": [], "falhas": []}

        alvo = set(rodando)

        if modelo_atual:
            alvo.add(modelo_atual)

        if not alvo:
            print(c(f"\n[openTARS] {t('msg.nenhuma_ia_carregada')}", Cor.CINZA))
            modelo_atual = None
            return {"sucesso": True, "encerrados": [], "falhas": []}

        print(c(f"\n[openTARS] {t('msg.encerrando_modelos', n=len(alvo), modelos=', '.join(sorted(alvo)))}", Cor.CIANO))

        pendentes = set(alvo)

        for tentativa in range(1, max_tentativas + 1):

            for modelo in list(pendentes):
                descarregar_modelo(modelo)

            time.sleep(espera_seg)

            ainda = listar_modelos_rodando()

            if ainda is None:
                print(c(f"[openTARS] {t('msg.ollama_perdido')}", Cor.AMARELO))
                break

            pendentes &= ainda

            if not pendentes:
                break

            if tentativa < max_tentativas:
                texto = t("msg.ainda_ativo", modelos=", ".join(sorted(pendentes)),
                          tentativa=tentativa, total=max_tentativas)
                print(c(f"[openTARS] {texto}", Cor.AMARELO))

        if not modelo_atual or modelo_atual not in pendentes:
            modelo_atual = None

        encerrados = alvo - pendentes

        if pendentes:
            print(c(f"[openTARS] {t('msg.encerramento_nao_confirmado', modelos=', '.join(sorted(pendentes)))}", Cor.VERMELHO))
        else:
            print(c(f"[openTARS] {t('msg.ias_descarregadas')}", Cor.VERDE))

        return {
            "sucesso": not pendentes,
            "encerrados": sorted(encerrados),
            "falhas": sorted(pendentes),
        }


def _cabem_juntos(carregados, novo):
    """Os dois modelos cabem na VRAM ao mesmo tempo? Se sim, o anterior
    fica carregado: no modo AUTO o openTARS alterna entre modelos o tempo
    todo, e descarregar à toa fazia a volta pra ele custar outro
    carregamento inteiro. Sem saber (sem GPU, sem estimativa), descarrega
    como antes, pra nunca empurrar modelo pra RAM/CPU."""

    if isinstance(carregados, str):
        carregados = [carregados]
    gpu = detectar_gpu()
    catalogo = _cache_catalogo.get("dados") or {}
    estimativas = [(catalogo.get(m) or {}).get("vram_estimado_gb") for m in list(carregados) + [novo]]
    if not gpu or None in estimativas:
        return False
    extra_contexto = len(estimativas) * contexto_ia() / 16384  # ~1GB de KV cache por 16k tokens, grosso modo
    return sum(estimativas) + extra_contexto <= gpu["vram_total_gb"] - 1.0


class OllamaOffline(Exception):
    """O Ollama não está rodando (conexão recusada)."""


def carregar_modelo(modelo, silencioso=False):
    """Carrega um modelo no Ollama (descarregando o anterior, se
    houver). Com silencioso=True, evita a caixa grande de status —
    usado no warm-up em segundo plano, pra não interromper o que o
    usuário estiver digitando com um bloco de texto grande."""

    global modelo_atual

    with lock_modelo:

        if modelo_atual == modelo:
            # já carregado — nada a fazer (bug corrigido: antes retornava
            # None aqui, o que era interpretado como falha de carregamento)
            return True

        # Olha o que está DE FATO carregado (pode ter ficado mais de um
        # quando cabiam juntos) e descarrega tudo se o novo não couber.
        rodando = (listar_modelos_rodando() or set()) | ({modelo_atual} if modelo_atual else set())
        rodando.discard(modelo)
        if rodando and not _cabem_juntos(sorted(rodando), modelo):
            # O ajudante (~400 MB) fica: descarregar ele só faria a
            # próxima classificação esperar ele carregar de novo.
            for outro in sorted(rodando - {MODELO_AJUDANTE}):
                emitir("descarregando", modelo=outro)
                descarregar_modelo(outro)
            modelo_atual = None

        emitir("carregando", modelo=modelo, segundo_plano=silencioso)

        inicio = time.time()

        try:
            # Load-only: um prompt vazio no /api/generate faz o Ollama
            # carregar o modelo na memória sem gastar tempo gerando
            # tokens de teste (bem mais rápido que uma chamada de chat
            # completa, principalmente em modelos grandes/lentos).
            resposta = _SESSION.post(
                OLLAMA_GENERATE_URL,
                json={
                    "model": modelo,
                    "prompt": "",
                    "keep_alive": KEEP_ALIVE,
                    # Mesmo num_ctx da conversa: se for diferente, o
                    # Ollama recarrega o modelo na primeira mensagem.
                    "options": {"num_ctx": contexto_ia()},
                },
                timeout=TIMEOUT_HTTP_LONGO,
            )

            if resposta.status_code != 200:
                detalhe = resposta.text.strip() or "-"
                print(c(f"[openTARS] {t('msg.erro_carregar_http', codigo=resposta.status_code, modelo=modelo, detalhe=detalhe)}", Cor.VERMELHO))
                return False

            modelo_atual = modelo
            emitir("carregado", modelo=modelo, segundos=time.time() - inicio)
            return True

        except requests.exceptions.ConnectionError:
            # Ollama desligado: não adianta tentar os outros modelos.
            raise OllamaOffline()
        except Exception as e:
            print(c(f"\n[openTARS] {t('msg.erro_carregar', modelo=modelo, erro=e)}", Cor.VERMELHO))
            return False


def _carregar_em_segundo_plano(modelo):
    """Dispara o carregamento de um modelo numa thread separada e
    devolve o controle na hora — usado ao trocar de IA manualmente
    (feedback imediato, sem travar o prompt) e no warm-up do modelo
    padrão ao iniciar o TARS. lock_modelo garante que, se o usuário já
    mandar a próxima mensagem antes do warm-up terminar, ela só espera
    o mesmo carregamento em vez de disparar um segundo em paralelo."""

    threading.Thread(
        target=carregar_modelo,
        args=(modelo,),
        kwargs={"silencioso": True},
        daemon=True,
    ).start()


# ============================================================
# HARDWARE E DISPONIBILIDADE DE MODELOS
# ============================================================

_cache_modelos_instalados = {"dados": None, "ts": 0.0}
CACHE_MODELOS_TTL = 60


CACHE_GPU_TTL = 300
_cache_gpu = {"dados": None, "ts": -CACHE_GPU_TTL}


def contexto_ia():
    """Tamanho do contexto (num_ctx) pedido ao Ollama pro modelo da
    conversa. O padrão do Ollama (4096 tokens) não cabe o prompt do
    openTARS + ferramentas (~4 mil tokens) + um print de tela: o Ollama
    cortava o começo da conversa e a IA esquecia o pedido no meio da
    tarefa. Mais contexto usa mais VRAM, então depende da GPU."""

    fixo = os.environ.get("TARS_CONTEXTO")
    if fixo and fixo.isdigit():
        return int(fixo)

    gpu = detectar_gpu()
    if gpu and gpu["vram_total_gb"] >= 16:
        return 16384
    return 8192


def detectar_gpu(forcar=False):
    """Consulta a GPU NVIDIA local via nvidia-smi. Retorna None se não
    houver nvidia-smi disponível (sem GPU dedicada ou driver ausente).
    Cacheado: essa função é consultada a cada mensagem em modo AUTO
    (pra ordenar a cadeia de fallback pelo que cabe no hardware), e
    rodar um subprocesso a cada turno seria desperdício — a VRAM total
    nunca muda, e a livre não precisa de uma leitura por mensagem pra
    essa heurística."""

    agora = time.time()

    if (
        not forcar
        and (agora - _cache_gpu["ts"]) < CACHE_GPU_TTL
    ):
        return _cache_gpu["dados"]

    resultado = _detectar_gpu_sem_cache()

    _cache_gpu["dados"] = resultado
    _cache_gpu["ts"] = agora

    return resultado


# Menos que isso é memória "emprestada" da RAM por um vídeo integrado
# (APU), não uma GPU onde o Ollama vá rodar modelo de verdade.
VRAM_MINIMA_GPU_DEDICADA_GB = 2.0

# Maior modelo (memória estimada) que ainda responde num tempo usável
# rodando só na CPU — por volta de um 8B quantizado em Q4.
LIMITE_MODELO_PRATICO_CPU_GB = 7.0


def _detectar_gpu_nvidia():
    if not shutil.which("nvidia-smi"):
        return None

    r = executar_subprocesso(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ],
        timeout=TIMEOUT_GPU,
    )

    if r["codigo"] != 0 or not r["stdout"]:
        return None

    try:
        # Várias placas: o Ollama divide o modelo entre elas, então o
        # que importa é a soma.
        total_mb = livre_mb = 0
        for linha in r["stdout"].splitlines():
            t, l = linha.split(",")
            total_mb += int(t.strip())
            livre_mb += int(l.strip())

        return {
            "vram_total_gb": round(total_mb / 1024, 1),
            "vram_livre_gb": round(livre_mb / 1024, 1),
            "fabricante": "NVIDIA",
        }

    except Exception:
        return None


def _detectar_gpu_amd(raiz_drm="/sys/class/drm"):
    """Placas AMD (driver amdgpu) informam a VRAM em arquivos do
    sistema — não precisa de ROCm nem de programa nenhum instalado."""

    total = usado = 0

    for dispositivo in sorted(Path(raiz_drm).glob("card[0-9]*/device")):
        try:
            if (dispositivo / "vendor").read_text().strip().lower() != "0x1002":
                continue
            t = int((dispositivo / "mem_info_vram_total").read_text().strip())
            u = int((dispositivo / "mem_info_vram_used").read_text().strip())
        except (OSError, ValueError):
            continue

        if t / 1024**3 < VRAM_MINIMA_GPU_DEDICADA_GB:
            continue  # vídeo integrado (ex: Ryzen 5700G)

        total += t
        usado += u

    if not total:
        return None

    return {
        "vram_total_gb": round(total / 1024**3, 1),
        "vram_livre_gb": round((total - usado) / 1024**3, 1),
        "fabricante": "AMD",
    }


def _detectar_gpu_sem_cache():
    return _detectar_gpu_nvidia() or _detectar_gpu_amd()


def ram_total_gb():
    try:
        return psutil.virtual_memory().total / 1024**3
    except Exception:
        return None  # psutil ausente/limitado: não dá pra saber


def listar_modelos_instalados(forcar=False):
    """Conjunto de modelos que o Ollama realmente tem baixados. Um
    conjunto vazio significa 'não deu pra checar' (Ollama offline) —
    nesse caso o resto do código assume modo aberto (não bloqueia a
    seleção automática por falta de informação)."""

    agora = time.time()

    if (
        not forcar
        and _cache_modelos_instalados["dados"] is not None
        and (agora - _cache_modelos_instalados["ts"]) < CACHE_MODELOS_TTL
    ):
        return _cache_modelos_instalados["dados"]

    try:
        r = _SESSION.get(OLLAMA_TAGS_URL, timeout=TIMEOUT_HTTP_CURTO)

        if r.status_code != 200:
            return _cache_modelos_instalados["dados"] or set()

        data = r.json()

        nomes = {
            item.get("name")
            for item in data.get("models", [])
            if item.get("name")
        }

        _cache_modelos_instalados["dados"] = nomes
        _cache_modelos_instalados["ts"] = agora

        return nomes

    except Exception:
        return _cache_modelos_instalados["dados"] or set()


def classificar_encaixe(vram_estimado_gb, gpu_info):
    """Classifica se um modelo (dado seu consumo estimado de VRAM) cabe
    confortavelmente na GPU, precisa de offload parcial pra CPU, ou vai
    rodar majoritariamente na CPU (bem mais lento). Só para exibição —
    quem decide o offload de fato é o próprio Ollama."""

    if vram_estimado_gb is None or not gpu_info:
        return "desconhecido"

    vram_total = gpu_info["vram_total_gb"]

    if vram_estimado_gb <= vram_total - 0.5:
        return "gpu"

    if vram_estimado_gb <= vram_total * 1.5:
        return "misto"

    return "cpu"


_EMOJI_CATEGORIA = {
    "visao": "👁",
    "codigo": "💻",
    "simples": "⚡",
    "geral": "🧠",
}

# Modelos ESPECIALISTAS em programação (qwen2.5-coder, codestral,
# deepseek-coder, codellama...). São ótimos pra escrever código e péssimos
# pra controlar o desktop: recusam abrir apps ou escrevem a chamada da
# ferramenta como texto. Por isso só atendem pedidos de código.
_RE_MODELO_CODIGO = re.compile(r"coder|codestral|starcoder|devstral|codellama|codegemma|codeqwen|(^|[^a-z])code([^a-z]|$)")


def eh_modelo_de_codigo(m):
    """O modelo do catálogo é um especialista em código?"""
    if m.get("categoria") == "codigo":
        return True
    return bool(_RE_MODELO_CODIGO.search(f"{m.get('tag', '')} {m.get('familia', '')}".lower()))

# bytes por parâmetro conforme o nível de quantização reportado pelo
# Ollama (details.quantization_level) — usado só pra estimar VRAM;
# aproximado de propósito, o Ollama é quem decide o offload de fato.
_BYTES_POR_PARAM_QUANT = {
    "Q2": 0.4, "Q3": 0.45, "Q4": 0.6, "Q5": 0.7,
    "Q6": 0.8, "Q8": 1.05, "F16": 2.1, "FP16": 2.1, "F32": 4.2,
}


def _show_modelo(tag):
    """Consulta /api/show pro modelo — parâmetros reais, quantização e
    a lista de capacidades que o próprio Ollama reporta (completion,
    tools, vision, thinking, embedding...). É isso que permite ao TARS
    se adaptar a QUALQUER modelo instalado, em vez de conhecer só uma
    lista fixa de nomes."""

    try:
        r = _SESSION.post(OLLAMA_SHOW_URL, json={"model": tag}, timeout=TIMEOUT_HTTP_MODELO_INFO)

        if r.status_code != 200:
            return None

        return r.json()

    except Exception:
        return None


def _parametros_em_bilhoes(texto):
    """Converte 'parameter_size' (ex: '8.2B', '600M') pra float em
    bilhões de parâmetros. None se não der pra entender."""

    if not texto:
        return None

    m = re.match(r"([\d.]+)\s*([BMK])", texto.strip(), re.IGNORECASE)

    if not m:
        return None

    valor = float(m.group(1))
    unidade = m.group(2).upper()

    if unidade == "B":
        return valor
    if unidade == "M":
        return valor / 1000
    if unidade == "K":
        return valor / 1_000_000

    return None


def _estimar_vram_gb(parametros_b, quantizacao):
    if parametros_b is None:
        return None

    chave = None

    if quantizacao:
        q = quantizacao.upper()
        # Usa as próprias chaves de _BYTES_POR_PARAM_QUANT como lista de
        # prefixos reconhecidos, em vez de repetir os mesmos nomes numa
        # segunda lista solta que precisaria ser mantida em sincronia.
        for prefixo in _BYTES_POR_PARAM_QUANT:
            if q.startswith(prefixo):
                chave = prefixo
                break

    bytes_por_param = _BYTES_POR_PARAM_QUANT.get(chave, 0.6)

    # +0.6GB de folga pra contexto/KV-cache, que não entra no peso
    # bruto dos parâmetros mas ocupa VRAM de verdade.
    return round(parametros_b * bytes_por_param + 0.6, 1)


def _categorizar_modelo(tag, capacidades, parametros_b, familia):
    """Decide pra que tipo de tarefa esse modelo é mais indicado, só
    com base no que o Ollama reporta sobre ele — sem precisar que
    alguém tenha cadastrado esse modelo específico antes.

    Isso é o que permite ao TARS considerar QUALQUER modelo baixado
    (não só uma lista fixa de nomes conhecidos) na hora de escolher
    automaticamente uma IA pra tarefa — qualquer coisa que o usuário
    baixar com 'ollama pull' entra no catálogo e participa da escolha
    a partir da próxima consulta ao catálogo (até CACHE_CATALOGO_TTL
    segundos depois), sem precisar mexer em nenhum código."""

    fam = (familia or "").lower()
    nome = tag.lower()

    # Modelo puramente de embedding (sem 'completion') não consegue
    # participar de uma conversa/tool-calling de jeito nenhum — incluí-
    # lo na cadeia de fallback quebraria o chat se ele fosse escolhido.
    # capacidades vazio (desconhecido) NÃO cai aqui — só quando o
    # Ollama afirma explicitamente que só sabe fazer embedding.
    if capacidades and "embedding" in capacidades and "completion" not in capacidades:
        return "embedding"
    # Ollama antigo não informa capacidades: reconhece pelo nome os modelos
    # de embeddings conhecidos (granite-embedding, nomic-embed-text...).
    if not capacidades and (
        "embed" in nome or embeddings.eh_modelo_de_embedding(tag)
    ):
        return "embedding"

    if "vision" in capacidades:
        return "visao"

    if parametros_b is not None and parametros_b <= 1.5:
        return "simples"

    if _RE_MODELO_CODIGO.search(f"{nome} {fam}"):
        return "codigo"

    return "geral"


CACHE_CATALOGO_TTL = 60
_cache_catalogo = {"dados": None, "ts": 0.0}


def catalogar_modelos(forcar=False):
    """Monta um catálogo com TODOS os modelos que o usuário realmente
    tem instalados no Ollama — consultando /api/show de cada um pra
    descobrir parâmetros, quantização e capacidades reais (tools,
    vision, thinking...), em vez de depender de uma lista fixa de
    modelos conhecidos. É isso que faz o TARS se adaptar a qualquer
    conjunto de IAs que o usuário tenha baixado.

    Cacheado (TTL) porque bate um /api/show por modelo instalado —
    rápido (é tudo local), mas não precisa refazer isso a cada
    mensagem."""

    global _cache_catalogo

    agora = time.time()

    if (
        not forcar
        and _cache_catalogo["dados"] is not None
        and (agora - _cache_catalogo["ts"]) < CACHE_CATALOGO_TTL
    ):
        return _cache_catalogo["dados"]

    nomes = listar_modelos_instalados(forcar=forcar)

    if not nomes:
        # Ollama offline ou nada instalado — mantém o catálogo anterior
        # (se houver) em vez de apagar tudo por uma falha passageira.
        return _cache_catalogo["dados"] or {}

    tags_ordenadas = sorted(nomes)

    # Um /api/show por modelo instalado — todos independentes entre si
    # (só leem dados, não mudam estado no Ollama), então rodam em
    # paralelo em vez de sequencialmente. Com poucos modelos o ganho é
    # pequeno, mas cresce direto com quantos o usuário tiver instalado,
    # e o custo de criar as threads é desprezível perto de uma chamada
    # de rede (mesmo local).
    with ThreadPoolExecutor(max_workers=min(8, len(tags_ordenadas)) or 1) as pool:
        infos_show = list(pool.map(_show_modelo, tags_ordenadas))

    catalogo = {}

    for tag, info_show in zip(tags_ordenadas, infos_show):

        info_show = info_show or {}
        detalhes = info_show.get("details", {}) or {}

        capacidades = set(info_show.get("capabilities") or [])
        parametros_b = _parametros_em_bilhoes(detalhes.get("parameter_size"))
        familia = detalhes.get("family", "") or ""
        quantizacao = detalhes.get("quantization_level", "")

        vram_estimado = _estimar_vram_gb(parametros_b, quantizacao)
        categoria = _categorizar_modelo(tag, capacidades, parametros_b, familia)

        catalogo[tag] = {
            "tag": tag,
            "familia": familia,
            "parametros_b": parametros_b,
            "quantizacao": quantizacao,
            "vram_estimado_gb": vram_estimado,
            "capacidades": capacidades,
            # Se /api/show não respondeu (Ollama antigo sem o campo
            # 'capabilities', por exemplo), não sabemos as capacidades
            # reais — nesse caso o TARS não filtra por elas (modo
            # aberto), em vez de excluir o modelo por falta de dado.
            "capacidades_conhecidas": bool(info_show),
            "categoria": categoria,
            "emoji": _EMOJI_CATEGORIA.get(categoria, "🤖"),
        }

    _cache_catalogo = {"dados": catalogo, "ts": agora}

    return catalogo


def modelos_com_capacidade(catalogo, capacidade):
    """Modelos do catálogo que suportam uma capacidade (ex: 'tools',
    'vision'), ou cuja capacidade é desconhecida (não filtra por falta
    de informação) — sempre excluindo modelos de embedding puro, que
    não conseguem participar de chat/tool-calling de jeito nenhum."""

    return [
        m for m in catalogo.values()
        if m["categoria"] != "embedding"
        and (not m["capacidades_conhecidas"] or capacidade in m["capacidades"])
    ]


def modelo_ajudante(catalogo=None):
    """O modelo mais leve disponível com suporte a 'tools' — usado como
    'ajudante' de baixo custo pra resolver nomes de app/site (ver
    perguntar_candidatos_app/perguntar_url_site), em vez de depender de
    um nome fixo que pode nem estar instalado nesta máquina."""

    catalogo = catalogo if catalogo is not None else catalogar_modelos()

    if MODELO_AJUDANTE in catalogo:
        return MODELO_AJUDANTE

    candidatos = modelos_com_capacidade(catalogo, "tools") or [
        m for m in catalogo.values() if m["categoria"] != "embedding"
    ]

    if not candidatos:
        return MODELO_PADRAO

    candidatos.sort(key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 999)

    return candidatos[0]["tag"]


_cache_cadeias = {}


def construir_cadeia_fallback(tarefa, catalogo=None):
    """Ordena os modelos instalados do mais ao menos indicado pra uma
    categoria de tarefa, pelas capacidades e pelo tamanho reais de cada um
    (não por uma lista fixa). O modo AUTO usa a ordem pra escolher e pra
    descer pro próximo se um modelo falhar.

    Regras:
    - especialista em código (qwen2.5-coder...) SÓ atende pedido de código;
      nas outras tarefas só entra se não houver mais nada instalado;
    - quem controla o PC (ação, busca, visão, técnico) precisa de 'tools';
    - modelo que roda devagar demais pra máquina (não cabe na GPU, ou
      grande demais pra CPU) vai pro fim da fila;
    - ação/busca: o menor modelo geral capaz (o suficiente pra acertar a
      ferramenta, sem gastar o mais pesado); código, técnico e geral: o
      maior prático; simples: o menor.

    Cacheado por catálogo: roda a cada mensagem."""

    usar_cache = catalogo is None
    catalogo = catalogo if catalogo is not None else catalogar_modelos()
    chave = (tarefa, _cache_catalogo.get("ts"), _cache_gpu.get("ts"), historico_modelos.versao)
    if usar_cache and chave in _cache_cadeias:
        return list(_cache_cadeias[chave])

    candidatos = [m for m in catalogo.values() if m["categoria"] != "embedding"]
    # Os ajudantes não conversam: só entram se não houver mais nada.
    candidatos = [m for m in candidatos if m["tag"] not in (MODELO_AJUDANTE, MODELO_AJUDANTE_ANTIGO)] or candidatos
    if not candidatos:
        return []

    def tools(m):
        return (not m["capacidades_conhecidas"]) or ("tools" in m["capacidades"])

    def params(m):
        return m["parametros_b"] if m["parametros_b"] is not None else 0

    gpu_info = detectar_gpu()
    ram_gb = ram_total_gb() if gpu_info is None else None

    def pratico(m):
        """Responde em segundos nesta máquina?"""
        if m.get("vram_estimado_gb") is None:
            return True
        if gpu_info is None:
            if ram_gb is None:
                return True
            return m["vram_estimado_gb"] <= min(LIMITE_MODELO_PRATICO_CPU_GB, ram_gb * 0.6)
        return classificar_encaixe(m["vram_estimado_gb"], gpu_info) in ("gpu", "misto")

    codigo = [m for m in candidatos if eh_modelo_de_codigo(m)]
    outros = [m for m in candidatos if m not in codigo]

    if tarefa == "codigo":
        # Especialista primeiro (o maior que roda bem), depois os gerais.
        ordenados = sorted(codigo, key=lambda m: (pratico(m), params(m)), reverse=True) \
            + sorted(outros, key=lambda m: (pratico(m), tools(m), params(m)), reverse=True)
    else:
        base = outros or candidatos
        if tarefa == "visao":
            # "Veja E clique": modelo que enxerga mas não chama ferramentas
            # só consegue descrever a tela.
            ordenados = sorted(base, key=lambda m: (
                "vision" in m["capacidades"], tools(m), pratico(m), params(m)), reverse=True)
        elif tarefa == "simples":
            ordenados = sorted(base, key=lambda m: (not tools(m), params(m) if m["parametros_b"] is not None else 999))
        elif tarefa in ("acao", "busca"):
            # O menorzinho ("simples") recusa, inventa ou erra a ferramenta:
            # não entra, se houver outro. Modelo com visão costuma ser maior
            # e mais lento sem precisar enxergar: depois dos gerais.
            base = [m for m in base if m["categoria"] != "simples"] or base
            ordenados = sorted(base, key=lambda m: (
                not tools(m), not pratico(m), "vision" in m["capacidades"], params(m)))
        else:  # "tecnico" (Linux, sistema) e "geral": o maior que roda bem
            ordenados = sorted(base, key=lambda m: (
                tools(m) or tarefa == "geral", pratico(m), params(m)), reverse=True)
        if outros:
            ordenados += sorted(codigo, key=params)  # só se o resto falhar ao carregar

    tags = [m["tag"] for m in ordenados]
    # Quem falhou a maioria das últimas vezes NESTA tarefa, neste PC, vai
    # pro fim da fila dela (os de programação continuam por último fora
    # de código).
    tags = historico_modelos.reordenar(
        tags, _tarefa_do_historico(tarefa),
        fixos_no_fim={m["tag"] for m in codigo} if tarefa != "codigo" and outros else (),
    )
    if usar_cache:
        if len(_cache_cadeias) > 64:
            _cache_cadeias.clear()
        _cache_cadeias[chave] = tags
    return list(tags)


def formatar_parametros(bilhoes):
    """0.75163 → '752M', 8.19 → '8.2B', 24.0 → '24B'."""
    if bilhoes < 1:
        return f"{round(bilhoes * 1000)}M"
    texto = f"{bilhoes:.1f}".rstrip("0").rstrip(".")
    return f"{texto}B"


def _imprimir_lista_modelos():
    """Fonte única usada tanto na tela inicial quanto no comando
    /modelo — lista dinamicamente o que está de fato instalado no
    Ollama, com as capacidades e estimativas de cada modelo."""

    gpu_info = detectar_gpu(forcar=True)
    catalogo = catalogar_modelos()

    if gpu_info:
        print(t(
            "lista.gpu", fabricante=gpu_info.get("fabricante") or "GPU",
            total=gpu_info["vram_total_gb"], livre=gpu_info["vram_livre_gb"],
        ))
    else:
        print(t("lista.sem_gpu"))

    print()

    if not catalogo:
        print(c(t("lista.nenhum_modelo"), Cor.AMARELO))
        print()
        return

    # Agrupa por categoria só pra exibição ficar organizada (visão,
    # técnico, simples, geral) — cada IA aparece do maior pro menor
    # dentro da própria categoria.
    ordem_categorias = ["visao", "codigo", "geral", "simples"]

    for categoria in ordem_categorias:

        modelos_cat = [
            m for m in catalogo.values()
            if m["categoria"] == categoria and m["tag"] not in (MODELO_AJUDANTE, MODELO_AJUDANTE_ANTIGO)
        ]

        if not modelos_cat:
            continue

        modelos_cat.sort(
            key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 0,
            reverse=True,
        )

        for info in modelos_cat:

            encaixe = classificar_encaixe(info["vram_estimado_gb"], gpu_info)
            marcador = t(f"encaixe.{encaixe}") if encaixe != "desconhecido" else ""

            capacidades_visiveis = sorted(
                info["capacidades"] & _CAPACIDADES_RELEVANTES
            )

            print(f"  {info['emoji']} {info['tag']}")

            detalhe_linha = t(f"categoria.{categoria}")
            if info["familia"]:
                detalhe_linha = f"{info['familia']} — {detalhe_linha}"
            print(f"     {detalhe_linha}")

            if info["parametros_b"] is not None:
                linha = f"     ~{formatar_parametros(info['parametros_b'])} params"
                if info["vram_estimado_gb"] is not None:
                    linha += f", ~{info['vram_estimado_gb']}GB VRAM"
                if marcador:
                    linha += f" — {marcador}"
                print(linha)

            if capacidades_visiveis:
                print(f"     {t('lista.capacidades')}: {', '.join(capacidades_visiveis)}")

            print()

    n_embedding = sum(1 for m in catalogo.values() if m["categoria"] == "embedding")

    if MODELO_AJUDANTE_ANTIGO in catalogo and MODELO_AJUDANTE_ANTIGO != MODELO_AJUDANTE:
        print(c(f"  ({t('lista.ajudante_antigo', modelo=MODELO_AJUDANTE_ANTIGO)})", Cor.CINZA))
        print()

    if n_embedding:
        print(c(f"  ({t('lista.embeddings', n=n_embedding)})", Cor.CINZA))
        print()


# ============================================================
# APLICAÇÕES
# ============================================================

# ------------------------------------------------------------
# Busca assistida por IA: em vez de manter uma lista fixa de aliases
# (app/site → nome real), pergunta pro modelo mais leve (rápido e já
# preparado pra respostas curtas) o que a entrada do usuário
# provavelmente significa. Isso generaliza pra qualquer app/site, não
# só os que alguém lembrou de cadastrar num dicionário.
# ------------------------------------------------------------

def _consultar_modelo_leve(prompt, formato=None):
    """Pergunta rápida e isolada ao modelo mais leve disponível (ver
    modelo_ajudante), sem mexer no estado de qual modelo está 'ativo'
    pra conversa principal — evita descarregar um modelo pesado só pra
    resolver um nome de app ou site no meio de uma tool call. Usa um
    timeout curto de propósito (TIMEOUT_HTTP_AJUDANTE): antes essa
    chamada herdava o timeout de 600s de uma conversa completa, o que
    travaria o pedido do usuário por até 10 minutos se o Ollama
    travasse no meio de uma resolução de nome de app/site."""

    return _chat_unico(
        modelo_ajudante(),
        prompt,
        temperatura=TEMPERATURA_AJUDANTE,
        timeout=TIMEOUT_HTTP_AJUDANTE,
        keep_alive=KEEP_ALIVE_AJUDANTE,
        formato=formato,
    )


def perguntar_candidatos_app(nome_pedido):
    """Em vez de um dicionário fixo de aliases, pergunta pro modelo
    leve quais nomes de executável/pacote Linux provavelmente
    correspondem ao app pedido. Generaliza pra qualquer aplicativo,
    inclusive os que nunca foram cadastrados manualmente."""

    prompt = (
        f"A Linux user asked to open an application called: '{nome_pedido}' "
        "(the name may be in any language).\n"
        "List 1 to 5 likely Linux executable or package names for it (real "
        "binary names, as installed via apt, flatpak or snap).\n"
        "Answer with the list of names."
    )

    conteudo = _consultar_modelo_leve(prompt, formato={
        "type": "object",
        "properties": {"nomes": {"type": "array", "items": {"type": "string"}, "maxItems": LIMITE_CANDIDATOS_APP}},
        "required": ["nomes"],
    })

    if not conteudo:
        return []

    dados = _json_da_resposta(conteudo)
    if isinstance(dados, dict) and isinstance(dados.get("nomes"), list):
        brutos = [str(n) for n in dados["nomes"]]
    else:  # Ollama antigo: texto separado por vírgula
        brutos = re.split(r"[,\n]", conteudo)

    candidatos = [
        parte.strip().strip(".").strip("'\"")
        for parte in brutos
        if parte.strip() and " " not in parte.strip()  # nome de programa não tem espaço
    ]

    return candidatos[:LIMITE_CANDIDATOS_APP]


def _dominio_existe(url):
    """Confere se o domínio de 'url' resolve de verdade via DNS — um
    modelo pequeno pode 'inventar' com muita confiança um domínio que
    simplesmente não existe (ex: pediram o canal de uma empresa no
    YouTube e ele respondeu algo como 'empresa.youtube.com', que nunca
    foi um formato real de URL do YouTube). Isso pega esse caso mais
    óbvio antes de abrir uma página que não existe; não garante que a
    página é a CERTA, só que o domínio é real."""

    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]

    if not host:
        return False

    # socket.setdefaulttimeout (usado antes) vale pro processo inteiro e
    # afetava as conexões com o Ollama rodando em outras threads.
    # Sem "with": ele esperaria a consulta terminar ao sair do bloco, e o
    # timeout não valeria nada com um DNS travado.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(socket.gethostbyname, host).result(timeout=TIMEOUT_DNS)
        return True
    except Exception:
        return False
    finally:
        pool.shutdown(wait=False)


def perguntar_url_site(nome_pedido):
    """Em vez de um dicionário fixo de sites conhecidos, pergunta pro
    modelo leve qual é a URL oficial do site/serviço pedido — mas um
    modelo pequeno não tem como saber de cor a URL exata de algo
    específico (o canal de uma empresa, uma página interna de um
    site), e nesses casos ele tende a inventar algo plausível em vez
    de admitir que não sabe. Por isso: o prompt pede explicitamente
    pra dizer 'desconhecido' quando não tiver certeza, e mesmo assim a
    URL que voltar é validada por DNS antes de ser usada — se o
    domínio nem existe, é claramente uma alucinação e a chamada
    devolve None (quem chamou cai pra uma busca no Google, que sempre
    acha a página certa)."""

    prompt = (
        f"What is the official URL of this website or service: '{nome_pedido}'?\n"
        "Answer ONLY with the full URL, starting with http:// or https://, "
        "with no other text.\n"
        "If you are not absolutely sure of the exact URL (for example, a "
        "specific page, channel or profile inside a bigger site), answer "
        "exactly: unknown. NEVER invent a URL just to give an answer."
    )

    conteudo = _consultar_modelo_leve(prompt, formato={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    })

    dados = _json_da_resposta(conteudo)
    if isinstance(dados, dict) and isinstance(dados.get("url"), str):
        conteudo = dados["url"]

    if not conteudo or any(p in normalizar(conteudo) for p in ("unknown", "desconhecido")):
        return None

    match = re.search(r"https?://[^\s\"'<>]+", conteudo)

    if not match:
        return None

    url = match.group(0)

    return url if _dominio_existe(url) else None


GOOGLE_SEARCH_URL = "https://www.google.com/search?q="

# Serviços onde "abrir o [X] no <serviço>" (ex: "canal da anthropic no
# youtube", "perfil da nasa no instagram") deve virar uma BUSCA dentro
# do serviço, não uma tentativa de adivinhar a URL exata do perfil/
# canal/vídeo — é justamente esse tipo de pedido que um modelo pequeno
# não tem como saber de cor, e nem uma busca genérica acerta de
# primeira sem contexto. Pesquisar dentro do próprio site é o
# comportamento que um usuário esperaria de qualquer forma (parecido
# com digitar na barra de busca do site). "google" também entra aqui
# pra "pesquisar X no google" nunca precisar da IA leve — é sempre uma
# busca direta, rápida e sem chance de alucinar uma URL.
_BUSCAS_POR_SERVICO = {
    "youtube": "https://www.youtube.com/results?search_query=",
    "google": GOOGLE_SEARCH_URL,
}

# Os verbos de busca ("pesquisar", "search", "buscar"...), os conectores
# do começo da consulta ("sobre", "about"...) e as preposições antes do
# serviço ("no youtube", "on youtube") vêm dos arquivos de idioma — ver
# _vocab().


def _resolver_busca(entrada):
    """Reconhece um pedido de busca — com um serviço citado ('...no
    youtube', '...no google') ou não (aí o padrão é Google) — e monta
    a URL de busca diretamente, sem passar pela IA leve. Isso é mais
    rápido (uma chamada a menos) E mais confiável (uma busca nunca
    'erra' feito uma URL adivinhada pode). Retorna None quando a
    entrada não parece um pedido de busca — nesse caso quem chamou
    segue pro caminho normal (domínio direto ou pergunta pra IA)."""

    entrada_n = normalizar(entrada)

    # Tokeniza só por ESPAÇO aqui (não por qualquer caractere não-
    # alfanumérico como _contem_palavra_inteira faz) — isso importa
    # porque "entrada" pode ser um nome de app/executável compacto tipo
    # "google-chrome", não só uma frase natural. Com o tokenizador
    # genérico, "google-chrome" vira dois tokens ("google" e "chrome"),
    # e "google" bateria como se fosse o serviço Google sozinho —
    # gerando uma busca sem sentido tipo "https://.../search?q=-chrome"
    # (bug real já visto em produção). Palavras separadas por espaço de
    # verdade continuam batendo normalmente.
    tokens_espaco = set(entrada_n.split())
    vocab = _vocab()

    tem_verbo_busca = bool(tokens_espaco & vocab["busca_verbos"])

    servico_citado = next(
        (s for s in _BUSCAS_POR_SERVICO if s in tokens_espaco),
        None,
    )

    if not tem_verbo_busca and not servico_citado:
        return None

    url_busca = _BUSCAS_POR_SERVICO.get(servico_citado, GOOGLE_SEARCH_URL)

    resto = entrada_n

    for verbo in vocab["busca_verbos"]:
        resto = re.sub(rf"\b{re.escape(verbo)}\b", " ", resto)

    if servico_citado:
        # Remove o nome do serviço e a preposição que normalmente vem
        # junto ("no youtube", "on youtube") — sem isso a busca ficava
        # com uma preposição solta no final ("canal da anthropic no").
        preposicoes = "|".join(re.escape(x) for x in vocab["busca_preposicoes"])
        resto = re.sub(rf"\b(?:(?:{preposicoes})\s+)?{re.escape(servico_citado)}\b", " ", resto)

    resto = re.sub(r"\s+", " ", resto).strip()

    # Conectores ("sobre", "por", "de"...) só são removidos do INÍCIO
    # da consulta (ex: "sobre buracos negros" → "buracos negros") —
    # nunca no meio ou fim, senão uma consulta legítima como "receita
    # de bolo de cenoura" perderia os "de" que fazem parte dela.
    mudou = True
    while mudou:
        mudou = False
        for conector in vocab["busca_conectores"]:
            nova = re.sub(rf"^{re.escape(conector)}\b\s*", "", resto)
            if nova != resto:
                resto = nova
                mudou = True

    if not resto:
        return None

    return url_busca + quote_plus(resto)


def _pastas_de_aplicativos():
    """Todas as pastas onde o menu do desktop procura atalhos (.desktop),
    seguindo a especificação XDG, mais as de Snap e Flatpak (no Ubuntu,
    o Firefox é Snap e o atalho dele só existe em /var/lib/snapd)."""

    dados_usuario = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    dados_sistema = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"

    bases = [dados_usuario] + [d for d in dados_sistema.split(":") if d]
    bases += [
        str(Path.home() / ".local/share/flatpak/exports/share"),
        "/var/lib/flatpak/exports/share",
        "/var/lib/snapd/desktop",
        "/usr/local/share",
        "/usr/share",
    ]

    pastas = []
    for base in bases:
        pasta = Path(base) / "applications"
        if pasta not in pastas:
            pastas.append(pasta)
    return pastas


# caminho do .desktop -> (mtime, entrada já lida). Sobrevive ao TTL do
# _cache_desktop: numa nova varredura, só arquivos alterados são relidos.
_cache_arquivos_desktop = {}


# O nome traduzido do app também vale: "abra a calculadora" precisa bater
# com Name[pt_BR]=Calculadora, já que o Name= padrão é em inglês
# (Calculator). Os idiomas considerados são os de entrada (o escolhido,
# os do sistema e o inglês) — ver _vocab()["locales"].

_PASTAS_LOCALE = ("/usr/share/locale-langpack", "/usr/share/locale")


@functools.lru_cache(maxsize=None)
def _catalogos_gettext(dominio, grupos):
    """Catálogos de tradução do app, um por grupo de locales
    (ex: ("pt_BR", "pt")). 'grupos' é uma tupla (entra na chave do cache)."""
    catalogos = []
    for grupo in grupos:
        for pasta in _PASTAS_LOCALE:
            try:
                catalogos.append(gettext.translation(dominio, pasta, languages=list(grupo)))
                break
            except (OSError, ValueError):
                continue
    return catalogos


def _traducoes_desktop(dominio, texto):
    vistos = []
    for catalogo in _catalogos_gettext(dominio, _vocab()["grupos_locale"]):
        try:
            t = catalogo.gettext(texto)
        except Exception:
            continue
        if t and t != texto and t not in vistos:
            vistos.append(t)
    return vistos


def _ler_desktop_entry(arquivo):
    """Lê só a seção [Desktop Entry] (as seções [Desktop Action ...]
    têm outros Exec=, como "nova janela anônima", que antes
    sobrescreviam o comando principal)."""

    try:
        texto = arquivo.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    campos = {}
    na_secao = False

    for linha in texto.splitlines():
        if linha.startswith("["):
            if na_secao:
                break  # acabou a [Desktop Entry]; o resto são ações
            na_secao = linha.strip() == "[Desktop Entry]"
            continue
        if not na_secao or linha.startswith("#"):
            continue
        chave, sep, valor = linha.partition("=")
        if sep:
            campos.setdefault(chave.strip(), valor.strip())

    nome = campos.get("Name")
    if not nome:
        return None
    if campos.get("Type", "Application") != "Application":
        return None
    if campos.get("Hidden", "").lower() == "true":
        return None

    outros_nomes = []
    for base in ("Name", "GenericName"):
        for sufixo in _vocab()["sufixos_desktop"]:
            valor = campos.get(base + sufixo)
            if valor and valor != nome and valor not in outros_nomes:
                outros_nomes.append(valor)

    # Ubuntu, Zorin e apps GNOME não guardam Name[pt_BR] no arquivo: a
    # tradução fica num catálogo gettext à parte (é assim que o menu
    # mostra "Calculadora"). Sem isso, "calculadora" não achava nada.
    dominio = campos.get("X-GNOME-Gettext-Domain") or campos.get("X-Ubuntu-Gettext-Domain")
    if dominio:
        for base in ("Name", "GenericName"):
            original = campos.get(base)
            if not original:
                continue
            for traducao in _traducoes_desktop(dominio, original):
                if traducao != nome and traducao not in outros_nomes:
                    outros_nomes.append(traducao)

    exec_cmd = campos.get("Exec")

    return {
        "nome": nome,
        "outros_nomes": outros_nomes,
        "arquivo": arquivo,
        "exec": exec_cmd,
        "terminal": campos.get("Terminal", "").lower() == "true",
        # Já normalizados aqui, uma vez por varredura (que fica em
        # cache), em vez de a cada busca: shlex e normalizar() eram o
        # grosso do tempo de procurar um app.
        "_nomes_n": [normalizar_nome_app(n) for n in [nome] + outros_nomes],
        "_exec_n": _exec_para_busca(exec_cmd),
    }


_RE_CODIGO_EXEC = re.compile(r"%[a-zA-Z]|[\"']")
_RE_ESPACOS = re.compile(r"\s+")


def _exec_para_busca(exec_cmd):
    """Mesmo texto que normalizar(limpar_exec(...)) produz pra comparar
    com o pedido, mas sem o shlex (que era a parte cara da varredura).
    Pra EXECUTAR o comando continua valendo limpar_exec/shlex."""
    if not exec_cmd:
        return ""
    return normalizar(_RE_ESPACOS.sub(" ", _RE_CODIGO_EXEC.sub("", exec_cmd)))


def comando_para_desktop_entry(arquivo, exec_cmd):
    """Como abrir um atalho .desktop neste desktop. gtk-launch é o ideal
    (respeita tudo que o atalho define), mas vem do GTK e pode não
    existir num KDE; então tenta o equivalente do GLib, o do KDE e,
    por fim, roda o Exec= do próprio atalho."""

    arquivo = Path(arquivo)

    # gtk-launch só acha atalhos pelo ID, nas pastas do XDG_DATA_DIRS;
    # um atalho do Snap/Flatpak fora dessa lista precisa ir pelo caminho.
    dados = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dados.append(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"))
    alcancavel_por_id = any(
        arquivo.parent == Path(d) / "applications" for d in dados if d
    )

    if shutil.which("gtk-launch") and alcancavel_por_id:
        return ["gtk-launch", arquivo.stem]
    if shutil.which("gio"):
        return ["gio", "launch", str(arquivo)]
    if shutil.which("kioclient"):
        return ["kioclient", "exec", str(arquivo)]
    if shutil.which("kioclient5"):
        return ["kioclient5", "exec", str(arquivo)]

    exec_limpo = limpar_exec(exec_cmd)
    try:
        partes = shlex.split(exec_limpo)
    except ValueError:
        partes = exec_limpo.split()
    return partes or ["xdg-open", str(arquivo)]


def desktop_entries(forcar=False):
    agora = time.time()

    if (
        not forcar
        and _cache_desktop["dados"] is not None
        and (agora - _cache_desktop["ts"]) < CACHE_APPS_TTL
    ):
        return _cache_desktop["dados"]

    encontrados = []
    ids_vistos = set()

    for pasta in _pastas_de_aplicativos():

        if not pasta.is_dir():
            continue

        try:
            for arquivo in sorted(pasta.glob("*.desktop")):

                # Mesmo ID em duas pastas: vale a primeira (a do
                # usuário sobrepõe a do sistema, como no menu).
                if arquivo.name in ids_vistos:
                    continue
                ids_vistos.add(arquivo.name)

                # Só relê o arquivo se ele mudou desde a última
                # varredura; senão, reaproveita o que já foi lido.
                try:
                    mtime = arquivo.stat().st_mtime_ns
                except OSError:
                    continue
                chave = str(arquivo)
                guardado = _cache_arquivos_desktop.get(chave)
                if guardado and guardado[0] == mtime:
                    entrada = guardado[1]
                else:
                    entrada = _ler_desktop_entry(arquivo)
                    _cache_arquivos_desktop[chave] = (mtime, entrada)

                if entrada:
                    encontrados.append(entrada)

        except Exception:
            pass

    _cache_desktop["dados"] = encontrados
    _cache_desktop["ts"] = agora

    return encontrados


def flatpak_apps(forcar=False):
    agora = time.time()

    if (
        not forcar
        and _cache_flatpak["dados"] is not None
        and (agora - _cache_flatpak["ts"]) < CACHE_APPS_TTL
    ):
        return _cache_flatpak["dados"]

    resultado = []

    if not shutil.which("flatpak"):
        _cache_flatpak["dados"] = resultado
        _cache_flatpak["ts"] = agora
        return resultado

    r = executar_subprocesso(
        ["flatpak", "list", "--app", "--columns=application,name"],
        timeout=TIMEOUT_FLATPAK_LISTAGEM,
    )

    if r["codigo"] == 0:

        for linha in r["stdout"].splitlines():

            partes = linha.split("\t")

            if len(partes) >= 2:

                app_id = partes[0].strip()
                nome = partes[1].strip()

                if app_id:
                    resultado.append({
                        "nome": nome,
                        "id": app_id,
                    })

    _cache_flatpak["dados"] = resultado
    _cache_flatpak["ts"] = agora

    return resultado


def limpar_exec(exec_cmd):
    if not exec_cmd:
        return ""

    try:
        partes = shlex.split(exec_cmd)
        partes = [p for p in partes if not p.startswith("%")]
        return " ".join(partes)

    except Exception:
        return exec_cmd


def _pontuar_normalizado(alvo_n, nome_n, exec_n):
    if alvo_n == nome_n:
        return 1000

    if alvo_n == exec_n:
        return 950

    if nome_n.startswith(alvo_n):
        return 800

    if nome_n.endswith(alvo_n):
        return 750

    if f" {alvo_n} " in f" {nome_n} ":
        return 700

    if alvo_n in nome_n:
        return 500

    if alvo_n in exec_n:
        return 400

    return 0


def pontuar_app(alvo, nome, exec_cmd=""):
    return _pontuar_normalizado(
        normalizar(alvo),
        normalizar(nome),
        normalizar(exec_cmd),
    )


def encontrar_app(alvo):
    alvo_original = alvo.strip()

    resultado = _buscar_app_no_sistema(alvo_original)

    if resultado:
        return resultado

    # Nada encontrado com o nome literal — em vez de desistir ou
    # depender de uma lista fixa de aliases, pergunta pro modelo leve
    # quais nomes de executável/pacote provavelmente correspondem ao
    # pedido, e tenta de novo a busca determinística com cada um.
    for candidato in perguntar_candidatos_app(alvo_original):

        resultado = _buscar_app_no_sistema(candidato)

        if resultado:
            resultado = dict(resultado)
            resultado["nome"] = alvo_original
            return resultado

    return None


def _buscar_app_no_sistema(alvo_original):
    """Busca determinística no sistema (sem IA), por ordem de
    confiança: executável exato no PATH, desktop entry por
    similaridade, flatpak instalado, e por fim um executável a partir
    de cada palavra do pedido."""

    alvo_original = alvo_original.strip()

    if not alvo_original:
        return None

    # 1. EXECUTÁVEL EXATO
    caminho = shutil.which(alvo_original)

    if caminho:
        return {
            "tipo": "executavel",
            "nome": alvo_original,
            "comando": [caminho],
            "origem": caminho,
        }

    # 2. DESKTOP ENTRY por similaridade
    entradas = desktop_entries()

    melhor = None
    melhor_pontuacao = 0

    alvo_n = normalizar_nome_app(alvo_original)

    for entrada in entradas:

        # Nome principal e os traduzidos (pt_BR) / genéricos contam
        # igual — "calculadora" acha o "Calculator".
        nomes_n = entrada.get("_nomes_n")
        if nomes_n is None:  # entrada montada fora de _ler_desktop_entry
            nomes_n = [normalizar_nome_app(n) for n in [entrada["nome"]] + entrada.get("outros_nomes", [])]
        exec_n = entrada.get("_exec_n")
        if exec_n is None:
            exec_n = normalizar(limpar_exec(entrada.get("exec", "")))

        pontos = max(_pontuar_normalizado(alvo_n, nome_n, exec_n) for nome_n in nomes_n)

        if pontos > melhor_pontuacao:
            melhor_pontuacao = pontos
            melhor = entrada

    if melhor and melhor_pontuacao >= 500:

        return {
            "tipo": "desktop",
            "nome": melhor["nome"],
            "arquivo": str(melhor["arquivo"]),
            "exec": melhor.get("exec"),
            "comando": comando_para_desktop_entry(melhor["arquivo"], melhor.get("exec")),
            "origem": str(melhor["arquivo"]),
        }

    # 3. FLATPAK
    alvo_n = normalizar(alvo_original)

    for app in flatpak_apps():

        app_nome_n = normalizar(app["nome"])

        if alvo_n == app_nome_n or alvo_n in app_nome_n:

            return {
                "tipo": "flatpak",
                "nome": app["nome"],
                "id": app["id"],
                "comando": ["flatpak", "run", app["id"]],
                "origem": "flatpak",
            }

    # 4. ÚLTIMA TENTATIVA: EXECUTÁVEL POR PALAVRA
    for parte in alvo_original.split():

        caminho = shutil.which(parte)

        if caminho:

            return {
                "tipo": "executavel",
                "nome": parte,
                "comando": [caminho],
                "origem": caminho,
            }

    return None


def _termos_do_app(app, encontrado):
    """Nomes pelos quais a janela/processo do app pode aparecer."""
    termos = [app, encontrado.get("nome", "")]
    partes_exec = limpar_exec(encontrado.get("exec") or "").split()
    if partes_exec and os.path.basename(partes_exec[0]) != "env":
        termos.append(os.path.basename(partes_exec[0]))
    if encontrado.get("arquivo"):
        stem = Path(encontrado["arquivo"]).stem  # org.gnome.Calculator
        termos += [stem, stem.split(".")[-1]]
    if encontrado.get("id"):
        termos.append(encontrado["id"].split(".")[-1])
    base = os.path.basename(encontrado["comando"][0])
    if base not in ("gtk-launch", "gio", "kioclient", "kioclient5", "flatpak", "xdg-open"):
        termos.append(base)
    return [t for t in dict.fromkeys(termos) if t]


def _processo_ativo(termos):
    alvos = {normalizar(t) for t in termos}
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            nome = normalizar(proc.info["name"] or "")
            cmd = normalizar(" ".join(proc.info["cmdline"] or []))
            if nome in alvos or any(a in cmd for a in alvos if len(a) > 2):
                return True
        except Exception:
            continue
    return False


def _esperar_janela_do_app(termos, ids_antes, proc=None):
    """Espera a janela do app aparecer. Devolve (janela, ja_estava_aberta).

    Se já havia uma janela do app, ela só é considerada "a" janela quando
    o app claramente reaproveitou ela (ficou em foco) ou quando nenhuma
    nova apareceu no tempo todo: um app lento (LibreOffice, Firefox frio)
    ainda vai abrir a janela nova, e o próximo clique iria pra antiga."""

    pid = getattr(proc, "pid", None)
    inicio = time.time()
    existente = None

    while time.time() - inicio < TIMEOUT_JANELA_APP_SEG:
        janelas = listar_janelas_x() or []
        novas = [j for j in janelas if j["id"] not in ids_antes]

        janela = next((j for j in novas if pid and j["pid"] == pid), None) \
            or encontrar_janela(termos, novas)
        if janela:
            return janela, False

        if existente is None:
            existente = encontrar_janela(termos, [j for j in janelas if j["id"] in ids_antes]) or False
        if existente and time.time() - inicio > 0.8 and janela_ativa_x() == existente["id"]:
            return existente, True

        time.sleep(INTERVALO_POLL_JANELA_SEG)

    return (existente or None), bool(existente)


# Arquivos que só existem ao lado de um app Electron/Chromium.
_MARCAS_ELECTRON = ("resources.pak", "chrome_100_percent.pak", "v8_context_snapshot.bin",
                    "snapshot_blob.bin", os.path.join("resources", "app.asar"))
OPCAO_ACESSIBILIDADE_CHROMIUM = "--force-renderer-accessibility"


def _eh_electron(comando):
    """O comando abre um app Electron/Chromium (Claude, Discord, VS Code,
    Slack, Chrome...)? Pelo que existe ao lado do executável, ou pelo
    script lançador que chama o electron."""
    try:
        real = os.path.realpath(shutil.which(comando[0]) or comando[0])
    except Exception:
        return False
    pasta = os.path.dirname(real)
    if any(os.path.exists(os.path.join(pasta, marca)) for marca in _MARCAS_ELECTRON):
        return True
    try:
        if os.path.getsize(real) < 20000:
            with open(real, "rb") as arquivo:
                inicio = arquivo.read(20000)
            return inicio.startswith(b"#!") and re.search(rb"electron|app\.asar|chromium|google-chrome", inicio,
                                                          re.I) is not None
    except Exception:
        pass
    return False


def abrir_aplicativo(app):
    app = app.strip()

    encontrado = encontrar_app(app)

    if not encontrado:

        return {
            "sucesso": False,
            "acao": "open_application",
            "aplicativo": app,
            "mensagem": f"APP NOT FOUND: '{app}'. No installed application matches this name.",
        }

    comando = list(encontrado["comando"])
    # Electron/Chromium só mostra os botões pra acessibilidade se pedirem:
    # com isso, o clique pelo nome funciona no Claude, Discord, VS Code...
    if _eh_electron(comando) and OPCAO_ACESSIBILIDADE_CHROMIUM not in comando:
        comando.append(OPCAO_ACESSIBILIDADE_CHROMIUM)
    termos = _termos_do_app(app, encontrado)
    origem = encontrado["nome"] if encontrado["tipo"] == "desktop" else encontrado.get("origem", "")
    base_resultado = {"acao": "open_application", "aplicativo": app, "encontrado_como": origem}

    janelas_x = listar_janelas_x()
    ids_antes = {j["id"] for j in janelas_x or []}

    try:
        proc = subprocess.Popen(
            comando,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            # Chromium/Electron (Claude, Discord, VS Code, Chrome) só
            # expõem os botões pra acessibilidade com isso: aí o clique
            # pelo nome funciona neles também. Os outros apps ignoram.
            env={**os.environ, "ACCESSIBILITY_ENABLED": "1"},
        )
    except Exception as e:
        return {**base_resultado, "sucesso": False, "mensagem": f"ERROR opening '{app}': {e}"}

    # Com X: espera a JANELA (não só o processo) e traz ela pra frente —
    # assim o próximo passo (print, clique, digitar) já acontece nela.
    # No Wayland o X só enxerga janelas XWayland: esperar a janela de um
    # app nativo seria esperar à toa o tempo todo.
    if janelas_x is not None and SESSAO_GRAFICA == "x11":
        janela, ja_aberta = _esperar_janela_do_app(termos, ids_antes, proc)
        if janela:
            try:
                if janela["id"] != janela_ativa_x():
                    focar_janela_x(janela["id"])
            except Exception:
                pass
            _usar_quadro_tela_inteira()
            return {
                **base_resultado,
                "sucesso": True,
                "janela": _janela_para_ia(janela),
                "mensagem": (
                    f"SUCCESS: '{app}' "
                    + ("was already open; its window was brought to the front" if ja_aberta else "opened")
                    + f" (window '{janela['titulo']}', in the foreground)."
                    + _dica_clique_por_nome()
                ),
            }

        if proc is not None and proc.poll() not in (None, 0):
            return {
                **base_resultado,
                "sucesso": False,
                "mensagem": f"ERROR: the command to open '{app}' failed (exit code {proc.returncode}).",
            }

        return {
            **base_resultado,
            "sucesso": True,
            "mensagem": (
                f"The command to open '{app}' ran, but no window appeared within "
                f"{TIMEOUT_JANELA_APP_SEG:.0f}s. It may be an app without a window or "
                "one that is still loading: check with take_screenshot."
            ),
        }

    # Sem acesso às janelas: confirma pelo processo.
    inicio_poll = time.time()
    ativo = False
    while time.time() - inicio_poll < TIMEOUT_POLL_PROCESSO_SEG:
        if _processo_ativo(termos):
            ativo = True
            break
        time.sleep(INTERVALO_POLL_PROCESSO_SEG)

    return {
        **base_resultado,
        "sucesso": True,
        "mensagem": (
            f"SUCCESS: the application '{app}' is running."
            if ativo
            else f"SUCCESS: the command to open '{app}' was executed."
        ),
    }


def _pids_protegidos():
    """O próprio openTARS e o terminal/sessão que o abriu: "feche o
    python" ou "feche o terminal" não podem derrubar o openTARS."""
    protegidos = {os.getpid()}
    try:
        protegidos |= {p.pid for p in psutil.Process().parents()}
    except Exception:
        pass
    return protegidos


def _terminar_processos(alvo_n):
    """Termina todo processo cujo nome bata exatamente ou cuja linha de
    comando contenha alvo_n como um argumento/palavra inteira (já
    normalizado). Retorna a lista de pids encerrados.

    Antes isso usava `alvo_n in cmdline` (substring solta) — "code"
    batia em QUALQUER processo com "code" em qualquer parte da linha
    de comando, inclusive coisas sem relação nenhuma com o app pedido.
    Comparar por token evita matar um processo errado por causa de uma
    coincidência de texto."""

    encerrados = []

    protegidos = _pids_protegidos()

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):

        try:

            pid = proc.info["pid"]
            if pid in protegidos:
                continue
            nome = normalizar(proc.info["name"] or "")
            cmdline = normalizar(" ".join(proc.info["cmdline"] or []))

            if alvo_n == nome:
                bateu = True
            elif " " in alvo_n:
                # alvo com espaço (ex: "gerenciador de arquivos") não é
                # um token só — comparar por substring direto continua
                # sendo a forma correta aqui, e o risco de bater à toa
                # é bem menor com uma frase inteira do que com uma
                # palavra solta.
                bateu = alvo_n in cmdline
            else:
                cmdline_tokens = set(re.split(r"[^a-z0-9]+", cmdline))
                bateu = alvo_n in cmdline_tokens

            if bateu:
                proc.terminate()
                encerrados.append(pid)

        except Exception:
            continue

    return encerrados


def _fechar_pelas_janelas(app):
    """Fecha as janelas cuja classe (o app dono) bate com o pedido.
    Título sozinho não conta: uma aba "Calculadora online" no navegador
    não é a calculadora."""

    protegidos = _pids_protegidos()
    janelas = [j for j in listar_janelas_x() or [] if j["pid"] not in protegidos]
    alvo = [j for j in janelas if _pontuar_janela(j, [app]) >= 2]
    if not alvo:
        encontrado = _buscar_app_no_sistema(app)
        if encontrado:
            termos = _termos_do_app(app, encontrado)
            alvo = [j for j in janelas if _pontuar_janela(j, termos) >= 2]
    if not alvo:
        return None

    for janela in alvo:
        try:
            fechar_janela_x(janela["id"])
        except Exception:
            pass

    ids = {j["id"] for j in alvo}
    limite = time.time() + 3
    while time.time() < limite:
        time.sleep(0.25)
        if not ids & {j["id"] for j in listar_janelas_x() or []}:
            break
    restantes = ids & {j["id"] for j in listar_janelas_x() or []}
    fechadas = [j["titulo"] for j in alvo if j["id"] not in restantes]
    abertas = [j["titulo"] for j in alvo if j["id"] in restantes]
    return fechadas, abertas


def fechar_aplicativo(app):
    alvo_n = normalizar(app)

    if normalizar_nome_app(app) in ("opentars", "tars", "open tars"):
        return {
            "sucesso": False,
            "acao": "close_application",
            "aplicativo": app,
            "mensagem": (
                "openTARS does not close itself in the middle of a request. Tell the "
                f"user that to quit they can type '{t('cmd.sair_principal')}' or close the window."
            ),
        }

    pelas_janelas = _fechar_pelas_janelas(app)
    if pelas_janelas is not None:
        fechadas, abertas = pelas_janelas
        if not abertas:
            return {
                "sucesso": True,
                "acao": "close_application",
                "aplicativo": app,
                "janelas_fechadas": fechadas,
                "mensagem": f"SUCCESS: closed {len(fechadas)} window(s) of '{app}'.",
            }
        # Janela que não fechou quase sempre está perguntando "salvar
        # alterações?". Matar o processo aqui faria o usuário perder o
        # que não salvou, então para e avisa.
        return {
            "sucesso": False,
            "acao": "close_application",
            "aplicativo": app,
            "janelas_abertas": abertas,
            "mensagem": (
                f"Asked '{app}' to close, but {len(abertas)} window(s) are still open "
                "(probably asking whether to save). Closing was not forced so nothing "
                "is lost: tell the user."
            ),
        }

    encerrados = _terminar_processos(alvo_n)

    if not encerrados:

        # Nenhum processo bateu com o nome literal — em vez de uma
        # lista fixa de aliases, pergunta pro modelo leve nomes
        # prováveis de executável e tenta de novo.
        for candidato in perguntar_candidatos_app(app):

            encerrados = _terminar_processos(normalizar(candidato))

            if encerrados:
                break

    if encerrados:

        return {
            "sucesso": True,
            "acao": "close_application",
            "aplicativo": app,
            "mensagem": f"SUCCESS: application '{app}' was closed.",
            "pids": encerrados,
        }

    return {
        "sucesso": False,
        "acao": "close_application",
        "aplicativo": app,
        "mensagem": f"No running process matches '{app}'.",
    }


# ============================================================
# SITES
# ============================================================

# Nomes que quase sempre significam "abra o NAVEGADOR", não "visite um
# site chamado assim" — um modelo (principalmente os menores) às vezes
# chama open_website pra isso, quando o certo seria open_application.
# Em vez de depender só do prompt pra evitar essa confusão, a própria
# ferramenta redireciona pra abrir o aplicativo de verdade.
# Marcas (iguais em qualquer idioma). A palavra "navegador" em cada
# idioma vem dos arquivos de idioma (palavras.navegador).
_MARCAS_NAVEGADORES = {
    "chrome", "google chrome", "chromium", "firefox", "brave", "edge",
    "microsoft edge", "opera", "vivaldi", "safari", "browser",
}


def abrir_site(site):
    entrada = site.strip()

    # Hífen vira espaço só pra essa checagem ("google-chrome" ==
    # "google chrome") — não afeta o resto da função.
    entrada_normalizada = re.sub(r"[\s-]+", " ", normalizar(entrada)).strip()

    if entrada_normalizada in _vocab()["navegadores"]:
        resultado = abrir_aplicativo(entrada)
        resultado["mensagem"] = (
            f"('{entrada}' was requested as a website, but it is a browser — "
            "opened it as an application instead) "
            + resultado.get("mensagem", "")
        )
        return resultado

    url = None

    if entrada.startswith(("http://", "https://")):
        url = entrada

    elif "." in entrada and " " not in entrada:
        # parece já ser um domínio (ex: "github.com")
        url = "https://" + entrada

    else:
        # Não é uma URL nem parece um domínio. Primeiro checa se é um
        # pedido de busca DENTRO de um serviço conhecido (ex: "canal
        # da anthropic no youtube") — pedir pra uma IA pequena adivinhar
        # a URL exata de algo assim é o tipo de pedido que ela não tem
        # como saber de cor, e ela tende a inventar um domínio que não
        # existe. Só depois disso tenta a IA leve pra qualquer outro
        # site desconhecido.
        url = _resolver_busca(entrada) or perguntar_url_site(entrada)

    if url is None:
        url = "https://www.google.com/search?q=" + quote_plus(entrada)

    try:

        webbrowser.open(url)

        return {
            "sucesso": True,
            "acao": "open_website",
            "entrada": entrada,
            "url": url,
            "mensagem": f"SUCCESS: opened '{entrada}' at {url}.",
        }

    except Exception as e:

        return {
            "sucesso": False,
            "acao": "open_website",
            "entrada": entrada,
            "mensagem": f"ERROR opening the website: {e}",
        }


def pesquisar_web(query, service=None):
    """Handler da ferramenta search_web — pensada pra ser a forma
    ÓBVIA e sem ambiguidade de fazer uma busca: a própria IA decide
    separar 'o que pesquisar' (query) de 'onde pesquisar' (service),
    em vez de o código ter que adivinhar isso tentando reconhecer
    verbos/serviços dentro de uma frase livre (o problema real de
    antes não era a IA ser burra, era a ferramenta open_website não
    ter um jeito claro de dizer 'isso aqui é uma busca, e isso aqui é
    só o termo' — ela só tinha um campo 'site' genérico, então um
    modelo pequeno podia repassar a frase cortada pela metade).

    Uma query vazia é um erro claro (a IA esqueceu de preencher o
    campo), não motivo pra adivinhar nada."""

    query = (query or "").strip()

    if not query:
        return {
            "sucesso": False,
            "acao": "search_web",
            "mensagem": "ERROR: no search terms given (empty 'query').",
        }

    servico_normalizado = normalizar(service or "").strip()
    url_base = _BUSCAS_POR_SERVICO.get(servico_normalizado, GOOGLE_SEARCH_URL)
    url = url_base + quote_plus(query)

    try:

        webbrowser.open(url)

        return {
            "sucesso": True,
            "acao": "search_web",
            "query": query,
            "servico": servico_normalizado or "google",
            "url": url,
            "mensagem": f"SUCCESS: searched for '{query}' and opened the results at {url}.",
        }

    except Exception as e:

        return {
            "sucesso": False,
            "acao": "search_web",
            "mensagem": f"ERROR while searching: {e}",
        }


# ============================================================
# OUTRAS FERRAMENTAS
# ============================================================

def diretorio_atual():
    return {
        "sucesso": True,
        "diretorio": os.getcwd(),
        "mensagem": f"Current directory: {os.getcwd()}",
    }


def listar_arquivos(caminho="."):
    try:

        pasta = Path(caminho).expanduser()

        if not pasta.exists():
            return {
                "sucesso": False,
                "mensagem": f"The path '{caminho}' does not exist.",
            }

        arquivos = [
            f"[{'DIR' if item.is_dir() else 'FILE'}] {item.name}"
            for item in sorted(pasta.iterdir(), key=lambda x: x.name.lower())
        ]

        return {
            "sucesso": True,
            "caminho": str(pasta.resolve()),
            "arquivos": arquivos[:LIMITE_ARQUIVOS_LISTADOS],
            "mensagem": f"{len(arquivos)} items found.",
        }

    except Exception as e:

        return {
            "sucesso": False,
            "mensagem": str(e),
        }


# Padrões de comando destrutivo o suficiente pra merecer uma
# confirmação antes de rodar — a IA (inclusive um modelo pequeno, que
# erra mais) manda qualquer comando pro terminal sem nenhuma trava
# hoje, e um comando mal interpretado nessa lista pode apagar dados ou
# derrubar o sistema sem chance de desfazer. Não é uma lista exaustiva
# de tudo que pode dar errado — é uma rede de segurança pros casos mais
# óbvios e mais caros.
_PADROES_COMANDO_PERIGOSO = [
    r"\brm\b.*\s-[a-zA-Z]*[rR]",  # rm -r/-rf/-fr/-R: apaga pastas inteiras
    r"\brm\b.*--recursive",
    r"\bfind\b.*\s-delete\b",
    r"\bshred\b",
    r"\bgit\b.*\b(reset\s+--hard|clean\s+-[a-z]*f)",
    r"\bdd\b.*\bif=",
    r"\bmkfs\b",
    r"\bwipefs\b",
    r"\bfdisk\b",
    r"\bparted\b",
    r">\s*/dev/(sd|nvme|hd)",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
    r"\bchmod\b.*-R.*\s/(\s|$)",
    r"\bchown\b.*-R.*\s/(\s|$)",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&?\s*\}\s*;\s*:",  # fork bomb
    r"\bmv\b.*\s/(\s|$).*>\s*/dev/null",
]

_CONFIRMAR_COMANDOS_PERIGOSOS = (
    os.environ.get("TARS_SEM_CONFIRMACAO_PERIGOSOS", "0") != "1"
)

# Função (comando) -> bool que a interface gráfica registra pra
# perguntar numa janela. None = pergunta no terminal com input().
confirmar_comando_hook = None


def _comando_e_perigoso(comando):
    return any(
        re.search(padrao, comando, re.IGNORECASE)
        for padrao in _PADROES_COMANDO_PERIGOSO
    )


def _confirmar_comando_perigoso(comando):
    """Pede confirmação explícita no terminal antes de rodar um
    comando que bateu com um padrão destrutivo conhecido. Pode ser
    desligado (por conta e risco do usuário) com a variável de
    ambiente TARS_SEM_CONFIRMACAO_PERIGOSOS=1, útil pra rodar o TARS
    de forma não-interativa."""

    if not _CONFIRMAR_COMANDOS_PERIGOSOS:
        return True

    imprimir_caixa(
        "⚠ " + t("perigo.titulo"),
        [comando[:200], t("perigo.explicacao")],
        cor_titulo=Cor.VERMELHO,
    )

    if confirmar_comando_hook is not None:
        # Interface gráfica: não há terminal pra digitar "s", então ela
        # registra aqui uma função que pergunta numa caixa de diálogo.
        try:
            confirmado = bool(confirmar_comando_hook(comando))
        except Exception:
            confirmado = False
    else:
        try:
            resposta = input(c(t("perigo.pergunta") + " ", Cor.AMARELO)).strip()
        except (EOFError, OSError):
            resposta = ""
        confirmado = normalizar(resposta) in _vocab()["respostas_sim"]

    LOG.info(
        "comando_perigoso confirmado=%s comando=%s",
        confirmado,
        _resumir_para_log(comando),
    )

    return confirmado


def executar_terminal(comando):

    if _comando_e_perigoso(comando) and not _confirmar_comando_perigoso(comando):
        return {
            "sucesso": False,
            "mensagem": (
                "Command cancelled: it matched a potentially destructive pattern "
                "and the user did not confirm it."
            ),
        }

    # "firefox &" ou "nohup x &": o programa fica rodando e segurava a
    # saída aberta, e o openTARS esperava o tempo limite inteiro à toa.
    if re.search(r"&\s*$", comando.strip()) and not comando.strip().endswith("&&"):
        try:
            subprocess.Popen(
                comando, shell=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return {"sucesso": True, "mensagem": "Command started in the background."}
        except Exception as e:
            return {"sucesso": False, "mensagem": str(e)}

    try:

        processo = subprocess.Popen(
            comando,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Sem terminal pra digitar: sudo/apt que pedem senha ou "S/n"
            # falham na hora em vez de travar até o tempo limite.
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        limite = time.time() + TIMEOUT_TERMINAL_COMANDO
        while True:
            try:
                saida, erro_saida = processo.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if _cancelar.is_set() or time.time() > limite:
                    try:
                        os.killpg(processo.pid, 15)
                    except Exception:
                        processo.kill()
                    processo.communicate()
                    if _cancelar.is_set():
                        return {"sucesso": False, "mensagem": "Command interrupted by the user."}
                    raise subprocess.TimeoutExpired(comando, TIMEOUT_TERMINAL_COMANDO)

        resultado = subprocess.CompletedProcess(comando, processo.returncode, saida, erro_saida)

        return {
            "sucesso": resultado.returncode == 0,
            "codigo": resultado.returncode,
            "stdout": resultado.stdout[-LIMITE_STDOUT_CHARS:],
            "stderr": resultado.stderr[-LIMITE_STDERR_CHARS:],
            "mensagem": (
                "Command finished."
                if resultado.returncode == 0
                else "Command finished with an error."
            ),
        }

    except subprocess.TimeoutExpired:

        return {
            "sucesso": False,
            "mensagem": (
                f"The command ran for more than {TIMEOUT_TERMINAL_COMANDO}s and was stopped. "
                "If it needs to run for a long time, suggest that the user runs it in a terminal."
            ),
        }

    except Exception as e:

        return {
            "sucesso": False,
            "mensagem": str(e),
        }


def informacoes_pc():
    memoria = psutil.virtual_memory()
    disco = psutil.disk_usage("/")

    return {
        "sucesso": True,
        "cpu": psutil.cpu_percent(interval=INTERVALO_CPU_PERCENT_SEG),
        "ram_percentual": memoria.percent,
        "ram_total_gb": round(memoria.total / 1024**3, 2),
        "ram_disponivel_gb": round(memoria.available / 1024**3, 2),
        "disco_percentual": disco.percent,
        "disco_livre_gb": round(disco.free / 1024**3, 2),
        "mensagem": "Computer information collected.",
    }


# O print enviado à IA é reduzido (ex: 2560x1440 -> 1280x720) e ela
# responde com coordenadas DESSA imagem. Sem converter de volta, o
# clique cairia no lugar errado (metade do caminho, numa tela 2x maior).
# Atualizado a cada take_screenshot; 1.0 enquanto não houver print.
# Com print de uma janela só, ox/oy guardam onde ela começa na tela.
_escala_print = {"x": 1.0, "y": 1.0, "ox": 0, "oy": 0, "largura": None, "altura": None, "definido": False}


def _escala_tela_inteira():
    """Quanto o print da tela inteira é reduzido (ex: 2.0 numa tela de
    2560px com prints de 1280px). Não depende de já ter tirado print."""
    try:
        largura = pyautogui.size()[0]
    except Exception:
        return 1.0
    return largura / min(largura, LARGURA_MAXIMA_SCREENSHOT) if largura else 1.0


def _usar_quadro_tela_inteira():
    """Volta as coordenadas de clique pro sistema do print de tela
    inteira. Chamado sempre que o openTARS informa posições nesse
    sistema (janelas abertas, tamanho da tela) ou quando a tela muda
    (abrir/focar janela): um recorte de janela antigo deixaria o clique
    com deslocamento errado."""
    escala = _escala_tela_inteira()
    _escala_print.update(x=escala, y=escala, ox=0, oy=0, largura=None, altura=None, definido=True)


def _para_tela(x, y):
    """Coordenadas do último print (as que a IA vê) -> pixels reais.
    Sem print nenhum ainda, vale o sistema do print de tela inteira (o
    tamanho de tela que o prompt informa à IA)."""
    if not _escala_print["definido"]:
        _usar_quadro_tela_inteira()
    return (
        int(round(_escala_print["ox"] + float(x) * _escala_print["x"])),
        int(round(_escala_print["oy"] + float(y) * _escala_print["y"])),
    )


def _para_print(x, y):
    """Pixels reais da tela -> coordenadas do último print."""
    if not _escala_print["definido"]:
        _usar_quadro_tela_inteira()
    return (
        int(round((x - _escala_print["ox"]) / _escala_print["x"])),
        int(round((y - _escala_print["oy"]) / _escala_print["y"])),
    )


def mover_mouse(x, y):
    try:
        pyautogui.moveTo(*_para_tela(x, y), duration=DURACAO_MOVER_MOUSE_SEG)
        return {"sucesso": True, "mensagem": f"Mouse moved to ({x}, {y})."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def posicao_mouse():
    try:
        x, y = _para_print(*pyautogui.position())
        return {
            "sucesso": True,
            "x": x,
            "y": y,
            "mensagem": f"The mouse is at ({x}, {y}).",
        }
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def tamanho_tela():
    try:
        # Nas coordenadas do print de tela inteira (o que a IA usa).
        _usar_quadro_tela_inteira()
        real_l, real_a = pyautogui.size()
        escala = _escala_tela_inteira()
        largura, altura = round(real_l / escala), round(real_a / escala)
        return {
            "sucesso": True,
            "largura": largura,
            "altura": altura,
            "mensagem": f"The screen is {largura}x{altura} pixels.",
        }
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def clicar_mouse(botao="left", x=None, y=None):
    try:
        # Clicar direto num ponto (x, y) evita precisar de duas
        # chamadas (move_mouse + click_mouse) pra cada clique.
        if x is not None and y is not None:
            tx, ty = _para_tela(x, y)
            pyautogui.click(x=tx, y=ty, button=botao)
            local = f" at ({x}, {y})"
        else:
            pyautogui.click(button=botao)
            local = ""

        return {"sucesso": True, "mensagem": f"Clicked the {botao} mouse button{local}."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def duplo_clique(x=None, y=None):
    try:
        if x is not None and y is not None:
            tx, ty = _para_tela(x, y)
            pyautogui.doubleClick(x=tx, y=ty)
        else:
            pyautogui.doubleClick()

        return {"sucesso": True, "mensagem": "Double click done."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def scroll_mouse(valor, x=None, y=None):
    try:
        if x is not None and y is not None:
            pyautogui.moveTo(*_para_tela(x, y), duration=DURACAO_MOVER_PARA_SCROLL_SEG)

        pyautogui.scroll(int(valor))
        return {"sucesso": True, "mensagem": f"Scrolled {valor}."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def _definir_clipboard(texto):
    """Coloca texto na área de transferência via xclip/xsel (o que
    estiver disponível). Usado pra digitar texto multilíngue — o
    pyautogui simula teclas físicas e não digita de forma confiável
    acentos, cedilha ou alfabetos fora do layout US (japonês, árabe,
    cirílico etc.); colar via clipboard funciona pra qualquer idioma
    independente do layout de teclado."""

    comandos = [
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]
    if SESSAO_GRAFICA == "wayland":
        # wl-copy fala direto com o compositor Wayland; xclip/xsel só
        # alcançam a área de transferência do XWayland.
        comandos.insert(0, ["wl-copy"])

    for comando in comandos:
        if shutil.which(comando[0]):
            try:
                subprocess.run(
                    comando,
                    input=texto.encode("utf-8"),
                    timeout=TIMEOUT_CLIPBOARD,
                    check=True,
                )
                return True
            except Exception:
                continue

    return False


def digitar_texto(texto):
    try:
        if texto.isascii():
            # Caminho rápido de sempre — nenhuma mudança de
            # comportamento ou desempenho pro caso comum (ASCII).
            pyautogui.write(texto, interval=INTERVALO_DIGITACAO_SEG)
            return {"sucesso": True, "mensagem": "Text typed."}

        # Texto com acentos/outros idiomas: cola via clipboard em vez
        # de tecla-a-tecla, o que também é mais rápido pra textos
        # longos além de mais confiável.
        if _definir_clipboard(texto):
            pyautogui.hotkey("ctrl", "v")
            return {"sucesso": True, "mensagem": "Text typed (pasted through the clipboard)."}

        # Sem xclip/xsel disponível: melhor esforço mesmo assim.
        pyautogui.write(texto, interval=INTERVALO_DIGITACAO_SEG)
        return {
            "sucesso": True,
            "mensagem": (
                "Text typed, but xclip/xsel is not installed: special characters "
                "may not have come out right."
            ),
        }

    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def pressionar_tecla(tecla):
    try:
        pyautogui.press(tecla)
        return {"sucesso": True, "mensagem": f"Pressed '{tecla}'."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def atalho_teclado(teclas):
    try:

        if isinstance(teclas, str):
            teclas = [x.strip() for x in teclas.split("+")]

        pyautogui.hotkey(*teclas)

        return {
            "sucesso": True,
            "mensagem": f"Pressed {'+'.join(teclas)}.",
        }

    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def esperar(segundos):
    """Pausa por um tempo curto — pra quando a própria IA decide que
    precisa dar um tempo entre dois passos de uma tarefa composta (ex:
    abrir um navegador e só então digitar na barra de endereço, já que
    abrir uma janela pesada pode levar mais que o instante que
    open_application já espera sozinho). Limitado a LIMITE_ESPERA_SEG
    pra IA não travar o TARS 'esperando' por muito tempo."""

    try:
        segundos = max(0.0, min(float(segundos), LIMITE_ESPERA_SEG))
    except (TypeError, ValueError):
        segundos = 1.0

    time.sleep(segundos)

    return {
        "sucesso": True,
        "mensagem": f"SUCCESS: waited {segundos:.1f}s.",
    }


# ============================================================
# JANELAS (X11 / XWayland)
# ============================================================
#
# Antes a IA só "via" a tela pelo print: não sabia se a calculadora
# tinha aberto, onde estava, nem se estava atrás de outra janela. Agora
# o openTARS lê a lista de janelas do gerenciador de janelas (padrão
# EWMH, o mesmo que a barra de tarefas usa), traz a janela certa pra
# frente e informa posição e tamanho. Usa o python-xlib que já vem
# junto com o pyautogui; sem X (Wayland puro, sem display), devolve
# lista vazia e as ferramentas explicam o motivo.

TIMEOUT_JANELA_APP_SEG = 10.0     # espera máxima pela janela de um app recém-aberto
INTERVALO_POLL_JANELA_SEG = 0.2


def _abrir_display_x():
    from Xlib import display as _xdisplay
    return _xdisplay.Display()


def _propriedade_x(d, janela, nome, tipo=None):
    from Xlib import X
    try:
        atom = d.intern_atom(nome)
        prop = janela.get_full_property(atom, tipo if tipo is not None else X.AnyPropertyType)
        return prop.value if prop else None
    except Exception:
        return None


def _texto_x(valor):
    if valor is None:
        return ""
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return str(valor)


def _info_janela_x(d, raiz, janela):
    """Título, classe, pid e retângulo (pixels reais da tela) de uma
    janela de nível superior."""

    titulo = _texto_x(_propriedade_x(d, janela, "_NET_WM_NAME", d.intern_atom("UTF8_STRING")))
    if not titulo:
        try:
            titulo = _texto_x(janela.get_wm_name())
        except Exception:
            titulo = ""

    try:
        classe = " ".join(janela.get_wm_class() or ())
    except Exception:
        classe = ""

    pid = _propriedade_x(d, janela, "_NET_WM_PID")
    pid = int(pid[0]) if pid is not None and len(pid) else None

    estado = _propriedade_x(d, janela, "_NET_WM_STATE")
    minimizada = bool(estado is not None and d.intern_atom("_NET_WM_STATE_HIDDEN") in list(estado))

    geo = janela.get_geometry()
    origem = janela.translate_coords(raiz, 0, 0)

    return {
        "id": janela.id,
        "titulo": titulo,
        "classe": classe,
        "pid": pid,
        "x": -origem.x,
        "y": -origem.y,
        "largura": geo.width,
        "altura": geo.height,
        "minimizada": minimizada,
    }


def listar_janelas_x():
    """Janelas de aplicativo abertas, da mais antiga pra mais nova.
    None se não houver como ler as janelas (sem X); [] se não há nenhuma."""

    if SESSAO_GRAFICA == "nenhuma" or ERRO_PYAUTOGUI:
        return None

    try:
        d = _abrir_display_x()
    except Exception:
        return None

    try:
        raiz = d.screen().root
        ids = _propriedade_x(d, raiz, "_NET_CLIENT_LIST")

        if ids is not None:
            janelas = [d.create_resource_object("window", int(i)) for i in ids]
            tipo_normal = d.intern_atom("_NET_WM_WINDOW_TYPE_NORMAL")
            tipo_dialogo = d.intern_atom("_NET_WM_WINDOW_TYPE_DIALOG")
        else:
            # Sem gerenciador de janelas compatível com EWMH: janelas
            # mapeadas direto na raiz.
            from Xlib import X
            janelas = [
                j for j in raiz.query_tree().children
                if j.get_attributes().map_state == X.IsViewable
            ]
            tipo_normal = tipo_dialogo = None

        resultado = []
        for janela in janelas:
            try:
                if tipo_normal is not None:
                    tipos = _propriedade_x(d, janela, "_NET_WM_WINDOW_TYPE")
                    # Barra de tarefas, papel de parede, menus: não são apps.
                    if tipos is not None and len(tipos) and not ({tipo_normal, tipo_dialogo} & set(tipos)):
                        continue
                info = _info_janela_x(d, raiz, janela)
                if info["titulo"] or info["classe"]:
                    resultado.append(info)
            except Exception:
                continue
        return resultado

    except Exception:
        return None

    finally:
        try:
            d.close()
        except Exception:
            pass


def janela_ativa_x():
    """id da janela em foco (ou None)."""
    if SESSAO_GRAFICA == "nenhuma" or ERRO_PYAUTOGUI:
        return None
    try:
        d = _abrir_display_x()
    except Exception:
        return None
    try:
        valor = _propriedade_x(d, d.screen().root, "_NET_ACTIVE_WINDOW")
        return int(valor[0]) if valor is not None and len(valor) and valor[0] else None
    finally:
        d.close()


def focar_janela_x(id_janela):
    """Traz a janela pra frente (e restaura se estava minimizada), do
    mesmo jeito que clicar nela na barra de tarefas."""

    from Xlib import X, protocol

    d = _abrir_display_x()
    try:
        raiz = d.screen().root
        janela = d.create_resource_object("window", int(id_janela))
        atom_ativa = d.intern_atom("_NET_ACTIVE_WINDOW")

        if _propriedade_x(d, raiz, "_NET_SUPPORTED") is not None:
            evento = protocol.event.ClientMessage(
                window=janela,
                client_type=atom_ativa,
                data=(32, [2, X.CurrentTime, 0, 0, 0]),  # 2 = pedido de um "pager"
            )
            raiz.send_event(
                evento,
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
            )
        else:
            janela.map()
            janela.raise_window()
            janela.set_input_focus(X.RevertToParent, X.CurrentTime)
        d.sync()
    finally:
        d.close()


def fechar_janela_x(id_janela):
    """Pede pra janela fechar, como clicar no X dela: o app pode
    perguntar se quer salvar, em vez de ser encerrado à força."""

    from Xlib import X, protocol

    d = _abrir_display_x()
    try:
        raiz = d.screen().root
        janela = d.create_resource_object("window", int(id_janela))
        evento = protocol.event.ClientMessage(
            window=janela,
            client_type=d.intern_atom("_NET_CLOSE_WINDOW"),
            data=(32, [X.CurrentTime, 2, 0, 0, 0]),
        )
        raiz.send_event(evento, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        d.sync()
    finally:
        d.close()


def _normalizar_titulo(texto):
    return re.sub(r"[^a-z0-9 ]+", " ", normalizar(texto or ""))


def _pontuar_janela(janela, termos):
    """Quanto uma janela combina com os termos (nome do app, binário,
    título pedido). 0 = não combina."""
    titulo = _normalizar_titulo(janela["titulo"])
    classe = _normalizar_titulo(janela["classe"])
    melhor = 0
    for termo in termos:
        t = _normalizar_titulo(termo).strip()
        if len(t) < 2:
            continue
        if t == titulo.strip() or t in classe.split():
            melhor = max(melhor, 3)
        elif t in classe:
            melhor = max(melhor, 2)
        elif t in titulo:
            melhor = max(melhor, 1)
    return melhor


def _termos_para_janela(nome):
    """"calculadora" -> também "Calculator", "gnome-calculator"...: o
    título e a classe da janela costumam estar em inglês ou no nome do
    programa, não no nome que o usuário falou."""
    termos = [nome]
    try:
        encontrado = _buscar_app_no_sistema(nome)
    except Exception:
        encontrado = None
    if encontrado:
        termos += _termos_do_app(nome, encontrado)
    return list(dict.fromkeys(termos))


def encontrar_janela_por_nome(nome, janelas=None):
    janelas = (listar_janelas_x() or []) if janelas is None else janelas
    return encontrar_janela([nome], janelas) or encontrar_janela(_termos_para_janela(nome), janelas)


def encontrar_janela(termos, janelas=None):
    """Melhor janela aberta para os termos; em empate, a mais recente."""
    janelas = (listar_janelas_x() or []) if janelas is None else janelas
    melhor, melhor_pontos = None, 0
    for janela in janelas:  # mais nova por último: >= deixa ela vencer
        pontos = _pontuar_janela(janela, termos)
        if pontos and pontos >= melhor_pontos:
            melhor, melhor_pontos = janela, pontos
    return melhor


def _janela_para_ia(janela):
    """Retângulo da janela nas mesmas coordenadas do print de tela
    inteira (o que a IA usa pra clicar)."""
    escala = _escala_tela_inteira()
    return {
        "titulo": janela["titulo"],
        "aplicativo": janela["classe"].split(" ")[-1] if janela["classe"] else "",
        "x": round(janela["x"] / escala),
        "y": round(janela["y"] / escala),
        "largura": round(janela["largura"] / escala),
        "altura": round(janela["altura"] / escala),
        "minimizada": janela["minimizada"],
    }


def listar_janelas():
    """Handler da ferramenta list_windows."""

    if ERRO_PYAUTOGUI:
        return {"sucesso": False, "mensagem": f"Cannot read the windows: {ERRO_PYAUTOGUI}."}

    janelas = listar_janelas_x()
    if not janelas:
        return {
            "sucesso": False,
            "mensagem": (
                "Could not read the window list"
                + (" (in a Wayland session only XWayland windows are visible)." if SESSAO_GRAFICA == "wayland" else ".")
            ),
        }

    _usar_quadro_tela_inteira()
    ativa = janela_ativa_x()
    lista = []
    for janela in reversed(janelas):  # mais recente primeiro
        item = _janela_para_ia(janela)
        item["em_foco"] = janela["id"] == ativa
        lista.append(item)

    return {
        "sucesso": True,
        "janelas": lista,
        "mensagem": (
            f"{len(lista)} open window(s). Positions are in full-screen screenshot "
            "coordinates; to click INSIDE a window, prefer click_element, or take a "
            "screenshot of that window first."
        ),
    }


def focar_janela(nome):
    """Handler da ferramenta focus_window."""

    if ERRO_PYAUTOGUI:
        return {"sucesso": False, "mensagem": f"Cannot control windows: {ERRO_PYAUTOGUI}."}

    janela = encontrar_janela_por_nome(nome)
    if not janela:
        return {
            "sucesso": False,
            "mensagem": f"No open window looks like '{nome}'. Use list_windows to see the windows.",
        }

    try:
        focar_janela_x(janela["id"])
        time.sleep(0.15)  # o gerenciador de janelas leva um instante
        _usar_quadro_tela_inteira()
    except Exception as e:
        return {"sucesso": False, "mensagem": f"Could not bring '{nome}' to the front: {e}"}

    return {
        "sucesso": True,
        "janela": _janela_para_ia(janela),
        "mensagem": f"SUCCESS: window '{janela['titulo']}' is in the foreground.",
    }


# Programas de captura que funcionam numa sessão Wayland (onde ler a
# tela pelo X devolve só preto), na ordem: GNOME, KDE, wlroots
# (Sway/Hyprland). O instalador recomenda o primeiro que couber.
_CAPTURADORES_WAYLAND = (
    ("gnome-screenshot", ["gnome-screenshot", "-f"]),
    ("spectacle", ["spectacle", "-b", "-n", "-f", "-o"]),
    ("grim", ["grim"]),
)

TIMEOUT_CAPTURA_SEG = 15


def _imagem_toda_preta(img):
    try:
        return img.convert("L").getextrema()[1] <= 5
    except Exception:
        return False


def _capturar_com_programa(cmd_base):
    import tempfile

    fd, caminho = tempfile.mkstemp(suffix=".png", prefix="opentars-print-")
    os.close(fd)

    try:
        subprocess.run(
            cmd_base + [caminho],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_CAPTURA_SEG,
            check=True,
        )
        with Image.open(caminho) as img:
            img.load()
            return img.copy()
    finally:
        try:
            os.unlink(caminho)
        except OSError:
            pass


def _capturar_tela():
    """Tira o print pelo caminho que funciona nesta sessão.

    X11: lê a tela direto pelo servidor X (Pillow/XCB). Não precisa de
    nenhum programa instalado. Antes o pyautogui exigia gnome-screenshot
    ou scrot, que o Ubuntu 24.04 e o Zorin 17 não trazem, e o print
    falhava num PC recém-instalado.

    Wayland: o X só enxerga janelas XWayland (o resto sai preto), então
    tenta os programas de captura do ambiente antes."""

    from PIL import ImageGrab

    erros = []

    if SESSAO_GRAFICA == "wayland":
        for programa, cmd in _CAPTURADORES_WAYLAND:
            if not shutil.which(programa):
                continue
            try:
                img = _capturar_com_programa(cmd)
                if not _imagem_toda_preta(img):
                    return img
                erros.append(f"{programa}: black image")
            except Exception as e:
                erros.append(f"{programa}: {e}")

    try:
        img = ImageGrab.grab()
        if SESSAO_GRAFICA == "wayland" and _imagem_toda_preta(img):
            raise RuntimeError("Wayland session and reading the screen through X returned a black image")
        return img
    except Exception as e:
        erros.append(str(e))

    # Último recurso: o caminho do pyautogui (usa gnome-screenshot/scrot
    # se existirem).
    try:
        return pyautogui.screenshot()
    except Exception as e:
        erros.append(str(e))

    dica = (
        " Install a screenshot tool: sudo apt install gnome-screenshot "
        "(GNOME/Zorin/Ubuntu), kde-spectacle (KDE) or grim (Sway/Hyprland)."
        if SESSAO_GRAFICA == "wayland"
        else ""
    )
    raise RuntimeError("; ".join(erros) + "." + dica)


def tirar_print(janela=None):
    """Captura a tela (ou só uma janela) e devolve a imagem em
    '_imagem_b64', que o loop anexa à conversa como mensagem de visão.

    Com 'janela', traz a janela pra frente e recorta só ela: a imagem
    fica menor (menos tokens) e sem redução, então os botões aparecem
    nítidos e o clique fica bem mais preciso que num print da tela
    inteira encolhida."""

    try:
        alvo = None
        if janela:
            alvo = encontrar_janela_por_nome(janela)
            if not alvo and _pode_abrir_sozinho(janela) and abrir_aplicativo(janela).get("sucesso"):
                alvo = encontrar_janela_por_nome(janela)
            if not alvo:
                return {
                    "sucesso": False,
                    "acao": "take_screenshot",
                    "mensagem": (
                        f"No open window looks like '{janela}'. Take a screenshot "
                        "without 'window' or use list_windows."
                    ),
                }
            try:
                if alvo["id"] != janela_ativa_x():
                    focar_janela_x(alvo["id"])
                    time.sleep(0.3)  # tempo de a janela ser redesenhada na frente
                # Posição pode ter mudado ao restaurar/focar.
                alvo = next((j for j in listar_janelas_x() or [] if j["id"] == alvo["id"]), alvo)
            except Exception:
                pass

        captura = _capturar_tela()
        if captura.mode != "RGB":  # só converte (e copia) se precisar
            captura = captura.convert("RGB")

        ox = oy = 0
        if alvo:
            tela_l, tela_a = captura.size
            x0, y0 = max(0, alvo["x"]), max(0, alvo["y"])
            x1 = min(tela_l, alvo["x"] + alvo["largura"])
            y1 = min(tela_a, alvo["y"] + alvo["altura"])
            if x1 - x0 > 10 and y1 - y0 > 10:
                captura = captura.crop((x0, y0, x1, y1))
                ox, oy = x0, y0
            else:
                alvo = None  # janela fora da tela: vai a tela inteira

        largura_original, altura_original = captura.size

        # Encolhe antes de codificar se a imagem for maior que o teto
        # configurado — ver LARGURA_MAXIMA_SCREENSHOT. Mantém a
        # proporção original; nunca AUMENTA uma imagem menor que o teto.
        if largura_original > LARGURA_MAXIMA_SCREENSHOT:
            razao = LARGURA_MAXIMA_SCREENSHOT / largura_original
            nova_altura = round(altura_original * razao)
            captura = captura.resize(
                (LARGURA_MAXIMA_SCREENSHOT, nova_altura),
                Image.LANCZOS,
            )

        buffer = io.BytesIO()
        # compress_level baixo troca um pouco de tamanho de arquivo
        # por velocidade de codificação — pra uma imagem que vai ser
        # descartada logo depois de analisada, isso vale a pena.
        captura.save(buffer, format="PNG", compress_level=1)

        imagem_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        largura, altura = captura.size

        _escala_print.update(
            x=largura_original / largura,
            y=altura_original / altura,
            ox=ox,
            oy=oy,
            largura=largura,
            altura=altura,
            definido=True,
        )

        do_que = f"of the window '{alvo['titulo']}'" if alvo else "of the screen"

        return {
            "sucesso": True,
            "acao": "take_screenshot",
            "mensagem": (
                f"SUCCESS: screenshot {do_que} taken ({largura_original}x{altura_original}"
                + (f", scaled down to {largura}x{altura}" if largura != largura_original else "")
                + "). To click or move the mouse, use pixel coordinates of THIS "
                "image (origin at its top-left corner); openTARS converts them to "
                "the real screen position."
            ),
            "_imagem_b64": imagem_b64,
        }

    except Exception as e:
        return {
            "sucesso": False,
            "acao": "take_screenshot",
            "mensagem": f"ERROR taking the screenshot: {e}",
        }


# ============================================================
# ELEMENTOS DA JANELA (árvore de acessibilidade)
# ============================================================
#
# Clicar num botão pelo NOME ("7", "=", "Salvar") em vez de print +
# coordenada: ver tars_acessibilidade.py. O print continua como plano B
# pra app que não expõe a árvore.

_acessibilidade_ligada = {"feito": False}

AVISO_SEM_ACESSIBILIDADE = (
    "The accessibility interface is not available ({motivo}). Use take_screenshot "
    "with window=<app> and click by coordinates. (To enable it: sudo apt install "
    "gir1.2-atspi-2.0 python3-gi at-spi2-core)"
)


def _dica_clique_por_nome():
    if acess.indisponivel():
        return ""
    return " To press its buttons, use click_element with the text shown on each button."


def _janela_x_do_pid(pid):
    if not pid:
        return None
    return next((j for j in listar_janelas_x() or [] if j["pid"] == pid), None)


TIMEOUT_JANELA_ACESSIVEL_SEG = 5
_abertos_sozinhos = {}  # app normalizado -> quando o openTARS tentou abrir


def _pode_abrir_sozinho(nome):
    """Abre no máximo uma vez a cada 30 s por app (uma fila de cliques não
    abre cinco calculadoras), nunca o próprio openTARS, e só se o nome
    for de um app instalado."""
    chave = normalizar(nome)
    if not chave or "tars" in chave:
        return False
    agora = time.time()
    if agora - _abertos_sozinhos.get(chave, 0) < 30:
        return False
    _abertos_sozinhos[chave] = agora
    return bool(encontrar_app(nome))


def _nota_aberto(alvo):
    return " (it was not open, so openTARS opened it first)" if alvo.get("aberto_agora") else ""


# Apps abertos que não expõem os botões (Electron/Chromium: Claude,
# Discord, VS Code, Slack...). Depois da primeira falha, click_element e
# list_elements respondem na hora com o caminho certo, em vez de a IA
# tentar nome por nome (log real: 14 cliques falhos seguidos no Claude).
_SEM_ARVORE = {}  # nome normalizado -> quando descobrimos
VALIDADE_SEM_ARVORE_SEG = 600


def _capturar_janela(nome):
    """(imagem, ox, oy, janela_x) da janela pedida, já na frente, ou None."""
    janela_x = encontrar_janela_por_nome(nome) if nome else None
    if not janela_x:
        return None
    try:
        if janela_x["id"] != janela_ativa_x():
            focar_janela_x(janela_x["id"])
            time.sleep(0.3)
        janela_x = next((j for j in listar_janelas_x() or [] if j["id"] == janela_x["id"]), janela_x)
    except Exception:
        pass
    captura = _capturar_tela()
    x0, y0 = max(0, janela_x["x"]), max(0, janela_x["y"])
    x1 = min(captura.width, janela_x["x"] + janela_x["largura"])
    y1 = min(captura.height, janela_x["y"] + janela_x["altura"])
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None
    return captura.crop((x0, y0, x1, y1)), x0, y0, janela_x


def _ocr_pronto():
    """None se dá pra clicar lendo a tela; senão, o motivo."""
    if not ocr.disponivel():
        return "the OCR program is not installed (sudo apt install tesseract-ocr)"
    if ERRO_PYAUTOGUI:
        return f"cannot control the mouse: {ERRO_PYAUTOGUI}"
    return None


def _ler_janela(nome):
    """(palavras, ox, oy, janela_x, imagem) lidas por OCR, ou None."""
    capturada = _capturar_janela(nome)
    if not capturada:
        return None
    imagem, ox, oy, janela_x = capturada
    palavras = ocr.ler(imagem, ocr.idiomas_para(i18n.idiomas_de_entrada()))
    return palavras, ox, oy, janela_x, imagem


# Clique pela visão (ícone sem texto): TARS_CLIQUE_POR_VISAO=0 desliga.
CLIQUE_POR_VISAO = os.environ.get("TARS_CLIQUE_POR_VISAO", "1") not in ("0", "off", "nao", "no")
TIMEOUT_VISAO_GRADE = 60


def _localizar_por_visao(nome, imagem, titulo):
    """(x, y) do elemento na imagem da janela, apontado por um modelo com
    visão numa grade (tars_ocr.localizar_por_grade), ou None."""
    if not CLIQUE_POR_VISAO:
        return None, None
    visao = _modelo_de_visao()
    if not visao:
        return None, None
    print(c(f"[openTARS] {t('msg.procurando_com_visao', alvo=nome, modelo=visao)}", Cor.CINZA))

    def perguntar(img, prompt, opcoes):
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", compress_level=1)
        bruto = _chat_unico(
            visao, prompt, temperatura=0.0, timeout=TIMEOUT_VISAO_GRADE, keep_alive=KEEP_ALIVE,
            formato={"type": "object", "properties": {"celula": {"type": "string", "enum": opcoes}},
                     "required": ["celula"]},
            imagens=[base64.b64encode(buffer.getvalue()).decode("ascii")],
        )
        dados = _json_da_resposta(bruto)
        if isinstance(dados, dict):
            return str(dados.get("celula", "")).strip().upper().replace("NONE", "none")
        achado = re.search(r"\b([A-H][1-8])\b", bruto or "")
        return achado.group(1) if achado else None

    try:
        return ocr.localizar_por_grade(imagem, nome, perguntar, titulo), visao
    except Exception:
        return None, visao


def clicar_por_texto(nome, janela, usar_visao=True):
    """Plano B do click_element: lê o texto da janela (OCR) e clica no
    lugar onde está escrito 'nome'. Funciona em qualquer app que MOSTRE o
    texto, com ou sem acessibilidade. Se o texto não aparece (ícone), um
    modelo com visão aponta o lugar numa grade (usar_visao)."""
    motivo = _ocr_pronto()
    if motivo or not janela:
        return None
    lido = _ler_janela(janela)
    if not lido:
        return None
    palavras, ox, oy, janela_x, imagem = lido
    achado = ocr.achar(palavras, nome)
    if not achado and usar_visao:
        ponto, visao = _localizar_por_visao(nome, imagem, janela_x["titulo"])
        if ponto:
            try:
                pyautogui.click(x=ox + ponto[0], y=oy + ponto[1])
            except Exception as e:
                return {"sucesso": False, "mensagem": f"Located '{nome}' but the click failed: {e}"}
            time.sleep(0.35)
            return {"sucesso": True, "janela": janela_x["titulo"], "elemento": nome, "mensagem": (
                f"SUCCESS: clicked where the vision model {visao} located '{nome}' in '{janela_x['titulo']}' "
                f"(x={ponto[0]}, y={ponto[1]} in the window). It was found by LOOKING at the screen, so it "
                "can be a little off: check the result (list_elements or take_screenshot) before going on.")}
    if not achado:
        return {"sucesso": False, "janela": janela_x["titulo"], "mensagem": (
            f"'{nome}' is not written anywhere visible in '{janela_x['titulo']}' (read by OCR). "
            + (f"Visible texts: {' | '.join(ocr.parecidos(palavras, nome))}. " if palavras else "")
            + "If it is an icon without text, use take_screenshot and click_mouse with x/y. "
            "(This app does not expose its buttons to accessibility. To write and send a message "
            f"in it: type_in_element(text=..., window='{janela}', submit=true).)")}
    try:
        pyautogui.click(x=ox + achado["x"], y=oy + achado["y"])
    except Exception as e:
        return {"sucesso": False, "mensagem": f"Found '{achado['texto']}' but the click failed: {e}"}
    time.sleep(0.35)
    return {"sucesso": True, "janela": janela_x["titulo"], "elemento": achado["texto"], "mensagem": (
        f"SUCCESS: clicked '{achado['texto']}' in '{janela_x['titulo']}' (found by reading the screen, "
        "since this app does not expose its buttons).")}


def _mensagem_sem_arvore(nome):
    if not _ocr_pronto():
        return (
            f"'{nome}' is open, but it does not expose its buttons to the accessibility interface "
            "(common in Electron/Chromium apps such as Claude, Discord, VS Code, Slack). You CAN still "
            f"use it: click_element(name, window='{nome}') clicks any VISIBLE text by reading the screen, "
            f"and list_elements(window='{nome}') returns the texts it shows. To write and send a message "
            f"(its text box usually has the keyboard focus): type_in_element(text=..., window='{nome}', "
            "submit=true). For icons without text: take_screenshot + click_mouse(x, y). Don't ask the "
            "user to do it: do it yourself."
        )
    return (
        f"'{nome}' is open, but it does not expose its buttons to the accessibility interface "
        "(common in Electron/Chromium apps such as Claude, Discord, VS Code, Slack). You CAN "
        "still use it. To write and send a message (its text box usually already has the "
        f"keyboard focus): type_in_element(text=<your text>, window='{nome}', submit=true). "
        f"To click something else: take_screenshot(window='{nome}') and click_mouse with x/y of "
        "that image. Do NOT use click_element or list_elements on it again, and don't ask the "
        "user to do it: do it yourself with those tools."
    )


def _digitar_na_janela(janela, texto, enviar=False):
    """Traz a janela pra frente e digita no campo que tem o foco (num app
    de chat, a caixa de mensagem). enviar=True aperta Enter no fim."""
    focada = focar_janela(janela)
    if not focada.get("sucesso"):
        return focada
    time.sleep(0.25)  # o app redesenha e devolve o foco à caixa de texto
    resultado = digitar_texto(texto or "")
    if not resultado.get("sucesso"):
        return resultado
    if enviar:
        tecla = pressionar_tecla("enter")
        if not tecla.get("sucesso"):
            return tecla
    titulo = (focada.get("janela") or {}).get("titulo") or janela
    return {"sucesso": True, "janela": titulo, "mensagem": (
        f"SUCCESS: typed the text into the focused box of '{titulo}'"
        + (" and pressed Enter to send it." if enviar else ". Use press_key('enter') to send it.")
        + " If it may have gone to the wrong place, check with take_screenshot.")}


def _marcar_sem_arvore(nome):
    if nome:
        _SEM_ARVORE[normalizar(nome)] = time.time()


def _sem_arvore(nome):
    quando = _SEM_ARVORE.get(normalizar(nome or ""))
    return quando is not None and time.time() - quando < VALIDADE_SEM_ARVORE_SEG


def _janela_acessivel(nome):
    """A janela (na árvore de acessibilidade) que a IA pediu, ou a ativa.
    Devolve (janela, erro)."""

    motivo = acess.indisponivel()
    if motivo:
        return None, AVISO_SEM_ACESSIBILIDADE.format(motivo=motivo)
    if nome and _sem_arvore(nome):
        return None, _mensagem_sem_arvore(nome)

    def procurar():
        janela_x, termos = None, ()
        if nome:
            termos = _termos_para_janela(nome)
            janela_x = encontrar_janela_por_nome(nome)
            # Só o título batendo (uma aba "Calculadora online" no
            # navegador) não basta pra dizer que é o app pedido.
            if janela_x and _pontuar_janela(janela_x, termos) < 2:
                janela_x = None
        else:
            ativa = janela_ativa_x()
            if ativa:
                janela_x = next((j for j in listar_janelas_x() or [] if j["id"] == ativa), None)
        return acess.achar_janela(
            pid=janela_x["pid"] if janela_x else None,
            titulo=janela_x["titulo"] if janela_x else None,
            termos=termos,
            ativa=not nome,
            ignorar_pids=_pids_protegidos(),
        )

    alvo = procurar()
    # Apps Qt/KDE (e alguns outros) só expõem a árvore com a
    # acessibilidade da sessão ligada: liga uma vez e procura de novo.
    if alvo is None and not _acessibilidade_ligada["feito"]:
        _acessibilidade_ligada["feito"] = True
        if acess.ativar():
            time.sleep(0.6)
            alvo = procurar()

    # A IA pulou o open_application ("clique no 1 da calculadora" com a
    # calculadora fechada): abre o app e espera a janela aparecer na
    # árvore, uma vez por app. Antes os cinco cliques falhavam em fila.
    aberta_no_x = bool(nome) and alvo is None and encontrar_janela_por_nome(nome) is not None
    if alvo is None and nome and not aberta_no_x and _pode_abrir_sozinho(nome):
        aberto = abrir_aplicativo(nome)
        if aberto.get("sucesso"):
            limite = time.time() + TIMEOUT_JANELA_ACESSIVEL_SEG
            while alvo is None and time.time() < limite:
                time.sleep(0.25)
                alvo = procurar()
            if alvo is not None:
                alvo = dict(alvo, aberto_agora=True)

    if alvo is None:
        if nome and (aberta_no_x or encontrar_janela_por_nome(nome) is not None):
            _marcar_sem_arvore(nome)
            return None, _mensagem_sem_arvore(nome)
        if nome:
            return None, (
                f"'{nome}' is not open, or does not expose its elements to the accessibility "
                "interface. If it is open, use take_screenshot with window=<app> and click by coordinates."
            )
        return None, (
            "No accessible window is in focus. Pass window=<app name>, or use "
            "take_screenshot and click by coordinates."
        )
    return alvo, None


def _listar_por_ocr(janela, filtro=None):
    if _ocr_pronto() or not janela:
        return None
    lido = _ler_janela(janela)
    if not lido:
        return None
    palavras, _, _, janela_x, _ = lido
    textos = [l for l in ocr.linhas(palavras) if not filtro or normalizar(filtro) in normalizar(l)]
    return {"sucesso": bool(textos), "janela": janela_x["titulo"], "textos": textos[:60], "mensagem": (
        f"'{janela_x['titulo']}' does not expose its buttons, so openTARS READ the window (OCR). "
        "These are the texts it shows (one per line). click_element(name=<text>, window=...) clicks "
        "any of them." if textos else f"No readable text in '{janela_x['titulo']}'.")}


def listar_elementos(janela=None, filtro=None):
    """Handler da ferramenta list_elements."""
    alvo, erro = _janela_acessivel(janela)
    if erro:
        if janela and _sem_arvore(janela):
            lido = _listar_por_ocr(janela, filtro)
            if lido:
                return lido
        return {"sucesso": False, "mensagem": erro}

    r = acess.listar(alvo, filtro)
    if not r["total"] and not r["textos"] and not filtro and janela:
        _marcar_sem_arvore(janela)
        lido = _listar_por_ocr(janela)
        if lido:
            return lido
        return {"sucesso": False, "janela": alvo["titulo"], "mensagem": _mensagem_sem_arvore(janela)}
    if not r["total"] and not r["textos"]:
        return {
            "sucesso": False,
            "janela": alvo["titulo"],
            "mensagem": (
                f"'{alvo['titulo']}' exposes no clickable elements"
                + (f" matching '{filtro}'" if filtro else "")
                + ". Use take_screenshot with window=<app> and click by coordinates."
            ),
        }
    return {
        "sucesso": True,
        "janela": alvo["titulo"],
        "elementos": r["elementos"],
        "textos": r["textos"],
        "mensagem": (
            f"{r['total']} element(s) in '{alvo['titulo']}'{_nota_aberto(alvo)} (grouped by role) and the texts "
            "it shows. Press one with click_element(name=...)."
        ),
    }


def _clicar_pelo_retangulo(alvo, retangulo):
    """Plano B de click_element: elemento sem ação de acessibilidade, mas
    com posição conhecida. Só com X11 e só se a posição cair dentro da
    janela (GTK 4 informa posições relativas, que clicariam no lugar errado)."""
    if SESSAO_GRAFICA != "x11" or ERRO_PYAUTOGUI or not retangulo:
        return False
    if not acess.coordenadas_confiaveis(alvo):
        return False
    x, y, largura, altura = retangulo
    janela_x = _janela_x_do_pid(alvo.get("pid"))
    if largura <= 0 or altura <= 0 or not janela_x:
        return False
    cx, cy = x + largura // 2, y + altura // 2
    dentro = (janela_x["x"] <= cx < janela_x["x"] + janela_x["largura"]
              and janela_x["y"] <= cy < janela_x["y"] + janela_x["altura"])
    if not dentro:
        return False
    try:
        pyautogui.click(x=cx, y=cy)
        return True
    except Exception:
        return False


def clicar_elemento(nome, janela=None, papel=None):
    """Handler da ferramenta click_element."""
    nome = (nome or "").strip()
    if not nome:
        return {"sucesso": False, "mensagem": "ERROR: 'name' is empty: pass the text shown on the element."}

    alvo, erro = _janela_acessivel(janela)
    if erro:
        if janela and _sem_arvore(janela):
            por_texto = clicar_por_texto(nome, janela)
            if por_texto:
                return por_texto
        return {"sucesso": False, "mensagem": erro}

    r = acess.clicar(alvo, nome, papel)
    if r["sucesso"]:
        textos = r.get("textos") or []
        mensagem = f"SUCCESS: clicked {r['elemento']} in '{alvo['titulo']}'{_nota_aberto(alvo)}."
        if textos:
            mensagem += " The window now shows: " + " | ".join(textos[:8])
        return {"sucesso": True, "janela": alvo["titulo"], "elemento": r["elemento"],
                "textos": textos[:8], "mensagem": mensagem}

    if not r["achou"]:
        parecidos = acess.parecidos(alvo, nome)
        if not parecidos and janela and not acess.listar(alvo, None)["total"]:
            _marcar_sem_arvore(janela)
            por_texto = clicar_por_texto(nome, janela)
            if por_texto and por_texto["sucesso"]:
                return por_texto
            return {"sucesso": False, "janela": alvo["titulo"], "mensagem": _mensagem_sem_arvore(janela)}
        # A árvore existe mas não tem esse nome (botão desenhado à mão,
        # canvas...): tenta achar o texto na tela antes de desistir.
        por_texto = clicar_por_texto(nome, janela or alvo.get("titulo"))
        if por_texto and por_texto["sucesso"]:
            return por_texto
        return {
            "sucesso": False,
            "janela": alvo["titulo"],
            "mensagem": (
                f"No element named '{nome}' in '{alvo['titulo']}'."
                + (f" Some of its elements: {', '.join(parecidos)}." if parecidos else "")
                + " Use list_elements to see them all, or take_screenshot to click by coordinates."
            ),
        }

    if _clicar_pelo_retangulo(alvo, r.get("retangulo")):
        return {"sucesso": True, "janela": alvo["titulo"], "elemento": r["elemento"],
                "mensagem": f"SUCCESS: clicked {r['elemento']} in '{alvo['titulo']}' (with the mouse)."}

    return {
        "sucesso": False,
        "janela": alvo["titulo"],
        "mensagem": (
            f"Found {r['elemento']} in '{alvo['titulo']}', but it cannot be pressed through "
            "accessibility. Use take_screenshot with window=<app> and click by coordinates."
        ),
    }


def escrever_em_elemento(nome, texto, janela=None, enviar=False):
    """Handler da ferramenta type_in_element."""
    alvo, erro = _janela_acessivel(janela)
    if erro:
        # App aberto sem árvore (Electron). Com o nome do campo ("Reply to
        # Claude", "Pesquisar"), clica no texto dele lendo a tela (OCR, sem
        # visão: digitar no lugar errado é pior que não achar); sem nome,
        # ou se não achar, digita no campo que já tem o foco.
        if janela and _sem_arvore(janela):
            if (nome or "").strip():
                clicado = clicar_por_texto(nome.strip(), janela, usar_visao=False)
                if clicado and clicado.get("sucesso"):
                    time.sleep(0.15)
                    resultado = digitar_texto(texto or "")
                    if resultado.get("sucesso") and enviar:
                        pressionar_tecla("enter")
                    if resultado.get("sucesso"):
                        resultado["mensagem"] = (
                            f"SUCCESS: clicked the field '{clicado['elemento']}' in '{clicado['janela']}' "
                            "(found by reading the screen) and typed the text"
                            + (" and pressed Enter." if enviar else "."))
                    return resultado
            return _digitar_na_janela(janela, texto, enviar)
        return {"sucesso": False, "mensagem": erro}

    r = acess.preencher(alvo, (nome or "").strip(), texto or "")
    if r["sucesso"]:
        if enviar:
            janela_x = _janela_x_do_pid(alvo.get("pid"))
            if janela_x:
                try:
                    focar_janela_x(janela_x["id"])
                    time.sleep(0.15)
                except Exception:
                    pass
            pressionar_tecla("enter")
        return {"sucesso": True, "janela": alvo["titulo"],
                "mensagem": f"SUCCESS: wrote the text into {r['elemento']} in '{alvo['titulo']}'"
                            + (" and pressed Enter." if enviar else ".")}

    if r.get("precisa_digitar"):
        # O campo não aceita texto pela acessibilidade: traz a janela pra
        # frente (o foco do teclado é da janela ativa) e digita.
        janela_x = _janela_x_do_pid(alvo.get("pid"))
        if janela_x:
            try:
                focar_janela_x(janela_x["id"])
                time.sleep(0.2)
            except Exception:
                pass
        resultado = digitar_texto(texto or "")
        if resultado.get("sucesso"):
            if enviar:
                pressionar_tecla("enter")
            resultado["mensagem"] = (f"SUCCESS: focused {r['elemento']} in '{alvo['titulo']}' and typed the text"
                                     + (" and pressed Enter." if enviar else "."))
        return resultado

    campos = r.get("campos") or []
    return {
        "sucesso": False,
        "janela": alvo["titulo"],
        "mensagem": (
            f"No text field named '{nome}' in '{alvo['titulo']}'."
            + (f" Its fields: {', '.join(campos)}." if campos else " It has no text field exposed.")
            + " You can also click the field and use type_text."
        ),
    }


# ============================================================
# FERRAMENTAS PARA OLLAMA
# ============================================================
#
# Descrições em inglês: é a língua que os modelos entendem melhor e
# não muda com o idioma da interface (a resposta ao usuário sai no
# idioma dele — ver SYSTEM_PROMPT).

def _ferramenta(nome, descricao, propriedades=None, obrigatorios=()):
    parametros = {"type": "object", "properties": propriedades or {}}
    if obrigatorios:
        parametros["required"] = list(obrigatorios)
    return {"type": "function", "function": {"name": nome, "description": descricao, "parameters": parametros}}


_TEXTO = {"type": "string"}
_INTEIRO = {"type": "integer"}

TOOLS = [
    _ferramenta(
        "open_application",
        "Opens ANY application installed on the system, by the common name the user "
        "used, in any language (browser, text editor, calculator, terminal, file "
        "manager, a game...). The tool resolves the real name by itself, waits for "
        "the window and brings it to the front. Always use it to open apps, even if "
        "the name is not known in advance.",
        {"app": {"type": "string", "description": "Name of the application to open, as the user said it."}},
        ["app"],
    ),
    _ferramenta(
        "close_application",
        "Closes an open application by the common name the user used ('claude', "
        "'the browser', 'calculator', 'spotify'). It finds the window or process by itself.",
        {"app": {"type": "string", "description": "Name of the application, as the user said it."}},
        ["app"],
    ),
    _ferramenta(
        "open_website",
        "Opens ANY website or online service in the browser, by its common name or "
        "URL. The tool resolves the real address by itself.",
        {"site": {"type": "string", "description": "Website name or URL."}},
        ["site"],
    ),
    _ferramenta(
        "search_web",
        "Searches the web and opens the results in the browser. Use it (instead of "
        "open_website) whenever the request is a SEARCH: searching, looking up or "
        "finding something, or asking for something 'on google'/'on youtube'.",
        {
            "query": {"type": "string", "description": (
                "Only the search terms, without the verb and without the service name "
                "(for 'search rtx 5060 on google', query is just 'rtx 5060').")},
            "service": {"type": "string", "description": (
                "Where to search, only if the user names a service (e.g. 'google', "
                "'youtube'). Leave it out otherwise: the default is Google.")},
        },
        ["query"],
    ),
    _ferramenta(
        "click_element",
        "Presses a button, menu item, tab, link or checkbox in an app window BY THE "
        "TEXT SHOWN ON IT (e.g. name='7', '=', 'Save', 'Settings'). Uses the "
        "accessibility interface (or, in apps that don't expose their buttons, reads the "
        "visible text on screen): no screenshot or coordinates needed, and it works "
        "with any model. Returns what the window shows afterwards (e.g. a calculator "
        "display). Prefer it over clicking by coordinates.",
        {
            "name": {"type": "string", "description": "Text shown on the element (or its tooltip). For an icon "
                                                       "without text, describe it: 'send icon', 'gear icon'."},
            "window": {"type": "string", "description": "App or window name (e.g. 'calculator'). Recommended."},
            "role": {"type": "string", "description": "Optional: 'button', 'menu item', 'tab', 'link', 'checkbox'..."},
        },
        ["name"],
    ),
    _ferramenta(
        "list_elements",
        "Lists what can be clicked in an app window (buttons, menus, fields, tabs, "
        "links, grouped by role) and the texts it shows (e.g. a calculator display). "
        "Use it to find names for click_element or to read the window without a screenshot.",
        {
            "window": {"type": "string", "description": "App or window name. Without it, the focused window."},
            "filter": {"type": "string", "description": "Optional: only elements whose name contains this."},
        },
    ),
    _ferramenta(
        "type_in_element",
        "Writes text into a text field of an app window, found by its name or label "
        "(e.g. 'Search', 'File name'). Without a name, uses the window's only field (or, in "
        "apps that don't expose their fields, the one with the keyboard focus, like a chat "
        "box). submit=true presses Enter afterwards (to send a message or search).",
        {
            "text": {"type": "string", "description": "Text to write."},
            "name": {"type": "string", "description": "Name or label of the field."},
            "window": {"type": "string", "description": "App or window name."},
            "submit": {"type": "boolean", "description": "Press Enter after writing."},
        },
        ["text"],
    ),
    _ferramenta(
        "execute_terminal",
        "Runs a command in the Linux terminal (non-interactive: no password prompts).",
        {"command": _TEXTO},
        ["command"],
    ),
    _ferramenta("get_current_directory", "Returns the current directory."),
    _ferramenta("list_files", "Lists files and folders in a directory.", {"path": _TEXTO}),
    _ferramenta("pc_info", "Gets CPU, RAM and disk usage."),
    _ferramenta("move_mouse", "Moves the mouse to a position on the screen.", {"x": _INTEIRO, "y": _INTEIRO}, ["x", "y"]),
    _ferramenta("get_mouse_position", "Returns the current mouse position (x, y)."),
    _ferramenta("get_screen_size", "Returns the screen resolution in pixels (width, height)."),
    _ferramenta(
        "click_mouse",
        "Clicks with the mouse. With x/y, clicks right at that position of the last "
        "screenshot (no move_mouse needed).",
        {"button": {"type": "string", "enum": ["left", "right", "middle"]}, "x": _INTEIRO, "y": _INTEIRO},
    ),
    _ferramenta("double_click", "Double-clicks (optionally at an x/y position).", {"x": _INTEIRO, "y": _INTEIRO}),
    _ferramenta(
        "scroll_mouse",
        "Scrolls the page/window (optionally at an x/y position).",
        {"amount": _INTEIRO, "x": _INTEIRO, "y": _INTEIRO},
        ["amount"],
    ),
    _ferramenta(
        "type_text",
        "Types text with the keyboard into the focused place, in any language "
        "(accents, other alphabets and emoji included).",
        {"text": _TEXTO},
        ["text"],
    ),
    _ferramenta("press_key", "Presses one key (e.g. 'enter', 'esc', 'tab').", {"key": _TEXTO}, ["key"]),
    _ferramenta(
        "hotkey",
        "Presses a key combination (e.g. ['ctrl', 's']).",
        {"keys": {"type": "array", "items": {"type": "string"}}},
        ["keys"],
    ),
    _ferramenta(
        "take_screenshot",
        "Takes a screenshot so you can SEE the screen. To work inside an app, pass "
        "'window' with the app name: the window comes to the front and the image shows "
        "only it, sharp. After a screenshot, click coordinates are pixels of THAT image.",
        {"window": {"type": "string", "description": "Optional: app/window name (e.g. 'calculator'). Without it, the whole screen."}},
    ),
    _ferramenta("list_windows", "Lists the open windows (title, app, position, which one has focus)."),
    _ferramenta(
        "focus_window",
        "Brings an open window to the front (restoring it if minimized), before typing "
        "or using shortcuts in it.",
        {"window": {"type": "string", "description": "App name or part of the title."}},
        ["window"],
    ),
    _ferramenta(
        "wait_seconds",
        "Waits a few seconds. open_application already waits for the window, so only "
        "use this for web pages or content that keeps loading after the window opened.",
        {"seconds": {"type": "number", "description": "How many seconds to wait (e.g. 1.5)."}},
        ["seconds"],
    ),
]


# ============================================================
# EXECUTOR CENTRAL
# ============================================================

# Dispatch table em vez de uma cadeia longa de if/elif: mais fácil de
# manter e de estender com novas ferramentas.
_DISPATCH_FERRAMENTAS = {
    "open_application": lambda a: abrir_aplicativo(a.get("app", "")),
    "close_application": lambda a: fechar_aplicativo(a.get("app", "")),
    "open_website": lambda a: abrir_site(a.get("site", "")),
    "search_web": lambda a: pesquisar_web(a.get("query", ""), a.get("service")),
    "execute_terminal": lambda a: executar_terminal(a.get("command", "")),
    "get_current_directory": lambda a: diretorio_atual(),
    "list_files": lambda a: listar_arquivos(a.get("path", ".")),
    "pc_info": lambda a: informacoes_pc(),
    "move_mouse": lambda a: mover_mouse(a.get("x", 0), a.get("y", 0)),
    "get_mouse_position": lambda a: posicao_mouse(),
    "get_screen_size": lambda a: tamanho_tela(),
    "click_mouse": lambda a: clicar_mouse(a.get("button", "left"), a.get("x"), a.get("y")),
    "double_click": lambda a: duplo_clique(a.get("x"), a.get("y")),
    "scroll_mouse": lambda a: scroll_mouse(a.get("amount", 0), a.get("x"), a.get("y")),
    "type_text": lambda a: digitar_texto(a.get("text", "")),
    "press_key": lambda a: pressionar_tecla(a.get("key", "")),
    "hotkey": lambda a: atalho_teclado(a.get("keys", [])),
    "take_screenshot": lambda a: tirar_print(a.get("window") or None),
    "click_element": lambda a: clicar_elemento(a.get("name", ""), a.get("window") or None, a.get("role") or None),
    "list_elements": lambda a: listar_elementos(a.get("window") or None, a.get("filter") or None),
    "type_in_element": lambda a: escrever_em_elemento(a.get("name", ""), a.get("text", ""), a.get("window") or None,
                                                      _booleano(a.get("submit"))),
    "list_windows": lambda a: listar_janelas(),
    "focus_window": lambda a: focar_janela(a.get("window", "")),
    "wait_seconds": lambda a: esperar(a.get("seconds", 1.0)),
}

_FERRAMENTAS_MOUSE_TECLADO = {
    "move_mouse", "click_mouse", "double_click", "scroll_mouse",
    "type_text", "press_key", "hotkey",
}

AVISO_WAYLAND_FERRAMENTA = (
    "NOTE: this is a Wayland session, where simulated clicks and keys only reach "
    "some applications (the ones running through XWayland). If nothing happened "
    "on screen, prefer click_element / type_in_element (they work on Wayland), or "
    "tell the user that mouse/keyboard control needs the Xorg session (chosen on "
    "the login screen)."
)


def _resumo_argumentos(argumentos):
    """O argumento principal, curto, pra mostrar o que a ferramenta vai
    fazer ("calculadora", "ls -la", "7, 2")."""
    if not isinstance(argumentos, dict) or not argumentos:
        return ""
    for chave in ("app", "site", "query", "command", "name", "window", "text", "key", "path", "filter"):
        valor = argumentos.get(chave)
        if isinstance(valor, str) and valor.strip():
            valor = " ".join(valor.split())
            return valor if len(valor) <= 60 else valor[:57] + "..."
    if "keys" in argumentos and isinstance(argumentos["keys"], list):
        return "+".join(str(k) for k in argumentos["keys"])
    if "x" in argumentos and "y" in argumentos:
        return f"{argumentos['x']}, {argumentos['y']}"
    if "seconds" in argumentos:
        return f"{argumentos['seconds']}s"
    return ""


# Ferramentas oferecidas por tarefa. Menos opções = menos erro nos
# modelos pequenos (24 ferramentas confundem um modelo de 4B). Se a IA
# recusar ou devolver a tarefa, ela ganha todas. Uma ferramenta fora da
# lista que a IA chamar mesmo assim ainda é executada.
_FERRAMENTAS_POR_TAREFA = {
    "simples": {"open_website", "search_web", "open_application", "close_application"},
    "busca": {"search_web", "open_website", "open_application", "take_screenshot", "click_element",
              "type_in_element", "list_elements", "press_key", "type_text", "wait_seconds", "focus_window"},
    "geral": {"search_web", "open_website", "open_application", "execute_terminal"},
    "codigo": {"execute_terminal", "list_files", "get_current_directory", "search_web", "open_application",
               "type_in_element"},
    "tecnico": {"execute_terminal", "pc_info", "list_files", "get_current_directory", "search_web",
                "open_website", "open_application", "close_application"},
}


def ferramentas_para(tarefa):
    permitidas = _FERRAMENTAS_POR_TAREFA.get(tarefa)
    if not permitidas:
        return TOOLS  # ação, visão e desconhecida: todas
    return [f for f in TOOLS if f["function"]["name"] in permitidas]


def _booleano(valor):
    """Modelos mandam true, "true", "yes", 1..."""
    if isinstance(valor, str):
        return valor.strip().lower() in ("true", "1", "yes", "sim", "y")
    return bool(valor)


# Nomes que os modelos inventam pras ferramentas -> o nome certo.
_APELIDOS_FERRAMENTAS = {
    "open_app": "open_application", "launch_app": "open_application", "launch_application": "open_application",
    "start_application": "open_application", "run_application": "open_application", "open_program": "open_application",
    "close_app": "close_application", "kill_application": "close_application", "quit_application": "close_application",
    "click": "click_mouse", "left_click": "click_mouse", "mouse_click": "click_mouse",
    "click_button": "click_element", "press_button": "click_element", "click_on": "click_element",
    "type": "type_text", "write_text": "type_text", "keyboard_type": "type_text", "input_text": "type_text",
    "press": "press_key", "key_press": "press_key", "keypress": "press_key",
    "screenshot": "take_screenshot", "capture_screen": "take_screenshot", "look_at_screen": "take_screenshot",
    "search": "search_web", "web_search": "search_web", "google_search": "search_web", "search_internet": "search_web",
    "open_url": "open_website", "open_browser": "open_website", "browse": "open_website", "visit_website": "open_website",
    "run_command": "execute_terminal", "terminal": "execute_terminal", "bash": "execute_terminal",
    "shell": "execute_terminal", "execute_command": "execute_terminal", "run_terminal": "execute_terminal",
    "wait": "wait_seconds", "sleep": "wait_seconds", "list_buttons": "list_elements", "read_window": "list_elements",
    "switch_window": "focus_window", "activate_window": "focus_window", "fill_field": "type_in_element",
}

# Nomes de argumento que os modelos trocam -> o nome certo.
_APELIDOS_ARGUMENTOS = {
    "app": ("app_name", "application", "application_name", "program", "name", "appname"),
    "url": ("website", "site", "link", "address", "uri"),
    "query": ("q", "search", "search_query", "term", "terms", "text"),
    "command": ("cmd", "shell_command", "bash"),
    "text": ("content", "message", "value", "string", "input"),
    "key": ("keys", "button", "keyname"),
    "window": ("window_name", "app", "application", "title", "window_title"),
    "name": ("element", "label", "button", "element_name", "target", "text"),
    "seconds": ("time", "duration", "secs", "s"),
    "path": ("directory", "folder", "dir"),
}


def _esquemas_ferramentas():
    if not _cache_esquemas:
        for f in TOOLS:
            fn = f["function"]
            parametros = fn.get("parameters") or {}
            _cache_esquemas[fn["name"]] = (parametros.get("properties") or {}, parametros.get("required") or [])
    return _cache_esquemas


_cache_esquemas = {}


def consertar_chamada(nome, argumentos):
    """Corrige os erros comuns dos modelos pequenos antes de executar:
    nome de ferramenta inventado ("open_app"), argumento com outro nome
    ("app_name"), número como texto ("120"), booleano como texto.
    Devolve (nome, argumentos, nota) — nota diz o que foi corrigido."""

    esquemas = _esquemas_ferramentas()
    notas = []
    original = nome
    if nome not in esquemas:
        chave = (nome or "").strip().lower().replace("-", "_").replace(" ", "_")
        novo = _APELIDOS_FERRAMENTAS.get(chave) or (chave if chave in esquemas else None)
        if not novo:
            parecidos = difflib.get_close_matches(chave, list(esquemas), n=1, cutoff=0.75)
            novo = parecidos[0] if parecidos else None
        if novo:
            nome = novo
            notas.append(f"tool '{original}' -> '{nome}'")
    if nome not in esquemas:
        return nome, argumentos, None

    propriedades, obrigatorios = esquemas[nome]
    argumentos = dict(argumentos or {})
    for certo in list(propriedades):
        if certo in argumentos:
            continue
        for apelido in _APELIDOS_ARGUMENTOS.get(certo, ()):
            if apelido in argumentos and apelido not in propriedades:
                argumentos[certo] = argumentos.pop(apelido)
                notas.append(f"argument '{apelido}' -> '{certo}'")
                break
    # Um obrigatório faltando e um único argumento desconhecido: é ele.
    faltando = [p for p in obrigatorios if p not in argumentos]
    desconhecidos = [k for k in argumentos if k not in propriedades]
    if len(faltando) == 1 and len(desconhecidos) == 1:
        argumentos[faltando[0]] = argumentos.pop(desconhecidos[0])
        notas.append(f"argument '{desconhecidos[0]}' -> '{faltando[0]}'")

    for chave, valor in list(argumentos.items()):
        tipo = (propriedades.get(chave) or {}).get("type")
        try:
            if tipo == "integer" and not isinstance(valor, int):
                argumentos[chave] = int(round(float(str(valor).strip())))
            elif tipo == "number" and isinstance(valor, str):
                argumentos[chave] = float(valor.strip())
            elif tipo == "boolean" and not isinstance(valor, bool):
                argumentos[chave] = _booleano(valor)
            elif tipo == "string" and isinstance(valor, (int, float)) and not isinstance(valor, bool):
                argumentos[chave] = str(valor)
            elif tipo == "array" and isinstance(valor, str):
                argumentos[chave] = [x.strip() for x in re.split(r"[+,]", valor) if x.strip()]
        except (TypeError, ValueError):
            pass
    return nome, argumentos, ("openTARS fixed the call: " + "; ".join(notas) + ".") if notas else None


def executar_ferramenta(nome, argumentos):

    nome, argumentos, conserto = consertar_chamada(nome, argumentos)
    inicio = time.time()

    emitir("ferramenta", nome=nome, resumo=_resumo_argumentos(argumentos))

    handler = _DISPATCH_FERRAMENTAS.get(nome)

    try:

        if handler is not None:
            resultado = handler(argumentos)
        else:
            resultado = {
                "sucesso": False,
                "mensagem": f"Unknown tool: {nome}",
            }

    except Exception as e:

        resultado = {
            "sucesso": False,
            "mensagem": f"Internal error in the tool '{nome}': {e}",
        }

    # Numa sessão Wayland, o pyautogui "consegue" mover/clicar/digitar
    # sem erro nenhum, mas o evento só chega a janelas XWayland. Avisa
    # a IA pra ela poder explicar ao usuário em vez de fingir sucesso.
    if SESSAO_GRAFICA == "wayland" and nome in _FERRAMENTAS_MOUSE_TECLADO:
        resultado["mensagem"] = (
            f"{resultado.get('mensagem', '')} {AVISO_WAYLAND_FERRAMENTA}"
        ).strip()

    if conserto:
        resultado["mensagem"] = f"{resultado.get('mensagem', '')} ({conserto})".strip()

    tempo = time.time() - inicio

    emitir("ferramenta_fim", nome=nome, sucesso=bool(resultado.get("sucesso")), segundos=tempo)

    LOG.info(
        "ferramenta=%s args=%s sucesso=%s tempo=%.2fs mensagem=%s",
        nome,
        _resumir_para_log(argumentos),
        resultado.get("sucesso"),
        tempo,
        _resumir_para_log(resultado.get("mensagem", ""))[:LIMITE_LOG_CAMPO_CHARS],
    )

    return resultado


# ============================================================
# SELEÇÃO DE MODELO
# ============================================================
#
# As palavras-chave de cada categoria vêm dos arquivos de idioma
# (detectar.<categoria>) e são juntadas uma vez em _vocab(): as do idioma
# escolhido, as dos idiomas do sistema e as do inglês. Aqui ficam só as
# que não mudam com o idioma (marcas e nomes técnicos).

_PALAVRAS_NEUTRAS = {
    "visao": ("mouse",),
    "fechar": (),
    "busca": (),
    "sites": ("youtube", "chatgpt", "google", "spotify", "discord", "steam", "brave", "firefox"),
    "abrir": (),
    "codigo": (
        "python", "javascript", "typescript", "java", "rust", "golang", "html", "css", "sql",
        "regex", "json", "script", "debug", "bug", "stacktrace", "kotlin", "php",
    ),
    "tecnico": (
        "linux", "terminal", "bash", "shell", "ubuntu", "zorin", "kernel", "driver", "gpu",
        "cuda", "ollama", "docker", "git", "apt", "sudo", "systemd", "grub", "nvidia",
    ),
    "acao": (),
}

# Ordem de checagem. Verbo ou alvo EXPLÍCITO ("feche", "abra", "youtube")
# vem antes de palavra-tópico solta: "abra o terminal" é uma AÇÃO (o
# "terminal" é só o alvo) e "abra o vs code" também — antes isso caía em
# "técnico" e ia pro modelo de programação, que não sabe abrir app. BUSCA
# vem antes de SITES: "pesquise X no google" precisa escolher entre
# search_web e open_website, o que o modelo minúsculo não faz direito.
# CÓDIGO (escrever/consertar programa) vem antes de TÉCNICO (Linux,
# sistema): só código vai pro especialista em programação.
_ORDEM_DETECCAO = (
    ("visao", "visao"),
    ("fechar", "acao"),
    ("busca", "busca"),
    ("sites", "simples"),
    ("abrir", "acao"),
    ("codigo", "codigo"),
    ("tecnico", "tecnico"),
    ("acao", "acao"),
)

# Limite de palavras pra considerar uma frase "conversa curta" (sem
# nenhuma das categorias acima) — bate-papo bem simples também cai no
# mesmo balde de modelo leve que abrir URL, em vez de sempre acordar o
# modelo mais pesado só pra dizer "oi".
_LIMITE_PALAVRAS_CONVERSA_SIMPLES = 6

# Até aqui, nem pergunta pro ajudante: é cumprimento/agradecimento.
_LIMITE_PALAVRAS_SEM_CLASSIFICAR = 3

# A partir de quantas palavras um pedido que cita um site ("youtube")
# deixa de ser "abra o site" e vira uma busca nele.
_PALAVRAS_SITE_COM_BUSCA = 5
# Só sites onde "X no site" é procurar algo (no Spotify/Discord/Steam o
# pedido longo costuma ser uma ação no app: "spotify pausa a música").
_SITES_DE_BUSCA = {"youtube", "google", "wikipedia", "reddit", "amazon", "github", "bing",
                   "duckduckgo", "stackoverflow", "mercadolivre", "netflix", "twitch"}

_CATEGORIAS_VALIDAS = ("visao", "codigo", "tecnico", "acao", "busca", "simples", "geral")

# Exemplos (few-shot) em vários idiomas: num modelo de 0.5B, mostrar uns
# casos resolvidos acerta bem mais do que só descrever as categorias. Não
# repetem as frases do --avaliar-classificador, pra não inflar a nota.
_PROMPT_CLASSIFICADOR = """You classify requests sent to an assistant that controls a Linux computer. The request may be in any language.
Categories:
- visao: needs to SEE the screen or use the interface (look, read, click, drag, mouse)
- codigo: writing, explaining or fixing program code (Python, JavaScript, a script, a function, a bug)
- tecnico: Linux, terminal, drivers, installing software, system errors and configuration
- acao: do something on the computer (open/close programs, files, folders, volume, network)
- busca: look something up on the internet
- simples: greeting, thanks, or opening a well-known website/app
- geral: knowledge question, writing, ideas, conversation

Examples:
"dá uma olhada nessa janela e me diz o que ela quer" -> visao
"click the green button at the bottom" -> visao
"escreve uma função que inverte uma string" -> codigo
"why does my loop never stop in javascript" -> codigo
"o pip reclama de externally managed environment" -> tecnico
"how do I see yesterday's systemd logs" -> tecnico
"apaga os arquivos temporários da área de trabalho" -> acao
"mute the sound" -> acao
"encontra reviews do galaxy s25 pra mim" -> busca
"¿cuánto está el dólar hoy?" -> busca
"opa, beleza?" -> simples
"open instagram" -> simples
"por que o céu é azul" -> geral
"help me write an email asking for time off" -> geral

Request: """

_FORMATO_CATEGORIA = {
    "type": "object",
    "properties": {"categoria": {"type": "string", "enum": list(_CATEGORIAS_VALIDAS)}},
    "required": ["categoria"],
}


_DESCRICAO_CATEGORIAS = {
    "visao": "needs to SEE the screen or use the interface (look, read, click, drag)",
    "codigo": "writing, explaining or fixing program code",
    "tecnico": "Linux, terminal, drivers, installing software, system errors and configuration",
    "acao": "do something on the computer (open/close programs, files, folders, volume, network, type in an app)",
    "busca": "look something up on the internet (videos, prices, news, reviews, tutorials)",
    "simples": "greeting, thanks, or just opening a well-known website/app",
    "geral": "knowledge question, writing, ideas, conversation",
}


# O que o ajudante pensou na última arbitragem (mostrado no --explicar).
_ultimo_motivo_ajudante = {"motivo": None}


def _classificar_com_ia(texto, finalistas=None, anterior=None, pistas=None):
    """Segunda camada de detecção de tarefa, só usada quando nenhuma
    palavra-chave reconheceu nada — só entra nos casos ambíguos, pra não
    pagar o custo extra na maioria das mensagens. Usa o modelo ajudante
    (minúsculo, fora da escolha de conversa); se ele não estiver
    instalado ou a chamada falhar, devolve None e quem chamou cai no
    comportamento padrão."""

    try:
        instalados = listar_modelos_instalados()
    except Exception:
        instalados = None

    if not instalados or MODELO_AJUDANTE not in instalados:
        return None

    _ultimo_motivo_ajudante["motivo"] = None
    if finalistas:
        # As camadas anteriores já reduziram a 2–3 opções: o ajudante só
        # desempata, com a descrição de cada uma (bem mais fácil pra um
        # modelo de 0,5B do que escolher entre 7), com as PISTAS que as
        # outras camadas acharam, e escrevendo antes o que o usuário quer
        # (o "motivo" vem antes da categoria no JSON: ele pensa, depois
        # escolhe).
        prompt = (
            "Classify this request to an assistant that controls a Linux computer. "
            "Choose ONE of these categories:\n"
            + "\n".join(f"- {c}: {_DESCRICAO_CATEGORIAS.get(c, c)}" for c in finalistas)
            + ("\n\nClues from faster checks (they can be wrong):\n" + "\n".join(f"- {p}" for p in pistas)
               if pistas else "")
            + (f"\n(The previous request was '{anterior}'.)" if anterior else "")
            + "\n\nFirst write in 'motivo' what the user wants done, in a few words; then choose."
            + f'\n\nRequest: "{texto}"'
        )
        formato = {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "maxLength": 120},
                "categoria": {"type": "string", "enum": list(finalistas)},
            },
            "required": ["motivo", "categoria"],
        }
    else:
        prompt, formato = _PROMPT_CLASSIFICADOR + f'"{texto}" ->', _FORMATO_CATEGORIA

    bruto = _chat_unico(
        MODELO_AJUDANTE,
        prompt,
        temperatura=TEMPERATURA_CLASSIFICADOR,
        timeout=TIMEOUT_CLASSIFICADOR,
        keep_alive=KEEP_ALIVE_AJUDANTE,
        formato=formato,
    )

    if not bruto:
        return None

    dados = _json_da_resposta(bruto)
    validas = finalistas or _CATEGORIAS_VALIDAS
    if isinstance(dados, dict) and dados.get("categoria") in validas:
        motivo = dados.get("motivo")
        _ultimo_motivo_ajudante["motivo"] = str(motivo)[:160] if motivo else None
        return dados["categoria"]

    # Ollama antigo (sem resposta forçada): procura a categoria no texto.
    # Com motivo antes, a escolha costuma ser a ÚLTIMA categoria citada.
    bruto_normalizado = normalizar(bruto)
    citadas = [(bruto_normalizado.rfind(c), c) for c in validas if c in bruto_normalizado]
    return max(citadas)[1] if citadas else None


def _classificar_por_embedding(texto, esperar=False):
    """Tarefa pelo significado (tars_embeddings), ou None. Não espera o
    índice ficar pronto (a não ser com esperar=True): na primeira vez ele
    é montado em segundo plano e, enquanto isso, o ajudante responde."""
    modelo = modelo_embedding()
    if not modelo:
        return None
    if not embeddings.pronto(modelo):
        if not esperar:
            embeddings.preparar_em_segundo_plano(modelo)
            return None
        if not embeddings.preparar(modelo):
            return None
    try:
        return embeddings.classificar(texto, modelo)
    except Exception:
        return None


def tarefa_por_palavras(texto):
    """Categoria só pelas palavras-chave (instantâneo, sem IA), ou None.
    Palavra solta bate como palavra inteira ("git" não bate em "digital");
    expressão com espaço bate como trecho."""

    t_norm = normalizar(texto)
    lista_tokens = [x for x in _RE_TOKENS.split(t_norm) if x]
    tokens = set(lista_tokens)
    for grupo, categoria in _ORDEM_DETECCAO:
        palavras, expressoes = _vocab()["detectar"][grupo]
        if tokens & palavras or any(e in t_norm for e in expressoes):
            # "abra o youtube" é só abrir o site (modelo leve); "vídeos de
            # receita de lasanha no youtube" tem o que procurar lá: busca
            # (o modelo minúsculo não sabe montar a pesquisa).
            if grupo == "sites" and len(lista_tokens) >= _PALAVRAS_SITE_COM_BUSCA:
                # Pedido longo citando um app/site: procurar algo nele
                # (youtube, google...) ou fazer algo nele (spotify, discord).
                return "busca" if tokens & _SITES_DE_BUSCA else "acao"
            return categoria
    return None


# Acertos/falhas de cada modelo por tarefa neste PC (tars_escolha.Historico).
def _caminho_historico():
    if os.environ.get("TARS_ARQUIVO_HISTORICO"):
        return Path(os.environ["TARS_ARQUIVO_HISTORICO"])
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "opentars" / "historico_modelos.json"


historico_modelos = escolha.Historico(_caminho_historico())


def _tarefa_do_historico(tarefa):
    """'acao_complexa' é a mesma tarefa 'acao' com outro modelo na frente."""
    return "acao" if tarefa == "acao_complexa" else tarefa


# Última tarefa decidida (camada de contexto): "agora clica no =" logo
# depois de "abra a calculadora" continua sendo uma ação.
_ultima_tarefa = {"categoria": None, "ts": 0.0}
_ultima_decisao = {"d": None}

# Nomes de app instalados (camada 4): cacheados junto com a varredura dos
# .desktop. Só o nome principal, com 4+ letras ("Spotify", "GIMP").
_cache_nomes_apps = {"ts": None, "nomes": frozenset()}


def _nomes_de_apps():
    entradas = desktop_entries()
    if _cache_nomes_apps["ts"] != _cache_desktop.get("ts"):
        nomes = set()
        for e in entradas or []:
            n = normalizar(e.get("nome") or "")
            if len(n) >= 4 and n not in _NOMES_APP_GENERICOS:
                nomes.add(n)
        _cache_nomes_apps.update(ts=_cache_desktop.get("ts"), nomes=frozenset(nomes))
    return _cache_nomes_apps["nomes"]


_NOMES_APP_GENERICOS = {"files", "arquivos", "settings", "configuracoes", "help", "ajuda", "text", "texto",
                        "terminal", "videos", "music", "musica", "photos", "fotos", "maps", "mapas",
                        "weather", "clima", "clock", "relogio", "calendar", "calendario", "contacts"}


def _cita_app(t_norm, tokens):
    for nome in _nomes_de_apps():
        if (" " in nome and nome in t_norm) or nome in tokens:
            return nome
    return None


def _votos_embedding(texto, votos):
    """Camada 5: o SENTIDO. Decidido (margem calibrada) vale mais; em
    dúvida, as duas mais parecidas viram finalistas. Devolve as notas
    (pra trilha e pras pistas do ajudante) ou None."""
    modelo = modelo_embedding()
    if not modelo:
        return None
    if not embeddings.pronto(modelo):
        embeddings.preparar_em_segundo_plano(modelo)
        return None
    try:
        notas = embeddings.notas(texto, modelo)
    except Exception:
        notas = None
    if not notas:
        return None
    info = embeddings.info(modelo)
    margem = info["margem"] if info else 1.0
    certo = len(notas) == 1 or notas[0][0] - notas[1][0] >= margem
    if certo:
        votos.dar(notas[0][1], escolha.PESO_EMBEDDING_CERTO, "embeddings")
    else:
        votos.dar(notas[0][1], escolha.PESO_EMBEDDING_DUVIDA, "embeddings")
        votos.dar(notas[1][1], escolha.PESO_EMBEDDING_DUVIDA * 0.6, "embeddings")
    return {"notas": notas[:3], "certo": certo, "margem": margem}


def _verbos_de_acao():
    return _vocab()["verbos_acao"]


def decidir_tarefa(texto, anterior=None):
    """A tarefa do pedido, decidida em camadas (ver tars_escolha.py).
    Devolve uma escolha.Decisao (categoria, via, votos, finalistas,
    várias etapas e a trilha do raciocínio, mostrada no --explicar).

    anterior: a tarefa do pedido anterior (camada de contexto). Sem ela,
    usa a última lembrada por lembrar_tarefa (até 5 minutos)."""

    t_norm = normalizar(texto)
    tokens = set(_RE_TOKENS.split(t_norm))
    palavras = len(t_norm.split())
    votos = escolha.Votos()
    trilha = []
    partes = escolha.partes_do_pedido(t_norm, _verbos_de_acao())
    multi = len(partes) >= 2

    def decisao(categoria, via):
        return escolha.Decisao(categoria, via, dict(votos.soma), votos.finalistas(_CATEGORIAS_VALIDAS),
                               multi_etapas=multi, trilha=trilha)

    # 1. Palavras-chave
    por_palavras = tarefa_por_palavras(texto)
    votos.dar(por_palavras, escolha.PESO_PALAVRA, "palavras")
    trilha.append(("palavras", por_palavras or "-"))

    # 2. Contexto: continuação curta do pedido anterior
    if anterior is None and time.time() - _ultima_tarefa["ts"] < escolha.VALIDADE_CONTEXTO_SEG:
        anterior = _ultima_tarefa["categoria"]
    if anterior and escolha.eh_continuacao(t_norm):
        votos.dar(anterior, escolha.PESO_CONTEXTO, "contexto")
        trilha.append(("contexto", f"{anterior} (continua o pedido anterior)"))

    # 3. Estrutura (código, erro, comando, link, pergunta)
    antes = dict(votos.soma)
    escolha.votos_estruturais(texto, t_norm, votos)
    estrutura = {c: v - antes.get(c, 0.0) for c, v in votos.soma.items() if v != antes.get(c, 0.0)}
    if estrutura:
        trilha.append(("estrutura", ", ".join(f"{c}+{v:.1f}" for c, v in estrutura.items())))
    if multi:
        trilha.append(("etapas", " | ".join(partes)))

    # Palavra-chave sem nada contra: decide já (sem gastar embeddings).
    clara = votos.decisao_clara()
    if clara and por_palavras and len(votos.soma) == 1:
        return decisao(clara[0], clara[1])

    # "oi", "valeu", "bom dia": nem precisa perguntar.
    if not votos.soma and palavras <= _LIMITE_PALAVRAS_SEM_CLASSIFICAR:
        trilha.append(("curto", "simples"))
        return decisao("simples", "curto")

    # 4. Cita um app instalado
    app_citado = None
    try:
        app_citado = _cita_app(t_norm, tokens)
    except Exception:
        pass
    if app_citado:
        votos.dar("acao", escolha.PESO_APP, "apps")
        trilha.append(("apps", app_citado))

    # 5. Embeddings
    emb = _votos_embedding(texto, votos)
    if emb:
        notas = ", ".join(f"{c} {n:.2f}" for n, c in emb["notas"])
        trilha.append(("embeddings", f"{notas} ({'decidido' if emb['certo'] else 'em dúvida'}, "
                                     f"margem {emb['margem']:.3f})"))

    clara = votos.decisao_clara()
    if clara:
        categoria, via = clara
    else:
        # 6. Ajudante, escolhendo só entre as finalistas, com as pistas
        finalistas = votos.finalistas(_CATEGORIAS_VALIDAS)
        if len(finalistas) == 1 and votos.soma[finalistas[0]] >= escolha.MINIMO_DECISAO:
            categoria, via = finalistas[0], "+".join(votos.origem[finalistas[0]])
        else:
            pistas = []
            if por_palavras:
                pistas.append(f"keywords point to: {por_palavras}")
            if estrutura:
                pistas.append("its format suggests: " + ", ".join(estrutura))
            if app_citado:
                pistas.append(f"it names the installed app '{app_citado}'")
            if emb:
                seguintes = [c for _, c in emb["notas"][1:2]]
                pistas.append(f"its meaning is closest to: {emb['notas'][0][1]}"
                              + (f" (then {seguintes[0]})" if seguintes else ""))
            if multi:
                pistas.append("it asks for several actions in a row")
            usar = finalistas if len(finalistas) >= 2 else None
            categoria = _classificar_com_ia(texto, finalistas=usar, anterior=anterior,
                                            **({"pistas": pistas} if usar and pistas else {}))
            via = "ajudante"
            trilha.append(("ajudante", f"{categoria or '-'} entre {', '.join(usar or _CATEGORIAS_VALIDAS)}"
                                       + (f" — \"{_ultimo_motivo_ajudante['motivo']}\""
                                          if _ultimo_motivo_ajudante.get("motivo") else "")))
            if not categoria:
                if finalistas:
                    categoria, via = finalistas[0], "melhor_voto"
                else:
                    categoria = "simples" if palavras <= _LIMITE_PALAVRAS_CONVERSA_SIMPLES else "geral"
                    via = "padrao"

    # 7. Coerência
    corrigida, motivo = escolha.coerencia(categoria, t_norm, votos)
    if motivo:
        trilha.append(("coerencia", f"{categoria} -> {corrigida}"))
        categoria, via = corrigida, f"{via}+{motivo}"
    return decisao(categoria, via)


def detectar_tarefa(texto):
    decisao = decidir_tarefa(texto)
    _ultima_decisao["d"] = decisao
    LOG.info("tarefa=%s", decisao.resumo())
    return decisao.categoria


def lembrar_tarefa(categoria):
    """Guarda a tarefa do pedido atual pra camada de contexto do próximo."""
    _ultima_tarefa.update(categoria=categoria, ts=time.time())


# Raciocínio ("think") do modelo: ajuda em programação e perguntas
# difíceis, mas numa ação simples ("fecha o claude") só atrasa — no log
# real, 31 s pra fechar um app, quase tudo pensando. Pedido longo em
# qualquer categoria costuma ter várias etapas: aí o raciocínio volta.
# TARS_PENSAR=sempre|nunca força um lado.
_TAREFAS_COM_RACIOCINIO = {"codigo", "tecnico", "geral"}
_PALAVRAS_PEDIDO_LONGO = 20


def deve_pensar(tarefa, texto="", multi_etapas=False):
    modo = normalizar(os.environ.get("TARS_PENSAR") or "auto")
    if modo in ("sempre", "always", "1", "on", "sim", "yes"):
        return True
    if modo in ("nunca", "never", "0", "off", "nao", "no"):
        return False
    if multi_etapas:
        # "abre X e faz Y": planejar antes evita parar na primeira etapa.
        return True
    if tarefa is None:
        tarefa = tarefa_por_palavras(texto)
    if tarefa is None or tarefa in _TAREFAS_COM_RACIOCINIO:
        return True
    return len((texto or "").split()) >= _PALAVRAS_PEDIDO_LONGO


def avaliar_classificador():
    """opentars --avaliar-classificador: mede, no Ollama deste PC, quanto
    os embeddings e o modelo ajudante acertam ao classificar pedidos
    (frases do idioma escolhido; diferentes dos exemplos)."""

    frases = [tuple(par) for par in i18n.lista("avaliacao") if isinstance(par, list) and len(par) == 2]
    print(c(f"\n{t('avaliacao.titulo', modelo=MODELO_AJUDANTE, n=len(frases))}\n", Cor.CIANO))

    if not ollama_online():
        print(c(t("aviso.ollama_offline", host=OLLAMA_HOST), Cor.VERMELHO))
        return 1
    instalados = listar_modelos_instalados(forcar=True)
    tem_ajudante = MODELO_AJUDANTE in instalados
    if not tem_ajudante and not modelo_embedding():
        print(c(t("avaliacao.sem_ajudante", modelo=MODELO_AJUDANTE), Cor.VERMELHO))
        return 1

    r = medir_classificador(frases)
    n = r["n"]

    def pct(x, total=None):
        return f"{100 * x / max(1, total if total is not None else n):.0f}"

    if r["modelo_emb"]:
        print(t("avaliacao.so_embedding", modelo=r["modelo_emb"], acertos=r["acertos_emb"], n=n,
                pct=pct(r["acertos_emb"]), decididos=r["decididos_emb"],
                pct_decididos=pct(r["acertos_emb"], r["decididos_emb"]), ms=f"{r['ms_emb']:.0f}"))
    else:
        print(c(t("avaliacao.sem_embedding", modelo=embeddings.MODELO_PADRAO), Cor.AMARELO))
    if tem_ajudante:
        print(t("avaliacao.so_ajudante", acertos=r["acertos_ia"], n=n, pct=pct(r["acertos_ia"])))
        print(t("avaliacao.tempo_medio", ms=f"{r['ms_medio']:.0f}"))
    print(t("avaliacao.com_palavras", acertos=r["acertos_total"], n=n, pct=pct(r["acertos_total"])) + "\n")

    for cat, (ok, total) in r["por_categoria"].items():
        if total:
            print(f"  {cat:<8} {ok}/{total}")

    for chave, erros in (("avaliacao.erros_embedding", r["erros_emb"]), ("avaliacao.erros", r["erros"] if tem_ajudante else [])):
        if erros:
            print(c("\n" + t(chave), Cor.AMARELO))
            for frase, esperado, resposta in erros:
                print(f"  {frase} | {esperado} | {resposta}")
    print()
    return 0


def explicar_escolha(texto):
    """opentars --explicar "pedido": mostra camada por camada como a
    tarefa é decidida e qual fila de modelos atende (com o placar de cada
    um neste PC). Não manda nada pra IA de conversa."""

    if not texto.strip():
        print(t("explicar.uso"))
        return 2
    print(c(f"\n{t('explicar.titulo', pedido=texto)}\n", Cor.CIANO))

    online = ollama_online()
    if online:
        catalogar_modelos(forcar=True)
        modelo_emb = modelo_embedding()
        if modelo_emb:
            embeddings.preparar(modelo_emb)
    else:
        print(c(t("aviso.ollama_offline", host=OLLAMA_HOST), Cor.AMARELO) + "\n")

    d = decidir_tarefa(texto)
    print(t("explicar.camadas"))
    for camada, conclusao in d.trilha:
        print(f"  {camada:<11} {conclusao}")
    votos = ", ".join(f"{cat}={v:.1f}" for cat, v in sorted(d.votos.items(), key=lambda x: -x[1])) or "-"
    print(t("explicar.votos", votos=votos))
    print(c(t("explicar.resultado", tarefa=f"{nome_tarefa(d.categoria)} ({d.categoria})", via=d.via), Cor.VERDE))
    if d.multi_etapas:
        partes = escolha.partes_do_pedido(normalizar(texto), _verbos_de_acao())
        print(t("explicar.multi", partes=" | ".join(partes)))

    if online:
        tarefa_hist = _tarefa_do_historico(d.categoria)
        print("\n" + t("explicar.fila"))
        fila = construir_cadeia_fallback(tarefa_da_fila(d.categoria, d.multi_etapas))[:6]
        if not fila:
            print(f"  {t('aviso.sem_modelo_conversa')}")
        for i, tag in enumerate(fila, 1):
            acertos, total = historico_modelos.placar(tag, tarefa_hist)
            if not total:
                placar = t("explicar.sem_placar")
            elif historico_modelos.ruim(tag, tarefa_hist):
                placar = t("explicar.placar_ruim", acertos=acertos, total=total)
            else:
                placar = t("explicar.placar", acertos=acertos, total=total)
            print(f"  {i}. {tag:<30} {placar}")

    pensa = deve_pensar(d.categoria, texto, d.multi_etapas)
    print("\n" + t("explicar.pensar", sim_nao=t("explicar.sim") if pensa else t("explicar.nao"),
                   n=len(ferramentas_para(d.categoria))) + "\n")
    return 0


def medir_classificador(frases):
    """Classifica as frases pelos embeddings, pelo ajudante e pelo caminho
    completo que o modo AUTO usa. Usado pelo --avaliar-classificador e
    pelo --autoteste. por_categoria conta o caminho completo."""

    catalogar_modelos(forcar=True)
    modelo_emb = modelo_embedding()
    if modelo_emb and not embeddings.preparar(modelo_emb):
        modelo_emb = None
    if modelo_emb:
        embeddings.notas("hello, how are you today?", modelo_emb)  # carrega o modelo antes de medir
    tem_ajudante = MODELO_AJUDANTE in (listar_modelos_instalados() or ())
    if tem_ajudante:
        _classificar_com_ia("hello, how are you today?")

    acertos_ia = acertos_total = acertos_emb = decididos_emb = 0
    tempos, tempos_emb = [], []
    por_categoria = {cat: [0, 0] for cat in _CATEGORIAS_VALIDAS}
    erros, erros_emb = [], []

    for frase, esperado in frases:
        if modelo_emb:
            inicio = time.time()
            resposta_emb = embeddings.classificar(frase, modelo_emb)
            tempos_emb.append(time.time() - inicio)
            if resposta_emb:
                decididos_emb += 1
            if resposta_emb == esperado:
                acertos_emb += 1
            else:
                erros_emb.append((frase, esperado, resposta_emb or "?"))
        if tem_ajudante:
            inicio = time.time()
            resposta_ia = _classificar_com_ia(frase)
            tempos.append(time.time() - inicio)
            if resposta_ia == esperado:
                acertos_ia += 1
            else:
                erros.append((frase, esperado, resposta_ia))

        final = detectar_tarefa(frase)
        por_categoria.setdefault(esperado, [0, 0])[1] += 1
        if final == esperado:
            acertos_total += 1
            por_categoria[esperado][0] += 1

    return {
        "n": len(frases),
        "acertos_ia": acertos_ia,
        "acertos_total": acertos_total,
        "ms_medio": 1000 * sum(tempos) / max(1, len(tempos)),
        "por_categoria": por_categoria,
        "erros": erros,
        "modelo_emb": modelo_emb,
        "acertos_emb": acertos_emb,
        "decididos_emb": decididos_emb,
        "ms_emb": 1000 * sum(tempos_emb) / max(1, len(tempos_emb)),
        "erros_emb": erros_emb,
    }


def proximo_modelo_disponivel(tarefa, excluir=()):
    """Devolve o modelo mais indicado pra essa categoria de tarefa,
    dentre os que estão de fato instalados — construindo a cadeia de
    fallback dinamicamente a partir do catálogo (ver
    construir_cadeia_fallback), não de uma lista fixa."""

    cadeia = construir_cadeia_fallback(tarefa)

    for candidato in cadeia:
        if candidato not in excluir:
            return candidato

    return MODELO_PADRAO


def tarefa_da_fila(tarefa, complexa=False):
    """A fila de modelos usada pra tarefa. Ação/busca com várias etapas
    ("abre o claude e faz uma pergunta") vai pra fila do maior modelo que
    roda bem: o menorzinho se perde no meio do caminho."""
    if complexa and tarefa in ("acao", "busca"):
        return "acao_complexa"
    return tarefa


def escolher_modelo(texto, tarefa=None, complexa=False):

    global modo_modelo
    global modelo_manual

    if modo_modelo == "MANUAL" and modelo_manual:
        return modelo_manual

    if tarefa is None:
        tarefa = detectar_tarefa(texto)

    return proximo_modelo_disponivel(tarefa_da_fila(tarefa, complexa))


# ============================================================
# COMANDOS DE MODELO
# ============================================================

def mostrar_modelos():

    print()
    print(t("term.ias_disponiveis"))
    print()

    _imprimir_lista_modelos()

    print(t("term.modo_atual", modo=modo_modelo))

    if modo_modelo == "MANUAL":
        print(t("term.modelo_fixado", modelo=modelo_manual))
    else:
        print(t("term.modo_auto_explica"))


def _contem_palavra_inteira(t_norm, expressoes):
    """Alguma das expressões aparece em t_norm (já normalizado) como
    palavra inteira? Um `in` ingênuo faz "git" bater dentro de "digital".
    Expressões com espaço são comparadas como trecho."""

    tokens = None  # calculado só se precisar
    for expr in expressoes:
        if " " in expr:
            if expr in t_norm:
                return True
        else:
            if tokens is None:
                tokens = set(_RE_TOKENS.split(t_norm))
            if expr in tokens:
                return True
    return False


# "o menor", "the smallest", "el más grande", "o de visão"...: resolvem pra
# um modelo do catálogo por critério, em vez de nome. As frases de cada
# idioma vêm dos arquivos de idioma (modelo.menor, modelo.maior...).
_CRITERIOS_MODELO = {
    "modelo.menor": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "modelo.maior": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "modelo.visao": lambda cat: _melhor_por_capacidade(cat, "vision"),
    "modelo.tecnico": lambda cat: _melhor_por_categoria(cat, "codigo"),
}


def _extremo_por_tamanho(catalogo, menor):
    candidatos = [m for m in catalogo.values() if m["parametros_b"] is not None]

    if not candidatos:
        candidatos = list(catalogo.values())

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 0,
        reverse=not menor,
    )

    return candidatos[0]["tag"]


def _melhor_por_capacidade(catalogo, capacidade):
    candidatos = [m for m in catalogo.values() if capacidade in m["capacidades"]]

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 0,
        reverse=True,
    )

    return candidatos[0]["tag"]


def _melhor_por_categoria(catalogo, categoria):
    candidatos = [m for m in catalogo.values() if m["categoria"] == categoria]

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 0,
        reverse=True,
    )

    return candidatos[0]["tag"]


def _tabela_resolucao_modelos(catalogo):
    """Tabela de 'texto normalizado' → tag do modelo, construída a
    partir do que está REALMENTE instalado (catalogo), não de uma
    lista fixa. Cada modelo instalado entra por: sua tag exata, a tag
    sem a versão/tamanho (ex: "qwen3" pra "qwen3:8b", quando único), e
    sua família reportada pelo Ollama."""

    tabela = {}

    contagem_prefixo = {}
    for tag in catalogo:
        prefixo = normalizar(tag.split(":")[0])
        contagem_prefixo[prefixo] = contagem_prefixo.get(prefixo, 0) + 1

    contagem_familia = {}
    for info in catalogo.values():
        fam_n = normalizar(info["familia"])
        if fam_n:
            contagem_familia[fam_n] = contagem_familia.get(fam_n, 0) + 1

    for tag, info in catalogo.items():

        tabela[normalizar(tag)] = tag

        prefixo = normalizar(tag.split(":")[0])
        if contagem_prefixo.get(prefixo) == 1:
            tabela[prefixo] = tag

        fam_n = normalizar(info["familia"])
        if fam_n and contagem_familia.get(fam_n) == 1:
            tabela[fam_n] = tag

    return tabela


def _resolver_alvo_modelo(alvo, catalogo):
    """Resolve um ALVO explícito de troca de modelo — o texto depois de
    '/modelo ', ou uma mensagem inteira que já é só o nome do modelo —
    contra o catálogo real: tag/família/prefixo exatos, ou um
    superlativo ('o menor', 'o de visão'). Retorna a tag ou None.

    IMPORTANTE: isso nunca é chamado com uma frase de tarefa qualquer
    do chat (ver interpretar_comando_modelo) — só com algo que o
    próprio usuário já indicou explicitamente ser um nome/critério de
    modelo, então não precisa (e não deve) tentar advinhar intenção a
    partir de verbos genéricos como 'usar' ou 'quero': foi exatamente
    esse tipo de heurística sobre texto livre que causava trocas de
    modelo indevidas em pedidos comuns como 'use suas ferramentas para
    abrir o youtube'."""

    if not catalogo:
        return None

    tabela = _tabela_resolucao_modelos(catalogo)

    if alvo in tabela:
        return tabela[alvo]

    for frase, resolvedor in _vocab()["superlativos"].items():
        if frase in alvo:
            resultado = resolvedor(catalogo)
            if resultado:
                return resultado

    return None


def _squash(texto):
    """Reduz um texto a só letras/números minúsculos, sem acento,
    espaço ou pontuação — pra comparar "gemma 270m" com a tag real
    "gemma3:270m" sem se importar com onde tem espaço, dois pontos ou
    um "3" que o usuário engoliu."""

    return re.sub(r"[^a-z0-9]", "", normalizar(texto))


def _resolver_modelo_por_texto_livre(texto_livre, catalogo):
    """Casamento mais tolerante que _resolver_alvo_modelo: usado
    quando o usuário já deixou explícito que quer trocar de modelo
    (comando "/modelo <algo>"), então não
    exige reconhecer a tag/família ao pé da letra — ignora pontuação e
    espaços, então "gemma 270m" casa com a tag "gemma3:270m" mesmo sem
    o "3" e sem o ":"."""

    if not catalogo or not texto_livre.strip():
        return None

    alvo = _squash(texto_livre)

    if not alvo:
        return None

    melhor_tag = None
    melhor_pontuacao = 0

    for tag, info in catalogo.items():
        for candidato in (tag, info.get("familia", "") or ""):
            squash = _squash(candidato)
            if squash and (alvo in squash or squash in alvo):
                if len(squash) > melhor_pontuacao:
                    melhor_pontuacao = len(squash)
                    melhor_tag = tag

    if melhor_tag:
        return melhor_tag

    # Ainda nada — tenta por partes: cada "palavra" relevante do texto
    # (2+ caracteres) precisa aparecer na tag, mesmo que fora de ordem
    # (ex: "o modelo 270m da gemma" ainda acha "gemma3:270m").
    tokens = [
        tok for tok in re.split(r"[^a-z0-9]+", normalizar(texto_livre))
        if len(tok) >= 2 and tok not in _vocab()["modelo_ignorar"]
    ]

    if not tokens:
        return None

    for tag in catalogo:
        squash = _squash(tag)
        if all(tok in squash for tok in tokens):
            return tag

    return None


def _resolver_modelo_com_ia(texto_livre, catalogo):
    """Último recurso pra troca de modelo, só chamado quando nada bateu
    por palavra-chave nem por aproximação de texto (ver
    _resolver_modelo_por_texto_livre) — pergunta pro modelo
    classificador minúsculo (MODELO_CLASSIFICADOR, se estiver
    instalado) qual dos modelos JÁ INSTALADOS mais combina com o que
    foi pedido, dando a ele só a lista real de tags (nunca inventa um
    modelo que não existe). Se o classificador não estiver disponível,
    a chamada falhar ou a resposta não bater com nenhuma tag real,
    devolve None e quem chamou trata como "não encontrado"."""

    if not catalogo:
        return None

    try:
        instalados = listar_modelos_instalados()
    except Exception:
        instalados = None

    if not instalados or MODELO_AJUDANTE not in instalados:
        return None

    tags = sorted(catalogo.keys())

    prompt = (
        "The user wants to switch the AI model used by a local assistant. These "
        "are the ONLY installed models: pick the one that best matches the request "
        "and answer only with its exact tag, copied from the list, with no "
        "explanation. If none matches, or the request is not about switching "
        "models, answer exactly: none\n\n"
        f"Installed models: {', '.join(tags)}\n\n"
        f"User request: {texto_livre}"
    )

    bruto = _chat_unico(
        MODELO_AJUDANTE,
        prompt,
        temperatura=TEMPERATURA_CLASSIFICADOR,
        timeout=TIMEOUT_CLASSIFICADOR,
        keep_alive=KEEP_ALIVE_AJUDANTE,
        # Só pode responder um nome que existe de verdade (ou "nenhum").
        formato={
            "type": "object",
            "properties": {"modelo": {"type": "string", "enum": tags + ["none"]}},
            "required": ["modelo"],
        },
    )

    if not bruto:
        return None

    dados = _json_da_resposta(bruto)
    if isinstance(dados, dict) and "modelo" in dados:
        return dados["modelo"] if dados["modelo"] in tags else None

    bruto_n = normalizar(bruto)

    for tag in tags:
        if normalizar(tag) == bruto_n or normalizar(tag) in bruto_n:
            return tag

    return None


def _eh_comando(t_norm, chave):
    """O texto (já normalizado) é um dos comandos da chave, em qualquer
    idioma? Ex: "/limpar", "/clear", "/limpiar"."""
    return t_norm in _vocab()["comandos"].get(chave, ())


def comando_idioma(argumento):
    """/idioma: sem argumento, lista; com um código ou nome, troca."""
    argumento = (argumento or "").strip()
    disponiveis = i18n.idiomas_disponiveis()
    if not argumento:
        print()
        print(t("idioma.lista_titulo"))
        for codigo, nome in disponiveis.items():
            marca = "●" if codigo == i18n.idioma_atual() else " "
            print(f"  {c(marca, Cor.VERDE)} {codigo:<6} {nome}")
        print(c(t("idioma.como_trocar"), Cor.CINZA))
        return
    alvo = normalizar(argumento)
    codigo = next((cod for cod, nome in disponiveis.items() if normalizar(nome).startswith(alvo)), None)
    codigo = i18n.definir_idioma(codigo or argumento)
    if codigo:
        print(c("\n✓ " + t("idioma.trocado", nome=i18n.nome_idioma(codigo)), Cor.VERDE))
    else:
        print(c("\n✗ " + t("idioma.nao_encontrado", pedido=argumento, lista=", ".join(disponiveis)), Cor.AMARELO))


def interpretar_comando_modelo(texto):
    """Comandos do chat (/modelo, /limpar, /idioma, encerrar...). Devolve
    True se o texto era um comando (e já foi tratado)."""

    global modo_modelo
    global modelo_manual

    t_norm = normalizar(texto)

    if _eh_comando(t_norm, "cmd.modelos"):
        mostrar_modelos()
        return True

    if _eh_comando(t_norm, "cmd.encerrar"):
        encerrar_todas_ias()
        return True

    if _eh_comando(t_norm, "cmd.limpar"):
        limpar_sessao()
        print(c("\n✓ " + t("msg.historico_apagado"), Cor.VERDE))
        return True

    prefixo_idioma = next((p for p in _vocab()["comandos"].get("cmd.idioma", ()) if t_norm == p or t_norm.startswith(p + " ")), None)
    if prefixo_idioma:
        comando_idioma(texto.strip()[len(prefixo_idioma):])
        return True

    if _eh_comando(t_norm, "cmd.auto"):

        modo_modelo = "AUTO"
        modelo_manual = None

        print(c("\n✓ " + t("msg.modo_auto_ativado"), Cor.VERDE))
        return True

    catalogo = catalogar_modelos()

    def _selecionar_modelo(modelo_pedido):
        global modo_modelo, modelo_manual
        modo_modelo = "MANUAL"
        modelo_manual = modelo_pedido
        print(c("\n✓ " + t("msg.modelo_manual", modelo=modelo_pedido), Cor.VERDE))
        LOG.info("troca_de_modelo modelo=%s modo=MANUAL", modelo_pedido)

        # Avisa de cara se esse modelo não suporta ferramentas: ele nunca
        # vai conseguir abrir apps/sites/executar nada, só conversar.
        info = catalogo.get(modelo_pedido)
        if info and info.get("capacidades_conhecidas") and "tools" not in info.get("capacidades", set()):
            print(c("  ⚠ " + t("msg.modelo_sem_ferramentas", modelo=modelo_pedido), Cor.AMARELO))

        # Carrega já, em segundo plano: quando o usuário mandar a próxima
        # mensagem, o modelo já pode estar pronto.
        _carregar_em_segundo_plano(modelo_pedido)

    # "/modelo <algo>" — a forma explícita de trocar de modelo por texto.
    # Tenta em ordem: correspondência exata/critério ("o menor"),
    # aproximação tolerante a pontuação/espaço e, por fim (só porque o
    # usuário já pediu explicitamente), o ajudante. Trocar de modelo a
    # partir de uma frase qualquer do chat (sem o comando) foi removido
    # de propósito: "use suas ferramentas para abrir o youtube" chegou a
    # trocar o modelo antes.
    prefixo_modelo = next((p for p in _vocab()["comandos"].get("cmd.modelo", ()) if t_norm.startswith(p + " ")), None)
    if prefixo_modelo:
        alvo = t_norm[len(prefixo_modelo) + 1:].strip()

        if _eh_comando(alvo, "cmd.auto_argumento"):
            modo_modelo = "AUTO"
            modelo_manual = None
            print(c("\n✓ " + t("msg.modo_auto_ativado"), Cor.VERDE))
            return True

        modelo_pedido = (
            _resolver_alvo_modelo(alvo, catalogo)
            or _resolver_modelo_por_texto_livre(alvo, catalogo)
            or _resolver_modelo_com_ia(alvo, catalogo)
        )

        if modelo_pedido:
            _selecionar_modelo(modelo_pedido)
        else:
            instalados_fmt = ", ".join(sorted(catalogo)) if catalogo else "-"
            print(c("\n✗ " + t("msg.modelo_nao_encontrado", pedido=alvo, instalados=instalados_fmt), Cor.AMARELO))

        return True

    # Mensagem inteira IGUAL (não "contém") a uma tag/família/critério
    # também troca sem o comando: "o menor problema pode causar um erro"
    # contém "o menor", mas não É "o menor".
    tabela = _tabela_resolucao_modelos(catalogo)
    superlativos = _vocab()["superlativos"]

    if t_norm in tabela:
        modelo_pedido = tabela[t_norm]
    elif t_norm in superlativos:
        modelo_pedido = superlativos[t_norm](catalogo)
    else:
        modelo_pedido = None

    if modelo_pedido:
        _selecionar_modelo(modelo_pedido)
        return True

    # Uma tag do Ollama ("familia:tamanho") como mensagem inteira: o
    # usuário quis trocar pra um modelo que ainda não está instalado.
    if re.fullmatch(r"[a-z0-9][a-z0-9_./-]*:[a-z0-9][a-z0-9_.-]*", t_norm):
        modo_modelo = "MANUAL"
        modelo_manual = t_norm
        print(c("\n✓ " + t("msg.modelo_manual", modelo=t_norm), Cor.AMARELO)
              + c(" " + t("msg.modelo_nao_instalado", modelo=t_norm), Cor.AMARELO))
        return True

    return False


# ============================================================
# PROMPT DO TARS
# ============================================================

SYSTEM_PROMPT = """
You are openTARS, an assistant that runs locally on the user's Linux computer
and controls it through tools. Python executes the tools; you decide which
ones to use and in what order.

You CAN open and close programs, click, type, see the screen and run
commands: that is what the tools are for. Never answer that you can't do
these things, and never tell the user to do them by hand: call the tool.
Only if it fails (success=false) do you explain the error.

ACTIONS
1. If the request requires doing something on the computer, use the tools.
   Never say you did something without calling the tool, and never describe
   in your final answer an action you did not execute.
2. Requests with several actions ("close X and open Y"): one tool per
   action, in the order asked, in the same turn. If you can't do a part,
   say which.
3. A result with success=true (or a message starting with "SUCCESS") means
   the action is done: don't repeat the tool or ask the user to do it. With
   success=false, explain the error in one useful sentence.

APPS, WEBSITES AND SEARCHES
4. Open an app: open_application with the name as the user said it, in any
   language ("calculadora", "file manager", "firefox"). The tool resolves the
   name; never refuse before trying. Browsers are apps (open_application),
   never open_website.
5. open_application already waits for the window and brings it to the
   front. Don't use wait_seconds after it.
6. Close: close_application with the name as the user said it ("claude",
   "the browser"); it finds the window/process by itself. Closing "all tabs"
   means closing the browser. If asked to close openTARS itself, tell the
   user to type "{sair}" or close the window.
7. A specific page or website: open_website. Any SEARCH ("search X", "X on
   youtube", someone's channel or profile): search_web, with query = only the
   search terms, uncut, and service only if the user names one. Never invent
   URLs.

WORKING INSIDE APPS
8. To press buttons, menu items, tabs or links, use click_element with the
   text shown on the element (e.g. name="7", "=", "Save") and window=<app>.
   It needs no screenshot and returns what the window shows afterwards (e.g.
   the calculator display). It works in ANY app: when the app doesn't expose
   its buttons (Claude, Discord, VS Code...), it reads the text on the screen,
   and for an icon without text describe it (name="send icon", "trash icon").
   list_elements shows what can be clicked and the texts of the window.
9. To write in an app: type_in_element with window=<app>, the field's name or
   the text it shows (e.g. "Search", "Reply to Claude") and submit=true to
   send it with Enter. Only if these fail, use take_screenshot with
   window=<app> and click_mouse with coordinates: pixels of the LAST
   screenshot (origin at its top-left corner), aiming at the center.
10. Do every step of the request before answering. After a sequence of clicks,
    check the result (the texts returned by click_element, list_elements or a
    screenshot) before saying you finished. Messages that start with
    "[Context from openTARS" are information (open windows, number of
    steps), not a new request.
11. Simple arithmetic ("what is 25+17"): answer directly, without opening
    anything. If asked to use the calculator, open it and do it there.

TERMINAL
12. execute_terminal runs without an interactive terminal: commands that ask
    for a password (sudo) or a confirmation fail. Prefer commands that only
    read information; to install or change the system, show the command and
    let the user run it.

STYLE
13. Be brief. Don't show your internal reasoning: one sentence saying what
    you did is enough.
14. Put any code, command or file content in a fenced block with its
    language (```python, ```bash ...): the user gets a copy button on it.
"""


_contexto_sistema_cache = {}


def _nome_distro():
    try:
        for linha in Path("/etc/os-release").read_text().splitlines():
            if linha.startswith("PRETTY_NAME="):
                return linha.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "Linux"


def contexto_sistema():
    """Fatos sobre ESTE computador, pro modelo não ter que adivinhar
    (antes o prompt dizia "Zorin OS" pra todo mundo)."""

    if not _contexto_sistema_cache:
        desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "unknown"
        try:
            real_l, real_a = pyautogui.size()
            escala = _escala_tela_inteira()
            tela = f"{round(real_l / escala)}x{round(real_a / escala)} (full-screen screenshot coordinates)"
        except Exception:
            tela = "no access to the screen"
        _contexto_sistema_cache.update(
            distro=_nome_distro(),
            desktop=desktop.replace(":", ", "),
            sessao=SESSAO_GRAFICA if SESSAO_GRAFICA != "nenhuma" else "no graphical session",
            tela=tela,
            usuario=Path.home().name,
            pasta=str(Path.home()),
        )

    ctx = _contexto_sistema_cache
    return (
        "\nTHIS COMPUTER\n"
        f"- System: {ctx['distro']}; desktop: {ctx['desktop']}; session: {ctx['sessao']}\n"
        f"- Screen: {ctx['tela']}\n"
        f"- User: {ctx['usuario']} (home folder {ctx['pasta']})\n"
        f"- Today: {time.strftime('%Y-%m-%d (%A)')}\n"
    )


def montar_system_prompt():
    """Regras (em inglês, que todo modelo entende melhor) + este PC + o
    idioma da resposta, escrito NO idioma escolhido: uma instrução na
    própria língua puxa o modelo pra responder nela."""
    return (
        SYSTEM_PROMPT.replace("{sair}", t("cmd.sair_principal"))
        + contexto_sistema()
        + "\n" + t("_ia") + "\n"
    )


# ============================================================
# CONVERSA
# ============================================================

# Uma conversa só, compartilhada por todos os modelos. Antes cada modelo
# tinha o seu histórico: no modo AUTO, "abra o firefox" ia pra um modelo
# e "agora feche ele" pra outro, que não sabia do que se tratava.
ARQUIVO_SESSOES = Path(
    os.environ.get("TARS_ARQUIVO_SESSOES", str(Path.home() / ".tars_sessoes.json"))
)

conversa = [{"role": "system", "content": SYSTEM_PROMPT}]

# Compatibilidade: código/testes antigos olham "sessoes".
sessoes = {}


def carregar_sessoes():
    """Lê a conversa salva. Aceita o formato antigo (um histórico por
    modelo): usa o maior deles. Devolve a lista de mensagens, já com o
    prompt de sistema atual na frente."""

    if not ARQUIVO_SESSOES.exists():
        return []

    try:
        dados = json.loads(ARQUIVO_SESSOES.read_text(encoding="utf-8"))
    except Exception as e:
        print(c(f"[openTARS] {t('msg.historico_ilegivel', arquivo=ARQUIVO_SESSOES, erro=e)}", Cor.AMARELO))
        return []

    if isinstance(dados, dict) and isinstance(dados.get("conversa"), list):
        mensagens = dados["conversa"]
    elif isinstance(dados, dict):
        listas = [v for v in dados.values() if isinstance(v, list)]
        mensagens = max(listas, key=len) if listas else []
    else:
        mensagens = []

    mensagens = [m for m in mensagens if isinstance(m, dict) and m.get("role") != "system"]
    if not mensagens:
        return []
    mensagens = [{"role": "system", "content": montar_system_prompt()}] + mensagens
    _remover_turnos_recusados(mensagens)
    return mensagens if len(mensagens) > 1 else []


def iniciar_conversa():
    """Carrega a conversa salva (terminal e interface usam isto)."""
    carregada = carregar_sessoes()
    if carregada:
        conversa[:] = carregada
    return len([m for m in conversa if m.get("role") == "user"])


def salvar_sessoes():
    """Grava a conversa sem imagens, de forma atômica (arquivo temporário
    + rename): um travamento no meio da escrita não corrompe o histórico."""

    try:
        limpas = [
            {k: v for k, v in m.items() if k != "images"}
            for m in conversa
            if isinstance(m, dict) and m.get("role") != "system"
        ]
        temporario = ARQUIVO_SESSOES.with_suffix(".tmp")
        temporario.write_text(
            json.dumps({"versao": 2, "conversa": limpas}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(temporario, ARQUIVO_SESSOES)
    except Exception as e:
        print(c(f"[openTARS] {t('msg.historico_nao_salvo', erro=e)}", Cor.AMARELO))


def limpar_sessao(modelo=None):
    """Apaga a conversa (comando /limpar)."""
    conversa[:] = [{"role": "system", "content": montar_system_prompt()}]
    salvar_sessoes()


def obter_sessao(modelo=None):
    """A conversa (a mesma pra qualquer modelo), com o prompt de sistema
    atualizado."""
    if not conversa or conversa[0].get("role") != "system":
        conversa.insert(0, {"role": "system", "content": ""})
    conversa[0]["content"] = montar_system_prompt()
    return conversa


LIMITE_CHARS_RESULTADO_ANTIGO = 600

# "Não consigo abrir aplicativos", "I can't close programs", "no puedo"...
# Modelo pequeno às vezes esquece que tem ferramentas e recusa. (Texto já
# normalizado: minúsculo e sem acento.)
_PADROES_RECUSA = [re.compile(p) for p in (
    r"\bnao (consigo|posso|sou capaz|tenho como|tenho a capacidade|tenho acesso|e possivel para mim)\b",
    r"\bnao (tenho|possuo) (ferramenta|funcao|permissao|essa capacidade)",
    r"\b(i can'?t|i cannot|i am unable|i'?m unable|i don'?t have (the ability|access|a tool))\b",
    r"\bno (puedo|soy capaz|tengo (acceso|la capacidad|herramientas|forma))\b",
    r"\b(je ne (peux|suis) pas|je n'ai pas (acces|la possibilite|la capacite))\b",
    r"\b(ich kann (das |dies |es )?nicht|ich bin nicht in der lage|ich habe keinen zugriff)\b",
)]


# "Clique você na caixa e digite": a IA devolve a tarefa pro usuário
# depois de uma ferramenta falhar, em vez de usar as outras.
_PADROES_DEVOLVE = [re.compile(p) for p in (
    r"\b(you can|you could|you'?ll need to|you need to|please)\b[^.]{0,60}\b(type|click|press|focus|enter)\b",
    r"\b(voce pode|voce precisa|voce tera que|por favor)\b[^.]{0,60}\b(digit|clic|escrev|apert|foc)",
    r"\b(puedes|tendras que|necesitas)\b[^.]{0,60}\b(escrib|haz clic|puls|pulsa)",
    r"\b(vous pouvez|vous devez)\b[^.]{0,60}\b(tap|clique|saisi|appuy)",
    r"\b(sie konnen|du kannst|sie mussen)\b[^.]{0,60}\b(tipp|klick|drück|druck|eingeb)",
    r"\b(cannot|can'?t|unable to) (directly )?(interact|click|access)",
)]


def _devolveu_pro_usuario(texto):
    t = normalizar(texto or "")
    return bool(t) and len(t) < 1500 and (_parece_recusa(texto) or any(p.search(t) for p in _PADROES_DEVOLVE))


def _parece_recusa(texto):
    t = normalizar(texto or "")
    return bool(t) and len(t) < 1500 and any(p.search(t) for p in _PADROES_RECUSA)


def _remover_turnos_recusados(mensagens):
    """Tira do histórico os pedidos que terminaram em "não consigo" sem a
    IA ter tentado nenhuma ferramenta. Uma recusa dessas na conversa
    ensina o próximo modelo a recusar também (foi o que fez o qwen3:8b
    dizer que não conseguia fechar um app depois do qwen2.5-coder)."""

    sistema, resto = mensagens[:1], mensagens[1:]
    turnos, atual = [], []
    for m in resto:
        if _e_pedido_do_usuario(m) and atual:
            turnos.append(atual)
            atual = []
        atual.append(m)
    if atual:
        turnos.append(atual)

    def recusado(turno):
        if any(m.get("tool_calls") for m in turno):
            return False
        respostas = [m for m in turno if m.get("role") == "assistant"]
        return bool(respostas) and _parece_recusa(respostas[-1].get("content"))

    limpos = [m for turno in turnos if not recusado(turno) for m in turno]
    removidos = len(resto) - len(limpos)
    mensagens[:] = sistema + limpos
    return removidos


def _e_pedido_do_usuario(m):
    """Mensagem 'user' escrita pela pessoa (não as automáticas do
    openTARS, que também vão com role=user)."""
    conteudo = m.get("content") or ""
    return m.get("role") == "user" and not conteudo.startswith("[")


def _podar_historico(mensagens):
    """Mantém o histórico dentro do limite sem cortar uma tarefa no meio.

    - Corta sempre no começo de um pedido do usuário: antes o corte podia
      deixar um resultado de ferramenta "órfão" no início, sem a chamada
      que o gerou, e alguns modelos se perdiam com isso.
    - Resultados de ferramentas de pedidos antigos (saída de comando,
      listas de arquivos) são encurtados: ocupavam contexto à toa."""

    sistema, resto = mensagens[:1], mensagens[1:]

    inicios = [i for i, m in enumerate(resto) if _e_pedido_do_usuario(m)]
    if len(resto) > LIMITE_HISTORICO_MENSAGENS and inicios:
        corte = next((i for i in inicios if len(resto) - i <= LIMITE_HISTORICO_MENSAGENS), inicios[-1])
        resto = resto[corte:]
        inicios = [i - corte for i in inicios if i >= corte]

    ultimo_pedido = inicios[-1] if inicios else 0
    for i, m in enumerate(resto):
        if i < ultimo_pedido and m.get("role") == "tool" and len(m.get("content", "")) > LIMITE_CHARS_RESULTADO_ANTIGO:
            m["content"] = m["content"][:LIMITE_CHARS_RESULTADO_ANTIGO] + "…(old result shortened)"

    mensagens[:] = sistema + resto


# ============================================================
# CHAT COM OLLAMA
# ============================================================

_MODELOS_SEM_THINKING = set()

# Modelos que confirmadamente não aceitam o parâmetro 'tools' (ex:
# gemma3:270m). Sem isso, escolher um modelo assim quebrava TODA
# mensagem com um 400 do Ollama — o openTARS sempre manda 'tools'.
_MODELOS_SEM_TOOLS = set()

# Modelos que não enxergam imagem (sem 'vision' no catálogo, ou o Ollama
# respondeu "does not support multimodal"). O print de tela não vai pra
# eles: um modelo com visão descreve a tela em texto.
_MODELOS_SEM_VISAO = set()


def modelo_enxerga(modelo):
    if modelo in _MODELOS_SEM_VISAO:
        return False
    try:
        info = catalogar_modelos().get(modelo)
    except Exception:
        info = None
    if info and info.get("capacidades_conhecidas"):
        return "vision" in info["capacidades"]
    return True  # não sabemos: tenta (e aprende com o erro)


def _erro_de_imagem(erro):
    """O modelo não aceita imagem nenhuma. Diferente de "tools not supported
    with images" (Ollama antigo): aí a imagem serve, as ferramentas não."""
    e = (erro or "").lower()
    if "tool" in e:
        return "multimodal" in e
    return "multimodal" in e or (("image" in e or "vision" in e) and "support" in e)


def _modelo_de_visao(excluir=()):
    """O modelo com visão que descreve prints pros modelos que não
    enxergam: o menor que roda bem nesta máquina."""
    try:
        catalogo = catalogar_modelos()
    except Exception:
        return None
    candidatos = [m for m in catalogo.values()
                  if "vision" in m["capacidades"] and m["tag"] not in _MODELOS_SEM_VISAO
                  and m["tag"] not in excluir and m["categoria"] != "embedding"]
    if not candidatos:
        return None
    gpu_info = detectar_gpu()

    def chave(m):
        v = m.get("vram_estimado_gb")
        cabe = v is None or gpu_info is None or classificar_encaixe(v, gpu_info) in ("gpu", "misto")
        return (not cabe, v if v is not None else 999)
    return min(candidatos, key=chave)["tag"]


def _descrever_print(imagem_b64, pedido, modelo_atual):
    """Texto descrevendo o print (janelas, botões, textos e posições em
    pixels DESTA imagem), feito por um modelo com visão. None se não
    houver nenhum ou se ele falhar."""
    visao = _modelo_de_visao(excluir=(modelo_atual,))
    if not visao:
        return None, None
    print(c(f"[openTARS] {t('msg.descrevendo_tela', modelo=modelo_atual, visao=visao)}", Cor.AMARELO))
    prompt = (
        "Describe this screenshot for an assistant that cannot see it and must operate "
        f"the computer to do: \"{pedido}\". List the open windows, the buttons, fields and "
        "texts that matter, each with its approximate center in pixel coordinates of this "
        f"image ({_escala_print.get('largura', '?')}x{_escala_print.get('altura', '?')}, origin "
        "at the top-left). Be concise and factual; no advice."
    )
    texto = _chat_unico(visao, prompt, temperatura=0.1, timeout=TIMEOUT_HTTP_LONGO,
                        keep_alive=KEEP_ALIVE, imagens=[imagem_b64])
    if not texto:
        _MODELOS_SEM_VISAO.add(visao)
        return None, None
    return texto, visao


def _print_em_texto(imagem_b64, pedido, modelo, janela=None):
    """Mensagem (sem imagem) que substitui o print pra um modelo que não
    enxerga."""
    descricao, visao = _descrever_print(imagem_b64, pedido, modelo)
    if descricao:
        if janela and _sem_arvore(janela):
            # Os nomes da descrição NÃO servem pro click_element nesse app.
            como = ("This app does not expose its buttons: click with click_mouse(x, y) using the "
                    "coordinates above, and write with type_in_element(text=..., window=..., submit=true).")
        else:
            como = ("To click, use click_element(name) if the app exposes its buttons; if it fails, "
                    "use click_mouse(x, y) with the coordinates above. To write, use type_text.")
        return (
            "[Automatic message from openTARS, not from the user] You cannot see images, so "
            f"the vision model {visao} described the screenshot you took:\n{descricao}\n\n"
            f"Continue the user's request: \"{pedido}\". {como}"
        )
    return (
        "[Automatic message from openTARS, not from the user] The screenshot was taken, but "
        "you cannot see images and no vision model is installed. Don't take screenshots: "
        "use list_elements(window=<app>) to read a window's buttons and texts, and "
        f"click_element to press them. Continue the user's request: \"{pedido}\"."
    )


def _tirar_imagens_da_conversa(mensagens, pedido, modelo):
    """Troca toda imagem da conversa por texto (o modelo recusou imagem)."""
    for m in mensagens:
        imagens = m.pop("images", None)
        if imagens:
            m["content"] = _print_em_texto(imagens[-1], pedido, modelo)


# Pedido de parar a resposta atual (botão "Parar" da janela, Ctrl+C).
_cancelar = threading.Event()


class RespostaCancelada(Exception):
    pass


# O que perguntar_modelo devolve quando o usuário para a resposta.
RESPOSTA_INTERROMPIDA = "(interrompido)"


_resposta_ativa = {"r": None}


def cancelar_resposta():
    """Interrompe a resposta/tarefa em andamento assim que possível: no
    meio do texto, enquanto o Ollama ainda lê a conversa (fecha a
    conexão), ou entre uma ferramenta e outra."""
    _cancelar.set()
    resposta = _resposta_ativa["r"]
    if resposta is not None:
        try:
            resposta.close()
        except Exception:
            pass


def _verificar_cancelamento():
    if _cancelar.is_set():
        raise RespostaCancelada()


def _preparar_payload_ollama(payload):
    """Remove de antemão os parâmetros que a gente já sabe (pelo
    catálogo, via /api/show, ou por uma falha anterior nesta sessão)
    que esse modelo não aceita."""

    modelo = payload.get("model")
    payload = dict(payload)

    try:
        info = catalogar_modelos().get(modelo)
    except Exception:
        info = None

    if info and info.get("capacidades_conhecidas"):
        capacidades = info["capacidades"]
        if "tools" not in capacidades:
            _MODELOS_SEM_TOOLS.add(modelo)
        if "thinking" not in capacidades:
            _MODELOS_SEM_THINKING.add(modelo)

    if modelo in _MODELOS_SEM_THINKING:
        payload.pop("think", None)

    if modelo in _MODELOS_SEM_TOOLS:
        payload.pop("tools", None)

    return payload


def _erro_de_recurso_nao_suportado(texto, recurso):
    t = (texto or "").lower()
    return recurso in t and ("support" in t or "suport" in t)


def _chamar_ollama_stream(payload):
    """Chamada de streaming ao Ollama. Se o Ollama disser que o modelo
    não suporta 'think' ou 'tools', tenta de novo sem isso e lembra pro
    resto da sessão.

    Antes qualquer erro (falta de memória, Ollama reiniciando) disparava
    as tentativas sem think/tools, e um sucesso depois de uma falha
    passageira marcava o modelo como "sem ferramentas" pra sempre."""

    modelo = payload.get("model")
    tentativa = _preparar_payload_ollama(payload)

    for _ in range(3):
        try:
            resposta = ollama_request(tentativa, stream=True)
        except Exception as e:
            return None, t("msg.erro_comunicar_ollama", erro=e)

        if resposta.status_code == 200:
            return resposta, None

        try:
            detalhe = resposta.text
        except Exception:
            detalhe = ""

        if "think" in tentativa and _erro_de_recurso_nao_suportado(detalhe, "think"):
            _MODELOS_SEM_THINKING.add(modelo)
            tentativa = {k: v for k, v in tentativa.items() if k != "think"}
            continue

        if "tools" in tentativa and _erro_de_recurso_nao_suportado(detalhe, "tool"):
            _MODELOS_SEM_TOOLS.add(modelo)
            tentativa = {k: v for k, v in tentativa.items() if k != "tools"}
            continue

        return None, t("msg.erro_ollama_http", codigo=resposta.status_code, detalhe=detalhe.strip() or "-")

    return None, t("msg.ollama_recusou")


def _executar_turno_streaming(modelo, mensagens, tools, pensar=True):
    """Envia a conversa ao Ollama em modo streaming e vai mostrando o
    pensamento e a resposta em tempo real. Devolve conteúdo, pensamento
    e as tool_calls. pensar=False desliga o raciocínio dos modelos que
    pensam (qwen3...): ver deve_pensar()."""

    payload = {
        "model": modelo,
        "messages": mensagens,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "think": bool(pensar),
        "options": {"temperature": TEMPERATURA_CONVERSA, "num_ctx": contexto_ia()},
    }

    if tools:
        payload["tools"] = tools

    resposta, erro = _chamar_ollama_stream(payload)

    if erro:
        return None, erro

    partes_conteudo = []
    partes_pensamento = []
    tool_calls = []

    pensando_ativo = False
    escrevendo_ativo = False

    _resposta_ativa["r"] = resposta
    try:
        if _cancelar.is_set():
            raise RespostaCancelada()

        for linha in resposta.iter_lines():

            if _cancelar.is_set():
                resposta.close()
                if pensando_ativo or escrevendo_ativo:
                    emitir("fim_stream")
                raise RespostaCancelada()

            if not linha:
                continue

            try:
                chunk = json.loads(linha)
            except Exception:
                continue

            if chunk.get("error"):
                return None, t("msg.erro_ollama", detalhe=chunk["error"])

            msg = chunk.get("message", {}) or {}

            pensamento = msg.get("thinking")

            if pensamento:
                emitir("pensamento", texto=pensamento, inicio=not pensando_ativo)
                pensando_ativo = True
                partes_pensamento.append(pensamento)

            pedaco = msg.get("content")

            if pedaco:
                emitir("resposta", texto=pedaco, inicio=not escrevendo_ativo, apos_pensamento=pensando_ativo)
                escrevendo_ativo = True
                partes_conteudo.append(pedaco)

            # Versões novas do Ollama podem mandar as chamadas de
            # ferramenta em mais de um pedaço: junta todas.
            if msg.get("tool_calls"):
                tool_calls.extend(msg["tool_calls"])

            if chunk.get("done"):
                if chunk.get("done_reason") == "length":
                    print(c(f"\n[openTARS] {t('msg.limite_contexto', tokens=contexto_ia())}", Cor.AMARELO))
                break

    except RespostaCancelada:
        raise
    except Exception as e:
        if _cancelar.is_set():  # conexão fechada pelo botão Parar
            if pensando_ativo or escrevendo_ativo:
                emitir("fim_stream")
            raise RespostaCancelada()
        return None, t("msg.erro_streaming", erro=e)
    finally:
        _resposta_ativa["r"] = None

    if pensando_ativo or escrevendo_ativo:
        emitir("fim_stream")

    return {
        "content": "".join(partes_conteudo).strip(),
        "thinking": "".join(partes_pensamento).strip(),
        "tool_calls": tool_calls,
    }, None


LIMITE_CUTUCADAS_RESPOSTA_VAZIA = 2

# Falhas seguidas: a partir de 3 da MESMA ferramenta, a IA é mandada
# mudar de estratégia; com 8 de qualquer uma, para e explica ao usuário.
LIMITE_FALHAS_MESMA_FERRAMENTA = 3
LIMITE_FALHAS_SEGUIDAS = 8


# Mensagens automáticas (vão pra IA, com role=user): começam com "[" —
# é assim que _e_pedido_do_usuario sabe que não foi a pessoa que escreveu.

def _mensagem_do_print(pedido):
    return (
        "[Automatic message from openTARS, not from the user] This is the "
        "screenshot you took with take_screenshot. Use it to continue the "
        f"user's request: \"{pedido}\". To click, use pixel coordinates of this "
        "image. Don't describe the image: go on with the next action (or answer, "
        "if the task is done)."
    )


def _mensagem_use_ferramentas(pedido):
    return (
        "[Automatic message from openTARS] You DO have tools for this "
        "(open_application, close_application, click_element, take_screenshot, "
        "execute_terminal and others). Don't say you can't: call the right tool "
        f"now for the user's request: \"{pedido}\"."
    )


_PADROES_SUCESSO = [re.compile(p) for p in (
    r"^(pronto|feito|prontinho|done|all set|finished|listo|hecho|c'?est fait|voila|fertig|erledigt)\b",
    r"\b(abri|fechei|cliquei|enviei|digitei|pesquisei|criei|apaguei|movi|executei|consegui)\b",
    r"\b(i (have )?(opened|closed|clicked|sent|typed|searched|created|deleted|moved|ran))\b",
    r"\b(successfully|com sucesso|con exito|avec succes|erfolgreich)\b",
)]


def _diz_que_conseguiu(texto):
    t = normalizar(texto or "")
    return bool(t) and len(t) < 1200 and any(p.search(t) for p in _PADROES_SUCESSO) \
        and not _parece_recusa(texto)


def _mensagem_sucesso_falso(pedido):
    return (
        "[Automatic message from openTARS] Careful: your LAST tool call FAILED (see its result), "
        "so the task is not done yet. Either fix it now with another approach, or tell the user "
        f"honestly what did not work. Don't say it worked. Request: \"{pedido}\"."
    )


LIMITE_REESCALAR = 2          # trocas de modelo por pedido (erro, recusa, nada funcionou)
LIMITE_JANELAS_CONTEXTO = 8
CONTEXTO_JANELAS = os.environ.get("TARS_CONTEXTO_JANELAS", "1") not in ("0", "off", "nao", "no")


def _mensagem_sem_ferramenta(pedido):
    return (
        "[Automatic message from openTARS] You answered as if it were done, but you did NOT call "
        "any tool, so nothing happened on the computer. Call the right tool(s) now to actually do "
        f"it: \"{pedido}\". Only after a tool result confirms it, say it's done."
    )


def _mensagem_outro_modelo(pedido):
    return (
        "[Automatic message from openTARS] The attempts above (by another model) all failed. You "
        "are taking over: use a DIFFERENT approach from the ones that failed (another tool, "
        "another window name, typing instead of clicking...). If there is really no way, tell the "
        f"user what blocks it. Request: \"{pedido}\"."
    )


def _desfecho(tarefa, conteudo, usou_ferramenta, algum_sucesso, mentiu):
    """Pro histórico do modelo: deu certo (True), falhou (False) ou não dá
    pra dizer (None: ex. todas as ferramentas falharam por causa do
    computador e a IA explicou direito)."""
    if not conteudo or mentiu:
        return False
    if _parece_recusa(conteudo) and not usou_ferramenta:
        return False
    if tarefa in ("acao", "visao", "busca"):
        return True if algum_sucesso else None
    return True


def _janelas_para_contexto():
    try:
        janelas = listar_janelas_x()
        ativa = janela_ativa_x() if janelas else None
    except Exception:
        return []
    if not janelas:
        return []
    protegidos = _pids_protegidos()
    saida = []
    for j in janelas:
        titulo = (j.get("titulo") or "").strip()
        if not titulo or j.get("pid") in protegidos:
            continue
        titulo = titulo if len(titulo) <= 50 else titulo[:47] + "..."
        saida.append(f"'{titulo}'" + (" (focused)" if j.get("id") == ativa else ""))
        if len(saida) >= LIMITE_JANELAS_CONTEXTO:
            break
    return saida


def _contexto_do_pedido(texto, tarefa, multi_etapas):
    """O que a IA precisa saber antes de agir: as janelas abertas (não
    precisa abrir o que já está aberto, sabe o nome certo da janela) e, se
    o pedido tem várias etapas, que é pra fazer TODAS."""
    linhas = []
    if CONTEXTO_JANELAS and tarefa in ("acao", "visao"):
        janelas = _janelas_para_contexto()
        if janelas:
            linhas.append("Open windows right now: " + "; ".join(janelas) + ".")
    if multi_etapas:
        partes = escolha.partes_do_pedido(normalizar(texto), _verbos_de_acao())
        if len(partes) >= 2:
            linhas.append(f"This request has {len(partes)} steps: do ALL of them, in order, before your final "
                          "answer (don't stop after the first one).")
    if not linhas:
        return None
    return "[Context from openTARS, not a new request] " + " ".join(linhas)


def _mensagem_faca_voce(pedido):
    return (
        "[Automatic message from openTARS] Don't hand the task back to the user: you have the "
        "tools to do it yourself. Read the last tool result again and follow the way it gives "
        "(e.g. type_in_element with window and submit=true to write in an app that doesn't expose "
        "its buttons, or take_screenshot + click_mouse). If you must invent content (a question, a "
        f"message), invent it. Do it now for: \"{pedido}\"."
    )


def _mensagem_continuar(pedido):
    return (
        "[Automatic message from openTARS] You neither called a tool nor "
        f"answered. Continue the user's request: \"{pedido}\" — call the next "
        "tool or, if you are done, say in one sentence what was done."
    )


_NOMES_FERRAMENTAS = None


def _ferramentas_no_texto(texto):
    """Chamadas de ferramenta escritas no texto da resposta, no formato
    [{"function": {"name", "arguments"}}], ou [] se não houver."""
    global _NOMES_FERRAMENTAS
    if _NOMES_FERRAMENTAS is None:
        _NOMES_FERRAMENTAS = {f["function"]["name"] for f in TOOLS}
    if '"name"' not in texto or not any(nome in texto for nome in _NOMES_FERRAMENTAS):
        return []
    decodificador = json.JSONDecoder()
    chamadas, pos = [], 0
    while True:
        pos = texto.find("{", pos)
        if pos < 0:
            break
        try:
            obj, fim = decodificador.raw_decode(texto, pos)
        except ValueError:
            pos += 1
            continue
        pos = fim
        if isinstance(obj, dict) and isinstance(obj.get("function"), dict):
            obj = obj["function"]
        argumentos = obj.get("arguments", obj.get("parameters")) if isinstance(obj, dict) else None
        if isinstance(argumentos, str):
            try:
                argumentos = json.loads(argumentos)
            except ValueError:
                argumentos = None
        if isinstance(obj, dict) and obj.get("name") in _NOMES_FERRAMENTAS and isinstance(argumentos, dict):
            chamadas.append({"function": {"name": obj["name"], "arguments": argumentos}})
    return chamadas


def _argumentos_da_chamada(funcao):
    argumentos = funcao.get("arguments", {})
    if isinstance(argumentos, str):
        try:
            argumentos = json.loads(argumentos)
        except Exception:
            argumentos = {}
    return argumentos if isinstance(argumentos, dict) else {}


def _carregar_com_fallback(modelo, tarefa):
    """Carrega o modelo pedido; no modo AUTO, se falhar, desce a cadeia
    de alternativas da tarefa."""

    candidatos = [modelo]
    if modo_modelo == "AUTO" and tarefa:
        candidatos += [m for m in construir_cadeia_fallback(tarefa) if m != modelo]

    for candidato in candidatos:
        if carregar_modelo(candidato):
            return candidato, candidatos
        print(c(f"[AUTO] {t('msg.falha_carregar_alternativa', modelo=candidato)}", Cor.AMARELO))

    return None, candidatos


# Resultados de ferramenta vão pra IA com chaves em inglês (o código usa
# as chaves em português; a IA entende melhor um JSON todo em inglês).
_CHAVES_PARA_IA = {
    "sucesso": "success", "mensagem": "message", "acao": "action", "aplicativo": "app",
    "encontrado_como": "found_as", "janela": "window", "janelas": "windows",
    "janelas_fechadas": "closed_windows", "janelas_abertas": "open_windows",
    "titulo": "title", "largura": "width", "altura": "height", "minimizada": "minimized",
    "em_foco": "focused", "diretorio": "directory", "caminho": "path", "arquivos": "files",
    "codigo": "exit_code", "entrada": "input", "servico": "service",
    "ram_percentual": "ram_percent", "ram_disponivel_gb": "ram_available_gb",
    "disco_percentual": "disk_percent", "disco_livre_gb": "disk_free_gb",
    "elementos": "elements", "elemento": "element", "textos": "texts",
}


def _para_ia(valor):
    if isinstance(valor, dict):
        return {_CHAVES_PARA_IA.get(k, k): _para_ia(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_para_ia(v) for v in valor]
    return valor


def perguntar_modelo(modelo, texto, tarefa=None, multi_etapas=False):

    pendentes = []  # ferramentas pedidas e ainda não executadas neste ciclo

    try:
        modelo_carregado, tentados = _carregar_com_fallback(modelo, tarefa)
    except OllamaOffline:
        modelo_carregado, tentados = None, [modelo]

    if not modelo_carregado and not ollama_online():
        # "Não consegui carregar o modelo" escondia a causa: o Ollama
        # está desligado (ex: parado pra usar o vLLM).
        mensagem_erro = t("aviso.ollama_offline", host=OLLAMA_HOST)
        print(c(f"\n[openTARS] {mensagem_erro}", Cor.VERMELHO))
        return mensagem_erro

    if not modelo_carregado:
        mensagem_erro = t("msg.nenhum_modelo_carregou", tentados=", ".join(tentados))
        print(c(f"\n[openTARS] {mensagem_erro}", Cor.VERMELHO))
        return mensagem_erro

    modelo = modelo_carregado
    usados = [modelo]
    mensagens = obter_sessao(modelo)
    _remover_turnos_recusados(mensagens)
    mensagens.append({"role": "user", "content": texto})
    contexto = _contexto_do_pedido(texto, tarefa, multi_etapas)
    if contexto:
        mensagens.append({"role": "user", "content": contexto})

    inicio_total = time.time()
    cutucadas = 0
    usou_ferramenta = False
    lembrou_ferramentas = False
    pensar = deve_pensar(tarefa, texto, multi_etapas)
    janela_do_print = None
    ultima_falhou = insistiu = conferiu_sucesso = cobrou_acao = False
    algum_sucesso = mentiu = False
    desfecho = None               # pro histórico: True deu certo, False falhou, None não conta
    reescaladas = 0
    ferramentas = ferramentas_para(tarefa)
    falhas_seguidas = {}          # ferramenta -> falhas desde o último sucesso
    chamadas_que_falharam = {}    # (ferramenta, argumentos) -> motivo

    def trocar_de_modelo(chave_mensagem):
        """Passa o pedido pro próximo modelo da fila (no modo AUTO, até
        LIMITE_REESCALAR vezes por pedido). O que falhou fica no placar."""
        nonlocal modelo, reescaladas
        if modo_modelo != "AUTO" or reescaladas >= LIMITE_REESCALAR:
            return False
        fila = construir_cadeia_fallback(tarefa_da_fila(tarefa or "acao", multi_etapas))
        proximo = next((m for m in fila if m not in usados), None)
        if not proximo:
            return False
        try:
            if not carregar_modelo(proximo):
                return False
        except OllamaOffline:
            return False
        historico_modelos.registrar(modelo, _tarefa_do_historico(tarefa), False)
        print(c(f"[AUTO] {t(chave_mensagem, modelo=modelo, novo=proximo)}", Cor.AMARELO))
        usados.append(proximo)
        modelo = proximo
        reescaladas += 1
        return True

    try:
        for ciclo in range(LIMITE_CICLOS_FERRAMENTAS):

            _verificar_cancelamento()

            imagem_no_turno = any(m.get("images") for m in mensagens)

            resultado, erro = _executar_turno_streaming(modelo, mensagens, ferramentas, pensar=pensar)

            if erro and imagem_no_turno and _erro_de_imagem(erro):
                # "Model does not support multimodal": esse modelo não
                # enxerga. Toda imagem da conversa vira texto (descrita
                # por um modelo com visão) e as ferramentas continuam.
                _MODELOS_SEM_VISAO.add(modelo)
                _tirar_imagens_da_conversa(mensagens, texto, modelo)
                resultado, erro = _executar_turno_streaming(modelo, mensagens, ferramentas, pensar=pensar)
            elif erro and imagem_no_turno:
                # Alguns Ollama antigos não aceitam 'tools' junto com
                # imagem: tenta de novo sem as ferramentas.
                print(c(f"[openTARS] {t('msg.imagem_sem_ferramentas', erro=erro)}", Cor.AMARELO))
                resultado, erro = _executar_turno_streaming(modelo, mensagens, None, pensar=pensar)

            if erro:
                # Modelo que quebrou (sem memória, chamada de ferramenta que
                # o Ollama não entendeu...): o próximo da fila continua a
                # MESMA conversa, com o que já foi feito.
                if not _erro_de_imagem(erro) and trocar_de_modelo("msg.tentando_outro_modelo"):
                    continue
                desfecho = False
                print(c(f"\n[openTARS] {erro}", Cor.VERMELHO))
                return erro

            conteudo = resultado["content"]
            tool_calls = resultado["tool_calls"]

            # Modelo que escreve a chamada da ferramenta como TEXTO
            # (<tool_call>{...}</tool_call>, um JSON solto): vira chamada
            # de verdade, em vez de aparecer pro usuário como resposta.
            if not tool_calls and conteudo:
                no_texto = _ferramentas_no_texto(conteudo)
                if no_texto:
                    tool_calls, conteudo = no_texto, ""

            if not tool_calls and not conteudo and ciclo > 0 and cutucadas < LIMITE_CUTUCADAS_RESPOSTA_VAZIA:
                # Modelo "pensou" mas não respondeu nem chamou ferramenta
                # no meio de uma tarefa: pede pra continuar em vez de
                # encerrar pela metade.
                cutucadas += 1
                print(c(f"[openTARS] {t('msg.ia_parou')}", Cor.AMARELO))
                mensagens.append({"role": "user", "content": _mensagem_continuar(texto)})
                continue

            if (
                not tool_calls and not usou_ferramenta and not lembrou_ferramentas
                and modelo not in _MODELOS_SEM_TOOLS and _parece_recusa(conteudo)
            ):
                # "Não consigo abrir aplicativos": lembra que consegue, uma
                # vez. A recusa não entra no histórico.
                lembrou_ferramentas = True
                ferramentas = TOOLS  # talvez faltasse a ferramenta certa na lista da tarefa
                print(c(f"[openTARS] {t('msg.ia_recusou')}", Cor.AMARELO))
                mensagens.append({"role": "user", "content": _mensagem_use_ferramentas(texto)})
                continue

            if (
                not tool_calls and not usou_ferramenta and lembrou_ferramentas
                and modo_modelo == "AUTO" and _parece_recusa(conteudo)
            ):
                # Recusou de novo mesmo lembrado: esse modelo não serve pra
                # esse pedido. Passa pro próximo da fila (uma vez).
                proximo = next((m for m in construir_cadeia_fallback(tarefa or "acao")
                                if m not in usados), None)
                try:
                    carregou = bool(proximo) and carregar_modelo(proximo)
                except OllamaOffline:
                    carregou = False
                if carregou:
                    historico_modelos.registrar(modelo, _tarefa_do_historico(tarefa), False)
                    print(c(f"[AUTO] {t('msg.trocando_modelo', modelo=modelo, novo=proximo)}", Cor.AMARELO))
                    usados.append(proximo)
                    modelo = proximo
                    lembrou_ferramentas = False
                    while mensagens and mensagens[-1].get("role") == "user" and mensagens[-1].get("content", "").startswith("["):
                        mensagens.pop()
                    continue

            if (
                not tool_calls and usou_ferramenta and ultima_falhou and not insistiu
                and tarefa in ("acao", "visao", None) and _devolveu_pro_usuario(conteudo)
            ):
                # Uma ferramenta falhou e a IA mandou o usuário fazer na mão
                # ("clique na caixa e digite"), com as ferramentas certas
                # disponíveis. Uma vez: manda ela mesma fazer.
                insistiu = True
                ferramentas = TOOLS
                print(c(f"[openTARS] {t('msg.ia_recusou')}", Cor.AMARELO))
                mensagens.append({"role": "user", "content": _mensagem_faca_voce(texto)})
                continue

            if (
                not tool_calls and usou_ferramenta and ultima_falhou and not conferiu_sucesso
                and _diz_que_conseguiu(conteudo)
            ):
                # "Pronto, abri!" logo depois de uma ferramenta que FALHOU:
                # a IA está inventando. Uma vez: conserta ou conta a verdade.
                conferiu_sucesso = mentiu = True
                mensagens.append({"role": "user", "content": _mensagem_sucesso_falso(texto)})
                continue

            if (
                not tool_calls and not usou_ferramenta and not cobrou_acao
                and tarefa in ("acao", "visao") and modelo not in _MODELOS_SEM_TOOLS
                and _diz_que_conseguiu(conteudo)
            ):
                # "Pronto, abri o Firefox!" sem ter chamado ferramenta
                # nenhuma: nada aconteceu no computador. Uma vez: faça.
                cobrou_acao = mentiu = True
                ferramentas = TOOLS
                mensagens.append({"role": "user", "content": _mensagem_sem_ferramenta(texto)})
                continue

            if not tool_calls:
                mensagens.append({"role": "assistant", "content": conteudo})
                if not conteudo:
                    print(c(f"[openTARS] {t('msg.sem_texto', modelo=modelo)}", Cor.AMARELO))
                desfecho = _desfecho(tarefa, conteudo, usou_ferramenta, algum_sucesso, mentiu)
                emitir("tempo", modo=modo_modelo, modelo=modelo, segundos=time.time() - inicio_total)
                return conteudo

            mensagens.append({"role": "assistant", "content": conteudo, "tool_calls": tool_calls})
            usou_ferramenta = True
            desistiu = False
            pendentes[:] = [ch.get("function", {}).get("name", "") for ch in tool_calls]
            ultima_imagem = None

            for chamada in tool_calls:

                _verificar_cancelamento()

                funcao = chamada.get("function", {})
                nome = funcao.get("name", "")
                argumentos = _argumentos_da_chamada(funcao)
                chave_chamada = (nome, json.dumps(argumentos, sort_keys=True, ensure_ascii=False))
                if chave_chamada in chamadas_que_falharam:
                    # Mesma chamada, mesmos argumentos, já falhou neste pedido.
                    resultado_ferramenta = {"sucesso": False, "mensagem": (
                        f"You already tried exactly this {nome} call and it failed: "
                        f"{chamadas_que_falharam[chave_chamada]} Do something different.")}
                else:
                    resultado_ferramenta = executar_ferramenta(nome, argumentos)
                pendentes.pop(0)
                if nome == "take_screenshot":
                    janela_do_print = argumentos.get("window") or None

                ultima_falhou = not resultado_ferramenta.get("sucesso")
                if resultado_ferramenta.get("sucesso"):
                    algum_sucesso = True
                    falhas_seguidas.clear()
                else:
                    chamadas_que_falharam[chave_chamada] = str(resultado_ferramenta.get("mensagem", ""))[:300]
                    falhas_seguidas[nome] = falhas_seguidas.get(nome, 0) + 1
                    total_falhas = sum(falhas_seguidas.values())
                    if falhas_seguidas[nome] >= LIMITE_FALHAS_MESMA_FERRAMENTA:
                        resultado_ferramenta["mensagem"] = (
                            f"{resultado_ferramenta.get('mensagem', '')} STOP: {nome} failed "
                            f"{falhas_seguidas[nome]} times in a row. Don't call it again for this; "
                            "change approach (another tool) or tell the user what is blocking you."
                        )
                    if total_falhas >= LIMITE_FALHAS_SEGUIDAS:
                        desistiu = True

                # A imagem do print nunca entra no JSON de texto (custaria
                # uma fortuna de tokens): vai como mensagem de visão.
                imagem_b64 = resultado_ferramenta.pop("_imagem_b64", None)
                if imagem_b64:
                    ultima_imagem = imagem_b64

                mensagens.append({
                    "role": "tool",
                    "tool_name": nome,  # ajuda o modelo a ligar resultado e chamada
                    "content": json.dumps(_para_ia(resultado_ferramenta), ensure_ascii=False),
                })

            # A imagem vai DEPOIS de todos os resultados: cada resultado
            # tem que vir logo após as chamadas que o geraram.
            if ultima_imagem and not modelo_enxerga(modelo):
                # Modelo sem visão: o print não vai (o Ollama recusaria o
                # pedido inteiro); vai a descrição em texto.
                mensagens.append({"role": "user", "content": _print_em_texto(ultima_imagem, texto, modelo, janela_do_print)})
            elif ultima_imagem:
                # Prints antigos saem da conversa: cada um ocupa ~1 mil
                # tokens de contexto e só a tela atual importa.
                for anterior in mensagens:
                    if anterior.get("images"):
                        anterior.pop("images")
                        anterior["content"] = "[previous screenshot removed: see the latest one]"

                # Vai como "user" (é onde o Ollama aceita imagem), mas
                # sem parecer um pedido novo: antes o modelo lia
                # "usuário mandou um print" e esquecia a tarefa.
                mensagens.append({
                    "role": "user",
                    "content": _mensagem_do_print(texto),
                    "images": [ultima_imagem],
                })

            if desistiu and not algum_sucesso and trocar_de_modelo("msg.tentando_outro_modelo"):
                # Nada funcionou (e nada mudou no computador): outro modelo
                # tenta, vendo o que já falhou (e sem poder repetir igual).
                falhas_seguidas.clear()
                desistiu = False
                mensagens.append({"role": "user", "content": _mensagem_outro_modelo(texto)})
                continue

            if desistiu:
                desfecho = False
                # Muitas falhas seguidas: a IA está andando em círculos.
                # Uma última rodada SEM ferramentas pra ela explicar ao
                # usuário o que travou, em vez de gastar as 20 etapas.
                print(c(f"[openTARS] {t('msg.muitas_falhas', n=LIMITE_FALHAS_SEGUIDAS)}", Cor.AMARELO))
                mensagens.append({"role": "user", "content": (
                    "[Automatic message from openTARS] Too many tool calls failed in a row. Stop "
                    "using tools: in 2-3 sentences, tell the user what you tried, what blocked you, "
                    "and what they can do (e.g. click it themselves, or another way to ask).")})
                resultado, erro = _executar_turno_streaming(modelo, mensagens, None, pensar=False)
                conteudo = (resultado or {}).get("content") or t("msg.muitas_falhas", n=LIMITE_FALHAS_SEGUIDAS)
                mensagens.append({"role": "assistant", "content": conteudo})
                emitir("tempo", modo=modo_modelo, modelo=modelo, segundos=time.time() - inicio_total)
                return conteudo

            emitir("analisando", modelo=modelo)

        desfecho = False
        mensagem_limite = t("msg.limite_etapas", n=LIMITE_CICLOS_FERRAMENTAS)
        print(c(f"\n[openTARS] {mensagem_limite}", Cor.VERMELHO))
        return mensagem_limite

    except (RespostaCancelada, KeyboardInterrupt):
        _cancelar.clear()
        # Toda chamada de ferramenta precisa de um resultado, senão o
        # próximo pedido vai com a conversa quebrada.
        for nome in pendentes:
            mensagens.append({
                "role": "tool",
                "tool_name": nome,
                "content": json.dumps({"success": False, "message": "Cancelled by the user."}),
            })
        mensagens.append({"role": "assistant", "content": "(interrupted by the user)"})
        print(c(f"\n[openTARS] {t('msg.interrompido')}", Cor.AMARELO))
        return RESPOSTA_INTERROMPIDA

    finally:
        _podar_historico(mensagens)
        if tarefa and modo_modelo == "AUTO":
            historico_modelos.registrar(modelo, _tarefa_do_historico(tarefa), desfecho)


def processar_mensagem(texto):
    """Um pedido do usuário do começo ao fim: comandos (/modelo,
    /limpar...), escolha do modelo, resposta e salvamento. Usado pelo
    terminal e pela janela, que antes repetiam essa lógica cada um."""

    # Limpa aqui (e não no meio do caminho): um Parar apertado enquanto
    # o openTARS ainda escolhe o modelo continua valendo.
    _cancelar.clear()

    if interpretar_comando_modelo(texto):
        return None

    tarefa = detectar_tarefa(texto) if modo_modelo == "AUTO" else None
    decisao = _ultima_decisao["d"] if tarefa else None
    if tarefa:
        lembrar_tarefa(tarefa)
    # Várias etapas também contam no modo MANUAL (pensar + lembrete).
    multi = decisao.multi_etapas if decisao else escolha.eh_multi_etapas(normalizar(texto), _verbos_de_acao())
    modelo = escolher_modelo(texto, tarefa=tarefa, complexa=multi)

    emitir("tarefa", modo=modo_modelo, tarefa=tarefa, modelo=modelo, via=decisao.via if decisao else None)

    try:
        return perguntar_modelo(modelo, texto, tarefa=tarefa, multi_etapas=multi)
    finally:
        salvar_sessoes()


# ============================================================
# TELA INICIAL
# ============================================================

def tela_inicial():

    imprimir_caixa(
        f"o p e n T A R S   {VERSAO}",
        [t("term.slogan")],
        cor_titulo=Cor.MAGENTA,
    )

    # Resumo rápido de quantos modelos há pra escolher, antes da
    # listagem detalhada (que já mostra a GPU logo abaixo).
    catalogo = catalogar_modelos()

    ajudantes = {MODELO_AJUDANTE, MODELO_AJUDANTE_ANTIGO}
    n_chat = sum(1 for m in catalogo.values() if m["categoria"] != "embedding" and m["tag"] not in ajudantes)
    n_embedding = sum(1 for m in catalogo.values() if m["categoria"] == "embedding")

    print()

    if catalogo:
        resumo_modelos = t("term.modelos_prontos", n=n_chat)
        if n_embedding:
            resumo_modelos += " " + t("term.embeddings_fora", n=n_embedding)
        print(c(resumo_modelos, Cor.CIANO))
        if MODELO_AJUDANTE in catalogo:
            print(c(t("term.ajudante_ok", modelo=MODELO_AJUDANTE), Cor.CINZA))
        else:
            print(c(t("aviso.sem_ajudante", modelo=MODELO_AJUDANTE), Cor.AMARELO))
        if n_chat == 0:
            print(c(t("aviso.sem_modelo_conversa"), Cor.AMARELO))
    else:
        print(c(t("lista.nenhum_modelo"), Cor.AMARELO))

    print()
    print(t("term.ias_disponiveis"))
    print()

    _imprimir_lista_modelos()

    print(c(t("term.modo_atual", modo="AUTO"), Cor.AZUL) + " — " + t("term.modo_auto_explica"))
    print()

    # Comandos alinhados numa tabela simples (nome + descrição).
    comandos = [
        (t("term.cmd_modelos"), t("term.cmd_modelos_desc")),
        (t("term.cmd_auto"), t("term.cmd_auto_desc")),
        (t("term.cmd_modelo"), t("term.cmd_modelo_desc")),
        (t("term.cmd_limpar"), t("term.cmd_limpar_desc")),
        (t("term.cmd_idioma"), t("term.cmd_idioma_desc", idioma=i18n.nome_idioma(i18n.idioma_atual()))),
        (t("term.cmd_encerrar"), t("term.cmd_encerrar_desc")),
        (t("term.cmd_parar"), t("term.cmd_parar_desc")),
        (t("cmd.sair_principal"), t("term.cmd_sair_desc")),
    ]
    largura_cmd = max(len(cmd) for cmd, _ in comandos)

    print(t("term.comandos"))
    for cmd, desc in comandos:
        print(f"  {c(cmd.ljust(largura_cmd), Cor.VERDE)}   {desc}")

    print()
    print(c(t("term.historico_em", arquivo=ARQUIVO_SESSOES) + "\n" + t("term.log_em", arquivo=ARQUIVO_LOG), Cor.CINZA))

    for aviso in avisos_de_ambiente():
        print()
        print(c(f"⚠ {aviso}", Cor.AMARELO))

    print()
    print(c(t("term.pronto"), Cor.NEGRITO + Cor.VERDE))
    print()

    # O ajudante classifica o primeiro pedido: já deixa ele carregando.
    aquecer_ajudante()


# ============================================================
# MAIN
# ============================================================

def _prompt_atual():
    """Prompt mostrando qual modelo vai atender a próxima mensagem."""

    if modo_modelo == "MANUAL" and modelo_manual:
        indicador = c(modelo_manual, Cor.AMARELO)
    else:
        indicador = c("auto", Cor.AZUL)

    return c(t("term.voce"), Cor.NEGRITO + Cor.VERDE) + f" [{indicador}] › "


def _valor_do_argumento(argumentos, nomes):
    """--idioma en / --idioma=en -> "en" ('' se o argumento vier sem valor)."""
    for i, arg in enumerate(argumentos):
        for nome in nomes:
            if arg == nome:
                return argumentos[i + 1] if i + 1 < len(argumentos) and not argumentos[i + 1].startswith("--") else ""
            if arg.startswith(nome + "="):
                return arg.split("=", 1)[1]
    return None


def main():

    argumentos = sys.argv[1:]

    if any(a in ("--version", "-v", "--versao") for a in argumentos):
        print(f"openTARS {VERSAO}")
        return

    if any(a in ("--help", "-h", "--ajuda") for a in argumentos):
        print(t("term.ajuda", versao=VERSAO))
        return

    idioma = _valor_do_argumento(argumentos, ("--idioma", "--language", "--lang"))
    if idioma is not None:
        if idioma:
            codigo = i18n.definir_idioma(idioma)
            if not codigo:
                print(t("idioma.nao_encontrado", pedido=idioma, lista=", ".join(i18n.idiomas_disponiveis())))
                sys.exit(1)
        else:
            comando_idioma("")
            return

    if "--avaliar-classificador" in argumentos:
        sys.exit(avaliar_classificador())

    for flag in ("--explicar", "--explain"):
        if flag in argumentos:
            pedido = " ".join(a for a in argumentos[argumentos.index(flag) + 1:] if not a.startswith("--"))
            sys.exit(explicar_escolha(pedido))

    if any(a in ("--diagnostico", "--diagnostic", "--autoteste", "--selftest") for a in argumentos):
        import tars_autoteste
        completo = any(a in ("--autoteste", "--selftest") for a in argumentos)
        sys.exit(tars_autoteste.executar(sys.modules[__name__], completo=completo))

    atalho = _valor_do_argumento(argumentos, ("--atalho", "--shortcut"))
    if atalho is not None:
        import tars_atalho
        sys.exit(tars_atalho.comando(atalho))

    pedidos = iniciar_conversa()
    if pedidos:
        print(c(f"[openTARS] {t('term.conversa_carregada', n=pedidos)}", Cor.CINZA))

    tela_inicial()

    palavras_sair = _vocab()["comandos"].get("cmd.sair", ())

    try:

        while True:

            try:
                texto = input(_prompt_atual()).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue

            if not texto:
                continue

            if normalizar(texto) in palavras_sair:
                print(c("\n" + t("term.encerrando"), Cor.CINZA))
                break

            try:
                processar_mensagem(texto)
            except KeyboardInterrupt:
                print(c("\n\n[openTARS] " + t("term.operacao_interrompida"), Cor.AMARELO))
            except Exception as e:
                print(c(f"\n[openTARS] {t('msg.erro_inesperado', erro=e)}", Cor.VERMELHO))

            print()

    except KeyboardInterrupt:
        print()

    finally:
        # Descarrega toda IA da memória ao sair, seja por "sair", Ctrl+C,
        # Ctrl+D (EOF) ou erro não tratado.
        encerrar_todas_ias()
        print(c(t("term.encerrado"), Cor.CINZA))


if __name__ == "__main__":
    main()
