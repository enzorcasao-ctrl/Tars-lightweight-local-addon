"""
Atalho global do openTARS: uma combinação de teclas (padrão Ctrl+Alt+Espaço)
que abre a barra rápida de qualquer lugar.

Quem "escuta" a tecla é o próprio ambiente de trabalho: o openTARS só
cadastra um atalho personalizado nas configurações dele, igual a quem vai
em Configurações > Teclado > Atalhos personalizados. Aparece lá, dá pra
mudar ou apagar por lá também. Suportados: GNOME (Ubuntu, Zorin, Pop!_OS,
Budgie), Cinnamon (Mint), MATE e XFCE. No KDE e nos demais, o comando a
cadastrar à mão é "opentars-gui --rapido".

Por que não Super+Espaço: no GNOME/Zorin ele já troca o layout do
teclado, e dois atalhos iguais brigam.

Uso: opentars --atalho            mostra a situação
     opentars --atalho ctrl+alt+space   cadastra/troca
     opentars --atalho off         remove
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import tars_i18n as i18n
from tars_i18n import t

PADRAO = "<Primary><Alt>space"
NOME = "openTARS"
MARCA_COMANDO = "--rapido"
TIMEOUT_SEG = 5

GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_CAMINHO = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
CINNAMON_SCHEMA = "org.cinnamon.desktop.keybindings"
CINNAMON_CAMINHO = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
MATE_ATALHOS = "org.mate.Marco.global-keybindings"
MATE_COMANDOS = "org.mate.Marco.keybinding-commands"
XFCE_CANAL = "xfce4-keyboard-shortcuts"

# Esquemas onde um atalho do próprio sistema pode já usar a combinação.
ESQUEMAS_DO_SISTEMA = {
    "gnome": ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
              "org.gnome.mutter.keybindings", GNOME_SCHEMA),
    "cinnamon": ("org.cinnamon.desktop.keybindings.wm", "org.cinnamon.desktop.keybindings.media-keys"),
    "mate": ("org.mate.Marco.global-keybindings", "org.mate.Marco.window-keybindings"),
}

# Nomes de tecla como o GTK espera (o resto: letra minúscula, F1..F12).
NOMES_TECLAS = {
    "space": "space", "espaco": "space", "espaço": "space", "espacio": "space", "espace": "space",
    "leertaste": "space", "enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape",
    "escape": "Escape", "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "insert": "Insert", "delete": "Delete", "backspace": "BackSpace",
}

MODIFICADORES = {
    "ctrl": "<Primary>", "control": "<Primary>", "primary": "<Primary>", "strg": "<Primary>",
    "alt": "<Alt>", "shift": "<Shift>", "super": "<Super>", "win": "<Super>", "meta": "<Super>",
    "mod4": "<Super>",
}


# ------------------------------------------------------------
# Combinação de teclas
# ------------------------------------------------------------

def normalizar_combinacao(texto):
    """"Ctrl+Alt+Space", "<Control><Alt>space", "super+t" -> formato GTK
    ("<Primary><Alt>space"). None se não der pra entender."""
    texto = (texto or "").strip()
    if not texto:
        return None
    if "<" in texto:
        mods = re.findall(r"<([^>]+)>", texto)
        tecla = re.sub(r"<[^>]+>", "", texto).strip()
    else:
        partes = [p.strip() for p in texto.replace("-", "+").split("+") if p.strip()]
        if not partes:
            return None
        mods, tecla = partes[:-1], partes[-1]
    saida = []
    for mod in mods:
        convertido = MODIFICADORES.get(mod.lower())
        if not convertido:
            return None
        if convertido not in saida:
            saida.append(convertido)
    if not tecla or not saida:
        return None
    nome = tecla.lower()
    if nome in NOMES_TECLAS:
        tecla = NOMES_TECLAS[nome]
    elif re.fullmatch(r"f\d{1,2}", nome):
        tecla = nome.upper()
    elif len(tecla) == 1:
        tecla = nome
    ordem = ["<Primary>", "<Shift>", "<Alt>", "<Super>"]
    return "".join(sorted(saida, key=ordem.index)) + tecla


def legivel(combinacao):
    """"<Primary><Alt>space" -> "Ctrl+Alt+Espaço" (no idioma escolhido)."""
    nomes = {"<Primary>": "Ctrl", "<Control>": "Ctrl", "<Alt>": "Alt", "<Shift>": "Shift", "<Super>": "Super"}
    mods = re.findall(r"<[^>]+>", combinacao or "")
    tecla = re.sub(r"<[^>]+>", "", combinacao or "")
    tecla = t("atalho.tecla_espaco") if tecla.lower() == "space" else tecla.upper() if len(tecla) == 1 else tecla.capitalize()
    return "+".join([nomes.get(m, m.strip("<>")) for m in mods] + [tecla])


def _mesma(a, b):
    return normalizar_combinacao(a) == normalizar_combinacao(b)


def _eh_nosso(comando):
    comando = comando or ""
    return MARCA_COMANDO in comando and ("opentars" in comando.lower() or "tars_gui" in comando)


def comando_barra_rapida():
    lancador = shutil.which("opentars-gui")
    if lancador:
        return f"{lancador} {MARCA_COMANDO}"
    gui = Path(__file__).resolve().with_name("tars_gui.py")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(gui))} {MARCA_COMANDO}"


# ------------------------------------------------------------
# Execução de comandos do sistema
# ------------------------------------------------------------

def _rodar(*cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SEG)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)


def _gsettings_get(esquema, chave):
    codigo, saida = _rodar("gsettings", "get", esquema, chave)
    return saida if codigo == 0 else None


def _gsettings_set(esquema, chave, valor):
    return _rodar("gsettings", "set", esquema, chave, valor)[0] == 0


def _lista_gvariant(texto):
    """"['a', 'b']" ou "@as []" -> ['a', 'b']."""
    return re.findall(r"'((?:[^'\\]|\\.)*)'", texto or "")


def _gvariant_texto(valor):
    return "'" + valor.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _gvariant_lista(valores):
    return "[" + ", ".join(_gvariant_texto(v) for v in valores) + "]" if valores else "@as []"


def _esquema_existe(esquema):
    codigo, saida = _rodar("gsettings", "list-schemas")
    return codigo == 0 and esquema in saida.split()


def _conflito_no_sistema(ambiente, combinacao):
    """Nome da ação do sistema que já usa a combinação, se houver."""
    for esquema in ESQUEMAS_DO_SISTEMA.get(ambiente, ()):
        codigo, saida = _rodar("gsettings", "list-recursively", esquema)
        if codigo != 0:
            continue
        for linha in saida.splitlines():
            partes = linha.split(None, 2)
            if len(partes) == 3 and any(_mesma(v, combinacao) for v in _lista_gvariant(partes[2])):
                return f"{esquema} {partes[1]}"
    return None


# ------------------------------------------------------------
# Ambientes
# ------------------------------------------------------------

def ambiente():
    atual = (os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "").upper()
    if "CINNAMON" in atual:
        return "cinnamon"
    if "MATE" in atual:
        return "mate"
    if "XFCE" in atual:
        return "xfce"
    if "KDE" in atual or "PLASMA" in atual:
        return "kde"
    if any(n in atual for n in ("GNOME", "UNITY", "BUDGIE", "ZORIN", "POP", "UBUNTU")):
        return "gnome"
    return None


_NOMES_AMBIENTE = {"gnome": "GNOME", "cinnamon": "Cinnamon", "mate": "MATE", "xfce": "XFCE", "kde": "KDE"}


def _gnome_itens():
    caminhos = _lista_gvariant(_gsettings_get(GNOME_SCHEMA, "custom-keybindings"))
    itens = []
    for caminho in caminhos:
        esquema = f"{GNOME_SCHEMA}.custom-keybinding:{caminho}"
        itens.append({
            "caminho": caminho,
            "comando": (_lista_gvariant(_gsettings_get(esquema, "command")) or [""])[0],
            "atalho": (_lista_gvariant(_gsettings_get(esquema, "binding")) or [""])[0],
        })
    return caminhos, itens


def _registrar_gnome(combinacao):
    if not shutil.which("gsettings") or not _esquema_existe(GNOME_SCHEMA):
        return False, t("atalho.sem_suporte")
    caminhos, itens = _gnome_itens()
    nosso = next((i for i in itens if _eh_nosso(i["comando"])), None)
    outro = next((i for i in itens if i is not nosso and _mesma(i["atalho"], combinacao)), None)
    if outro:
        return False, t("atalho.conflito", acao=outro["comando"] or outro["caminho"])
    sistema = _conflito_no_sistema("gnome", combinacao)
    if sistema:
        return False, t("atalho.conflito", acao=sistema)
    if nosso:
        caminho = nosso["caminho"]
    else:
        usados = set(caminhos)
        n = 0
        while f"{GNOME_CAMINHO}custom{n}/" in usados:
            n += 1
        caminho = f"{GNOME_CAMINHO}custom{n}/"
    esquema = f"{GNOME_SCHEMA}.custom-keybinding:{caminho}"
    ok = (_gsettings_set(esquema, "name", _gvariant_texto(NOME))
          and _gsettings_set(esquema, "command", _gvariant_texto(comando_barra_rapida()))
          and _gsettings_set(esquema, "binding", _gvariant_texto(combinacao)))
    if ok and caminho not in caminhos:
        ok = _gsettings_set(GNOME_SCHEMA, "custom-keybindings", _gvariant_lista(caminhos + [caminho]))
    return ok, None if ok else t("atalho.falhou")


def _remover_gnome():
    caminhos, itens = _gnome_itens()
    nossos = [i["caminho"] for i in itens if _eh_nosso(i["comando"])]
    if not nossos:
        return True
    for caminho in nossos:
        _rodar("gsettings", "reset-recursively", f"{GNOME_SCHEMA}.custom-keybinding:{caminho}")
    return _gsettings_set(GNOME_SCHEMA, "custom-keybindings", _gvariant_lista([c for c in caminhos if c not in nossos]))


def _cinnamon_itens():
    nomes = _lista_gvariant(_gsettings_get(CINNAMON_SCHEMA, "custom-list"))
    itens = []
    for nome in nomes:
        esquema = f"{CINNAMON_SCHEMA}.custom-keybinding:{CINNAMON_CAMINHO}{nome}/"
        itens.append({
            "nome": nome,
            "comando": (_lista_gvariant(_gsettings_get(esquema, "command")) or [""])[0],
            "atalhos": _lista_gvariant(_gsettings_get(esquema, "binding")),
        })
    return nomes, itens


def _registrar_cinnamon(combinacao):
    if not shutil.which("gsettings") or not _esquema_existe(CINNAMON_SCHEMA):
        return False, t("atalho.sem_suporte")
    nomes, itens = _cinnamon_itens()
    nosso = next((i for i in itens if _eh_nosso(i["comando"])), None)
    outro = next((i for i in itens if i is not nosso and any(_mesma(a, combinacao) for a in i["atalhos"])), None)
    if outro:
        return False, t("atalho.conflito", acao=outro["comando"] or outro["nome"])
    sistema = _conflito_no_sistema("cinnamon", combinacao)
    if sistema:
        return False, t("atalho.conflito", acao=sistema)
    if nosso:
        nome = nosso["nome"]
    else:
        n = 0
        while f"custom{n}" in nomes:
            n += 1
        nome = f"custom{n}"
    esquema = f"{CINNAMON_SCHEMA}.custom-keybinding:{CINNAMON_CAMINHO}{nome}/"
    ok = (_gsettings_set(esquema, "name", _gvariant_texto(NOME))
          and _gsettings_set(esquema, "command", _gvariant_texto(comando_barra_rapida()))
          and _gsettings_set(esquema, "binding", _gvariant_lista([combinacao])))
    if ok and nome not in nomes:
        ok = _gsettings_set(CINNAMON_SCHEMA, "custom-list", _gvariant_lista(nomes + [nome]))
    return ok, None if ok else t("atalho.falhou")


def _remover_cinnamon():
    nomes, itens = _cinnamon_itens()
    nossos = [i["nome"] for i in itens if _eh_nosso(i["comando"])]
    for nome in nossos:
        _rodar("gsettings", "reset-recursively", f"{CINNAMON_SCHEMA}.custom-keybinding:{CINNAMON_CAMINHO}{nome}/")
    return not nossos or _gsettings_set(CINNAMON_SCHEMA, "custom-list", _gvariant_lista([n for n in nomes if n not in nossos]))


def _registrar_mate(combinacao):
    if not shutil.which("gsettings") or not _esquema_existe(MATE_COMANDOS):
        return False, t("atalho.sem_suporte")
    livre = None
    for n in range(1, 13):
        comando = (_lista_gvariant(_gsettings_get(MATE_COMANDOS, f"command-{n}")) or [""])[0]
        atalho = (_lista_gvariant(_gsettings_get(MATE_ATALHOS, f"run-command-{n}")) or [""])[0]
        if _eh_nosso(comando):
            livre = n
            break
        if _mesma(atalho, combinacao):
            return False, t("atalho.conflito", acao=comando or f"run-command-{n}")
        if livre is None and not comando and atalho in ("", "disabled"):
            livre = n
    if livre is None:
        return False, t("atalho.sem_espaco")
    sistema = _conflito_no_sistema("mate", combinacao)
    if sistema and f"run-command-{livre}" not in sistema:
        return False, t("atalho.conflito", acao=sistema)
    ok = (_gsettings_set(MATE_COMANDOS, f"command-{livre}", _gvariant_texto(comando_barra_rapida()))
          and _gsettings_set(MATE_ATALHOS, f"run-command-{livre}", _gvariant_texto(combinacao)))
    return ok, None if ok else t("atalho.falhou")


def _remover_mate():
    for n in range(1, 13):
        comando = (_lista_gvariant(_gsettings_get(MATE_COMANDOS, f"command-{n}")) or [""])[0]
        if _eh_nosso(comando):
            _rodar("gsettings", "reset", MATE_COMANDOS, f"command-{n}")
            _rodar("gsettings", "reset", MATE_ATALHOS, f"run-command-{n}")
    return True


def _xfconf(*args):
    return _rodar("xfconf-query", "-c", XFCE_CANAL, *args)


def _registrar_xfce(combinacao):
    if not shutil.which("xfconf-query"):
        return False, t("atalho.sem_suporte")
    for base in ("/commands/custom/", "/xfwm4/custom/"):
        codigo, atual = _xfconf("-p", base + combinacao)
        if codigo == 0 and atual and not _eh_nosso(atual):
            return False, t("atalho.conflito", acao=atual)
    _remover_xfce()
    ok = _xfconf("-p", "/commands/custom/" + combinacao, "-n", "-t", "string", "-s", comando_barra_rapida())[0] == 0
    return ok, None if ok else t("atalho.falhou")


def _remover_xfce():
    codigo, saida = _xfconf("-l", "-v")
    if codigo != 0:
        return False
    for linha in saida.splitlines():
        partes = linha.split(None, 1)
        if len(partes) == 2 and partes[0].startswith("/commands/custom/") and _eh_nosso(partes[1]):
            _xfconf("-p", partes[0], "-r")
    return True


_REGISTRAR = {"gnome": _registrar_gnome, "cinnamon": _registrar_cinnamon, "mate": _registrar_mate, "xfce": _registrar_xfce}
_REMOVER = {"gnome": _remover_gnome, "cinnamon": _remover_cinnamon, "mate": _remover_mate, "xfce": _remover_xfce}


# ------------------------------------------------------------
# API
# ------------------------------------------------------------

def registrar(combinacao=None):
    """Cadastra (ou troca) o atalho. Devolve (ok, mensagem)."""
    combinacao = normalizar_combinacao(combinacao) if combinacao else (i18n.ler_config().get("atalho") or PADRAO)
    if not combinacao:
        return False, t("atalho.invalido")
    qual = ambiente()
    funcao = _REGISTRAR.get(qual)
    if funcao is None:
        motivo = t("atalho.manual", ambiente=_NOMES_AMBIENTE.get(qual, qual or "?"), comando="opentars-gui --rapido")
        i18n.salvar_config(atalho=combinacao, atalho_registrado=False, atalho_tentado=True)
        return False, motivo
    ok, motivo = funcao(combinacao)
    i18n.salvar_config(atalho=combinacao, atalho_registrado=ok, atalho_tentado=True,
                       atalho_ambiente=_NOMES_AMBIENTE.get(qual, qual))
    if ok:
        return True, t("atalho.registrado", atalho=legivel(combinacao), ambiente=_NOMES_AMBIENTE.get(qual, qual))
    return False, motivo


def remover():
    qual = ambiente()
    funcao = _REMOVER.get(qual)
    ok = funcao() if funcao else True
    i18n.salvar_config(atalho_registrado=False, atalho_tentado=True, atalho_desligado=True)
    return ok, t("atalho.removido")


def estado():
    config = i18n.ler_config()
    combinacao = config.get("atalho") or PADRAO
    info = {"combinacao": combinacao, "legivel": legivel(combinacao), "ambiente": config.get("atalho_ambiente"),
            "registrado": bool(config.get("atalho_registrado"))}
    if not info["registrado"]:
        qual = ambiente()
        if qual not in _REGISTRAR:
            info["motivo"] = t("atalho.manual", ambiente=_NOMES_AMBIENTE.get(qual, qual or "?"),
                               comando="opentars-gui --rapido")
    return info


def garantir_registrado():
    """Na primeira vez que a janela abre: cadastra o atalho padrão (uma
    tentativa só; quem desligou com --atalho off não é incomodado)."""
    config = i18n.ler_config()
    if config.get("atalho_tentado") or config.get("atalho_desligado"):
        return None
    if os.environ.get("TARS_SEM_ATALHO") == "1":
        return None
    return registrar()


def comando(argumento):
    """opentars --atalho [combinação|off]"""
    argumento = (argumento or "").strip()
    if not argumento or argumento.lower() in ("status", "estado"):
        info = estado()
        if info["registrado"]:
            print(t("atalho.registrado", atalho=info["legivel"], ambiente=info.get("ambiente") or "?"))
        else:
            print(info.get("motivo") or t("atalho.nao_registrado"))
            print(t("atalho.como_registrar"))
        return 0
    if argumento.lower() in ("off", "desligar", "remover", "none", "no", "nao", "não"):
        ok, mensagem = remover()
        print(mensagem)
        return 0 if ok else 1
    ok, mensagem = registrar(argumento)
    print(mensagem)
    return 0 if ok else 1
