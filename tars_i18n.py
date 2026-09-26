#!/usr/bin/env python3
"""
Idiomas do openTARS.

Todo texto que aparece pro usuário mora em idiomas/<código>.json (um
arquivo por idioma, chave -> texto). O código só pede a chave:

    from tars_i18n import t
    t("gui.enviar")                      # "Enviar" / "Send" / "Enviar"...
    t("msg.carregado", modelo="qwen3")   # com valores no lugar de {modelo}

Adicionar um idioma = copiar o en.json, traduzir os valores e salvar com
o código do idioma (ex: it.json). O menu passa a mostrar o idioma novo
sozinho; nada no código muda. Chave que faltar num idioma cai no inglês.

A escolha do usuário fica em ~/.config/opentars/config.json. Sem escolha,
vale o idioma do sistema (LANG), se houver arquivo pra ele.

Também serve o instalador (setup.sh, em bash):
    python3 tars_i18n.py --shell setup.   ->   T_setup_xxx='...' (pra eval)
"""

import json
import os
import shlex
import sys
import threading
from pathlib import Path

PASTA_IDIOMAS = Path(
    os.environ.get("TARS_DIR_IDIOMAS") or Path(__file__).resolve().parent / "idiomas"
)

# Idioma de reserva: chave que faltar no idioma escolhido vem daqui.
IDIOMA_RESERVA = "en"


def _pasta_config():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "opentars"


ARQUIVO_CONFIG = Path(os.environ.get("TARS_ARQUIVO_CONFIG") or _pasta_config() / "config.json")

_trava = threading.Lock()
_catalogos = {}          # código -> dict já lido do JSON
_disponiveis = None      # código -> nome nativo (cache)
_config = None           # conteúdo do config.json (cache)
_ouvintes = []           # funções chamadas quando o idioma muda

_atual = None            # código do idioma em uso
_cat_atual = {}          # dicionário do idioma em uso (troca atômica)
_cat_reserva = {}


# ------------------------------------------------------------
# Catálogos
# ------------------------------------------------------------

def _carregar(codigo):
    """Lê idiomas/<codigo>.json uma vez só. Arquivo quebrado vira {} (as
    chaves caem no idioma de reserva) em vez de derrubar o programa."""
    cat = _catalogos.get(codigo)
    if cat is None:
        try:
            with open(PASTA_IDIOMAS / f"{codigo}.json", encoding="utf-8") as f:
                cat = json.load(f)
            if not isinstance(cat, dict):
                raise ValueError("o arquivo precisa ser um objeto JSON")
        except FileNotFoundError:
            cat = {}
        except Exception as e:  # JSON inválido: avisa uma vez e segue
            print(f"[openTARS] idiomas/{codigo}.json inválido ({e})", file=sys.stderr)
            cat = {}
        _catalogos[codigo] = cat
    return cat


def idiomas_disponiveis():
    """{código: nome no próprio idioma}, na ordem alfabética dos nomes."""
    global _disponiveis
    if _disponiveis is None:
        achados = {}
        try:
            arquivos = sorted(PASTA_IDIOMAS.glob("*.json"))
        except OSError:
            arquivos = []
        for arquivo in arquivos:
            cat = _carregar(arquivo.stem)
            if cat:
                achados[arquivo.stem] = str(cat.get("_nome") or arquivo.stem)
        _disponiveis = dict(sorted(achados.items(), key=lambda kv: kv[1].casefold()))
    return _disponiveis


def nome_idioma(codigo):
    return idiomas_disponiveis().get(codigo, codigo)


def _normalizar_codigo(valor):
    """'pt-BR.UTF-8@euro' -> 'pt_BR'; 'en' -> 'en'; 'C' -> ''."""
    valor = (valor or "").split(".")[0].split("@")[0].replace("-", "_").strip()
    if valor in ("", "C", "POSIX"):
        return ""
    partes = valor.split("_", 1)
    return partes[0].lower() + ("_" + partes[1].upper() if len(partes) > 1 else "")


def melhor_idioma(pedido):
    """O arquivo que melhor atende um código: exato, depois o idioma sem a
    região (pt_PT -> pt_BR), depois qualquer região do mesmo idioma."""
    codigo = _normalizar_codigo(pedido)
    if not codigo:
        return None
    disponiveis = idiomas_disponiveis()
    if codigo in disponiveis:
        return codigo
    base = codigo.split("_")[0]
    if base in disponiveis:
        return base
    return next((c for c in disponiveis if c.split("_")[0] == base), None)


def locales_do_sistema():
    """Os códigos do sistema como estão (pt_PT, de_AT...), sem mapear pros
    arquivos de idioma: servem pra achar Name[pt_PT]= nos atalhos de apps."""
    codigos = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for parte in (os.environ.get(var) or "").split(":"):
            codigo = _normalizar_codigo(parte)
            if codigo and codigo not in codigos:
                codigos.append(codigo)
    return codigos


def idiomas_do_sistema():
    """Códigos pedidos pelo sistema, em ordem de preferência (LANGUAGE pode
    ter vários, separados por ':')."""
    pedidos = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for parte in (os.environ.get(var) or "").split(":"):
            codigo = melhor_idioma(parte)
            if codigo and codigo not in pedidos:
                pedidos.append(codigo)
    return pedidos


# ------------------------------------------------------------
# Configuração do usuário
# ------------------------------------------------------------

