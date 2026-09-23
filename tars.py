#!/usr/bin/env python3

import os
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
from pathlib import Path
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor

import socket
import logging
from logging.handlers import RotatingFileHandler

import requests
import psutil
from PIL import Image


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
        raise RuntimeError(f"controle de mouse/teclado indisponível: {self._motivo}")


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
    ERRO_PYAUTOGUI = (
        "nenhuma sessão gráfica encontrada (variável DISPLAY vazia)"
        if SESSAO_GRAFICA == "nenhuma"
        else f"{type(_e).__name__}: {_e}"
    )
    pyautogui = _PyautoguiIndisponivel(ERRO_PYAUTOGUI)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

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

KEEP_ALIVE = "30m"
KEEP_ALIVE_AJUDANTE = "5m"
KEEP_ALIVE_CLASSIFICADOR = "1m"

# Usado só como último recurso, se o catálogo dinâmico (ver mais
# abaixo) vier vazio por algum motivo (Ollama offline, sem nenhum
# modelo baixado). O TARS não depende mais de uma lista fixa de
# modelos — ele descobre o que está instalado e como cada um se
# comporta consultando o próprio Ollama.
MODELO_PADRAO = os.environ.get("TARS_MODELO_PADRAO", "qwen3:0.6b")

# Modelo minúsculo dedicado só a classificar tarefa quando as palavras-
# chave de detectar_tarefa() não reconhecem nada (ver _classificar_com_ia
# mais abaixo). É opcional: se não estiver instalado, o TARS simplesmente
# não usa essa camada extra e cai no comportamento de antes (categoria
# "geral"). O instalador (.deb) já baixa esse modelo automaticamente;
# configurável via env var pra quem empacotar/rodar diferente.
MODELO_CLASSIFICADOR = os.environ.get("TARS_MODELO_CLASSIFICADOR", "gemma3:270m")

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
LIMITE_CICLOS_FERRAMENTAS = 8       # máximo de idas-e-voltas com tool calls por mensagem
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
        print(f"[TARS] aviso: log de auditoria desativado ({e}).")
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

# Tabela de tradução construída uma única vez (muito mais rápida
# que 20 chamadas .replace() encadeadas, já que normalizar() é
# chamada com muita frequência: uma vez por processo do sistema,
# uma vez por app instalado, etc.)
_TABELA_NORMALIZACAO = str.maketrans({
    "á": "a", "à": "a", "ã": "a", "â": "a", "ä": "a",
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "í": "i", "ì": "i", "î": "i", "ï": "i",
    "ó": "o", "ò": "o", "õ": "o", "ô": "o", "ö": "o",
    "ú": "u", "ù": "u", "û": "u", "ü": "u",
    "ç": "c",
})


def normalizar(texto):
    return texto.lower().strip().translate(_TABELA_NORMALIZACAO)


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
            "stderr": "tempo limite excedido",
        }

    except Exception as e:
        return {
            "codigo": -1,
            "stdout": "",
            "stderr": str(e),
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

    try:
        resposta = _SESSION.post(
            OLLAMA_URL,
            json={
                "model": modelo,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "keep_alive": keep_alive,
                "options": {"temperature": temperatura},
            },
            timeout=timeout,
        )
    except Exception:
        return None

    if resposta.status_code != 200:
        return None

    try:
        return (resposta.json().get("message", {}).get("content") or "").strip()
    except Exception:
        return None


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
        avisos.append(
            f"O Ollama não está respondendo em {OLLAMA_HOST}. Inicie o "
            "serviço (sudo systemctl start ollama) ou confira a variável "
            "OLLAMA_HOST. Sem ele, o openTARS não tem IA pra responder."
        )

    if ERRO_PYAUTOGUI:
        avisos.append(
            f"Controle de mouse/teclado desligado: {ERRO_PYAUTOGUI}. "
            "Conversa, abrir apps/sites e comandos de terminal continuam funcionando."
        )
    elif SESSAO_GRAFICA == "wayland":
        avisos.append(
            "Sessão Wayland detectada: abrir apps/sites, comandos e prints "
            "funcionam, mas cliques e digitação simulados só chegam a "
            "alguns aplicativos. Para controle total, na tela de login "
            "clique na engrenagem e escolha a sessão com \"Xorg\"."
        )

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
            print(
                c(
                    "\n[TARS] não consegui falar com o Ollama para checar "
                    "modelos em execução (ele pode estar offline).",
                    Cor.AMARELO,
                )
            )
            modelo_atual = None
            return {"sucesso": False, "encerrados": [], "falhas": []}

        alvo = set(rodando)

        if modelo_atual:
            alvo.add(modelo_atual)

        if not alvo:
            print(c("\n[TARS] nenhuma IA carregada no momento.", Cor.CINZA))
            modelo_atual = None
            return {"sucesso": True, "encerrados": [], "falhas": []}

        print(
            c(
                f"\n[TARS] encerrando {len(alvo)} modelo(s): "
                f"{', '.join(sorted(alvo))}...",
                Cor.CIANO,
            )
        )

        pendentes = set(alvo)

        for tentativa in range(1, max_tentativas + 1):

            for modelo in list(pendentes):
                descarregar_modelo(modelo)

            time.sleep(espera_seg)

            ainda = listar_modelos_rodando()

            if ainda is None:
                print(
                    c(
                        "[TARS] perdi contato com o Ollama durante a "
                        "checagem de confirmação.",
                        Cor.AMARELO,
                    )
                )
                break

            pendentes &= ainda

            if not pendentes:
                break

            if tentativa < max_tentativas:
                print(
                    c(
                        f"[TARS] ainda ativo: {', '.join(sorted(pendentes))} "
                        f"— tentando de novo ({tentativa}/{max_tentativas})...",
                        Cor.AMARELO,
                    )
                )

        if not modelo_atual or modelo_atual not in pendentes:
            modelo_atual = None

        encerrados = alvo - pendentes

        if pendentes:
            print(
                c(
                    f"[TARS] AVISO: não consegui confirmar o encerramento "
                    f"de: {', '.join(sorted(pendentes))}.",
                    Cor.VERMELHO,
                )
            )
        else:
            print(
                c(
                    "[TARS] todas as IAs foram descarregadas da memória "
                    "(confirmado).",
                    Cor.VERDE,
                )
            )

        return {
            "sucesso": not pendentes,
            "encerrados": sorted(encerrados),
            "falhas": sorted(pendentes),
        }


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

        if modelo_atual:
            print(c(f"\n[IA] descarregando {modelo_atual}...", Cor.CINZA))
            descarregar_modelo(modelo_atual)
            modelo_atual = None

        if silencioso:
            print(c(f"\n[IA] carregando {modelo} em segundo plano...", Cor.CINZA))
        else:
            imprimir_caixa(
                f"Carregando IA: {modelo}",
                ["Isso pode demorar no primeiro uso."],
            )

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
                },
                timeout=TIMEOUT_HTTP_LONGO,
            )

            if resposta.status_code != 200:
                detalhe = resposta.text.strip() or "sem detalhes"
                print(c(f"[ERRO] Ollama ({resposta.status_code}) ao carregar {modelo}: {detalhe}", Cor.VERMELHO))
                return False

            modelo_atual = modelo

            tempo = time.time() - inicio

            print(c(f"\n✓ {modelo} carregado em {tempo:.2f}s", Cor.VERDE))

            return True

        except Exception as e:
            print(c(f"\n[ERRO] Não foi possível carregar {modelo}: {e}", Cor.VERMELHO))
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


_MARCADOR_ENCAIXE = {
    "gpu": "cabe na GPU",
    "misto": "GPU + CPU",
    "cpu": "majoritariamente CPU, bem mais lento",
    "desconhecido": "",
}

_EMOJI_CATEGORIA = {
    "visao": "👁",
    "tecnico": "💻",
    "simples": "⚡",
    "geral": "🧠",
}

_DESCRICAO_CATEGORIA = {
    "visao": "visão, tela e interfaces gráficas",
    "tecnico": "programação, Linux e tarefas técnicas",
    "simples": "tarefas simples e respostas rápidas",
    "geral": "conversa geral e raciocínio",
}

# Pistas no nome/família do modelo que sugerem forte capacidade técnica
# (código, Linux, etc.) — usadas só como heurística de categorização,
# nunca como lista exigida: um modelo que não bater com nada aqui ainda
# entra em "geral" ou "tecnico" pelo tamanho.
_FAMILIAS_TECNICAS = (
    "coder", "code", "deepseek", "mistral", "codestral",
    "starcoder", "granite", "devstral",
)

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

    if "vision" in capacidades:
        return "visao"

    if parametros_b is not None and parametros_b <= 1.5:
        return "simples"

    if any(chave in fam or chave in nome for chave in _FAMILIAS_TECNICAS):
        return "tecnico"

    if parametros_b is not None and parametros_b >= 14:
        # Modelo grande sem especialidade clara: melhor usado como
        # "força bruta" pra tarefas técnicas/complexas do que como
        # bate-papo comum (mais lento pra pouco ganho em conversa
        # simples).
        return "tecnico"

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

    candidatos = modelos_com_capacidade(catalogo, "tools") or [
        m for m in catalogo.values() if m["categoria"] != "embedding"
    ]

    if not candidatos:
        return MODELO_PADRAO

    candidatos.sort(key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 999)

    return candidatos[0]["tag"]


