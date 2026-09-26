"""
Controle de aplicativos pela árvore de acessibilidade (AT-SPI).

É o mesmo caminho que um leitor de tela usa: cada janela expõe seus
botões, campos e textos com nome e papel ("botão '7'", "campo 'Buscar'").
Com isso o openTARS clica num botão PELO NOME, sem print e sem
coordenada:

- rápido: um clique leva milissegundos, contra um print + uma rodada do
  modelo de visão pra achar a posição;
- funciona com qualquer modelo (não precisa de visão);
- funciona no Wayland, onde o clique simulado do pyautogui não chega:
  a ação vai direto pro aplicativo pelo D-Bus.

Apps GTK (GNOME, Zorin), Qt/KDE, Firefox e LibreOffice expõem essa árvore.
Quando um app não expõe (alguns jogos, apps Electron sem acessibilidade),
a ferramenta avisa e a IA volta pro print + coordenadas.

Este módulo não importa o tars.py (evita importar o núcleo duas vezes
quando ele roda como script): recebe do núcleo o que precisa saber da
janela (pid, título, nomes possíveis) e devolve dicionários simples.
"""

import os
import threading
import time
import unicodedata

# Limites da varredura: um navegador expõe a página inteira (milhares de
# nós); o que interessa pra clicar está no que está visível.
LIMITE_NOS = 2500
LIMITE_TEMPO_SEG = 1.5
LIMITE_ELEMENTOS_IA = 120
LIMITE_TEXTOS_IA = 25
LIMITE_CHARS_TEXTO = 200
VALIDADE_CACHE_SEG = 3.0
# Depois do clique, o app processa a ação no próprio loop: os textos da
# janela (ex: o visor da calculadora) são lidos quando mudarem, até isso.
ESPERA_MAXIMA_APOS_ACAO_SEG = 0.6
INTERVALO_LEITURA_SEG = 0.05

# Papel no AT-SPI (value_nick) -> nome curto que a IA vê.
PAPEIS_CLICAVEIS = {
    "push-button": "button",
    "toggle-button": "toggle button",
    "check-box": "checkbox",
    "radio-button": "radio button",
    "menu-item": "menu item",
    "check-menu-item": "menu item",
    "radio-menu-item": "menu item",
    "menu": "menu",
    "link": "link",
    "page-tab": "tab",
    "combo-box": "combo box",
    "list-item": "list item",
    "tree-item": "tree item",
    "table-cell": "cell",
    "spin-button": "spin button",
    "slider": "slider",
    "icon": "icon",
    "switch": "switch",
    "button": "button",
}
PAPEIS_CAMPO = {
    "entry": "text field",
    "password-text": "password field",
    "text": "text field",
    "search-box": "search field",
    "terminal": "terminal",
}
PAPEIS_TEXTO = {
    "label", "heading", "paragraph", "static", "status-bar", "alert",
    "notification", "caption", "text", "entry", "document-text", "tooltip",
}
PAPEIS_JANELA = {"frame", "window", "dialog", "alert", "file-chooser", "color-chooser", "font-chooser"}

# Ação preferida quando o elemento tem várias.
ACOES_PREFERIDAS = ("click", "press", "activate", "jump", "toggle", "open", "select", "expand or contract")

# Símbolos que a IA escreve de um jeito e o app mostra de outro.
SINONIMOS = {"*": "×", "x": "×", "/": "÷", "-": "−", "–": "−", "sqrt": "√", "pi": "π"}

_atspi = {"modulo": None, "erro": None, "tentou": False}
_trava = threading.Lock()
_cache = {}  # id da janela -> (instante, [itens], janela)


def _barramento_acessivel():
    """O barramento de acessibilidade está no ar? A biblioteca Atspi, se não
    achar, ABORTA o processo inteiro (g_error, não exceção) — então a
    checagem é feita antes, por um caminho que só devolve erro."""
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not (runtime and os.path.exists(os.path.join(runtime, "bus"))):
        return "no D-Bus session"
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
                      GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 3000, None)
        return None
    except Exception as e:  # at-spi2-core ausente, sessão sem barramento...
        return f"accessibility bus not available ({type(e).__name__})"


