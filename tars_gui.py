#!/usr/bin/env python3
"""
Interface gráfica do openTARS.

Só Tkinter + Pillow (já instalados com o openTARS). Todo o "cérebro"
continua no tars.py: esta janela chama as mesmas funções do modo terminal
e recebe o que acontece como EVENTOS (ferramenta rodando, modelo
carregando, raciocínio e resposta chegando), já estruturados — não
precisa ler o texto impresso, que muda com o idioma.

Duas faces, um processo só:
- a janela principal (conversa completa);
- a barra rápida (opentars-gui --rapido, ou o atalho global): uma caixa
  flutuante pra pedir algo sem trocar de janela.
Abrir de novo não cria outro processo: ver tars_instancia.py.
"""

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import tars as core
import tars_i18n as i18n
import tars_instancia
from tars_i18n import t


# ============================================================
# IDENTIDADE VISUAL
# ============================================================

FUNDO = "#0B1020"
SUPERFICIE = "#121A33"
SUPERFICIE_2 = "#1A2446"
BORDA = "#27335C"
TEXTO = "#E8ECF4"
TEXTO_2 = "#8A93A6"
TEXTO_3 = "#5E6781"
DESTAQUE = "#5FD97A"
DESTAQUE_HOVER = "#7BE693"
DESTAQUE_TEXTO = "#0B1020"
ROXO = "#B69CFF"
AZUL = "#6EA8FF"
AMARELO = "#F2C14E"
VERMELHO = "#FF6B6B"
VERMELHO_HOVER = "#FF8585"
FUNDO_CODIGO = "#0E1428"

# A barra rápida fica aberta em segundo plano pra abrir na hora; sem uso
# (e com a janela principal fechada) por este tempo, o processo encerra.
MINUTOS_OCIOSO = float(os.environ.get("TARS_OCIOSO_MIN", "30"))

# Linhas de bastidor (modelo escolhido, carregando, ferramentas): ficam
# coladas umas nas outras, sem linha em branco entre elas.
ESTILOS_COMPACTOS = {"meta", "suave", "ferramenta", "ferramenta_detalhe", "ferramenta_icone", "ferramenta_ok", "tempo"}
# Os que começam uma linha nova de bastidor.
ESTILOS_INICIO_DE_LINHA = {"meta", "suave", "tempo", "ferramenta_icone"}


def automatico():
    return t("gui.automatico")


def exemplos():
    return i18n.lista("gui.exemplos")


def _primeira_fonte(opcoes, padrao):
    try:
        disponiveis = set(tkfont.families())
    except tk.TclError:
        return padrao
    return next((f for f in opcoes if f in disponiveis), padrao)


def desenhar_logo(tamanho):
    """O ícone do openTARS (quatro placas, como o robô do filme, com a
    faixa verde) desenhado com Pillow: a janela não depende de um PNG
    instalado em algum lugar."""
    from PIL import Image, ImageDraw

    escala = 4  # desenha grande e reduz: bordas suaves
    lado = tamanho * escala
    img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = lado / 128
    d.rounded_rectangle([4 * u, 4 * u, 124 * u, 124 * u], radius=26 * u, fill=SUPERFICIE_2)
    for x in (30, 47, 66, 83):
        d.rounded_rectangle([x * u, 24 * u, (x + 15) * u, 104 * u], radius=3 * u, fill=TEXTO)
    d.rounded_rectangle([45 * u, 42 * u, 83 * u, 52 * u], radius=3 * u, fill=DESTAQUE)
    return img.resize((tamanho, tamanho), Image.LANCZOS)


# ============================================================
# X11: janela sem borda, foco e fim do "carregando" do atalho
# ============================================================

def _com_display_x(funcao):
    try:
        from Xlib import display as xdisplay
        d = xdisplay.Display()
    except Exception:
        return False
    try:
        funcao(d)
        d.sync()
        return True
    except Exception:
        return False
    finally:
        try:
            d.close()
        except Exception:
            pass


def _janela_cliente_x(d, janela_tk):
    """A janela X que o gerenciador de janelas enxerga: no Tk é a "wrapper",
    mãe da janela interna (winfo_id). Existe antes mesmo de aparecer."""
    interna = d.create_resource_object("window", janela_tk.winfo_id())
    return interna.query_tree().parent


def x_sem_bordas(janela_tk):
    """Tira a moldura (dica do Motif, que GNOME/KDE/XFCE respeitam) e a
    janela da barra de tarefas. Chamado com a janela escondida: o
    gerenciador de janelas lê isso quando ela aparece."""

    def aplicar(d):
        from Xlib import Xatom
        w = _janela_cliente_x(d, janela_tk)
        motif = d.intern_atom("_MOTIF_WM_HINTS")
        w.change_property(motif, motif, 32, [2, 0, 0, 0, 0])
        estados = [d.intern_atom(n) for n in ("_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER", "_NET_WM_STATE_ABOVE")]
        w.change_property(d.intern_atom("_NET_WM_STATE"), Xatom.ATOM, 32, estados)

    return _com_display_x(aplicar)


def _momento_do_id(id_inicio):
    m = re.search(r"_TIME(\d+)$", id_inicio or "")
    return int(m.group(1)) if m else 0


def x_ativar(janela_tk, id_inicio=None):
    """Pede o foco como um "pager" (a barra de tarefas): o GNOME não
    bloqueia esse pedido como "roubo de foco" de um app em segundo plano."""

    def aplicar(d):
        from Xlib import X, protocol
        w = _janela_cliente_x(d, janela_tk)
        evento = protocol.event.ClientMessage(
            window=w, client_type=d.intern_atom("_NET_ACTIVE_WINDOW"),
            data=(32, [2, _momento_do_id(id_inicio) or X.CurrentTime, 0, 0, 0]),
        )
        d.screen().root.send_event(evento, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)

    return _com_display_x(aplicar)


def x_fim_do_carregamento(id_inicio):
    """Avisa o ambiente que o que o atalho/menu lançou já abriu (senão o
    cursor fica "carregando" até estourar o tempo)."""
    if not id_inicio:
        return False

    def aplicar(d):
        from Xlib import X, protocol
        raiz = d.screen().root
        dummy = raiz.create_window(-100, -100, 1, 1, 0, X.CopyFromParent, override_redirect=True)
        valor = id_inicio.replace("\\", "\\\\").replace('"', '\\"')
        texto = f'remove: ID="{valor}"'.encode("utf-8") + b"\0"
        inicio, meio = d.intern_atom("_NET_STARTUP_INFO_BEGIN"), d.intern_atom("_NET_STARTUP_INFO")
        for i in range(0, len(texto), 20):
            evento = protocol.event.ClientMessage(
                window=dummy, client_type=inicio if i == 0 else meio,
                data=(8, texto[i:i + 20].ljust(20, b"\0")),
            )
            raiz.send_event(evento, event_mask=X.PropertyChangeMask)
        dummy.destroy()

    return _com_display_x(aplicar)


# ============================================================
# WIDGETS ARREDONDADOS (Canvas)
# ============================================================