def construir_cadeia_fallback(tarefa, catalogo=None):
    """Ordena os modelos instalados do mais indicado ao menos indicado
    para uma categoria de tarefa, com base nas capacidades e no
    tamanho reais de cada um (não numa lista fixa). O modo AUTO usa
    essa ordem tanto pra escolher o modelo ideal quanto pra descer a
    cadeia se o primeiro falhar ao carregar."""

    catalogo = catalogo if catalogo is not None else catalogar_modelos()

    # Todo modelo instalado participa automaticamente — o catálogo
    # (catalogar_modelos) já reflete o que está de fato baixado no
    # Ollama, então qualquer IA nova que o usuário baixar entra na
    # escolha sozinha, sem precisar tocar em nenhum código. A única
    # exclusão é modelo de embedding puro (categoria "embedding"), que
    # não tem como participar de uma conversa.
    candidatos = [m for m in catalogo.values() if m["categoria"] != "embedding"]

    if not candidatos:
        return []

    # O TARS sempre manda 'tools' pro Ollama (é assim que ele abre
    # apps, sites etc.), então prioriza modelos com esse suporte
    # confirmado; um modelo sem capacidades conhecidas ainda entra
    # (modo aberto), só depois dos que confirmadamente suportam.
    def suporta_tools(m):
        return (not m["capacidades_conhecidas"]) or ("tools" in m["capacidades"])

    candidatos = [m for m in candidatos if suporta_tools(m)] or candidatos

    def params(m):
        return m["parametros_b"] if m["parametros_b"] is not None else 0

    # "Praticidade": um modelo que cabe na GPU (ou em GPU+CPU) responde
    # em segundos; um que só roda majoritariamente na CPU pode levar
    # minutos. Prioriza modelos práticos antes de recorrer aos
    # gigantes lentos, mesmo quando estes são "mais inteligentes" no
    # papel — isso evita que o modo AUTO escolha, por exemplo, o maior
    # modelo instalado pra uma pergunta casual e demore uma eternidade
    # pra responder. Só entra como critério quando há como saber (GPU
    # detectada e VRAM estimada); sem essa informação, não penaliza.
    gpu_info = detectar_gpu()
    ram_gb = ram_total_gb() if gpu_info is None else None

    def pratico(m):
        if m.get("vram_estimado_gb") is None:
            return True
        if gpu_info is None:
            # Sem GPU, tudo roda na CPU: até ~8B (quantizado) ainda
            # responde em segundos; acima disso vira minutos. E o modelo
            # tem que caber com folga na RAM.
            if ram_gb is None:
                return True
            return m["vram_estimado_gb"] <= min(LIMITE_MODELO_PRATICO_CPU_GB, ram_gb * 0.6)
        return classificar_encaixe(m["vram_estimado_gb"], gpu_info) in ("gpu", "misto")

    if tarefa == "visao":
        com_visao = [m for m in candidatos if "vision" in m["capacidades"]]
        com_visao.sort(key=lambda m: (pratico(m), params(m)), reverse=True)
        sem_visao = [m for m in candidatos if m not in com_visao]
        sem_visao.sort(key=lambda m: (pratico(m), params(m)), reverse=True)
        ordenados = com_visao + sem_visao

    elif tarefa == "tecnico":
        ordenados = sorted(
            candidatos,
            key=lambda m: (
                1 if m["categoria"] == "tecnico" else 0,
                pratico(m),
                params(m),
            ),
            reverse=True,
        )

    elif tarefa == "simples":
        # Tarefas simples já querem o menor modelo disponível de
        # propósito (resposta quase instantânea) — praticidade não
        # muda nada aqui, todo modelo pequeno já é prático. É a única
        # categoria que pode cair no modelo minúsculo (ex: qwen 0.6b):
        # abrir uma URL/site e bate-papo bem curto não precisam de
        # mais raciocínio que isso.
        ordenados = sorted(
            candidatos,
            key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 999,
        )

    elif tarefa in ("acao", "busca"):
        # Ações no desktop (abrir/fechar programas, digitar, clicar,
        # rodar comandos no terminal) e BUSCAS (que exigem escolher
        # entre search_web e open_website, e separar termo de serviço)
        # precisam de raciocínio melhor do que o modelo mais simples do
        # catálogo entrega de forma confiável — é justamente aqui que o
        # menorzinho costumava recusar abrir um app, inventar que rodou
        # algo sem chamar a ferramenta, ou (no caso de busca) chamar a
        # ferramenta errada / cortar a frase de busca pela metade. Por
        # isso essas categorias nunca usam um modelo classificado como
        # "simples" (o baldinho do qwen 0.6b fica reservado pra abrir
        # um site já conhecido ou bate-papo curto, onde não há escolha
        # nenhuma de ferramenta a fazer). Em vez de sempre bater no
        # maior modelo disponível também, pega o menor entre os que
        # sobraram (o suficiente pra não errar, sem gastar o mais
        # pesado à toa) — isso distribui a carga entre os modelos de
        # porte médio em vez de concentrar tudo nos dois extremos.
        nao_triviais = [m for m in candidatos if m["categoria"] != "simples"]
        base = nao_triviais or candidatos
        ordenados = sorted(base, key=lambda m: (not pratico(m), params(m)))

    else:  # "geral"
        ordenados = sorted(candidatos, key=lambda m: (pratico(m), params(m)), reverse=True)

    return [m["tag"] for m in ordenados]


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
        fabricante = f"{gpu_info['fabricante']} " if gpu_info.get("fabricante") else ""
        print(
            f"GPU {fabricante}detectada: {gpu_info['vram_total_gb']}GB VRAM "
            f"({gpu_info['vram_livre_gb']}GB livres agora)"
        )
    else:
        print("GPU dedicada não detectada — modelos vão rodar na CPU.")

    print()

    if not catalogo:
        print(
            c(
                "Nenhum modelo encontrado no Ollama (ele pode estar "
                "offline, ou nenhum modelo foi baixado ainda — use "
                "'ollama pull <modelo>').",
                Cor.AMARELO,
            )
        )
        print()
        return

    # Agrupa por categoria só pra exibição ficar organizada (visão,
    # técnico, simples, geral) — cada IA aparece do maior pro menor
    # dentro da própria categoria.
    ordem_categorias = ["visao", "tecnico", "geral", "simples"]

    for categoria in ordem_categorias:

        modelos_cat = [m for m in catalogo.values() if m["categoria"] == categoria]

        if not modelos_cat:
            continue

        modelos_cat.sort(
            key=lambda m: m["parametros_b"] if m["parametros_b"] is not None else 0,
            reverse=True,
        )

        for info in modelos_cat:

            encaixe = classificar_encaixe(info["vram_estimado_gb"], gpu_info)
            marcador = _MARCADOR_ENCAIXE.get(encaixe, "")

            capacidades_visiveis = sorted(
                info["capacidades"] & _CAPACIDADES_RELEVANTES
            )

            print(f"  {info['emoji']} {info['tag']}")

            detalhe_linha = _DESCRICAO_CATEGORIA.get(categoria, "")
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
                print(f"     capacidades: {', '.join(capacidades_visiveis)}")

            print()

    n_embedding = sum(1 for m in catalogo.values() if m["categoria"] == "embedding")

    if n_embedding:
        print(
            c(
                f"  ({n_embedding} modelo(s) de embedding instalado(s) "
                "não aparecem aqui — servem só para gerar vetores, não "
                "para conversar, então não entram na escolha de IA.)",
                Cor.CINZA,
            )
        )
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

def _consultar_modelo_leve(prompt):
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
    )


def perguntar_candidatos_app(nome_pedido):
    """Em vez de um dicionário fixo de aliases, pergunta pro modelo
    leve quais nomes de executável/pacote Linux provavelmente
    correspondem ao app pedido. Generaliza pra qualquer aplicativo,
    inclusive os que nunca foram cadastrados manualmente."""

    prompt = (
        "Um usuário de Linux (Zorin OS) pediu para abrir um "
        f"aplicativo chamado: '{nome_pedido}'.\n"
        "Liste de 1 a 5 nomes prováveis de executável ou pacote Linux "
        "para esse aplicativo (nomes reais de binários, como os "
        "instalados via apt, flatpak ou snap; ex: 'brave-browser', "
        "'gnome-calculator').\n"
        "Responda APENAS com os nomes separados por vírgula — sem "
        "explicação, sem markdown, sem numeração."
    )

    conteudo = _consultar_modelo_leve(prompt)

    if not conteudo:
        return []

    candidatos = [
        parte.strip().strip(".").strip("'\"")
        for parte in re.split(r"[,\n]", conteudo)
        if parte.strip()
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

    try:
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]

        if not host:
            return False

        socket.setdefaulttimeout(TIMEOUT_DNS)
        socket.gethostbyname(host)
        return True

    except Exception:
        return False

    finally:
        socket.setdefaulttimeout(None)


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
        "Qual é a URL oficial do site ou serviço a seguir: "
        f"'{nome_pedido}'?\n"
        "Responda APENAS com a URL completa, começando com http:// ou "
        "https://, sem nenhum texto adicional.\n"
        "Se não tiver certeza absoluta da URL exata (por exemplo, uma "
        "página, canal ou perfil específico de alguém dentro de um "
        "site maior), responda exatamente: desconhecido — NUNCA "
        "invente uma URL só para dar alguma resposta."
    )

    conteudo = _consultar_modelo_leve(prompt)

    if not conteudo or "desconhecido" in normalizar(conteudo):
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

# Verbos que indicam claramente um pedido de BUSCA (não de abrir uma
# página específica) — quando presentes, mesmo sem nenhum serviço
# citado, a busca vai direto pro Google em vez de tentar adivinhar uma
# URL exata pra "buscar X".
_VERBOS_BUSCA = {
    "pesquisar", "pesquise", "pesquisa", "buscar", "busque", "busca",
    "procurar", "procure", "procura",
}

_CONECTORES_BUSCA = ("sobre", "por", "de", "a respeito de", "referente a")


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

    tem_verbo_busca = bool(tokens_espaco & _VERBOS_BUSCA)

    servico_citado = next(
        (s for s in _BUSCAS_POR_SERVICO if s in tokens_espaco),
        None,
    )

    if not tem_verbo_busca and not servico_citado:
        return None

    url_busca = _BUSCAS_POR_SERVICO.get(servico_citado, GOOGLE_SEARCH_URL)

    resto = entrada_n

    for verbo in _VERBOS_BUSCA:
        resto = re.sub(rf"\b{verbo}\b", " ", resto)

    if servico_citado:
        # Remove o nome do serviço e a preposição que normalmente vem
        # junto ("no youtube", "do youtube") — sem isso a busca ficava
        # com uma preposição solta no final ("canal da anthropic no").
        resto = re.sub(rf"\b(no|na|do|da|de)?\s*{servico_citado}\b", " ", resto)

    resto = re.sub(r"\s+", " ", resto).strip()

    # Conectores ("sobre", "por", "de"...) só são removidos do INÍCIO
    # da consulta (ex: "sobre buracos negros" → "buracos negros") —
    # nunca no meio ou fim, senão uma consulta legítima como "receita
    # de bolo de cenoura" perderia os "de" que fazem parte dela.
    mudou = True
    while mudou:
        mudou = False
        for conector in _CONECTORES_BUSCA:
            nova = re.sub(rf"^{conector}\b\s*", "", resto)
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