def _modulo():
    if not _atspi["tentou"]:
        _atspi["tentou"] = True
        if os.environ.get("TARS_SEM_ACESSIBILIDADE") == "1":
            _atspi["erro"] = "disabled (TARS_SEM_ACESSIBILIDADE=1)"
        else:
            try:
                import gi
                gi.require_version("Atspi", "2.0")
                from gi.repository import Atspi
                _atspi["erro"] = _barramento_acessivel()
                if not _atspi["erro"]:
                    _atspi["modulo"] = Atspi
            except Exception as e:  # pacote gir1.2-atspi-2.0 / python3-gi ausente
                _atspi["erro"] = f"{type(e).__name__}: {e}"
    return _atspi["modulo"]


def indisponivel():
    """None se a acessibilidade pode ser usada; senão o motivo."""
    return None if _modulo() else _atspi["erro"]


def chave_texto(texto):
    """Pra comparar nomes: sem acento, minúsculo, espaços simples."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return " ".join(texto.casefold().split())


def ativar():
    """Liga a acessibilidade da sessão (org.a11y.Status.IsEnabled), como
    um leitor de tela faz. Apps Qt/KDE e alguns outros só expõem a árvore
    com isso ligado. Vale até sair da sessão; não muda nada permanente."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        return True
    except Exception:
        return False


# ------------------------------------------------------------
# Janelas
# ------------------------------------------------------------

def _estados(no):
    try:
        return {s.value_nick for s in no.get_state_set().get_states()}
    except Exception:
        return set()


def _papel(no):
    try:
        return no.get_role().value_nick
    except Exception:
        return ""


def _nome(no):
    try:
        return no.get_name() or ""
    except Exception:
        return ""


def _filhos(no):
    try:
        n = no.get_child_count()
    except Exception:
        return
    for i in range(n):
        try:
            filho = no.get_child_at_index(i)
        except Exception:
            continue
        if filho is not None:
            yield filho


def janelas():
    """Janelas de nível superior que aparecem na tela, com o app dono."""
    Atspi = _modulo()
    if Atspi is None:
        return []
    saida = []
    try:
        desktop = Atspi.get_desktop(0)
    except Exception:
        return []
    for app in _filhos(desktop):
        try:
            pid = app.get_process_id()
        except Exception:
            pid = None
        nome_app = _nome(app)
        for janela in _filhos(app):
            if _papel(janela) not in PAPEIS_JANELA:
                continue
            estados = _estados(janela)
            if estados and "showing" not in estados and "visible" not in estados:
                continue
            saida.append({
                "no": janela, "app": nome_app, "pid": pid, "titulo": _nome(janela),
                "ativa": "active" in estados,
            })
    return saida


def _pontos_janela(janela, termos):
    """3: nome exato do app ou título; 2: parte do nome do app; 1: o título
    TERMINA com o termo ("Sem título 1 - LibreOffice Writer"). Título que
    só CONTÉM o termo não conta: uma aba "Calculadora online - Chrome" não
    é a calculadora (navegador põe o nome da página na frente)."""
    titulo = chave_texto(janela["titulo"])
    app = chave_texto(janela["app"])
    melhor = 0
    for termo in termos:
        t = chave_texto(termo)
        if len(t) < 2:
            continue
        if t == titulo or t == app:
            melhor = max(melhor, 3)
        elif t in app.replace("-", " ").split() or t in app:
            melhor = max(melhor, 2)
        elif titulo.endswith(t):
            melhor = max(melhor, 1)
    return melhor