def _retangulo_arredondado(canvas, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pontos = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return canvas.create_polygon(pontos, smooth=True, **kw)


class BotaoRedondo(tk.Canvas):
    """Botão com cantos arredondados e hover — o tk.Button não tem."""

    def __init__(self, pai, texto, comando, fonte, cor, cor_hover, cor_texto,
                 fundo, raio=10, padx=16, pady=8, largura=None):
        self._fonte = fonte
        self._padx = padx
        self._largura_fixa = largura
        medida = tkfont.Font(font=fonte)
        largura = largura or medida.measure(texto) + 2 * padx
        altura = medida.metrics("linespace") + 2 * pady
        super().__init__(pai, width=largura, height=altura, bg=fundo,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._comando = comando
        self._cores = (cor, cor_hover, cor_texto)
        self._raio = raio
        self._texto = texto
        self._ativo = True
        self._hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._clique)
        self.bind("<Configure>", lambda e: self._desenhar())
        self._desenhar()

    def _set_hover(self, valor):
        self._hover = valor
        self._desenhar()

    def _clique(self, _evento):
        if self._ativo and self._comando:
            self._comando()

    def configurar(self, texto=None, cor=None, cor_hover=None, cor_texto=None, ativo=None):
        if texto is not None and texto != self._texto:
            self._texto = texto
            if not self._largura_fixa:  # o texto mudou (outro idioma): a largura acompanha
                self.configure(width=tkfont.Font(font=self._fonte).measure(texto) + 2 * self._padx)
        c, h, tx = self._cores
        self._cores = (cor or c, cor_hover or h, cor_texto or tx)
        if ativo is not None:
            self._ativo = ativo
            self.configure(cursor="hand2" if ativo else "arrow")
        self._desenhar()

    def _desenhar(self):
        self.delete("all")
        cor, cor_hover, cor_texto = self._cores
        preenchimento = cor_hover if (self._hover and self._ativo) else cor
        if not self._ativo:
            preenchimento = BORDA
        w, h = self.winfo_width() or int(self["width"]), self.winfo_height() or int(self["height"])
        _retangulo_arredondado(self, 1, 1, w - 1, h - 1, self._raio, fill=preenchimento, outline=preenchimento)
        self.create_text(w / 2, h / 2, text=self._texto, fill=cor_texto if self._ativo else TEXTO_2, font=self._fonte)


# ============================================================
# EVENTOS DO NÚCLEO -> CONVERSA FORMATADA
# ============================================================

def formatar_evento(tipo, dados):
    """Um evento do núcleo (ver tars.emitir) em [(texto, estilo)] pra caixa
    de conversa. Estilo None = texto normal da resposta."""

    if tipo == "ferramenta":
        detalhe = f"  {dados['resumo']}" if dados.get("resumo") else ""
        return [("▸ ", "ferramenta_icone"), (core.nome_ferramenta(dados["nome"]), "ferramenta"), (detalhe, "ferramenta_detalhe")]
    if tipo == "ferramenta_fim":
        if dados.get("sucesso"):
            return [(f"   ✓ {dados['segundos']:.2f}s\n", "ferramenta_ok")]
        return [(f"   ✗ {t('msg.falhou')}\n", "erro")]
    if tipo == "tarefa":
        if dados.get("modo") == "AUTO":
            return [(f"\n{dados['modelo']} · {t('gui.tarefa', tarefa=core.nome_tarefa(dados['tarefa']))}\n", "meta")]
        return [(f"\n{dados['modelo']} · {t('gui.escolhido_por_voce')}\n", "meta")]
    if tipo == "carregando":
        return [(t("gui.carregando", modelo=dados["modelo"]) + "\n", "suave")]
    if tipo == "carregado":
        return [(t("gui.pronto_em", modelo=dados["modelo"], segundos=f"{dados['segundos']:.1f}") + "\n", "suave")]
    if tipo == "pensamento":
        trechos = [("\n" + t("gui.pensando") + "  ", "pensamento_rotulo")] if dados.get("inicio") else []
        return trechos + [(dados["texto"], "pensamento")]
    if tipo == "resposta":
        trechos = [("\nopenTARS\n", "rotulo_ia")] if dados.get("inicio") else []
        return trechos + [(dados["texto"], None)]
    if tipo == "fim_stream":
        return [("\n", None)]
    if tipo == "tempo":
        return [(t("gui.respondeu_em", segundos=f"{dados['segundos']:.1f}"), "tempo")]
    return []  # descarregando, analisando: bastidor que não interessa aqui


_PADRAO_ANSI = re.compile(r"\033\[(\d+)m")
_COR_PARA_ESTILO = {"90": "suave", "31": "erro", "32": "ok", "33": "aviso", "34": "meta", "35": "destaque", "36": "ferramenta"}
_RE_CAIXA = re.compile(r"^[╭╰]")
_RE_CAIXA_LINHA = re.compile(r"^│\s?(.*?)\s*│?\s*$")


def _segmentos_ansi(texto):
    """[(trecho, {códigos de cor ativos})] a partir do texto com ANSI."""
    ativos, pos, saida = set(), 0, []
    for m in _PADRAO_ANSI.finditer(texto):
        if m.start() > pos:
            saida.append((texto[pos:m.start()], frozenset(ativos)))
        codigo = m.group(1)
        if codigo == "0":
            ativos.clear()
        else:
            ativos.add(codigo)
        pos = m.end()
    if pos < len(texto):
        saida.append((texto[pos:], frozenset(ativos)))
    return saida


def formatar_saida(texto):
    """Texto impresso pelo núcleo (listagens, avisos, confirmações) em
    [(texto, estilo)]: a cor ANSI vira o estilo; as bordas das caixas do
    terminal somem."""

    limpo = _PADRAO_ANSI.sub("", texto)
    so_texto = limpo.strip()
    if _RE_CAIXA.match(so_texto):
        return []
    m = _RE_CAIXA_LINHA.match(so_texto)
    if m and so_texto.startswith("│"):
        return [(f"  {m.group(1).strip()}\n", "aviso")]

    saida = []
    for trecho, codigos in _segmentos_ansi(texto):
        trecho = trecho.replace("[openTARS] ", "").replace("[IA] ", "")
        estilo = None
        for codigo in sorted(codigos):
            estilo = _COR_PARA_ESTILO.get(codigo, estilo)
        saida.append((trecho, estilo))
    return saida


class _EscritorParaFila:
    """Fica no lugar do sys.stdout: tudo que o núcleo imprime vai pra
    uma fila que a thread da janela lê."""

    def __init__(self, fila):
        self.fila = fila
        self._pular_quebra = False

    def write(self, texto):
        # Aviso que o python-xlib imprime quando não acha o ~/.Xauthority:
        # ruído, não conversa (o print manda a quebra de linha à parte).
        if texto.startswith("Xlib.xauth:"):
            self._pular_quebra = True
            return
        if self._pular_quebra and texto == "\n":
            self._pular_quebra = False
            return
        self._pular_quebra = False
        if texto:
            self.fila.put(texto)

    def flush(self):
        pass


class _NaInterface:
    """Algo pra rodar na thread do Tkinter, entregue pela mesma fila.
    Widgets (e até self.after) não podem ser usados de outra thread."""

    def __init__(self, funcao):
        self.funcao = funcao


def configurar_estilos(texto, f):
    """Estilos da caixa de conversa (janela principal e barra rápida)."""
    texto.tag_configure("rotulo_voce", font=f["pequena_negrito"], foreground=DESTAQUE, spacing1=16)
    texto.tag_configure("voce", font=f["texto"], foreground=TEXTO, spacing3=6)
    texto.tag_configure("rotulo_ia", font=f["pequena_negrito"], foreground=ROXO, spacing1=10)
    texto.tag_configure("meta", font=f["pequena"], foreground=TEXTO_3, spacing1=4)
    texto.tag_configure("tempo", font=f["pequena"], foreground=TEXTO_3, spacing1=4)
    texto.tag_configure("ferramenta_icone", font=f["pequena"], foreground=DESTAQUE, lmargin1=6)
    texto.tag_configure("ferramenta", font=f["pequena_negrito"], foreground=TEXTO_2)
    texto.tag_configure("ferramenta_detalhe", font=f["mono"], foreground=TEXTO_3)
    texto.tag_configure("ferramenta_ok", font=f["pequena"], foreground=DESTAQUE)
    texto.tag_configure("pensamento_rotulo", font=f["pequena_negrito"], foreground=TEXTO_3, spacing1=6)
    texto.tag_configure("pensamento", font=f["italico"], foreground=TEXTO_3, lmargin1=0, lmargin2=0)
    texto.tag_configure("suave", font=f["pequena"], foreground=TEXTO_3)
    texto.tag_configure("ok", foreground=DESTAQUE)
    texto.tag_configure("aviso", foreground=AMARELO)
    texto.tag_configure("erro", foreground=VERMELHO)
    texto.tag_configure("destaque", foreground=ROXO)
    texto.tag_configure("negrito", font=f["negrito"])
    texto.tag_configure("codigo", font=f["mono"], background=SUPERFICIE_2, foreground=TEXTO)
    texto.tag_configure("boas_vindas_titulo", font=f["grande"], foreground=TEXTO, justify=tk.CENTER, spacing1=40)
    texto.tag_configure("boas_vindas_sub", font=f["texto"], foreground=TEXTO_2, justify=tk.CENTER, spacing1=6, spacing3=18)
    texto.tag_configure("centro", justify=tk.CENTER)
    texto.tag_configure("cartao_aviso", font=f["pequena"], foreground=AMARELO, justify=tk.CENTER, spacing1=4)
    texto.tag_configure("nota", font=f["pequena"], foreground=TEXTO_3, justify=tk.CENTER, spacing1=14)


_RE_BLOCO_CODIGO = re.compile(r"```([\w+#.-]*)[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)
LINHAS_MAXIMAS_CODIGO = 24


class BlocoDeCodigo(tk.Frame):
    """Janelinha de código dentro da resposta, como no Claude e no
    ChatGPT: nome da linguagem, botão Copiar e o código em fonte fixa."""

    def __init__(self, pai, codigo, linguagem, fontes, ao_rolar=None):
        super().__init__(pai, bg=BORDA, padx=1, pady=1)
        self.codigo = codigo
        topo = tk.Frame(self, bg=SUPERFICIE_2, padx=10, pady=4)
        topo.pack(fill=tk.X)
        tk.Label(topo, text=linguagem or t("gui.codigo"), font=fontes["pequena"], fg=TEXTO_2,
                 bg=SUPERFICIE_2).pack(side=tk.LEFT)
        self.botao = BotaoRedondo(topo, t("gui.copiar"), self.copiar, fontes["pequena"], SUPERFICIE,
                                  BORDA, TEXTO, SUPERFICIE_2, raio=6, padx=10, pady=3)
        self.botao.pack(side=tk.RIGHT)
        linhas = codigo.split("\n")
        self.fonte = tkfont.Font(font=fontes["mono_codigo"])
        self.texto = tk.Text(
            self, bg=FUNDO_CODIGO, fg=TEXTO, font=fontes["mono_codigo"], wrap=tk.NONE, relief=tk.FLAT,
            borderwidth=0, highlightthickness=0, padx=12, pady=8, cursor="xterm",
            height=min(len(linhas), LINHAS_MAXIMAS_CODIGO), selectbackground=BORDA,
        )
        self.texto.insert("1.0", codigo)
        self.texto.configure(state=tk.DISABLED)
        self.texto.pack(fill=tk.BOTH, expand=True)
        self._maior_linha = max((self.fonte.measure(l) for l in linhas), default=0)
        self._barra = None
        if ao_rolar and len(linhas) <= LINHAS_MAXIMAS_CODIGO:
            # Bloco sem rolagem própria: a roda do mouse rola a conversa.
            for evento in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.texto.bind(evento, ao_rolar)

    def ajustar_largura(self, pixels):
        largura = max(20, (pixels - 26) // max(1, self.fonte.measure("0")))
        self.texto.configure(width=largura)
        precisa = self._maior_linha > pixels - 30
        if precisa and self._barra is None:
            self._barra = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.texto.xview,
                                        style="Fino.Horizontal.TScrollbar")
            self.texto.configure(xscrollcommand=self._barra.set)
            self._barra.pack(fill=tk.X)
        elif not precisa and self._barra is not None:
            self._barra.destroy()
            self._barra = None

    def copiar(self):
        self.clipboard_clear()
        self.clipboard_append(self.codigo)
        self.botao.configurar(t("gui.copiado") + " ✓", cor=DESTAQUE, cor_texto=DESTAQUE_TEXTO)
        self.after(1600, lambda: self.botao.configurar(t("gui.copiar"), cor=SUPERFICIE, cor_texto=TEXTO))


class AreaConversa:
    """Um tk.Text que recebe trechos (texto, estilo) já formatados e cuida
    do espaçamento: bastidor compacto, raciocínio num parágrafo só, nunca
    mais de uma linha em branco seguida."""

    def __init__(self, widget, fontes=None):
        self.t = widget
        self.fontes = fontes
        self.ultimo_estilo = None
        self.blocos = []
        widget.bind("<Configure>", lambda e: self._ajustar_blocos(), add="+")

    def _largura_util(self):
        t_ = self.t
        return max(200, t_.winfo_width() - 2 * int(t_.cget("padx")) - 12)

    def _ajustar_blocos(self):
        largura = self._largura_util()
        vivos = []
        for bloco in self.blocos:
            try:
                bloco.ajustar_largura(largura)
                vivos.append(bloco)
            except tk.TclError:
                pass
        self.blocos = vivos

    def _rolar(self, evento):
        passo = -1 if (getattr(evento, "num", 0) == 4 or getattr(evento, "delta", 0) > 0) else 1
        self.t.yview_scroll(passo * 3, "units")
        return "break"

    def _blocos_de_codigo(self, inicio):
        """```linguagem ... ``` vira uma janelinha com botão Copiar."""
        t_ = self.t
        texto = t_.get(inicio, "end-1c")
        for m in reversed(list(_RE_BLOCO_CODIGO.finditer(texto))):
            codigo = m.group(2).rstrip("\n")
            if not codigo.strip():
                continue
            ini, fim = f"{inicio}+{m.start()}c", f"{inicio}+{m.end()}c"
            t_.delete(ini, fim)
            bloco = BlocoDeCodigo(t_, codigo, m.group(1).lower(), self.fontes, ao_rolar=self._rolar)
            bloco.ajustar_largura(self._largura_util())
            t_.window_create(ini, window=bloco, padx=0, pady=6)
            self.blocos.append(bloco)

    def escrever(self, trechos):
        t_ = self.t
        t_.configure(state=tk.NORMAL)
        for trecho, estilo in trechos:
            if not trecho:
                continue
            fim_atual = t_.get("end-2c", "end-1c")
            if not trecho.strip("\n"):
                # Quebra de linha solta depois de uma ferramenta: o "✓ 0.4s"
                # vem na mesma linha. Depois de linha de bastidor: só termina
                # a linha, sem deixar linha em branco.
                if self.ultimo_estilo in ("ferramenta", "ferramenta_detalhe", "ferramenta_icone"):
                    continue
                if self.ultimo_estilo in ESTILOS_COMPACTOS:
                    trecho = "" if fim_atual == "\n" else "\n"
                    if not trecho:
                        continue
            elif estilo in ESTILOS_INICIO_DE_LINHA and (self.ultimo_estilo in ESTILOS_COMPACTOS or self.ultimo_estilo == "pensamento"):
                trecho = ("" if fim_atual == "\n" else "\n") + trecho.lstrip("\n")
            trecho = re.sub(r"\n{3,}", "\n\n", trecho)
            if trecho.startswith("\n"):
                final = t_.get("end-3c", "end-1c")
                ja_tem = len(final) - len(final.rstrip("\n"))
                sobra = max(0, 2 - ja_tem)
                corpo = trecho.lstrip("\n")
                trecho = "\n" * min(sobra, len(trecho) - len(corpo)) + corpo
                if t_.index("end-1c") == "1.0":
                    trecho = corpo
            if not trecho:
                continue
            t_.insert(tk.END, trecho, (estilo,) if estilo else ())
            if estilo == "rotulo_ia":
                t_.mark_set("resposta_inicio", "end-1c")
                t_.mark_gravity("resposta_inicio", tk.LEFT)
            self.ultimo_estilo = estilo
        t_.see(tk.END)
        t_.configure(state=tk.DISABLED)

    def formatar_markdown(self):
        """Depois que a resposta termina: **negrito** e `código` viram
        estilo de verdade (durante o streaming os pedaços chegam
        quebrados, então só dá pra fazer no fim)."""
        t_ = self.t
        try:
            inicio = t_.index("resposta_inicio")
        except tk.TclError:
            return
        t_.configure(state=tk.NORMAL)
        if self.fontes:
            self._blocos_de_codigo(inicio)
        for padrao, estilo, tamanho_marca in ((r"\*\*[^*\n]+?\*\*", "negrito", 2), (r"`[^`\n]+`", "codigo", 1)):
            contagem = tk.IntVar()
            pos = inicio
            while True:
                pos = t_.search(padrao, pos, tk.END, regexp=True, count=contagem)
                if not pos:
                    break
                fim = f"{pos}+{contagem.get()}c"
                conteudo = t_.get(pos, fim)[tamanho_marca:-tamanho_marca]
                tags = [tg for tg in t_.tag_names(pos) if tg != "sel"]
                t_.delete(pos, fim)
                t_.insert(pos, conteudo, tuple(tags) + (estilo,))
                pos = f"{pos}+{len(conteudo)}c"
        t_.mark_unset("resposta_inicio")
        t_.configure(state=tk.DISABLED)
        # Depois que as janelinhas de código ganham tamanho: rola até o fim.
        t_.after(60, lambda: t_.yview_moveto(1.0))

    def limpar(self):
        self.t.configure(state=tk.NORMAL)
        self.t.delete("1.0", tk.END)
        self.t.configure(state=tk.DISABLED)
        self.ultimo_estilo = None
        self.blocos = []


class CaixaDeTexto(tk.Canvas):
    """Campo de digitação com borda arredondada, dica cinza e um botão
    opcional dentro (janela principal e barra rápida)."""

    def __init__(self, pai, fontes, dica, fundo, altura=64, ao_enviar=None):
        super().__init__(pai, bg=fundo, height=altura, highlightthickness=0, bd=0)
        self.dica = dica
        self.entrada = tk.Entry(
            self, bg=SUPERFICIE, fg=TEXTO, insertbackground=DESTAQUE, relief=tk.FLAT,
            font=fontes, highlightthickness=0, bd=0, disabledbackground=SUPERFICIE,
        )
        self.botao = None  # quem usa pode pôr um botão (filho deste canvas)
        self._com_dica = False
        if ao_enviar:
            self.entrada.bind("<Return>", ao_enviar)
        self.entrada.bind("<FocusIn>", self._tirar_dica)
        self.entrada.bind("<FocusOut>", self._por_dica)
        self.bind("<Configure>", self.desenhar)

    def desenhar(self, _evento=None):
        self.delete("fundo")
        w, h = self.winfo_width(), self.winfo_height()
        cor_borda = DESTAQUE if self.focus_get() is self.entrada else BORDA
        _retangulo_arredondado(self, 2, 2, w - 2, h - 2, 16, fill=SUPERFICIE, outline=cor_borda, width=1.5, tags="fundo")
        self.tag_lower("fundo")
        largura_botao = int(self.botao["width"]) if self.botao is not None else 0
        self.delete("entrada", "botao")
        self.create_window(20, h / 2, window=self.entrada, anchor="w",
                           width=max(80, w - largura_botao - (50 if self.botao is not None else 40)), tags="entrada")
        if self.botao is not None:
            self.create_window(w - 12, h / 2, window=self.botao, anchor="e", tags="botao")
        if not self.entrada.get():
            self._por_dica()

    def _tirar_dica(self, _evento=None):
        if self._com_dica:
            self.entrada.delete(0, tk.END)
            self.entrada.configure(fg=TEXTO)
            self._com_dica = False
        self.desenhar()

    def _por_dica(self, _evento=None):
        try:
            focado = self.focus_get()
        except (KeyError, tk.TclError):
            focado = None
        if not self.entrada.get() and focado is not self.entrada:
            self.entrada.insert(0, self.dica)
            self.entrada.configure(fg=TEXTO_3)
            self._com_dica = True
        if _evento is not None:
            self.desenhar()

    def trocar_dica(self, dica):
        tinha = self._com_dica
        if tinha:
            self.entrada.delete(0, tk.END)
            self._com_dica = False
        self.dica = dica
        if tinha:
            self._por_dica()

    def texto(self):
        return "" if self._com_dica else self.entrada.get().strip()

    def definir(self, texto):
        """Põe texto na caixa (exemplo, histórico), já sem a dica cinza."""
        self._com_dica = False
        self.entrada.configure(fg=TEXTO)
        self.entrada.delete(0, tk.END)
        self.entrada.insert(0, texto)
        self.entrada.focus_set()

    def limpar(self):
        self.entrada.delete(0, tk.END)


# ============================================================
# BARRA RÁPIDA
# ============================================================

class BarraRapida(tk.Toplevel):
    """Caixa flutuante do atalho global: pede, vê a resposta, some."""

    LARGURA = 680
    LINHAS_MAXIMAS = 14

    def __init__(self, app):
        super().__init__(app, bg=BORDA, class_="opentars")
        self.app = app
        self.withdraw()
        self.title("openTARS")
        self.resizable(False, False)
        self.configure(padx=1, pady=1)
        f = app.fontes

        corpo = tk.Frame(self, bg=SUPERFICIE, padx=14, pady=12)
        corpo.pack(fill=tk.BOTH, expand=True)

        topo = tk.Frame(corpo, bg=SUPERFICIE)
        topo.pack(fill=tk.X)
        if app.img_logo_pequeno is not None:
            tk.Label(topo, image=app.img_logo_pequeno, bg=SUPERFICIE).pack(side=tk.LEFT, padx=(2, 8))
        self.caixa = CaixaDeTexto(topo, f["grande_texto"], t("rapido.dica"), SUPERFICIE, altura=52, ao_enviar=self._enviar)
        self.caixa.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.caixa.entrada.bind("<Escape>", self._esc)
        self.caixa.entrada.bind("<Control-Return>", lambda e: self._abrir_janela())
        self.bind("<Escape>", self._esc)

        self.area = tk.Text(
            corpo, wrap=tk.WORD, bg=SUPERFICIE, fg=TEXTO, font=f["texto"], state=tk.DISABLED,
            borderwidth=0, highlightthickness=0, padx=10, pady=6, height=1, cursor="arrow",
            selectbackground=BORDA, spacing1=2, spacing3=2,
        )
        configurar_estilos(self.area, f)
        self.conversa = AreaConversa(self.area, f)

        rodape = tk.Frame(corpo, bg=SUPERFICIE)
        rodape.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.status = tk.Label(rodape, text="", font=f["pequena"], fg=TEXTO_3, bg=SUPERFICIE, anchor="w")
        self.status.pack(side=tk.LEFT)
        self.link = tk.Label(rodape, text="", font=f["pequena"], fg=AZUL, bg=SUPERFICIE, cursor="hand2")
        self.link.pack(side=tk.RIGHT)
        self.link.bind("<Button-1>", lambda e: self._abrir_janela())
        self.aplicar_idioma()

    def aplicar_idioma(self):
        self.caixa.trocar_dica(t("rapido.dica"))
        self.status.configure(text=t("rapido.ajuda"))
        self.link.configure(text=t("rapido.abrir_janela") + " ↗")

    def _largura(self):
        return min(self.LARGURA, max(420, self.winfo_screenwidth() - 80))

    def _posicionar(self):
        self.update_idletasks()
        largura = self._largura()
        altura = self.winfo_reqheight()
        x = (self.winfo_screenwidth() - largura) // 2
        y = int(self.winfo_screenheight() * 0.2)
        self.geometry(f"{largura}x{altura}+{x}+{y}")

    def mostrar(self, id_inicio=None):
        if not self.winfo_viewable():
            self.update_idletasks()  # a "wrapper" do X só existe depois disso
            x_sem_bordas(self)
            self._posicionar()
            self.deiconify()
        self.attributes("-topmost", True)
        self.lift()
        self.update_idletasks()
        x_ativar(self, id_inicio)
        self.caixa.entrada.focus_force()
        self.caixa.desenhar()
        self.app.marcar_uso()

    def esconder(self):
        self.withdraw()
        self.app.marcar_uso()

    def _esc(self, _evento=None):
        if self.app.ocupado:
            core.cancelar_resposta()
        else:
            self.esconder()
        return "break"

    def _abrir_janela(self):
        self.esconder()
        self.app.mostrar_janela()
        return "break"

    def _enviar(self, _evento=None):
        texto = self.caixa.texto()
        if not texto or self.app.ocupado:
            return "break"
        self.caixa.limpar()
        self.conversa.limpar()
        self._mostrar_area()
        self.conversa.escrever([(texto + "\n", "voce")])
        # Durante a tarefa a barra sai da frente: o app que a IA abre ou
        # clica fica visível (e um print não pega a barra por cima).
        self.attributes("-topmost", False)
        self.app.enviar(texto, da_barra=True)
        return "break"

    def _mostrar_area(self):
        if not self.area.winfo_ismapped():
            self.area.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.ajustar_altura()

    def ajustar_altura(self):
        """A área de resposta cresce com o texto (até LINHAS_MAXIMAS). As
        linhas quebradas são estimadas pela largura da fonte: contar as
        linhas na tela só funciona depois que a área já apareceu."""
        if not self.area.winfo_manager():
            return
        fonte = tkfont.Font(font=self.area.cget("font"))
        largura = max(100, self._largura() - 2 * 14 - 2 * 10 - 8)
        linhas = sum(
            max(1, -(-fonte.measure(linha) // largura))
            for linha in self.area.get("1.0", "end-1c").split("\n")
        ) + sum(int(b.texto.cget("height")) + 2 for b in self.conversa.blocos)
        self.area.configure(height=max(1, min(self.LINHAS_MAXIMAS, linhas)))
        if self.winfo_viewable():
            self._posicionar()

    def terminou(self):
        """Resposta pronta: a barra volta pra frente com ela."""
        self.conversa.formatar_markdown()
        self.ajustar_altura()
        if self.winfo_viewable():
            self.attributes("-topmost", True)
            self.lift()
            self.caixa.entrada.focus_force()


# ============================================================
# JANELA
# ============================================================

class JanelaTars(tk.Tk):

    def __init__(self, escondida=False):
        super().__init__(className="opentars")
        if escondida:
            self.withdraw()

        self.title("openTARS")
        self.geometry("980x680")
        self.minsize(680, 460)
        self.configure(bg=FUNDO)

        self._fila_saida = queue.Queue()
        self.ocupado = False
        self._historico_entrada = []
        self._pos_historico = 0
        self._animacao = 0
        self._info = {}
        self._online = None
        self._pedido_da_barra = False
        self._ultimo_uso = time.monotonic()
        self._aviso_atalho = None

        self._fontes()
        self._estilos_ttk()
        self._icone()
        self._montar_widgets()
        configurar_estilos(self.texto, self.fontes)
        self.conversa = AreaConversa(self.texto, self.fontes)
        self.barra = None

        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

        # Eventos do núcleo (ferramentas, streaming...) chegam estruturados;
        # o que ele imprime (listagens, avisos) chega pelo stdout.
        self._stdout_original = sys.stdout
        sys.stdout = _EscritorParaFila(self._fila_saida)
        core.definir_ouvinte_saida(lambda tipo, dados: self._fila_saida.put(("evento", tipo, dados)))

        # Comando perigoso pede confirmação numa caixa de diálogo.
        core.confirmar_comando_hook = self._confirmar_comando_perigoso

        self._rodar_em_segundo_plano(self._iniciar, apos=self._popular_modelos)
        self._puxar_fila()
        self._animar_status()
        self.after(60_000, self._checar_ocioso)

    # compatibilidade com código/testes antigos
    @property
    def _ocupado(self):
        return self.ocupado

    # ------------------------------------------------------------
    # Aparência
    # ------------------------------------------------------------

    def _fontes(self):
        sans = _primeira_fonte(("Inter", "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans"), "TkDefaultFont")
        mono = _primeira_fonte(("JetBrains Mono", "Ubuntu Mono", "Noto Sans Mono", "DejaVu Sans Mono"), "TkFixedFont")
        self.fontes = {
            "texto": (sans, 11), "negrito": (sans, 11, "bold"), "pequena": (sans, 9),
            "pequena_negrito": (sans, 9, "bold"), "titulo": (sans, 14, "bold"), "grande": (sans, 20, "bold"),
            "grande_texto": (sans, 13), "italico": (sans, 10, "italic"), "mono": (mono, 9),
            "mono_codigo": (mono, 10),
        }
        f = self.fontes
        self.f_texto, self.f_negrito, self.f_pequena = f["texto"], f["negrito"], f["pequena"]
        self.f_pequena_negrito, self.f_titulo = f["pequena_negrito"], f["titulo"]

    def _estilos_ttk(self):
        estilo = ttk.Style(self)
        try:
            estilo.theme_use("clam")
        except tk.TclError:
            pass
        estilo.configure(
            "IA.TCombobox", fieldbackground=SUPERFICIE_2, background=SUPERFICIE_2,
            foreground=TEXTO, arrowcolor=TEXTO_2, bordercolor=BORDA,
            lightcolor=SUPERFICIE_2, darkcolor=SUPERFICIE_2, padding=(8, 4),
            selectbackground=SUPERFICIE_2, selectforeground=TEXTO,
        )
        estilo.map(
            "IA.TCombobox",
            fieldbackground=[("readonly", SUPERFICIE_2)],
            foreground=[("readonly", TEXTO)],
            bordercolor=[("focus", DESTAQUE)],
        )
        self.option_add("*TCombobox*Listbox.background", SUPERFICIE)
        self.option_add("*TCombobox*Listbox.foreground", TEXTO)
        self.option_add("*TCombobox*Listbox.selectBackground", DESTAQUE)
        self.option_add("*TCombobox*Listbox.selectForeground", DESTAQUE_TEXTO)
        self.option_add("*TCombobox*Listbox.font", self.fontes["texto"])

        estilo.layout("Fino.Vertical.TScrollbar", [(
            "Vertical.Scrollbar.trough",
            {"children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"},
        )])
        estilo.configure("Fino.Vertical.TScrollbar", troughcolor=FUNDO, background=BORDA,
                         bordercolor=FUNDO, lightcolor=BORDA, darkcolor=BORDA, gripcount=0, width=8)
        estilo.map("Fino.Vertical.TScrollbar", background=[("active", TEXTO_3)])
        estilo.layout("Fino.Horizontal.TScrollbar", [(
            "Horizontal.Scrollbar.trough",
            {"children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "we"},
        )])
        estilo.configure("Fino.Horizontal.TScrollbar", troughcolor=FUNDO_CODIGO, background=BORDA,
                         bordercolor=FUNDO_CODIGO, lightcolor=BORDA, darkcolor=BORDA, gripcount=0, width=8)

    def _icone(self):
        self.img_logo_pequeno = None
        self._img_logo = None
        try:
            from PIL import ImageTk
            self._img_icone = ImageTk.PhotoImage(desenhar_logo(128))
            self._img_logo = ImageTk.PhotoImage(desenhar_logo(30))
            self.img_logo_pequeno = ImageTk.PhotoImage(desenhar_logo(26))
            self.iconphoto(True, self._img_icone)
        except Exception:
            pass

    # ------------------------------------------------------------
    # Montagem
    # ------------------------------------------------------------

    def _montar_widgets(self):
        f = self.fontes

        # --- cabeçalho ---
        topo = tk.Frame(self, bg=SUPERFICIE, height=58)
        topo.pack(fill=tk.X)
        topo.pack_propagate(False)
        tk.Frame(self, bg=BORDA, height=1).pack(fill=tk.X)

        marca = tk.Frame(topo, bg=SUPERFICIE)
        marca.pack(side=tk.LEFT, padx=(18, 0))
        if self._img_logo:
            tk.Label(marca, image=self._img_logo, bg=SUPERFICIE).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(marca, text="openTARS", font=f["titulo"], fg=TEXTO, bg=SUPERFICIE).pack(side=tk.LEFT)
        partes = core.VERSAO.split(".")
        rotulo_versao = ".".join(partes[:2]) if partes[2:] == ["0"] else core.VERSAO
        tk.Label(marca, text=f" {rotulo_versao} ", font=f["pequena_negrito"], fg=DESTAQUE_TEXTO,
                 bg=DESTAQUE).pack(side=tk.LEFT, padx=(8, 0), pady=2)

        direita = tk.Frame(topo, bg=SUPERFICIE)
        direita.pack(side=tk.RIGHT, padx=18)

        self.botao_limpar = BotaoRedondo(
            direita, t("gui.nova_conversa"), self._limpar_conversa, f["pequena"],
            SUPERFICIE_2, BORDA, TEXTO, SUPERFICIE, raio=8, padx=12, pady=6,
        )
        self.botao_limpar.pack(side=tk.RIGHT, padx=(10, 0))

        self.botao_idioma = BotaoRedondo(
            direita, self._rotulo_idioma(), self._menu_idioma, f["pequena_negrito"],
            SUPERFICIE_2, BORDA, TEXTO_2, SUPERFICIE, raio=8, padx=10, pady=6,
        )
        self.botao_idioma.pack(side=tk.RIGHT, padx=(10, 0))

        self.var_ia = tk.StringVar(value=automatico())
        self.combo_ia = ttk.Combobox(direita, textvariable=self.var_ia, values=[automatico()],
                                     state="readonly", width=22, style="IA.TCombobox", font=f["texto"])
        self.combo_ia.pack(side=tk.RIGHT)
        self.combo_ia.bind("<<ComboboxSelected>>", self._ao_escolher_ia)
        self.rotulo_ia = tk.Label(direita, text=t("gui.ia"), font=f["pequena_negrito"], fg=TEXTO_2, bg=SUPERFICIE)
        self.rotulo_ia.pack(side=tk.RIGHT, padx=(0, 8))

        self.status_ollama = tk.Label(direita, text="●  " + t("gui.conectando"), font=f["pequena"], fg=TEXTO_2, bg=SUPERFICIE)
        self.status_ollama.pack(side=tk.RIGHT, padx=(0, 18))

        # --- rodapé (status) ---
        rodape = tk.Frame(self, bg=FUNDO)
        rodape.pack(side=tk.BOTTOM, fill=tk.X, padx=22, pady=(0, 10))
        self.status_esq = tk.Label(rodape, text="", font=f["pequena"], fg=TEXTO_3, bg=FUNDO, anchor="w")
        self.status_esq.pack(side=tk.LEFT)
        self.dica_teclas = tk.Label(rodape, text=t("gui.teclas"), font=f["pequena"], fg=TEXTO_3, bg=FUNDO)
        self.dica_teclas.pack(side=tk.RIGHT)

        # --- caixa de digitação ---
        self.caixa = CaixaDeTexto(self, f["texto"], t("gui.dica_entrada"), FUNDO, ao_enviar=self._ao_enviar)
        self.botao_enviar = self.caixa.botao = BotaoRedondo(
            self.caixa, t("gui.enviar"), self._ao_clicar_botao, f["negrito"],
            DESTAQUE, DESTAQUE_HOVER, DESTAQUE_TEXTO, SUPERFICIE, raio=10, padx=18, pady=8, largura=104,
        )
        self.caixa.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=(4, 6))
        self.entrada = self.caixa.entrada
        self.entrada.bind("<Up>", lambda e: self._navegar_historico(-1))
        self.entrada.bind("<Down>", lambda e: self._navegar_historico(1))
        self.bind("<Escape>", lambda e: self._parar())

        # --- conversa ---
        meio = tk.Frame(self, bg=FUNDO)
        meio.pack(fill=tk.BOTH, expand=True)
        barra = ttk.Scrollbar(meio, orient=tk.VERTICAL, style="Fino.Vertical.TScrollbar")
        barra.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4), pady=6)
        self.texto = tk.Text(
            meio, wrap=tk.WORD, bg=FUNDO, fg=TEXTO, insertbackground=TEXTO,
            font=f["texto"], state=tk.DISABLED, borderwidth=0, highlightthickness=0,
            padx=34, pady=18, spacing1=2, spacing3=2, yscrollcommand=barra.set,
            cursor="arrow", selectbackground=BORDA,
        )
        self.texto.pack(fill=tk.BOTH, expand=True)
        barra.configure(command=self.texto.yview)

        self.entrada.focus_set()

    # ------------------------------------------------------------
    # Idioma
    # ------------------------------------------------------------

    @staticmethod
    def _rotulo_idioma():
        return f"{t('_rotulo')} ▾"

    def _menu_idioma(self):
        menu = tk.Menu(self, tearoff=0, bg=SUPERFICIE, fg=TEXTO, activebackground=DESTAQUE,
                       activeforeground=DESTAQUE_TEXTO, selectcolor=DESTAQUE, bd=0, relief=tk.FLAT,
                       font=self.fontes["texto"])
        self._var_idioma = tk.StringVar(value=i18n.idioma_atual())
        for codigo, nome in i18n.idiomas_disponiveis().items():
            menu.add_radiobutton(label=nome, value=codigo, variable=self._var_idioma,
                                 command=lambda c=codigo: self.trocar_idioma(c))
        x = self.botao_idioma.winfo_rootx()
        y = self.botao_idioma.winfo_rooty() + self.botao_idioma.winfo_height() + 4
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def trocar_idioma(self, codigo):
        if i18n.definir_idioma(codigo):
            self._aplicar_idioma()

    def _aplicar_idioma(self):
        """Troca todos os textos da janela (e da barra) pro idioma novo."""
        self.botao_limpar.configurar(t("gui.nova_conversa"))
        self.botao_idioma.configurar(self._rotulo_idioma())
        self.rotulo_ia.configure(text=t("gui.ia"))
        self.dica_teclas.configure(text=t("gui.teclas"))
        self.caixa.trocar_dica(t("gui.dica_entrada"))
        self._botao_modo_parar(self.ocupado)
        self._atualizar_status(self._info)
        self._popular_modelos()
        if self._na_tela_inicial():
            self._mostrar_boas_vindas(self._info)
        if self.barra is not None:
            self.barra.aplicar_idioma()

    # ------------------------------------------------------------
    # Texto da conversa
    # ------------------------------------------------------------

    def _escrever(self, trechos):
        self.conversa.escrever(trechos)
        if self._pedido_da_barra and self.barra is not None:
            self.barra.conversa.escrever([tr for tr in trechos if tr[1] != "pensamento"])
            self.barra.ajustar_altura()

    def _puxar_fila(self):
        try:
            while True:
                item = self._fila_saida.get_nowait()
                if isinstance(item, _NaInterface):
                    try:
                        item.funcao()
                    except Exception:
                        pass
                elif isinstance(item, tuple):
                    self._escrever(formatar_evento(item[1], item[2]))
                else:
                    self._escrever(formatar_saida(item))
        except queue.Empty:
            pass
        finally:
            self.after(40, self._puxar_fila)

    def _depois_da_resposta(self):
        self.conversa.formatar_markdown()
        if self._pedido_da_barra and self.barra is not None:
            self.barra.terminou()
        self._pedido_da_barra = False
        self._popular_modelos()
        self.marcar_uso()

    def _na_tela_inicial(self):
        return getattr(self, "_boas_vindas", False)

    def _mostrar_boas_vindas(self, info):
        tx = self.texto
        tx.configure(state=tk.NORMAL)
        tx.delete("1.0", tk.END)
        self.conversa.ultimo_estilo = None
        tx.insert(tk.END, t("gui.boas_vindas") + "\n", "boas_vindas_titulo")
        tx.insert(tk.END, t("gui.boas_vindas_sub") + "\n", "boas_vindas_sub")

        chips = tk.Frame(tx, bg=FUNDO)
        for i, exemplo in enumerate(exemplos()):
            BotaoRedondo(
                chips, exemplo, lambda e=exemplo: self._usar_exemplo(e), self.fontes["pequena"],
                SUPERFICIE, SUPERFICIE_2, TEXTO_2, FUNDO, raio=14, padx=14, pady=7,
            ).grid(row=i // 2, column=i % 2, padx=5, pady=5, sticky="ew")
        tx.insert(tk.END, "", "centro")
        tx.window_create(tk.END, window=chips)
        tx.insert(tk.END, "\n", "centro")
        tx.tag_add("centro", "end-2l", "end-1c")

        for aviso in info.get("avisos", []):
            tx.insert(tk.END, f"\n⚠  {aviso}\n", "cartao_aviso")
        if info.get("pedidos"):
            tx.insert(tk.END, "\n" + t("gui.conversa_carregada", n=info["pedidos"]) + "\n", "nota")
        if info.get("atalho"):
            tx.insert(tk.END, "\n" + t("gui.atalho_dica", atalho=info["atalho"]) + "\n", "nota")
        tx.configure(state=tk.DISABLED)
        self._boas_vindas = True

    def _sair_da_tela_inicial(self):
        if getattr(self, "_boas_vindas", False):
            self.conversa.limpar()
            self._boas_vindas = False

    def _usar_exemplo(self, exemplo):
        if self.ocupado:
            return
        self.caixa.definir(exemplo)
        self._ao_enviar()

    # compatibilidade
    def _definir_texto(self, texto):
        self.caixa.definir(texto)

    # ------------------------------------------------------------
    # Status
    # ------------------------------------------------------------

    def _atualizar_status(self, info):
        if "online" in info:
            if info["online"]:
                self.status_ollama.configure(text="●  " + t("gui.ollama_ok"), fg=DESTAQUE)
            else:
                self.status_ollama.configure(text="●  " + t("gui.ollama_offline"), fg=VERMELHO)
        partes = [t("gui.n_modelos", n=info.get("n_modelos", 0))]
        if info.get("ajudante"):
            partes.append(t("gui.ajudante", modelo=info["ajudante"]))
        gpu = info.get("gpu")
        partes.append(f"{gpu.get('fabricante', 'GPU')} {gpu['vram_total_gb']:.0f} GB" if gpu else t("gui.sem_gpu"))
        self._resumo_status = "   ·   ".join(partes)

    def _animar_status(self):
        if self.ocupado:
            self._animacao = (self._animacao + 1) % 4
            texto = t("gui.trabalhando") + "." * self._animacao
            self.status_esq.configure(text=texto, fg=DESTAQUE)
            if self.barra is not None and self._pedido_da_barra:
                self.barra.status.configure(text=texto, fg=DESTAQUE)
        else:
            self.status_esq.configure(text=getattr(self, "_resumo_status", ""), fg=TEXTO_3)
            if self.barra is not None:
                self.barra.status.configure(text=t("rapido.ajuda"), fg=TEXTO_3)
        self.after(400, self._animar_status)

    # ------------------------------------------------------------
    # Trabalho em segundo plano
    # ------------------------------------------------------------

    def _iniciar(self):
        """Carrega a conversa salva e coleta o que a tela de boas-vindas
        e a barra de status mostram (fora da thread da janela: consulta
        o Ollama e cadastra o atalho global na primeira vez)."""
        info = {"pedidos": core.iniciar_conversa()}
        try:
            info["online"] = core.ollama_online()
            catalogo = core.catalogar_modelos()
            ajudantes = {core.MODELO_AJUDANTE, core.MODELO_AJUDANTE_ANTIGO}
            info["n_modelos"] = sum(
                1 for m in catalogo.values()
                if m["categoria"] != "embedding" and m["tag"] not in ajudantes
            )
            info["ajudante"] = core.MODELO_AJUDANTE if core.MODELO_AJUDANTE in catalogo else None
            info["gpu"] = core.detectar_gpu()
            info["avisos"] = core.avisos_de_ambiente()
            if catalogo and not info["ajudante"]:
                info["avisos"].append(t("aviso.sem_ajudante", modelo=core.MODELO_AJUDANTE))
            if catalogo and info["n_modelos"] == 0:
                info["avisos"].append(t("aviso.sem_modelo_conversa"))
            core.aquecer_ajudante()
        except Exception as e:
            info.setdefault("avisos", []).append(t("gui.erro_modelos", erro=e))

        try:
            import tars_atalho
            tars_atalho.garantir_registrado()
            estado = tars_atalho.estado()
            if estado.get("registrado"):
                info["atalho"] = estado["legivel"]
        except Exception:
            pass

        def aplicar():
            self._info = info
            self._atualizar_status(info)
            if not self.conversa.t.get("1.0", "end-1c").strip() or getattr(self, "_boas_vindas", False):
                self._mostrar_boas_vindas(info)

        self._fila_saida.put(_NaInterface(aplicar))

    def _rodar_em_segundo_plano(self, funcao, *args, apos=None):
        """Roda uma função do núcleo numa thread separada (a janela não
        trava) e troca o botão pra "Parar" enquanto isso."""

        self.ocupado = True
        self._botao_modo_parar(True)

        def alvo():
            try:
                funcao(*args)
            except Exception as e:
                self._fila_saida.put(f"\n[openTARS] {t('msg.erro_inesperado', erro=e)}\n")
            finally:
                self.ocupado = False
                self._fila_saida.put(_NaInterface(lambda: self._botao_modo_parar(False)))
                if apos:
                    self._fila_saida.put(_NaInterface(apos))

        self._trabalho = threading.Thread(target=alvo, daemon=True)
        self._trabalho.start()

    # ------------------------------------------------------------
    # Escolha da IA (um seletor só: Automático ou um modelo)
    # ------------------------------------------------------------

    def _modelos_escolhiveis(self):
        try:
            instalados = set(core.listar_modelos_instalados())
        except Exception:
            instalados = set()
        catalogo = core._cache_catalogo.get("dados") or {}
        fora = {core.MODELO_AJUDANTE, core.MODELO_AJUDANTE_ANTIGO}
        return sorted(
            m for m in instalados
            if m not in fora and (catalogo.get(m) or {}).get("categoria") != "embedding"
        )

    def _popular_modelos(self):
        """Sincroniza o seletor com o núcleo (um "/modelo x" digitado
        também aparece aqui)."""
        modelos = self._modelos_escolhiveis()
        if core.modo_modelo == "MANUAL" and core.modelo_manual and core.modelo_manual not in modelos:
            modelos.append(core.modelo_manual)
        self.combo_ia["values"] = [automatico()] + modelos
        if core.modo_modelo == "MANUAL" and core.modelo_manual:
            self.var_ia.set(core.modelo_manual)
        else:
            self.var_ia.set(automatico())

    def _ao_escolher_ia(self, _evento=None):
        escolha = self.var_ia.get()
        if escolha == automatico():
            core.modo_modelo = "AUTO"
            core.modelo_manual = None
            self._escrever([("\n" + t("gui.modo_auto") + "\n", "meta")])
            return
        if escolha == core.modelo_manual and core.modo_modelo == "MANUAL":
            return
        core.modo_modelo = "MANUAL"
        core.modelo_manual = escolha
        self._escrever([("\n" + t("gui.modelo_fixado", modelo=escolha) + "\n", "meta")])
        try:
            core._carregar_em_segundo_plano(escolha)
        except Exception:
            pass

    # ------------------------------------------------------------
    # Ações
    # ------------------------------------------------------------

    def _botao_modo_parar(self, parar):
        if parar:
            self.botao_enviar.configurar(t("gui.parar"), VERMELHO, VERMELHO_HOVER, TEXTO, ativo=True)
        else:
            self.botao_enviar.configurar(t("gui.enviar"), DESTAQUE, DESTAQUE_HOVER, DESTAQUE_TEXTO, ativo=True)

    def _ao_clicar_botao(self):
        if self.ocupado:
            self._parar()
        else:
            self._ao_enviar()

    def _parar(self):
        """Interrompe a resposta ou a tarefa em andamento."""
        if self.ocupado:
            core.cancelar_resposta()
            self.botao_enviar.configurar(t("gui.parando"), ativo=False)

    def _navegar_historico(self, passo):
        if not self._historico_entrada:
            return "break"
        self._pos_historico = max(0, min(len(self._historico_entrada), self._pos_historico + passo))
        anterior = self._historico_entrada[self._pos_historico] if self._pos_historico < len(self._historico_entrada) else ""
        self.caixa.definir(anterior)
        return "break"

    def _limpar_conversa(self):
        if self.ocupado:
            return
        core.limpar_sessao()
        self._mostrar_boas_vindas({k: v for k, v in self._info.items() if k in ("atalho",)})

    def _ao_enviar(self, _evento=None):
        if self.ocupado:
            return
        texto = self.caixa.texto()
        if not texto:
            return
        self.caixa.limpar()
        self.enviar(texto)

    def enviar(self, texto, da_barra=False):
        """Manda um pedido pro núcleo (da caixa principal ou da barra)."""
        if self.ocupado:
            return
        if not self._historico_entrada or self._historico_entrada[-1] != texto:
            self._historico_entrada.append(texto)
        self._pos_historico = len(self._historico_entrada)
        self._sair_da_tela_inicial()
        self._pedido_da_barra = da_barra
        self.conversa.escrever([("\n" + t("gui.voce") + "\n", "rotulo_voce"), (texto + "\n", "voce")])
        self.marcar_uso()
        self._rodar_em_segundo_plano(core.processar_mensagem, texto, apos=self._depois_da_resposta)

    def _confirmar_comando_perigoso(self, comando):
        """Chamado da thread de trabalho: pergunta na thread da janela e
        espera a resposta."""

        resposta = {"ok": False}
        pronto = threading.Event()

        def perguntar():
            try:
                pai = self.barra if (self._pedido_da_barra and self.barra is not None) else self
                resposta["ok"] = messagebox.askyesno(
                    t("perigo.titulo_janela"),
                    t("perigo.dialogo", comando=comando[:400]),
                    icon=messagebox.WARNING,
                    default=messagebox.NO,
                    parent=pai,
                )
            finally:
                pronto.set()

        self._fila_saida.put(_NaInterface(perguntar))
        pronto.wait()
        return resposta["ok"]

    # ------------------------------------------------------------
    # Janela, barra rápida e outras aberturas
    # ------------------------------------------------------------

    def marcar_uso(self):
        self._ultimo_uso = time.monotonic()

    def pedido_externo(self, comando, id_inicio=None):
        """Recado de outra abertura (tars_instancia), vindo de outra thread."""
        acao = self.mostrar_barra if comando == "rapido" else self.mostrar_janela
        self._fila_saida.put(_NaInterface(lambda: acao(id_inicio)))

    def mostrar_janela(self, id_inicio=None):
        self.deiconify()
        self.lift()
        self.update_idletasks()
        x_ativar(self, id_inicio)
        x_fim_do_carregamento(id_inicio)
        self.entrada.focus_force()
        self.marcar_uso()

    def mostrar_barra(self, id_inicio=None):
        if self.barra is None:
            self.barra = BarraRapida(self)
        self.barra.mostrar(id_inicio)
        x_fim_do_carregamento(id_inicio)

    def _checar_ocioso(self):
        """Aberto só pela barra rápida, sem uso há muito tempo: encerra."""
        janela_visivel = self.winfo_viewable()
        barra_visivel = self.barra is not None and self.barra.winfo_viewable()
        parado = time.monotonic() - self._ultimo_uso > MINUTOS_OCIOSO * 60
        if not janela_visivel and not barra_visivel and not self.ocupado and parado:
            self._ao_fechar()
            return
        self.after(60_000, self._checar_ocioso)

    def _ao_fechar(self):
        core.cancelar_resposta()
        trabalho = getattr(self, "_trabalho", None)
        if trabalho is not None and trabalho.is_alive():
            trabalho.join(timeout=3)
        try:
            core.salvar_sessoes()
            core.encerrar_todas_ias()
        except Exception:
            pass
        core.definir_ouvinte_saida(None)
        sys.stdout = self._stdout_original
        self.destroy()


def main():
    argumentos = sys.argv[1:]
    comando = "rapido" if any(a in ("--rapido", "--quick") for a in argumentos) else "mostrar"
    id_inicio = os.environ.pop("DESKTOP_STARTUP_ID", None)

    # Já aberto? Só pede pra ele mostrar a janela (ou a barra) e sai.
    servidor = tars_instancia.Servidor()
    if not servidor.travar():
        if tars_instancia.enviar(comando, id_inicio):
            return
        # O dono do cadeado não respondeu (travado?): abre assim mesmo.

    app = JanelaTars(escondida=(comando == "rapido"))
    servidor.escutar(app.pedido_externo)
    if comando == "rapido":
        app.after(80, lambda: app.mostrar_barra(id_inicio))
    else:
        app.after(80, lambda: x_fim_do_carregamento(id_inicio))
    try:
        app.mainloop()
    finally:
        servidor.fechar()


if __name__ == "__main__":
    main()