# Idiomas cujo nome traduzido também vale pra achar o app: "abra a
# calculadora" precisa bater com Name[pt_BR]=Calculadora, já que o
# Name= padrão é em inglês (Calculator).
_SUFIXOS_IDIOMA_APP = ("[pt_BR]", "[pt]")


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
        for sufixo in ("",) + _SUFIXOS_IDIOMA_APP:
            valor = campos.get(base + sufixo)
            if valor and valor != nome and valor not in outros_nomes:
                outros_nomes.append(valor)

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
        "_nomes_n": [normalizar(n) for n in [nome] + outros_nomes],
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

    alvo_n = normalizar(alvo_original)

    for entrada in entradas:

        # Nome principal e os traduzidos (pt_BR) / genéricos contam
        # igual — "calculadora" acha o "Calculator".
        nomes_n = entrada.get("_nomes_n")
        if nomes_n is None:  # entrada montada fora de _ler_desktop_entry
            nomes_n = [normalizar(n) for n in [entrada["nome"]] + entrada.get("outros_nomes", [])]
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


def abrir_aplicativo(app):
    app = app.strip()

    encontrado = encontrar_app(app)

    if not encontrado:

        return {
            "sucesso": False,
            "acao": "open_application",
            "aplicativo": app,
            "mensagem": (
                f"APLICATIVO NÃO ENCONTRADO: '{app}'. "
                "A ferramenta não conseguiu localizar o aplicativo."
            ),
        }

    comando = encontrado["comando"]

    try:

        subprocess.Popen(
            comando,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # confirmação básica — checa por polling em vez de um sleep
        # fixo: a maioria dos apps (calculadora, editor de texto) já
        # sobe em bem menos de 1s, então detectar isso cedo deixa o
        # próximo passo (digitar, clicar) rodar mais rápido; e um app
        # pesado (navegador, suíte de escritório) ganha até
        # TIMEOUT_POLL_PROCESSO_SEG pra aparecer, em vez de só 0.25s
        # fixos como antes.
        executavel = comando[0]
        base = os.path.basename(executavel)
        app_lower = app.lower()

        ativo = False
        inicio_poll = time.time()

        while time.time() - inicio_poll < TIMEOUT_POLL_PROCESSO_SEG:

            for proc in psutil.process_iter(["name", "cmdline"]):

                try:

                    nome_proc = proc.info["name"] or ""
                    cmdline = " ".join(proc.info["cmdline"] or [])

                    if (
                        base == nome_proc
                        or base in cmdline
                        or app_lower in cmdline.lower()
                    ):
                        ativo = True
                        break

                except Exception:
                    continue

            if ativo:
                break

            time.sleep(INTERVALO_POLL_PROCESSO_SEG)

        origem = (
            encontrado["nome"]
            if encontrado["tipo"] == "desktop"
            else encontrado.get("origem", "")
        )

        if ativo:

            return {
                "sucesso": True,
                "acao": "open_application",
                "aplicativo": app,
                "encontrado_como": origem,
                "mensagem": (
                    f"SUCESSO: o aplicativo '{app}' foi aberto corretamente."
                ),
            }

        # Mesmo se não conseguirmos confirmar o processo,
        # o comando foi executado.
        return {
            "sucesso": True,
            "acao": "open_application",
            "aplicativo": app,
            "encontrado_como": origem,
            "mensagem": f"SUCESSO: o comando para abrir '{app}' foi executado.",
        }

    except Exception as e:

        return {
            "sucesso": False,
            "acao": "open_application",
            "aplicativo": app,
            "mensagem": f"ERRO ao abrir '{app}': {e}",
        }


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

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):

        try:

            pid = proc.info["pid"]
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


def fechar_aplicativo(app):
    alvo_n = normalizar(app)

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
            "mensagem": f"SUCESSO: aplicativo '{app}' encerrado.",
            "pids": encerrados,
        }

    return {
        "sucesso": False,
        "acao": "close_application",
        "aplicativo": app,
        "mensagem": f"Nenhum processo correspondente a '{app}' foi encontrado.",
    }


# ============================================================
# SITES
# ============================================================

# Nomes que quase sempre significam "abra o NAVEGADOR", não "visite um
# site chamado assim" — um modelo (principalmente os menores) às vezes
# chama open_website pra isso, quando o certo seria open_application.
# Em vez de depender só do prompt pra evitar essa confusão, a própria
# ferramenta redireciona pra abrir o aplicativo de verdade.
_NOMES_NAVEGADORES = {
    "chrome", "google chrome", "firefox", "brave", "edge",
    "microsoft edge", "opera", "safari", "navegador", "browser",
}


def abrir_site(site):
    entrada = site.strip()

    # Hífen vira espaço só pra essa checagem ("google-chrome" ==
    # "google chrome") — não afeta o resto da função.
    entrada_normalizada = re.sub(r"[\s-]+", " ", normalizar(entrada)).strip()

    if entrada_normalizada in _NOMES_NAVEGADORES:
        resultado = abrir_aplicativo(entrada)
        resultado["mensagem"] = (
            f"(pedido era abrir '{entrada}' como site, mas isso é um "
            "navegador — abri como aplicativo em vez disso) "
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
            "mensagem": f"SUCESSO: o site '{entrada}' foi aberto em {url}.",
        }

    except Exception as e:

        return {
            "sucesso": False,
            "acao": "open_website",
            "entrada": entrada,
            "mensagem": f"ERRO ao abrir o site: {e}",
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
            "mensagem": "ERRO: nenhum termo de busca foi informado (campo 'query' vazio).",
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
            "mensagem": f"SUCESSO: pesquisei '{query}' e abri o resultado em {url}.",
        }

    except Exception as e:

        return {
            "sucesso": False,
            "acao": "search_web",
            "mensagem": f"ERRO ao pesquisar: {e}",
        }


# ============================================================
# OUTRAS FERRAMENTAS
# ============================================================

def diretorio_atual():
    return {
        "sucesso": True,
        "diretorio": os.getcwd(),
        "mensagem": f"Diretório atual: {os.getcwd()}",
    }