def achar_janela(pid=None, titulo=None, termos=(), ativa=False, ignorar_pids=()):
    """A janela acessível que corresponde ao pedido.

    pid/titulo vêm da lista de janelas do X (mais confiável); termos são os
    nomes possíveis do app ("calculadora", "Calculator", "gnome-calculator");
    ativa=True pega a janela em foco. Dentro do app, a janela em foco
    ganha: um diálogo aberto ("Salvar alterações?") é onde está o botão."""

    todas = [j for j in janelas() if j["pid"] not in ignorar_pids]
    if not todas:
        return None

    def melhor_do_app(lista):
        if titulo:
            exata = [j for j in lista if j["titulo"] == titulo]
            ativas = [j for j in lista if j["ativa"]]
            return (ativas or exata or lista)[0]
        return next((j for j in lista if j["ativa"]), lista[0])

    if pid:
        do_app = [j for j in todas if j["pid"] == pid]
        if do_app:
            return melhor_do_app(do_app)

    if termos:
        pontuadas = [(_pontos_janela(j, termos), j) for j in todas]
        maximo = max((p for p, _ in pontuadas), default=0)
        if maximo:
            candidatas = [j for p, j in pontuadas if p == maximo]
            return melhor_do_app(candidatas)
        return None

    if ativa or not termos:
        return next((j for j in todas if j["ativa"]), None)
    return None


# ------------------------------------------------------------
# Elementos
# ------------------------------------------------------------

def _texto_do_no(no, papel):
    Atspi = _modulo()
    if papel in PAPEIS_CAMPO or papel in ("document-text", "paragraph"):
        try:
            n = Atspi.Text.get_character_count(no)
            if n:
                return Atspi.Text.get_text(no, 0, min(n, LIMITE_CHARS_TEXTO))
        except Exception:
            pass
    return _nome(no)


def _nome_do_elemento(no, papel):
    """Nome visível: o do próprio elemento; sem ele, a descrição (dica) ou
    o rótulo filho (botões com ícone + texto)."""
    nome = _nome(no)
    if nome:
        return nome
    try:
        nome = no.get_description() or ""
    except Exception:
        nome = ""
    if nome:
        return nome
    for filho in _filhos(no):
        if _papel(filho) in ("label", "static"):
            nome = _nome(filho)
            if nome:
                return nome
    return ""


def _varrer(raiz):
    """Percorre a janela e devolve [(no, papel, nome, estados, tipo)], tipo
    = 'clicavel', 'campo' ou 'texto'. Não desce em partes escondidas, e o
    texto de dentro de um botão não conta como texto solto da janela."""
    itens = []
    inicio = time.monotonic()
    visitados = 0
    pilha = [(raiz, False)]
    while pilha:
        no, dentro_de_clicavel = pilha.pop()
        visitados += 1
        if visitados > LIMITE_NOS or time.monotonic() - inicio > LIMITE_TEMPO_SEG:
            break
        papel = _papel(no)
        estados = _estados(no)
        if no is not raiz and estados and "showing" not in estados:
            continue
        clicavel = False
        if papel in PAPEIS_CAMPO and ("editable" in estados or papel != "text"):
            itens.append((no, papel, _nome_do_elemento(no, papel), estados, "campo"))
        elif papel in PAPEIS_CLICAVEIS:
            itens.append((no, papel, _nome_do_elemento(no, papel), estados, "clicavel"))
            clicavel = True
        elif papel in PAPEIS_TEXTO and not dentro_de_clicavel:
            itens.append((no, papel, "", estados, "texto"))
        # Filhos em ordem (a pilha inverte).
        marca = dentro_de_clicavel or clicavel
        pilha.extend(reversed([(f, marca) for f in _filhos(no)]))
    return itens


def _itens(janela, forcar=False):
    raiz = janela["no"]
    chave = id(raiz)
    with _trava:
        guardado = _cache.get(chave)
        if (guardado and not forcar and guardado[2] is raiz
                and time.monotonic() - guardado[0] < VALIDADE_CACHE_SEG):
            return guardado[1]
    itens = _varrer(raiz)
    with _trava:
        if len(_cache) > 32:
            _cache.clear()
        _cache[chave] = (time.monotonic(), itens, raiz)
    return itens


