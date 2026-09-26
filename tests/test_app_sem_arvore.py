"""2.6.1: app aberto que não expõe os botões (Electron: Claude, Discord...).

Log real: "open claude and type..." -> a janela do Claude não tem árvore de
acessibilidade; a IA tentou click_element com 14 nomes diferentes (tirados
da descrição do print), todos falhando, até o limite de etapas.

O xmessage (X puro, sem acessibilidade) faz o papel do app Electron."""

import os
import shutil
import subprocess
import sys
import time

from _util import carregar_tars

tars = carregar_tars()

# ------------------------------------------------------------
# 1. Parte com a janela de verdade (precisa de X + D-Bus + xmessage)
# ------------------------------------------------------------
tem_x = (os.environ.get("DISPLAY") and os.environ.get("DBUS_SESSION_BUS_ADDRESS")
         and shutil.which("xmessage") and not tars.acess.indisponivel())
if tem_x:
    proc = subprocess.Popen(["xmessage", "-name", "clauded", "-title", "Claude", "Olá, eu sou um app Electron"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        limite = time.time() + 10
        while time.time() < limite and not tars.encontrar_janela_por_nome("Claude"):
            time.sleep(0.2)
        abriu = []
        tars.abrir_aplicativo = lambda app: abriu.append(app) or {"sucesso": True}
        r = tars.executar_ferramenta("click_element", {"name": "New chat", "window": "Claude"})
        assert not r["sucesso"] and "does not expose" in r["mensagem"] and "type_in_element" in r["mensagem"], r
        assert not abriu, "a janela já estava aberta: não abre o app de novo"
        r2 = tars.executar_ferramenta("list_elements", {"window": "Claude"})
        if tars.ocr.disponivel():
            # 2.9: sem árvore, a janela é lida pela tela (OCR)
            assert r2["sucesso"] and "READ the window" in r2["mensagem"] and r2["textos"], r2
        else:
            assert not r2["sucesso"] and "Do NOT use click_element" in r2["mensagem"]
        print("OK: janela aberta sem botões expostos -> diz na hora pra usar type_in_element/click_mouse (e não reabre o app)")

        # type_in_element numa janela dessas: foca a janela, digita no campo com foco e aperta Enter
        digitado, teclas = [], []
        tars.digitar_texto = lambda txt: digitado.append(txt) or {"sucesso": True}
        tars.pressionar_tecla = lambda k: teclas.append(k) or {"sucesso": True}
        r3 = tars.executar_ferramenta("type_in_element", {"text": "Qual a capital da França?", "window": "Claude",
                                                          "submit": "true"})
        assert r3["sucesso"] and digitado == ["Qual a capital da França?"] and teclas == ["enter"], (r3, digitado, teclas)
        ativa = tars.janela_ativa_x()
        assert ativa == tars.encontrar_janela_por_nome("Claude")["id"], "a janela do app veio pra frente antes de digitar"
        print("OK: type_in_element(window, submit=true) num app sem botões: foca a janela, digita e envia")
    finally:
        proc.kill()
else:
    print("PULADO (parte 1): precisa de DISPLAY, D-Bus, xmessage e acessibilidade")

# ------------------------------------------------------------
# 2. O loop do log: a IA insiste no click_element com nomes diferentes
# ------------------------------------------------------------
tars.carregar_modelo = lambda m, silencioso=False: True
tars.contexto_ia = lambda: 8192
tars.modo_modelo = "MANUAL"
tars.catalogar_modelos = lambda forcar=False: {}
nomes = iter(f"Botão {i}" for i in range(100))
turnos = []


def turno(modelo, msgs, tools, **kw):
    turnos.append(bool(tools))
    if not tools:
        return {"content": "Não consegui clicar: o app não mostra os botões. Clique você no campo.", "tool_calls": []}, None
    return {"content": "", "tool_calls": [{"function": {"name": "click_element",
                                                         "arguments": {"name": next(nomes), "window": "Claude"}}}]}, None


tars._executar_turno_streaming = turno
tars._DISPATCH_FERRAMENTAS["click_element"] = lambda a: {"sucesso": False, "mensagem": f"No element named '{a['name']}'."}
tars.conversa = []
resposta = tars.perguntar_modelo("qwen3:8b", "open claude and type hello", tarefa="acao")
assert "Não consegui" in resposta, resposta
assert turnos.count(True) == tars.LIMITE_FALHAS_SEGUIDAS and turnos[-1] is False, turnos
resultados = [m["content"] for m in tars.obter_sessao("qwen3:8b") if m.get("role") == "tool"]
assert "STOP" not in resultados[1] and "STOP: click_element failed 3 times" in resultados[2], resultados[:3]
print(f"OK: 3 falhas seguidas -> 'mude de estratégia'; {tars.LIMITE_FALHAS_SEGUIDAS} -> para e explica ao usuário "
      f"(antes: 20 etapas)")

# 3. A mesma chamada que já falhou nem roda de novo
rodou = []
tars._DISPATCH_FERRAMENTAS["click_element"] = lambda a: rodou.append(a) or {"sucesso": False, "mensagem": "nope."}
chamadas = iter([("click_element", {"name": "Send", "window": "Claude"})] * 2 + [None])


def turno2(modelo, msgs, tools, **kw):
    proxima = next(chamadas)
    if proxima is None or not tools:
        return {"content": "ok", "tool_calls": []}, None
    return {"content": "", "tool_calls": [{"function": {"name": proxima[0], "arguments": proxima[1]}}]}, None


tars._executar_turno_streaming = turno2
tars.perguntar_modelo("qwen3:8b", "type hello in claude", tarefa="acao")
ultimo = [m["content"] for m in tars.obter_sessao("qwen3:8b") if m.get("role") == "tool"][-1]
assert len(rodou) == 1 and "already tried exactly this" in ultimo, (rodou, ultimo)
print("OK: repetir exatamente a mesma chamada que falhou não roda de novo (a IA é avisada)")

# 4. Descrição da tela de um app sem botões expostos: manda clicar por coordenada
tars._descrever_print = lambda img, pedido, modelo: ("Message box at (400, 700). Send button at (760, 700).", "gemma4:e4b")
tars._marcar_sem_arvore("Claude")
msg = tars._print_em_texto("x", "type hello", "qwen3:8b", "Claude")
assert "click_mouse(x, y)" in msg and "click_element(name)" not in msg, msg
msg_outro = tars._print_em_texto("x", "type hello", "qwen3:8b", "calculator")
assert "click_element(name)" in msg_outro
print("OK: com um app sem botões expostos, a descrição da tela manda usar click_mouse(x, y) e type_text")

# 5. Abrir app pede a árvore de acessibilidade ao Chromium/Electron
envs = []


class Popen:
    def __init__(self, *a, **kw):
        envs.append(kw.get("env") or {})
        self.returncode = 0

    def poll(self):
        return 0


fonte = carregar_tars()  # abrir_aplicativo original (a parte 1 trocou o do outro)
fonte.subprocess.Popen = Popen
fonte.encontrar_app = lambda app: {"comando": ["claude-desktop"], "nome": "Claude", "tipo": "desktop"}
fonte.listar_janelas_x = lambda: None
fonte._processo_ativo = lambda termos: True
fonte.abrir_aplicativo("claude")
assert envs and envs[-1].get("ACCESSIBILITY_ENABLED") == "1", envs
print("OK: apps são abertos com ACCESSIBILITY_ENABLED=1 (Electron/Chromium passam a expor os botões)")

# 6. Log real da 2.6.1: uma falha e a IA manda o usuário fazer na mão
chamadas_ia = iter([
    ("tool", "click_element", {"name": "Ask a question", "window": "Claude"}),
    ("texto", "I cannot directly interact with the Claude app's buttons. To ask a question, focus on the text box and type your question, then press Enter."),
    ("tool", "type_in_element", {"text": "What is the capital of France?", "window": "Claude", "submit": True}),
    ("texto", "Done: I asked Claude 'What is the capital of France?'."),
])
recebeu = []


def turno3(modelo, msgs, tools, **kw):
    recebeu.append(msgs[-1]["content"])
    tipo, *resto = next(chamadas_ia)
    if tipo == "texto":
        return {"content": resto[0], "tool_calls": []}, None
    return {"content": "", "tool_calls": [{"function": {"name": resto[0], "arguments": resto[1]}}]}, None


tars._executar_turno_streaming = turno3
tars._DISPATCH_FERRAMENTAS["click_element"] = lambda a: {"sucesso": False, "mensagem": tars._mensagem_sem_arvore("Claude")}
escrito = []
tars._DISPATCH_FERRAMENTAS["type_in_element"] = lambda a: escrito.append(a) or {"sucesso": True, "mensagem": "SUCCESS: typed and sent."}
resposta = tars.perguntar_modelo("qwen3:8b", "abra o claude e faça uma pergunta simples", tarefa="acao")
assert resposta.startswith("Done") and escrito and escrito[0]["submit"] is True, (resposta, escrito)
assert any("Don't hand the task back" in r for r in recebeu)
assert not any("I cannot directly interact" in (m.get("content") or "") for m in tars.obter_sessao("qwen3:8b"))
print("OK: 'foque a caixa e digite você' depois de uma falha -> a IA é mandada fazer ela mesma, e faz")

# 7. Electron/Chromium detectado -> abre com --force-renderer-accessibility
import tempfile  # noqa: E402

pasta = tempfile.mkdtemp(prefix="opentars-electron-")
for arquivo, conteudo in (("claude-desktop", b"\x7fELF..."), ("resources.pak", b"")):
    with open(os.path.join(pasta, arquivo), "wb") as f:
        f.write(conteudo)
script = os.path.join(pasta, "discord")
with open(script, "wb") as f:
    f.write(b"#!/bin/sh\nexec /usr/lib/electron/electron /usr/lib/discord/app.asar \"$@\"\n")
comum = os.path.join(tempfile.mkdtemp(), "gnome-calculator")
with open(comum, "wb") as f:
    f.write(b"\x7fELF-normal")
assert fonte._eh_electron([os.path.join(pasta, "claude-desktop")]), "resources.pak ao lado: Electron"
assert fonte._eh_electron([script]), "script lançador que chama o electron"
assert not fonte._eh_electron([comum]) and not fonte._eh_electron(["flatpak", "run", "x"])
envs.clear()
chamados = []


class PopenComando(Popen):
    def __init__(self, comando, *a, **kw):
        chamados.append(list(comando))
        super().__init__(comando, *a, **kw)


fonte.subprocess.Popen = PopenComando
fonte.encontrar_app = lambda app: {"comando": [os.path.join(pasta, "claude-desktop")], "nome": "Claude", "tipo": "desktop"}
fonte.abrir_aplicativo("claude")
fonte.encontrar_app = lambda app: {"comando": [comum], "nome": "Calculadora", "tipo": "desktop"}
fonte.abrir_aplicativo("calculadora")
assert chamados[0][-1] == "--force-renderer-accessibility" and chamados[1] == [comum], chamados
print("OK: app Electron é aberto com --force-renderer-accessibility (os outros, do jeito normal)")

print("\nTUDO OK.")