def listar_arquivos(caminho="."):
    try:

        pasta = Path(caminho).expanduser()

        if not pasta.exists():
            return {
                "sucesso": False,
                "mensagem": f"O caminho '{caminho}' não existe.",
            }

        arquivos = [
            f"[{'DIR' if item.is_dir() else 'FILE'}] {item.name}"
            for item in sorted(pasta.iterdir(), key=lambda x: x.name.lower())
        ]

        return {
            "sucesso": True,
            "caminho": str(pasta.resolve()),
            "arquivos": arquivos[:LIMITE_ARQUIVOS_LISTADOS],
            "mensagem": f"{len(arquivos)} itens encontrados.",
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
    r"\brm\b.*-[a-z]*r[a-z]*f|\brm\b.*-[a-z]*f[a-z]*r",  # rm -rf / -fr, em qualquer ordem de flags
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
        "⚠ COMANDO POTENCIALMENTE PERIGOSO",
        [
            comando[:200],
            "Pode apagar dados ou afetar o sistema de forma irreversível.",
        ],
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
            resposta = input(
                c("Executar mesmo assim? (s/N) ", Cor.AMARELO)
            ).strip().lower()
        except (EOFError, OSError):
            resposta = ""
        confirmado = resposta in ("s", "sim", "y", "yes")

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
                "Comando cancelado: bateu com um padrão potencialmente "
                "destrutivo e não foi confirmado pelo usuário."
            ),
        }

    try:

        resultado = subprocess.run(
            comando,
            shell=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_TERMINAL_COMANDO,
        )

        return {
            "sucesso": resultado.returncode == 0,
            "codigo": resultado.returncode,
            "stdout": resultado.stdout[-LIMITE_STDOUT_CHARS:],
            "stderr": resultado.stderr[-LIMITE_STDERR_CHARS:],
            "mensagem": (
                "Comando executado."
                if resultado.returncode == 0
                else "Comando terminou com erro."
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
        "mensagem": "Informações do computador obtidas.",
    }


def mover_mouse(x, y):
    try:
        pyautogui.moveTo(int(x), int(y), duration=DURACAO_MOVER_MOUSE_SEG)
        return {"sucesso": True, "mensagem": f"Mouse movido para ({x}, {y})."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def posicao_mouse():
    try:
        x, y = pyautogui.position()
        return {
            "sucesso": True,
            "x": x,
            "y": y,
            "mensagem": f"Mouse está em ({x}, {y}).",
        }
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def tamanho_tela():
    try:
        largura, altura = pyautogui.size()
        return {
            "sucesso": True,
            "largura": largura,
            "altura": altura,
            "mensagem": f"Tela tem {largura}x{altura} pixels.",
        }
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def clicar_mouse(botao="left", x=None, y=None):
    try:
        # Clicar direto num ponto (x, y) evita precisar de duas
        # chamadas (move_mouse + click_mouse) pra cada clique.
        if x is not None and y is not None:
            pyautogui.click(x=int(x), y=int(y), button=botao)
            local = f" em ({x}, {y})"
        else:
            pyautogui.click(button=botao)
            local = ""

        return {"sucesso": True, "mensagem": f"Mouse clicado com botão {botao}{local}."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def duplo_clique(x=None, y=None):
    try:
        if x is not None and y is not None:
            pyautogui.doubleClick(x=int(x), y=int(y))
        else:
            pyautogui.doubleClick()

        return {"sucesso": True, "mensagem": "Duplo clique executado."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def scroll_mouse(valor, x=None, y=None):
    try:
        if x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y), duration=DURACAO_MOVER_PARA_SCROLL_SEG)

        pyautogui.scroll(int(valor))
        return {"sucesso": True, "mensagem": f"Scroll executado: {valor}."}
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
            return {"sucesso": True, "mensagem": "Texto digitado."}

        # Texto com acentos/outros idiomas: cola via clipboard em vez
        # de tecla-a-tecla, o que também é mais rápido pra textos
        # longos além de mais confiável.
        if _definir_clipboard(texto):
            pyautogui.hotkey("ctrl", "v")
            return {"sucesso": True, "mensagem": "Texto digitado (via clipboard)."}

        # Sem xclip/xsel disponível: melhor esforço mesmo assim.
        pyautogui.write(texto, interval=INTERVALO_DIGITACAO_SEG)
        return {
            "sucesso": True,
            "mensagem": (
                "Texto digitado, mas sem xclip/xsel instalado — "
                "caracteres especiais podem não ter saído certos."
            ),
        }

    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def pressionar_tecla(tecla):
    try:
        pyautogui.press(tecla)
        return {"sucesso": True, "mensagem": f"Tecla '{tecla}' pressionada."}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


def atalho_teclado(teclas):
    try:

        if isinstance(teclas, str):
            teclas = [x.strip() for x in teclas.split("+")]

        pyautogui.hotkey(*teclas)

        return {
            "sucesso": True,
            "mensagem": f"Atalho executado: {'+'.join(teclas)}.",
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
        "mensagem": f"SUCESSO: esperou {segundos:.1f}s.",
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
                erros.append(f"{programa}: imagem preta")
            except Exception as e:
                erros.append(f"{programa}: {e}")

    try:
        img = ImageGrab.grab()
        if SESSAO_GRAFICA == "wayland" and _imagem_toda_preta(img):
            raise RuntimeError("a sessão é Wayland e a leitura pelo X devolveu uma tela preta")
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
        " Instale um capturador: sudo apt install gnome-screenshot "
        "(GNOME/Zorin/Ubuntu), kde-spectacle (KDE) ou grim (Sway/Hyprland)."
        if SESSAO_GRAFICA == "wayland"
        else ""
    )
    raise RuntimeError("; ".join(erros) + "." + dica)


def tirar_print():
    """Captura a tela atual. A imagem em base64 fica em '_imagem_b64'
    (removida do JSON antes de virar histórico de texto) e é anexada
    à conversa como uma mensagem de usuário com 'images', pro modelo
    com suporte a visão (Gemma 3, Qwen 3.8) realmente enxergar a
    tela — não só receber um texto dizendo que um print foi tirado."""

    try:
        captura = _capturar_tela()
        if captura.mode != "RGB":  # só converte (e copia) se precisar
            captura = captura.convert("RGB")

        largura_original, altura_original = captura.size

        # Encolhe antes de codificar se a tela for maior que o teto
        # configurado — ver LARGURA_MAXIMA_SCREENSHOT. Mantém a
        # proporção original; nunca AUMENTA uma tela menor que o teto.
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

        return {
            "sucesso": True,
            "acao": "take_screenshot",
            "mensagem": (
                f"SUCESSO: print da tela capturado "
                f"({largura_original}x{altura_original}"
                + (
                    f", redimensionado para {largura}x{altura} antes de "
                    "enviar"
                    if (largura, altura) != (largura_original, altura_original)
                    else ""
                )
                + ")."
            ),
            "_imagem_b64": imagem_b64,
        }

    except Exception as e:
        return {
            "sucesso": False,
            "acao": "take_screenshot",
            "mensagem": f"ERRO ao capturar a tela: {e}",
        }


# ============================================================
# FERRAMENTAS PARA OLLAMA
# ============================================================

TOOLS = [

    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": (
                "Abre QUALQUER aplicativo instalado no sistema, "
                "identificado pelo nome comum que o usuário usar "
                "(navegador, editor de texto, calculadora, terminal, "
                "gerenciador de arquivos, um jogo, etc.) — a ferramenta "
                "resolve o nome real por conta própria. Sempre use esta "
                "ferramenta para abrir aplicativos, mesmo que o nome "
                "pedido não seja reconhecido de antemão."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Nome do aplicativo que deve ser aberto.",
                    }
                },
                "required": ["app"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "close_application",
            "description": "Fecha um aplicativo atualmente aberto.",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string"}},
                "required": ["app"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "open_website",
            "description": (
                "Abre QUALQUER site ou serviço online no navegador, "
                "pelo nome comum ou pela URL — a ferramenta resolve o "
                "endereço real por conta própria, mesmo que o site não "
                "seja reconhecido de antemão."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {
                        "type": "string",
                        "description": "Nome do site ou URL.",
                    }
                },
                "required": ["site"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Pesquisa um termo na web e abre o resultado no navegador. "
                "Use esta ferramenta (em vez de open_website) sempre que o "
                "pedido for uma BUSCA — 'pesquisar', 'buscar', 'procurar' "
                "algo, ou perguntar por algo 'no google'/'no youtube' — em "
                "vez de tentar montar a URL de busca você mesmo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Só os termos da busca em si, sem o verbo "
                            "('pesquisar', 'buscar'...) e sem o nome do "
                            "serviço (ex: para 'pesquise rtx 5060 no "
                            "google', query é apenas 'rtx 5060')."
                        ),
                    },
                    "service": {
                        "type": "string",
                        "description": (
                            "Onde pesquisar, se o usuário citar um "
                            "serviço específico (ex: 'google', 'youtube'). "
                            "Se nada for citado, não inclua este campo — "
                            "o padrão já é o Google."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "execute_terminal",
            "description": "Executa um comando no terminal Linux.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "get_current_directory",
            "description": "Retorna o diretório atual.",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lista arquivos e pastas de um diretório.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "pc_info",
            "description": "Obtém informações sobre CPU, RAM e armazenamento.",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "move_mouse",
            "description": "Move o mouse para uma posição da tela.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "get_mouse_position",
            "description": "Retorna a posição atual do mouse (x, y).",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "get_screen_size",
            "description": "Retorna a resolução da tela em pixels (largura, altura).",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "click_mouse",
            "description": (
                "Clica com o mouse. Se x/y forem informados, clica "
                "direto nessa posição (sem precisar de move_mouse antes)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                    },
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Executa um duplo clique (opcionalmente numa posição x/y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "scroll_mouse",
            "description": "Rola a página/janela (opcionalmente numa posição x/y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["amount"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "Digita texto usando o teclado, em qualquer idioma "
                "(acentos, outros alfabetos, emoji incluídos)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Pressiona uma tecla.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Executa uma combinação de teclas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["keys"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": (
                "Tira um print da tela atual para você poder VER o "
                "que está acontecendo. Use quando o usuário pedir pra "
                "olhar, analisar ou descrever a tela, uma janela, ou "
                "algo que só faz sentido vendo a interface."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "wait_seconds",
            "description": (
                "Espera um pouco antes do próximo passo, em segundos "
                "(no máximo alguns segundos). Use isso DEPOIS de abrir "
                "um app ou site e ANTES de digitar/clicar nele, quando "
                "achar que ele pode ainda estar carregando (navegador, "
                "programas mais pesados) — evita digitar ou clicar "
                "antes da janela estar pronta pra receber."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "Quantos segundos esperar (ex: 1.5).",
                    }
                },
                "required": ["seconds"],
            },
        },
    },
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
    "take_screenshot": lambda a: tirar_print(),
    "wait_seconds": lambda a: esperar(a.get("seconds", 1.0)),
}

_FERRAMENTAS_MOUSE_TECLADO = {
    "move_mouse", "click_mouse", "double_click", "scroll_mouse",
    "type_text", "press_key", "hotkey",
}

AVISO_WAYLAND_FERRAMENTA = (
    "ATENÇÃO: a sessão gráfica é Wayland, onde cliques e teclas simulados "
    "só chegam a alguns aplicativos (os que rodam via XWayland). Se nada "
    "aconteceu na tela, diga ao usuário que o controle de mouse/teclado "
    "precisa da sessão Xorg (escolhida na tela de login)."
)


def executar_ferramenta(nome, argumentos):

    inicio = time.time()

    print(c(f"[TARS] ferramenta: {nome}", Cor.CIANO))

    handler = _DISPATCH_FERRAMENTAS.get(nome)

    try:

        if handler is not None:
            resultado = handler(argumentos)
        else:
            resultado = {
                "sucesso": False,
                "mensagem": f"Ferramenta desconhecida: {nome}",
            }

    except Exception as e:

        resultado = {
            "sucesso": False,
            "mensagem": f"Erro interno na ferramenta '{nome}': {e}",
        }

    # Numa sessão Wayland, o pyautogui "consegue" mover/clicar/digitar
    # sem erro nenhum, mas o evento só chega a janelas XWayland. Avisa
    # a IA pra ela poder explicar ao usuário em vez de fingir sucesso.
    if SESSAO_GRAFICA == "wayland" and nome in _FERRAMENTAS_MOUSE_TECLADO:
        resultado["mensagem"] = (
            f"{resultado.get('mensagem', '')} {AVISO_WAYLAND_FERRAMENTA}"
        ).strip()

    tempo = time.time() - inicio

    cor_status = Cor.VERDE if resultado.get("sucesso") else Cor.VERMELHO
    marcador = "✓" if resultado.get("sucesso") else "✗"

    print(c(f"[TARS] {marcador} {nome} concluído ({tempo:.2f}s)", cor_status))

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

# Listas movidas para o escopo do módulo: antes eram recriadas a cada
# chamada de detectar_tarefa(), que roda em todo turno do usuário.
_PALAVRAS_VISAO = (
    "veja minha tela", "olhe minha tela", "analise minha tela",
    "o que esta na tela", "clique", "clique em", "pressione",
    "digite na tela", "interface", "janela", "botao", "botão",
    "mouse", "teclado",
)

_PALAVRAS_TECNICO = (
    "python", "codigo", "código", "programacao", "programação",
    "programar", "script", "linux", "terminal", "bash", "shell",
    "ubuntu", "zorin", "kernel", "driver", "gpu", "cuda", "ollama",
    "docker", "git", "erro", "debug", "compilar",
)

# Verbos que indicam FECHAR ou uma ação composta/ambígua sobre janelas
# e abas — checados antes de qualquer coisa, porque "feche o brave"
# ou "feche todas as abas" não podem cair no balde de site conhecido
# só por citarem o nome do navegador; fechar quase sempre carrega
# ambiguidade (fechar o quê exatamente, tudo ou só uma aba?) que pede
# um modelo com raciocínio melhor que o mais simples do catálogo.
_PALAVRAS_FECHAR = (
    "feche ", "fecha ", "fechar ", "abas", "aba do", "aba de",
)

# Alvos bem conhecidos (site ou app) onde ABRIR é um único tool call
# óbvio, sem ambiguidade nenhuma pra resolver — é a única "ação" leve
# o bastante pro modelo minúsculo (ex: qwen 0.6b) dar conta sozinho.
_PALAVRAS_SITES = (
    "site ", "abra o site", "abra a pagina", "abra a página",
    "youtube", "chatgpt", "google", "spotify", "discord", "steam",
    "brave", "firefox", "calculadora",
)

# Verbos de busca ("pesquisar", "buscar"...) formam categoria PRÓPRIA,
# separada de _PALAVRAS_SITES: na prática, uma busca precisa que o
# modelo escolha a ferramenta certa (search_web, não open_website) E
# separe corretamente "o que pesquisar" de "onde pesquisar" — duas
# decisões de raciocínio que o modelo minúsculo (ex: qwen 0.6b) mostrou
# na prática não fazer de forma confiável (ele tende a repetir o padrão
# de chamada da mensagem anterior em vez de reavaliar qual ferramenta
# usar). Por isso "busca" nunca usa esse modelo — ver
# construir_cadeia_fallback — igual já acontecia com "acao".
_PALAVRAS_BUSCA = (
    "pesquisar", "pesquise", "pesquisa", "buscar", "busque", "busca",
    "procurar", "procure", "procura",
)

# Verbos genéricos de ação no desktop — abrir programas, rodar
# comandos de terminal etc. — quando o alvo NÃO é um site/app bem
# conhecido (ex: "abra o gerenciador de arquivos"). Aqui o pedido
# costuma ser ambíguo (qual app exatamente? um comando de verdade ou
# só uma conta de matemática escrita em português?) e exige um modelo
# com raciocínio melhor que o mais simples do catálogo — foi
# justamente esse tipo de pedido que o modelo minúsculo recusava ou
# confundia com um comando de terminal.
_PALAVRAS_ACAO = (
    "abra ", "abre ", "abrir ", "inicie ", "inicia ", "iniciar ",
    "execute ", "executar ", "rodar ", "rode ", "comando",
    "arquivo", "arquivos", "pasta",
)

# Limite de palavras pra considerar uma frase "conversa curta" (sem
# nenhuma das categorias acima) — bate-papo bem simples também cai no
# mesmo balde de modelo leve que abrir URL, em vez de sempre acordar o
# modelo mais pesado só pra dizer "oi".
_LIMITE_PALAVRAS_CONVERSA_SIMPLES = 6

_CATEGORIAS_VALIDAS = ("visao", "tecnico", "acao", "busca", "simples", "geral")

_PROMPT_CLASSIFICADOR = (
    "Classifique o pedido do usuário em UMA destas categorias e "
    "responda só com a palavra da categoria, em minúsculas, sem "
    "explicação nenhuma:\n"
    "visao = ver/analisar a tela, clicar, mover o mouse, digitar em "
    "algum lugar da interface gráfica\n"
    "tecnico = programação, Linux, terminal, comandos técnicos, "
    "depurar código\n"
    "acao = abrir ou fechar programas, rodar comandos no computador "
    "(quando não é claramente visao nem tecnico)\n"
    "busca = pesquisar, buscar ou procurar algo na internet\n"
    "simples = abrir um site/app bem conhecido, ou bate-papo bem curto\n"
    "geral = qualquer outra pergunta, pedido de conteúdo ou conversa\n\n"
    "Pedido do usuário: "
)


def _classificar_com_ia(texto):
    """Segunda camada de detecção de tarefa, só usada quando nenhuma
    palavra-chave de detectar_tarefa() reconheceu nada — ou seja, só
    entra em ação nos casos ambíguos, pra não pagar o custo extra na
    maioria das mensagens (que já são resolvidas pelas palavras-chave,
    de graça). Usa um modelo minúsculo dedicado (ver
    MODELO_CLASSIFICADOR) que fica de fora do catálogo principal de
    conversa — se ele não estiver instalado, ou a chamada falhar por
    qualquer motivo, simplesmente devolve None e quem chamou cai no
    comportamento de antes (categoria "geral")."""

    try:
        instalados = listar_modelos_instalados()
    except Exception:
        instalados = None

    if not instalados or MODELO_CLASSIFICADOR not in instalados:
        return None

    bruto = _chat_unico(
        MODELO_CLASSIFICADOR,
        _PROMPT_CLASSIFICADOR + texto,
        temperatura=TEMPERATURA_CLASSIFICADOR,
        timeout=TIMEOUT_CLASSIFICADOR,
        keep_alive=KEEP_ALIVE_CLASSIFICADOR,
    )

    if not bruto:
        return None

    bruto_normalizado = normalizar(bruto)

    for categoria in _CATEGORIAS_VALIDAS:
        if categoria in bruto_normalizado:
            return categoria

    return None


def detectar_tarefa(texto):
    t = normalizar(texto)

    # _contem_palavra_inteira evita que uma palavra-chave curta bata
    # como substring solta dentro de outra palavra sem relação nenhuma
    # (ex: "git" dentro de "digital", "erro" dentro de "aterrorizante")
    # — um `in` ingênuo já causou esse tipo de falso positivo antes na
    # detecção de troca de modelo, e as mesmas listas curtas aqui
    # tinham o mesmo risco.
    if _contem_palavra_inteira(t, _PALAVRAS_VISAO):
        return "visao"

    # FECHAR e SITES vêm antes de TECNICO de propósito: eles reconhecem
    # um verbo ou alvo EXPLÍCITO e inequívoco ("feche ", "youtube",
    # "google"...), enquanto TECNICO só reconhece uma palavra-tópico
    # solta que pode aparecer dentro de qualquer frase — inclusive uma
    # que não tem nada de técnico, tipo "abra o google chrome e
    # pesquise ollama" (o "ollama" ali é só o ASSUNTO da busca, não um
    # pedido técnico). Um verbo/alvo explícito é sinal mais forte que
    # uma palavra-tópico e por isso deve ganhar a prioridade.
    if _contem_palavra_inteira(t, _PALAVRAS_FECHAR):
        return "acao"

    # BUSCA vem antes de SITES: "pesquise X no google" precisa que o
    # modelo escolha entre duas ferramentas (search_web x open_website)
    # e separe corretamente o termo do serviço — raciocínio que o
    # modelo minúsculo de "simples" não faz de forma confiável (ver
    # _PALAVRAS_BUSCA). Um pedido só de abrir um site conhecido, sem
    # nenhum verbo de busca, continua em "simples" normalmente.
    if _contem_palavra_inteira(t, _PALAVRAS_BUSCA):
        return "busca"

    if _contem_palavra_inteira(t, _PALAVRAS_SITES):
        return "simples"

    if _contem_palavra_inteira(t, _PALAVRAS_TECNICO):
        return "tecnico"

    if _contem_palavra_inteira(t, _PALAVRAS_ACAO):
        return "acao"

    if len(t.split()) <= _LIMITE_PALAVRAS_CONVERSA_SIMPLES:
        return "simples"

    # Nenhuma palavra-chave bateu — antes de cair direto em "geral",
    # tenta a IA classificadora minúscula (se estiver instalada). Ela
    # aguenta frases fora do padrão sem precisar carregar o modelo
    # principal de conversa só pra decidir qual modelo usar.
    categoria_ia = _classificar_com_ia(texto)

    return categoria_ia if categoria_ia else "geral"


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


def escolher_modelo(texto, tarefa=None):

    global modo_modelo
    global modelo_manual

    if modo_modelo == "MANUAL" and modelo_manual:
        return modelo_manual

    if tarefa is None:
        tarefa = detectar_tarefa(texto)

    return proximo_modelo_disponivel(tarefa)


# ============================================================
# COMANDOS DE MODELO
# ============================================================

def mostrar_modelos():

    print()
    print("IAs disponíveis:")
    print()

    _imprimir_lista_modelos()

    print(f"Modo atual: {modo_modelo}")

    if modo_modelo == "MANUAL":
        print(f"Modelo fixado: {modelo_manual}")
    else:
        print(
            "O TARS escolhe automaticamente "
            "a IA mais adequada para cada tarefa."
        )


def _contem_palavra_inteira(t, expressoes):
    """Verifica se alguma das 'expressoes' aparece em t como palavra
    inteira — não como substring solta dentro de outra palavra. Um
    `in` ingênuo faz "git" bater dentro de "digital", ou "erro" dentro
    de "aterrorizante", disparando falsos positivos de categorização.
    Expressões com mais de uma palavra (ex: "feche ") continuam
    comparadas por substring direto, já que são frases inteiras, não
    uma palavra só — usado por detectar_tarefa() para as categorias de
    tarefa (ver mais abaixo)."""

    tokens = None  # calculado só se precisar (frases de uma palavra só)

    for expr in expressoes:
        if " " in expr:
            if expr in t:
                return True
        else:
            if tokens is None:
                tokens = set(re.split(r"[^a-z0-9]+", t))
            if expr in tokens:
                return True

    return False


# Palavras que não ajudam em nada a identificar QUAL modelo foi pedido
# (conectores comuns) — removidas antes da comparação por token em
# _resolver_modelo_por_texto_livre, senão uma frase como "o gemma
# 270m, por favor" exigiria que a tag contivesse literalmente "favor"
# pra bater.
_PALAVRAS_IGNORAR_RESOLUCAO = {
    "o", "a", "os", "as", "de", "do", "da", "dos", "das", "pra", "para",
    "por", "favor", "modelo", "ia", "llm", "no", "na", "use", "usar",
    "usa", "quero", "troca", "trocar", "troque", "mude", "mudar",
    "carregue", "carregar", "selecione", "selecionar",
}

# Superlativos que resolvem pra um modelo do catálogo por critério, em
# vez de nome — funcionam com qualquer conjunto de IAs instaladas.
_SUPERLATIVOS_MODELO = {
    "o menor": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "a menor": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "o mais leve": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "mais leve": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "o mais rapido": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "o mais rápido": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "mais rapido": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "mais rápido": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "o padrao": lambda cat: _extremo_por_tamanho(cat, menor=True),
    "o padrão": lambda cat: _extremo_por_tamanho(cat, menor=True),

    "o maior": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "a maior": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "o mais forte": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "mais forte": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "o mais poderoso": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "mais poderoso": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "o mais inteligente": lambda cat: _extremo_por_tamanho(cat, menor=False),
    "mais inteligente": lambda cat: _extremo_por_tamanho(cat, menor=False),

    "o de visao": lambda cat: _melhor_por_capacidade(cat, "vision"),
    "o de visão": lambda cat: _melhor_por_capacidade(cat, "vision"),
    "modelo de visao": lambda cat: _melhor_por_capacidade(cat, "vision"),
    "modelo de visão": lambda cat: _melhor_por_capacidade(cat, "vision"),
    "o que enxerga": lambda cat: _melhor_por_capacidade(cat, "vision"),

    "o tecnico": lambda cat: _melhor_por_categoria(cat, "tecnico"),
    "o técnico": lambda cat: _melhor_por_categoria(cat, "tecnico"),
    "o de programacao": lambda cat: _melhor_por_categoria(cat, "tecnico"),
    "o de programação": lambda cat: _melhor_por_categoria(cat, "tecnico"),
    "o de codigo": lambda cat: _melhor_por_categoria(cat, "tecnico"),
    "o de código": lambda cat: _melhor_por_categoria(cat, "tecnico"),
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

    for frase, resolvedor in _SUPERLATIVOS_MODELO.items():
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
        if len(tok) >= 2 and tok not in _PALAVRAS_IGNORAR_RESOLUCAO
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

    if not instalados or MODELO_CLASSIFICADOR not in instalados:
        return None

    tags = sorted(catalogo.keys())

    prompt = (
        "O usuário quer trocar o modelo de IA usado por um assistente "
        "local. Estes são os ÚNICOS modelos instalados — escolha o que "
        "mais combina com o pedido e responda só com a tag exata, "
        "copiada da lista, sem explicação nenhuma. Se nenhum combinar "
        "ou o pedido não for sobre trocar de modelo, responda "
        "exatamente: nenhum\n\n"
        f"Modelos instalados: {', '.join(tags)}\n\n"
        f"Pedido do usuário: {texto_livre}"
    )

    bruto = _chat_unico(
        MODELO_CLASSIFICADOR,
        prompt,
        temperatura=TEMPERATURA_CLASSIFICADOR,
        timeout=TIMEOUT_CLASSIFICADOR,
        keep_alive=KEEP_ALIVE_CLASSIFICADOR,
    )

    if not bruto:
        return None

    bruto_n = normalizar(bruto)

    for tag in tags:
        if normalizar(tag) == bruto_n or normalizar(tag) in bruto_n:
            return tag

    return None


# Formas de pedir para descarregar TODAS as IAs da memória (não só a
# atual). Todas já normalizadas (minúsculas, sem acento).
_COMANDOS_ENCERRAR_TODAS = [
    "/encerrar",
    "/encerrar todas",
    "/encerrar todas llms",
    "/encerrar todas as ias",
    "/encerrar todos os modelos",
    "encerrar todas llms",
    "encerrar todas as llms",
    "encerrar todas as ias",
    "encerrar todos os modelos",
    "descarregar todas as ias",
    "descarregar todos os modelos",
    "descarregar todas as llms",
    "parar todas as ias",
    "parar todos os modelos",
    "parar todas as llms",
]


def interpretar_comando_modelo(texto):

    global modo_modelo
    global modelo_manual

    t = normalizar(texto)

    if t in ["/modelo", "modelos", "mostrar modelos", "listar modelos"]:
        mostrar_modelos()
        return True

    if t in _COMANDOS_ENCERRAR_TODAS:
        encerrar_todas_ias()
        return True

    if t in ["/limpar", "limpar conversa", "esquecer conversa", "esqueca a conversa"]:
        modelo_da_sessao = (
            modelo_manual if (modo_modelo == "MANUAL" and modelo_manual) else modelo_atual
        )
        limpar_sessao(modelo_da_sessao)
        print(c("\n✓ Histórico de conversa apagado.", Cor.VERDE))
        return True

    if (
        t == "/modelo auto"
        or "modo automatico" in t
        or "modo automático" in texto.lower()
        or "volte para automatico" in t
        or "volte para automático" in texto.lower()
    ):

        modo_modelo = "AUTO"
        modelo_manual = None

        print(c("\n✓ Modo AUTO ativado.", Cor.VERDE))
        return True

    catalogo = catalogar_modelos()

    def _selecionar_modelo(modelo_pedido):
        global modo_modelo, modelo_manual
        modo_modelo = "MANUAL"
        modelo_manual = modelo_pedido
        print(c(f"\n✓ Modelo manual selecionado: {modelo_pedido}", Cor.VERDE))
        LOG.info("troca_de_modelo modelo=%s modo=MANUAL", modelo_pedido)

        # Avisa de cara se esse modelo não suporta ferramentas — sem
        # isso, ele nunca vai conseguir abrir apps/sites/executar nada,
        # só conversar em texto, e é melhor o usuário saber disso antes
        # de ficar tentando do que descobrir só quando um comando falhar.
        info = catalogo.get(modelo_pedido)
        if info and info.get("capacidades_conhecidas") and "tools" not in info.get("capacidades", set()):
            print(
                c(
                    f"  ⚠ {modelo_pedido} não suporta ferramentas — vai "
                    "responder só em texto, sem conseguir abrir apps, "
                    "sites, executar comandos etc.",
                    Cor.AMARELO,
                )
            )

        # Carrega já, em segundo plano: quando o usuário mandar a
        # próxima mensagem de verdade, o modelo já pode estar pronto
        # (ou pelo menos adiantado), em vez de só começar a carregar
        # depois que a pergunta chegar.
        _carregar_em_segundo_plano(modelo_pedido)

    # Comando explícito "/modelo <algo>" — única forma de trocar de
    # modelo por texto. Tenta em ordem: correspondência exata/
    # superlativo, depois aproximação tolerante a pontuação/espaço,
    # depois — só nesse último caso, e só porque o usuário já digitou
    # explicitamente "/modelo" — pergunta pra IA classificadora
    # minúscula.
    #
    # Trocar de modelo a partir de uma frase qualquer do chat (sem
    # "/modelo") foi removido de propósito: verbos do dia a dia como
    # "usar", "quero" ou "coloca" aparecem o tempo todo em pedidos que
    # não têm NADA a ver com IA ("use suas ferramentas para abrir o
    # youtube"), e tentar adivinhar a intenção a partir deles é
    # inerentemente frágil — foi exatamente isso que causou uma troca
    # de modelo indevida antes. "/modelo" é rápido de digitar e nunca
    # tem ambiguidade nenhuma.
    if t.startswith("/modelo "):
        alvo = t[len("/modelo "):].strip()

        modelo_pedido = (
            _resolver_alvo_modelo(alvo, catalogo)
            or _resolver_modelo_por_texto_livre(alvo, catalogo)
            or _resolver_modelo_com_ia(alvo, catalogo)
        )

        if modelo_pedido:
            _selecionar_modelo(modelo_pedido)
        else:
            instalados_fmt = (
                ", ".join(sorted(catalogo)) if catalogo else "nenhum modelo encontrado"
            )
            print(
                c(
                    f"\n✗ Não encontrei nenhum modelo instalado parecido "
                    f"com \"{alvo}\". Instalados: {instalados_fmt}",
                    Cor.AMARELO,
                )
            )

        return True

    # Mensagem inteira IGUAL (não "contém") a uma tag/família/prefixo/
    # superlativo continua reconhecida sem precisar do "/modelo" — a
    # exigência de igualdade exata (não substring) é o que evita cair
    # de novo na armadilha que a detecção por verbo tinha: uma frase
    # comum como "o menor problema pode causar um erro grande" contém
    # "o menor" como substring, mas não É "o menor", então não bate
    # aqui. Só _resolver_alvo_modelo (usado no comando "/modelo", onde
    # o texto já é sabidamente sobre modelo) pode usar substring com
    # segurança.
    tabela = _tabela_resolucao_modelos(catalogo)

    if t in tabela:
        modelo_pedido = tabela[t]
    elif t in _SUPERLATIVOS_MODELO:
        modelo_pedido = _SUPERLATIVOS_MODELO[t](catalogo)
    else:
        modelo_pedido = None

    if modelo_pedido:
        _selecionar_modelo(modelo_pedido)
        return True

    # Nada bateu — mas se o usuário digitou uma tag exata do Ollama
    # ("familia:tamanho") como mensagem inteira, ele provavelmente quis
    # trocar pra um modelo que ainda não está instalado. Avisa em vez
    # de tratar como uma mensagem normal de chat.
    if re.fullmatch(r"[a-z0-9][a-z0-9_./-]*:[a-z0-9][a-z0-9_.-]*", t):
        modo_modelo = "MANUAL"
        modelo_manual = t

        print(
            c(f"\n✓ Modelo manual selecionado: {t}", Cor.AMARELO)
            + c(
                " (não parece estar instalado no Ollama ainda — "
                "o carregamento pode falhar; rode 'ollama pull "
                f"{t}')",
                Cor.AMARELO,
            )
        )

        return True

    return False


# ============================================================
# PROMPT DO TARS
# ============================================================

SYSTEM_PROMPT = """
Você é TARS, um assistente de desktop rodando localmente no Linux.

Você é o cérebro do computador. Python executa as ferramentas, mas
VOCÊ deve decidir quando e como usar as ferramentas.

REGRAS IMPORTANTES:

1. Sempre use ferramentas quando a solicitação do usuário exigir uma
   ação no computador.

2. Para abrir um aplicativo, use open_application.

3. Para fechar um aplicativo, use close_application.

4. Para abrir um site, use open_website.

5. Não diga que uma ação falhou antes de analisar o resultado da ferramenta.

6. Se uma ferramenta retornar:
   sucesso=true
   ou uma mensagem começando com "SUCESSO",
   considere a ação realizada.

7. Se a ferramenta retornar sucesso=false, explique o erro de forma
   curta e útil.

8. Depois que uma ferramenta for executada, analise o RESULTADO REAL
   retornado pela ferramenta antes de responder.

9. Nunca invente que uma ferramenta foi executada.

10. Para abrir qualquer aplicativo, chame open_application com o nome
    exatamente como o usuário disse (app="<nome citado>"), mesmo que
    você não reconheça esse nome — a ferramenta resolve isso, não você.

11. Para um pedido genérico ("o navegador", "o gerenciador de
    arquivos"), passe esse mesmo termo como app — a ferramenta tenta
    resolver, e você só relata problema se ela realmente falhar.

12. Quando o nome citado puder ser tanto um site quanto um aplicativo
    instalado, e o usuário não deixar claro qual dos dois quer, prefira
    open_application se ele mencionar "app"/"programa"/"instalado", e
    open_website se mencionar "site"/"página"/"navegador"; na ausência
    de qualquer pista, escolha o que for mais comum pra esse nome.

13. Se o usuário disser explicitamente "abra o site X", sempre use
    open_website, mesmo que X também exista como aplicativo instalado.

14. Seja curto em tarefas simples.

15. Não mostre seu raciocínio interno privado.
    Você pode fornecer uma explicação curta do que fez.

16. Se uma ferramenta retornar sucesso, não peça para o usuário
    fazer manualmente aquilo que você acabou de executar.

17. Você pode chamar várias ferramentas quando necessário.

18. Não chame novamente uma ferramenta que já realizou com sucesso
    a mesma ação, a menos que seja necessário.

19. O usuário está usando Zorin OS/Linux.

20. Responda em português brasileiro quando o usuário falar português.

21. Para ver, analisar ou descrever a tela/uma janela, use
    take_screenshot — o print aparece anexado na mensagem seguinte.

22. open_application aceita QUALQUER nome de app, em português ou
    inglês ("files", "calculadora", "gerenciador de arquivos",
    "nautilus" são todos válidos). NUNCA recuse um pedido de abrir
    algo dizendo que "não existe uma ferramenta pra isso" — sempre
    tente open_application primeiro; só depois de ela FALHAR de
    verdade (sucesso=false) é que você explica o erro.

23. Para contas e cálculos matemáticos simples (ex: "quanto é 25+17"),
    responda o resultado direto no texto. Não abra a calculadora nem
    use execute_terminal pra isso — você já sabe fazer conta. EXCEÇÃO:
    se o usuário pedir explicitamente pra abrir a calculadora e usar
    ela pro cálculo (ex: "abra a calculadora e some 25+17"), aí sim
    abra o app de verdade e digite a conta nela (ver regra 27).

24. Em pedidos com mais de uma ação ("feche X e abra Y", "feche as
    abas do navegador e deixe só o site Z"), chame uma ferramenta para
    CADA ação, na ordem pedida, dentro do mesmo turno. Nunca descreva
    no texto final uma ação que você não chamou de verdade — se não
    conseguir fazer uma parte do pedido, diga isso explicitamente em
    vez de fingir que fez.

25. Fechar "todas as abas" de um navegador só é possível fechando o
    aplicativo inteiro com close_application (isso encerra todas as
    janelas/abas dele de uma vez). Avise que reabrir um site depois é
    uma ação separada (open_website), feita após o fechamento.

26. Para QUALQUER pedido de busca ("pesquisar/buscar/procurar X", "X
    no google", "X no youtube"), use search_web — NUNCA open_website
    pra isso. Em search_web, separe bem os dois campos: query = só o
    termo que a pessoa quer pesquisar, exatamente como ela disse, sem
    cortar nem resumir nada (ex: para "pesquise rtx 5060 no google",
    query="rtx 5060"); service = o nome do site de busca só se a
    pessoa citar um (ex: "google", "youtube") — se ela não citar
    nenhum, não preencha esse campo, o padrão já é o Google. Use
    open_website só para abrir uma página/site específico e já
    conhecido (ex: "abra o github.com", "abra o gmail"). NUNCA tente
    adivinhar ou inventar a URL de uma página, canal ou perfil
    específico de algo (ex: "o canal da Nasa") — isso também é busca,
    então use search_web em vez de chutar um link.

27. Tarefa composta que envolve abrir algo e DEPOIS digitar/clicar
    nele (ex: "abra a calculadora e some 25+17", "abra o navegador e
    pesquise sobre X", "abra o bloco de notas e escreva Y") é uma
    sequência de ferramentas no mesmo turno: primeiro open_application
    ou open_website, depois — se achar que o app pode ainda estar
    carregando (navegador e programas mais pesados; a maioria dos
    apps leves como calculadora e editor de texto não precisa disso)
    — wait_seconds com um valor pequeno (1 a 2s), e só então
    type_text/click_mouse/press_key/hotkey no que foi aberto. Numa
    calculadora, digite a expressão com type_text e finalize com
    press_key (Return) ou hotkey de igual, conforme o app.

28. wait_seconds serve só pra dar tempo de algo carregar depois de
    abrir — não é uma forma de esperar por qualquer outra coisa, e não
    deve ser chamado repetidamente só pra "aguardar mais".

29. Navegador (Chrome, Google Chrome, Firefox, Brave, Edge, Opera,
    Safari) é sempre um APLICATIVO — use open_application pra abrir
    ele, nunca open_website (mesmo que o nome pareça um domínio). Só
    use open_website quando o pedido for pra abrir uma PÁGINA/site
    dentro do navegador.
"""


# ============================================================
# SESSÕES
# ============================================================

# Onde o histórico de conversa de cada modelo é salvo em disco, pra
# sobreviver a fechar/abrir o TARS de novo. Configurável por variável
# de ambiente pelo mesmo motivo do OLLAMA_HOST: não travar num único
# caminho fixo.
ARQUIVO_SESSOES = Path(
    os.environ.get("TARS_ARQUIVO_SESSOES", str(Path.home() / ".tars_sessoes.json"))
)


def carregar_sessoes():
    """Lê o histórico salvo em disco, se existir. A mensagem de sistema
    de cada sessão é sempre substituída pelo SYSTEM_PROMPT atual (não
    o que estava salvo) — assim uma atualização nas regras do TARS vale
    pra conversas antigas também, em vez de ficar presa à versão do
    prompt de quando a sessão foi salva."""

    if not ARQUIVO_SESSOES.exists():
        return {}

    try:
        dados = json.loads(ARQUIVO_SESSOES.read_text(encoding="utf-8"))
    except Exception as e:
        print(
            c(
                f"[TARS] não consegui ler o histórico salvo em "
                f"{ARQUIVO_SESSOES} ({e}) — começando do zero.",
                Cor.AMARELO,
            )
        )
        return {}

    resultado = {}

    for modelo, mensagens in (dados or {}).items():

        if not isinstance(mensagens, list) or not mensagens:
            continue

        mensagens = [{"role": "system", "content": SYSTEM_PROMPT}] + [
            m for m in mensagens if isinstance(m, dict) and m.get("role") != "system"
        ]

        resultado[modelo] = mensagens

    return resultado


def salvar_sessoes():
    """Salva o histórico de conversa de cada modelo em disco. Nunca
    grava imagens (prints de tela) — elas já não entram no histórico
    podado (LIMITE_HISTORICO_MENSAGENS), mas por segurança essa função
    também as remove explicitamente, pra não deixar o arquivo gigante
    nem guardar prints antigos indefinidamente no disco. Falha em
    salvar não é motivo pra travar o programa — só avisa."""

    try:
        dados = {}

        for modelo, mensagens in sessoes.items():

            limpas = []

            for m in mensagens:
                if not isinstance(m, dict) or m.get("role") == "system":
                    continue
                m_limpa = {k: v for k, v in m.items() if k != "images"}
                limpas.append(m_limpa)

            if limpas:
                dados[modelo] = limpas

        ARQUIVO_SESSOES.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    except Exception as e:
        print(c(f"[TARS] não consegui salvar o histórico da conversa: {e}", Cor.AMARELO))


def limpar_sessao(modelo):
    """Apaga o histórico de conversa de um modelo específico (comando
    '/limpar') — sem isso, uma vez que as sessões passaram a persistir
    em disco, não haveria como começar uma conversa do zero sem editar
    o arquivo à mão."""

    if modelo in sessoes:
        del sessoes[modelo]

    salvar_sessoes()


def obter_sessao(modelo):

    if modelo not in sessoes:
        sessoes[modelo] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    return sessoes[modelo]


# ============================================================
# CHAT COM OLLAMA
# ============================================================

_MODELOS_SEM_THINKING = set()

# Modelos que confirmadamente não aceitam o parâmetro 'tools' (ex:
# gemma3:270m, que é minúsculo demais e nem tenta suportar function
# calling). Sem isso, escolher manualmente um modelo assim como IA de
# conversa principal quebrava TODA mensagem com um 400 do Ollama —
# o TARS sempre manda 'tools' pra poder abrir apps/sites.
_MODELOS_SEM_TOOLS = set()


def _preparar_payload_ollama(payload):
    """Remove de antemão os parâmetros que a gente já sabe (pelo
    catálogo, via /api/show, ou por uma falha anterior nesta sessão)
    que esse modelo não aceita — evita gastar uma chamada fadada ao
    erro toda vez que o modelo já provou ou declarou que não suporta
    'think'/'tools'."""

    modelo = payload.get("model")
    payload = dict(payload)

    # Descoberta proativa: se o catálogo (cache de /api/show) já sabe
    # as capacidades reais desse modelo, usa isso em vez de esperar um
    # 400 do Ollama pra descobrir.
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


def _chamar_ollama_stream(payload):
    """Faz a chamada de streaming ao Ollama. Se 'think' e/ou 'tools'
    não forem aceitos pelo modelo/versão instalada, tenta de novo sem
    eles (nessa ordem: sem think, sem tools, sem os dois) — e lembra
    disso pro resto da sessão, pra não repetir uma tentativa fadada ao
    erro em toda mensagem seguinte com esse mesmo modelo."""

    modelo = payload.get("model")
    tinha_think = "think" in payload
    tinha_tools = "tools" in payload

    base = _preparar_payload_ollama(payload)

    tentativas = [base]

    if "think" in base:
        sem_think = dict(base)
        sem_think.pop("think", None)
        tentativas.append(sem_think)

    if "tools" in base:
        sem_tools = dict(base)
        sem_tools.pop("tools", None)
        tentativas.append(sem_tools)

    if "think" in base and "tools" in base:
        sem_ambos = dict(base)
        sem_ambos.pop("think", None)
        sem_ambos.pop("tools", None)
        tentativas.append(sem_ambos)

    ultimo_erro = "Erro desconhecido ao comunicar com Ollama."

    for tentativa in tentativas:
        try:
            resposta = ollama_request(tentativa, stream=True)
        except Exception as e:
            ultimo_erro = f"Erro ao comunicar com Ollama: {e}"
            continue

        if resposta.status_code == 200:
            if tinha_think and "think" not in tentativa:
                _MODELOS_SEM_THINKING.add(modelo)
            if tinha_tools and "tools" not in tentativa:
                _MODELOS_SEM_TOOLS.add(modelo)
            return resposta, None

        try:
            ultimo_erro = f"Erro do Ollama ({resposta.status_code}): {resposta.text}"
        except Exception:
            ultimo_erro = f"Erro do Ollama ({resposta.status_code})"

    return None, ultimo_erro


def _executar_turno_streaming(modelo, mensagens, tools):
    """Envia a conversa ao Ollama em modo streaming e vai imprimindo o
    pensamento e a escrita da IA em tempo real — igual ao `ollama run`
    puro no terminal — em vez de esperar a resposta inteira pra só
    então mostrar tudo de uma vez. Devolve o conteúdo final, o
    pensamento e as tool_calls (que chegam de uma vez, não token a
    token)."""

    payload = {
        "model": modelo,
        "messages": mensagens,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "think": True,
        "options": {"temperature": TEMPERATURA_CONVERSA},
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

    try:
        for linha in resposta.iter_lines():

            if not linha:
                continue

            try:
                chunk = json.loads(linha)
            except Exception:
                continue

            msg = chunk.get("message", {}) or {}

            pensamento = msg.get("thinking")

            if pensamento:
                if not pensando_ativo:
                    print(c("\n[pensando] ", Cor.CINZA), end="", flush=True)
                    pensando_ativo = True
                print(c(pensamento, Cor.CINZA), end="", flush=True)
                partes_pensamento.append(pensamento)

            pedaco = msg.get("content")

            if pedaco:
                if pensando_ativo and not escrevendo_ativo:
                    print()
                if not escrevendo_ativo:
                    print(c("\nTARS › ", Cor.NEGRITO + Cor.MAGENTA), end="", flush=True)
                    escrevendo_ativo = True
                print(pedaco, end="", flush=True)
                partes_conteudo.append(pedaco)

            if msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]

            if chunk.get("done"):
                break

    except Exception as e:
        return None, f"Erro durante o streaming: {e}"

    if pensando_ativo or escrevendo_ativo:
        print()

    return {
        "content": "".join(partes_conteudo).strip(),
        "thinking": "".join(partes_pensamento).strip(),
        "tool_calls": tool_calls,
    }, None


def perguntar_modelo(modelo, texto, tarefa=None):

    candidatos = [modelo]

    # Em modo AUTO, se o modelo escolhido falhar ao carregar (não
    # instalado, sem VRAM/RAM suficiente, Ollama travado etc.), desce
    # a cadeia de fallback da tarefa em vez de simplesmente desistir.
    if modo_modelo == "AUTO" and tarefa:
        candidatos += [
            m for m in construir_cadeia_fallback(tarefa)
            if m != modelo
        ]

    modelo_carregado = None
    tentados = []

    for candidato in candidatos:

        tentados.append(candidato)

        if carregar_modelo(candidato):
            modelo_carregado = candidato
            break

        print(c(f"[AUTO] falha ao carregar {candidato}, tentando alternativa...", Cor.AMARELO))

    if not modelo_carregado:
        mensagem_erro = (
            "Não consegui carregar nenhum modelo disponível para essa "
            f"tarefa (tentei: {', '.join(tentados)})."
        )
        print(c(f"\n[TARS] {mensagem_erro}", Cor.VERMELHO))
        return mensagem_erro

    modelo = modelo_carregado

    mensagens = obter_sessao(modelo)

    mensagens.append({"role": "user", "content": texto})

    inicio_total = time.time()

    # --------------------------------------------------------
    # LOOP DE FERRAMENTAS
    # --------------------------------------------------------

    for ciclo in range(LIMITE_CICLOS_FERRAMENTAS):

        imagem_no_turno = bool(mensagens) and isinstance(mensagens[-1], dict) and bool(mensagens[-1].get("images"))

        resultado, erro = _executar_turno_streaming(modelo, mensagens, TOOLS)

        # Alguns modelos/versões do Ollama não aceitam 'tools' junto
        # com uma mensagem que carrega imagem (visão). Se o turno
        # anterior acabou de anexar um print de tela e a chamada
        # falhou, tenta de novo só com texto — depois de ver a
        # imagem, o pedido normalmente é só descrever/responder,
        # não chamar outra ferramenta.
        if erro and imagem_no_turno:
            print(c(f"[TARS] falha ao analisar a imagem com ferramentas habilitadas ({erro}); tentando novamente somente com texto...", Cor.AMARELO))
            resultado, erro = _executar_turno_streaming(modelo, mensagens, None)

        if erro:
            print(c(f"\n[TARS] {erro}", Cor.VERMELHO))
            return erro

        conteudo = resultado["content"]
        tool_calls = resultado["tool_calls"]

        # ----------------------------------------------------
        # SEM FERRAMENTA
        # ----------------------------------------------------

        if not tool_calls:

            mensagens.append({"role": "assistant", "content": conteudo})

            # guarda apenas histórico textual útil
            if len(mensagens) > LIMITE_HISTORICO_MENSAGENS:
                mensagens[:] = [mensagens[0]] + mensagens[-(LIMITE_HISTORICO_MENSAGENS - 1):]

            tempo_total = time.time() - inicio_total

            cor_modo = Cor.AZUL if modo_modelo == "AUTO" else Cor.AMARELO

            if not conteudo:
                print(c(f"[TARS] {modelo} não retornou nenhum texto nesta resposta.", Cor.AMARELO))

            print(c(f"[{modo_modelo}] {modelo} — tempo total: {tempo_total:.2f}s", cor_modo))

            return conteudo

        # ----------------------------------------------------
        # TEMOS TOOL CALL
        # ----------------------------------------------------

        mensagens.append({
            "role": "assistant",
            "content": conteudo,
            "tool_calls": tool_calls,
        })

        for chamada in tool_calls:

            funcao = chamada.get("function", {})
            nome = funcao.get("name", "")
            argumentos_raw = funcao.get("arguments", {})

            if isinstance(argumentos_raw, str):
                try:
                    argumentos = json.loads(argumentos_raw)
                except Exception:
                    argumentos = {}
            else:
                argumentos = argumentos_raw

            resultado_ferramenta = executar_ferramenta(nome, argumentos)

            # O print de tela vem com a imagem à parte — nunca deixa
            # a imagem entrar no JSON de texto (custaria uma fortuna
            # de tokens); ela é anexada como mensagem de visão logo
            # abaixo, só quando essa ferramenta específica é usada.
            imagem_b64 = resultado_ferramenta.pop("_imagem_b64", None)

            resultado_json = json.dumps(resultado_ferramenta, ensure_ascii=False)

            # IMPORTANTE: usamos role=tool e vinculamos o resultado
            # ao ciclo atual.
            mensagens.append({"role": "tool", "content": resultado_json})

            if imagem_b64:
                mensagens.append({
                    "role": "user",
                    "content": "(print da tela anexado acima — analise a imagem)",
                    "images": [imagem_b64],
                })

        print(c(f"[IA] {modelo} está analisando o resultado da ferramenta...", Cor.CINZA))

    mensagem_limite = (
        "A IA atingiu o limite de etapas de ferramentas "
        "sem concluir a solicitação."
    )
    print(c(f"\n[TARS] {mensagem_limite}", Cor.VERMELHO))
    return mensagem_limite


# ============================================================
# TELA INICIAL
# ============================================================

def tela_inicial():

    imprimir_caixa(
        "o p e n T A R S",
        ["Assistente de desktop 100% local, rodando via Ollama."],
        cor_titulo=Cor.MAGENTA,
    )

    # Resumo rápido de quantos modelos há pra escolher, antes da
    # listagem detalhada (que já mostra a GPU logo abaixo).
    catalogo = catalogar_modelos()

    n_chat = sum(1 for m in catalogo.values() if m["categoria"] != "embedding")
    n_embedding = sum(1 for m in catalogo.values() if m["categoria"] == "embedding")

    print()

    # GPU já é mostrada logo abaixo por _imprimir_lista_modelos — aqui
    # só o resumo de quantos modelos há pra escolher, que ela não diz.
    if catalogo:
        resumo_modelos = f"{n_chat} modelo(s) de chat prontos pra uso"
        if n_embedding:
            resumo_modelos += f" ({n_embedding} de embedding fora da escolha)"
        print(c(resumo_modelos, Cor.CIANO))
    else:
        print(
            c(
                "Nenhum modelo encontrado no Ollama ainda — use "
                "'ollama pull <modelo>'.",
                Cor.AMARELO,
            )
        )

    print()
    print("IAs disponíveis:")
    print()

    _imprimir_lista_modelos()

    print(
        c("Modo atual: AUTO", Cor.AZUL)
        + " — o openTARS escolhe sozinho a IA mais adequada pra cada tarefa."
    )
    print()

    # Comandos alinhados numa tabela simples (nome + descrição) em vez
    # de linhas soltas — mais fácil de escanear rápido.
    comandos = [
        ("/modelo", "mostra todos os modelos instalados"),
        ("/modelo auto", "volta pro modo automático"),
        ("/modelo <nome>", "troca de modelo manualmente (ex: /modelo qwen 8b)"),
        ("/limpar", "apaga o histórico da conversa atual"),
        ("encerrar todas llms", "descarrega todas as IAs da memória"),
        ("sair", "encerra o openTARS"),
    ]
    largura_cmd = max(len(cmd) for cmd, _ in comandos)

    print("Comandos:")
    for cmd, desc in comandos:
        print(f"  {c(cmd.ljust(largura_cmd), Cor.VERDE)}   {desc}")

    print()
    print(
        c(
            f"Histórico salvo em: {ARQUIVO_SESSOES}\n"
            f"Log de auditoria em: {ARQUIVO_LOG}",
            Cor.CINZA,
        )
    )

    for aviso in avisos_de_ambiente():
        print()
        print(c(f"⚠ {aviso}", Cor.AMARELO))

    print()
    print(c("openTARS pronto.", Cor.NEGRITO + Cor.VERDE))
    print()

    # Warm-up em segundo plano do modelo mais leve DE FATO instalado
    # (não um nome fixo): ele é usado tanto pra tarefas simples quanto
    # pra resolver apps/sites via IA, então já deixá-lo carregando
    # desde já evita esperar o carregamento no meio da primeira
    # mensagem real.
    _carregar_em_segundo_plano(modelo_ajudante(catalogo))


# ============================================================
# MAIN
# ============================================================

def _prompt_atual():
    """Prompt dinâmico mostrando qual modelo vai atender a próxima
    mensagem — informação útil o suficiente pra ficar sempre visível
    dado o quanto a troca de modelo é central nesse sistema."""

    if modo_modelo == "MANUAL" and modelo_manual:
        indicador = c(modelo_manual, Cor.AMARELO)
    else:
        indicador = c("auto", Cor.AZUL)

    return c("Você", Cor.NEGRITO + Cor.VERDE) + f" [{indicador}] › "


def main():

    carregadas = carregar_sessoes()

    if carregadas:
        sessoes.update(carregadas)
        print(
            c(
                f"[TARS] histórico de conversa carregado de {ARQUIVO_SESSOES} "
                f"({len(carregadas)} modelo(s)).",
                Cor.CINZA,
            )
        )

    tela_inicial()

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

            if texto.lower() in ["sair", "exit", "quit", "/sair"]:
                print(c("\nEncerrando TARS...", Cor.CINZA))
                break

            if interpretar_comando_modelo(texto):
                continue

            tarefa = detectar_tarefa(texto) if modo_modelo == "AUTO" else None
            modelo = escolher_modelo(texto, tarefa=tarefa)

            if modo_modelo == "AUTO":
                print(c(f"\n[AUTO] tarefa detectada: {tarefa} → modelo: {modelo}", Cor.AZUL))
            else:
                print(c(f"\n[MANUAL] modelo fixado: {modelo}", Cor.AMARELO))

            try:
                perguntar_modelo(modelo, texto, tarefa=tarefa)

            except KeyboardInterrupt:
                print(c("\n\n[TARS] operação interrompida.", Cor.AMARELO))

            except Exception as e:
                print(c(f"\n[TARS] erro inesperado: {e}", Cor.VERMELHO))

            salvar_sessoes()

            print()

    except KeyboardInterrupt:
        print()

    finally:
        # Descarrega toda IA da memória ao sair, seja por /sair,
        # Ctrl+C, Ctrl+D (EOF) ou erro não tratado.
        encerrar_todas_ias()
        print(c("TARS encerrado.", Cor.CINZA))


if __name__ == "__main__":
    main()