def _textos(itens):
    textos = []
    for no, papel, _nome_item, _estados_item, tipo in itens:
        if tipo not in ("texto", "campo"):
            continue
        texto = " ".join((_texto_do_no(no, papel) or "").split())
        if texto and texto not in textos:
            textos.append(texto[:LIMITE_CHARS_TEXTO])
        if len(textos) >= LIMITE_TEXTOS_IA:
            break
    return textos


def _rotulo(papel, nome, estados):
    nome_ia = PAPEIS_CLICAVEIS.get(papel) or PAPEIS_CAMPO.get(papel) or papel
    extra = "" if not estados or "sensitive" in estados or "enabled" in estados else " (disabled)"
    return f"{nome_ia} '{nome}'{extra}" if nome else f"{nome_ia} (no name){extra}"


def listar(janela, filtro=None):
    """Elementos clicáveis e campos (agrupados por papel) + textos que a
    janela mostra. É o que a ferramenta list_elements devolve à IA."""
    itens = _itens(janela, forcar=True)
    filtro_k = chave_texto(filtro) if filtro else ""
    grupos, total = {}, 0
    for _no, papel, nome, estados, tipo in itens:
        if tipo == "texto" or (not nome and tipo == "clicavel"):
            continue
        if filtro_k and filtro_k not in chave_texto(nome):
            continue
        if total >= LIMITE_ELEMENTOS_IA:
            break
        papel_ia = PAPEIS_CLICAVEIS.get(papel) or PAPEIS_CAMPO.get(papel) or papel
        desativado = bool(estados) and "sensitive" not in estados and "enabled" not in estados
        grupos.setdefault(papel_ia, []).append((nome or "(no name)") + (" (disabled)" if desativado else ""))
        total += 1
    return {"elementos": grupos, "textos": _textos(itens), "total": total}


def _pontos_nome(alvo_k, nome_k):
    if not nome_k:
        return 0
    if alvo_k == nome_k:
        return 100
    if SINONIMOS.get(alvo_k) == nome_k:
        return 90
    if nome_k.startswith(alvo_k) and len(alvo_k) >= 2:
        return 60
    if len(alvo_k) >= 3 and alvo_k in nome_k:
        return 40
    return 0


def _achar(itens, nome, papel=None, tipos=("clicavel", "campo")):
    alvo_k = chave_texto(nome)
    papel_k = chave_texto(papel) if papel else ""
    melhor, melhor_pontos = None, 0
    for item in itens:
        no, papel_item, nome_item, estados, tipo = item
        if tipo not in tipos:
            continue
        pontos = _pontos_nome(alvo_k, chave_texto(nome_item))
        if not pontos:
            continue
        papel_ia = PAPEIS_CLICAVEIS.get(papel_item) or PAPEIS_CAMPO.get(papel_item) or papel_item
        if papel_k and papel_k in (chave_texto(papel_ia), chave_texto(papel_item)):
            pontos += 5
        if not estados or "sensitive" in estados or "enabled" in estados:
            pontos += 3
        if pontos > melhor_pontos:  # empate: o primeiro na ordem da tela
            melhor, melhor_pontos = item, pontos
    return melhor


def parecidos(janela, nome, limite=8):
    """Nomes da janela mais próximos do pedido (pra IA tentar de novo)."""
    alvo_k = chave_texto(nome)
    nomes = []
    for _no, _papel_item, nome_item, _estados, tipo in _itens(janela):
        if tipo != "texto" and nome_item and nome_item not in nomes:
            nomes.append(nome_item)
    nomes.sort(key=lambda n: (alvo_k not in chave_texto(n), len(n)))
    return nomes[:limite]