def ler_config():
    global _config
    if _config is None:
        try:
            dados = json.loads(ARQUIVO_CONFIG.read_text(encoding="utf-8"))
            _config = dados if isinstance(dados, dict) else {}
        except Exception:
            _config = {}
    return _config


def salvar_config(**mudancas):
    """Grava de forma atômica (temporário + rename). Devolve True se deu."""
    with _trava:
        dados = dict(ler_config())
        dados.update(mudancas)
        try:
            ARQUIVO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporario = ARQUIVO_CONFIG.with_suffix(".tmp")
            temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporario, ARQUIVO_CONFIG)
        except Exception:
            return False
        global _config
        _config = dados
        return True


# ------------------------------------------------------------
# Idioma em uso
# ------------------------------------------------------------

def _escolher_inicial():
    for pedido in (os.environ.get("TARS_IDIOMA"), ler_config().get("idioma")):
        codigo = melhor_idioma(pedido)
        if codigo:
            return codigo
    sistema = idiomas_do_sistema()
    if sistema:
        return sistema[0]
    return IDIOMA_RESERVA if IDIOMA_RESERVA in idiomas_disponiveis() else next(iter(idiomas_disponiveis()), IDIOMA_RESERVA)


def _aplicar(codigo):
    global _atual, _cat_atual, _cat_reserva
    _cat_reserva = _carregar(IDIOMA_RESERVA)
    _cat_atual = _carregar(codigo)
    _atual = codigo


def idioma_atual():
    if _atual is None:
        _aplicar(_escolher_inicial())
    return _atual


def definir_idioma(codigo, salvar=True):
    """Troca o idioma (e grava a escolha). Devolve o código aplicado, ou
    None se não houver arquivo pra esse idioma."""
    escolhido = melhor_idioma(codigo)
    if not escolhido:
        return None
    anterior = idioma_atual()
    _aplicar(escolhido)
    if salvar:
        salvar_config(idioma=escolhido)
    if escolhido != anterior:
        for funcao in list(_ouvintes):
            try:
                funcao(escolhido)
            except Exception:
                pass
    return escolhido


def ao_mudar_idioma(funcao):
    """Registra uma função(codigo) chamada a cada troca de idioma."""
    _ouvintes.append(funcao)
    return funcao


def idiomas_de_entrada():
    """Idiomas em que o usuário provavelmente ESCREVE: o escolhido, os do
    sistema e o inglês. Usado nas palavras-chave (detectar tarefa, achar
    app pelo nome traduzido) — não todos os idiomas, pra "fecha" (fechar,
    em português) não disparar num pedido em espanhol ("fecha" = data)."""
    vistos = []
    for codigo in [idioma_atual(), *idiomas_do_sistema(), IDIOMA_RESERVA]:
        if codigo and codigo not in vistos and codigo in idiomas_disponiveis():
            vistos.append(codigo)
    return vistos


# ------------------------------------------------------------
# Textos
# ------------------------------------------------------------

def t(chave, **valores):
    """Texto da chave no idioma em uso (ou no de reserva). Com valores,
    preenche os {campos}; um campo que falte não quebra: volta o texto cru."""
    if _atual is None:
        idioma_atual()
    texto = _cat_atual.get(chave)
    if not isinstance(texto, str):
        texto = _cat_reserva.get(chave)
        if not isinstance(texto, str):
            return chave
    if valores:
        try:
            return texto.format(**valores)
        except (KeyError, IndexError, ValueError):
            return texto
    return texto


def lista(chave):
    """Lista da chave (exemplos, palavras-chave...) no idioma em uso."""
    if _atual is None:
        idioma_atual()
    valor = _cat_atual.get(chave)
    if not isinstance(valor, list):
        valor = _cat_reserva.get(chave)
    return list(valor) if isinstance(valor, list) else []


def juntar(chave, idiomas=None):
    """União (sem repetir, na ordem) das listas de uma chave em vários
    idiomas — por padrão, os idiomas de entrada."""
    saida, vistos = [], set()
    for codigo in idiomas or idiomas_de_entrada():
        valor = _carregar(codigo).get(chave)
        for item in valor if isinstance(valor, list) else ():
            marca = json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item
            if marca not in vistos:
                vistos.add(marca)
                saida.append(item)
    return saida


def em_todos(chave):
    """União da chave em TODOS os idiomas (comandos como /limpar, /clear,
    /limpiar: explícitos, não há risco de engano)."""
    return juntar(chave, list(idiomas_disponiveis()))


def chaves(prefixo=""):
    """Chaves conhecidas (do idioma de reserva e do atual) com o prefixo."""
    idioma_atual()
    return sorted(k for k in {*_cat_reserva, *_cat_atual} if k.startswith(prefixo))


def _para_shell(prefixo):
    linhas = []
    for chave in chaves(prefixo):
        texto = t(chave)
        if isinstance(texto, str):
            nome = "T_" + "".join(ch if ch.isalnum() else "_" for ch in chave)
            linhas.append(f"{nome}={shlex.quote(texto)}")
    return "\n".join(linhas)


if __name__ == "__main__":
    argumentos = sys.argv[1:]
    if len(argumentos) == 2 and argumentos[0] == "--shell":
        print(_para_shell(argumentos[1]))
    elif argumentos == ["--lista"]:
        for codigo, nome in idiomas_disponiveis().items():
            print(f"{codigo}\t{nome}")
    else:
        print("uso: tars_i18n.py --shell <prefixo> | --lista", file=sys.stderr)
        sys.exit(2)