def _acionar(no):
    """Executa a ação padrão do elemento (clicar, pressionar, ativar)."""
    Atspi = _modulo()
    try:
        n = Atspi.Action.get_n_actions(no)
    except Exception:
        n = 0
    if not n:
        return False
    ler_nome = getattr(Atspi.Action, "get_name", None) or Atspi.Action.get_action_name
    nomes = []
    for i in range(n):
        try:
            nomes.append((ler_nome(no, i) or "").lower())
        except Exception:
            nomes.append("")
    indice = next((nomes.index(a) for a in ACOES_PREFERIDAS if a in nomes), 0)
    try:
        return bool(Atspi.Action.do_action(no, indice))
    except Exception:
        return False


def _retangulo(no):
    Atspi = _modulo()
    try:
        r = Atspi.Component.get_extents(no, Atspi.CoordType.SCREEN)
        return (r.x, r.y, r.width, r.height)
    except Exception:
        return None


def coordenadas_confiaveis(janela):
    """As posições informadas pelo app são de tela? GTK 4 informa posições
    relativas (um botão no meio da janela aparece em 0,0)."""
    try:
        app = janela["no"].get_application()
        return not (app.get_toolkit_name() == "GTK" and (app.get_toolkit_version() or "").startswith("4"))
    except Exception:
        return False


def clicar(janela, nome, papel=None):
    """Clica no elemento pelo nome. Devolve um dict com 'sucesso'; sem
    ação disponível, devolve 'retangulo' pro núcleo tentar o clique de
    mouse (só vale se as coordenadas forem de tela, conferido lá)."""
    item = _achar(_itens(janela), nome, papel)
    if item is None:
        item = _achar(_itens(janela, forcar=True), nome, papel)
    if item is None:
        return {"sucesso": False, "achou": False}

    no, papel_item, nome_item, estados, _tipo = item
    rotulo = _rotulo(papel_item, nome_item, estados)
    itens = _itens(janela)
    antes = _textos(itens)
    if _acionar(no):
        return {"sucesso": True, "achou": True, "elemento": rotulo, "textos": _textos_depois(itens, antes)}
    return {"sucesso": False, "achou": True, "elemento": rotulo, "retangulo": _retangulo(no)}


def _textos_depois(itens, antes):
    """Os textos da janela depois de uma ação. O app leva um instante pra
    atualizar (a calculadora do GNOME, ~0,3 s): espera a mudança, até um
    limite, em vez de devolver o visor antigo."""
    limite = time.monotonic() + ESPERA_MAXIMA_APOS_ACAO_SEG
    while True:
        time.sleep(INTERVALO_LEITURA_SEG)
        agora = _textos(itens)
        if agora != antes or time.monotonic() >= limite:
            return agora


def preencher(janela, nome, texto):
    """Escreve num campo pelo nome (ou no único campo da janela, se o
    nome não bater). Sem suporte a edição direta, põe o foco no campo e
    devolve precisa_digitar=True: o núcleo digita pelo teclado."""
    Atspi = _modulo()
    itens = _itens(janela, forcar=True)
    campos = [i for i in itens if i[4] == "campo"]
    item = _achar(itens, nome, tipos=("campo",)) if nome else None
    if item is None and len(campos) == 1:
        item = campos[0]
    if item is None:
        focado = [i for i in campos if "focused" in i[3]]
        item = focado[0] if focado else None
    if item is None:
        return {"sucesso": False, "achou": False, "campos": [c[2] or "(no name)" for c in campos][:10]}

    no, papel_item, nome_item, estados, _tipo = item
    rotulo = _rotulo(papel_item, nome_item, estados)
    try:
        if Atspi.EditableText.set_text_contents(no, texto):
            limite = time.monotonic() + ESPERA_MAXIMA_APOS_ACAO_SEG
            while time.monotonic() < limite:
                time.sleep(INTERVALO_LEITURA_SEG)
                if texto.strip() in (_texto_do_no(no, papel_item) or ""):
                    return {"sucesso": True, "achou": True, "elemento": rotulo}
    except Exception:
        pass
    try:
        Atspi.Component.grab_focus(no)
    except Exception:
        pass
    return {"sucesso": False, "achou": True, "elemento": rotulo, "precisa_digitar": True}


def limpar_cache():
    with _trava:
        _cache.clear()
