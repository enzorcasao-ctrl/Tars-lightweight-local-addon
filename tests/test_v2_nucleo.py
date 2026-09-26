"""Mudanças da 2.0 no núcleo (sem display): cancelar resposta, fallback
de think/tools só quando o Ollama diz que não suporta, visão preferindo
modelos com ferramentas, terminal que não trava e comandos perigosos."""

import time

from _util import carregar_tars

tars = carregar_tars()


# ------------------------------------------------------------------
# Cancelar no meio do texto
# ------------------------------------------------------------------
class RespostaLenta:
    status_code = 200

    def __init__(self):
        self.fechada = False

    def iter_lines(self):
        for i in range(100):
            if i == 3:
                tars.cancelar_resposta()  # como o botão Parar
            yield f'{{"message": {{"content": "p{i} "}}, "done": false}}'.encode()

    def close(self):
        self.fechada = True


lenta = RespostaLenta()
tars._chamar_ollama_stream = lambda payload: (lenta, None)
tars.carregar_modelo = lambda m: True
tars.modo_modelo = "MANUAL"
resposta = tars.perguntar_modelo("m", "conte uma história")
assert resposta == "(interrompido)" and lenta.fechada, resposta
assert tars.conversa[-1]["content"] == "(interrupted by the user)"
assert not tars._cancelar.is_set(), "o próximo pedido não pode nascer cancelado"
print("OK: Parar interrompe o texto na hora e deixa a conversa consistente")


# ------------------------------------------------------------------
# think/tools: só desliga quando o Ollama diz que não suporta
# ------------------------------------------------------------------
class R:
    def __init__(self, status, texto=""):
        self.status_code, self.text = status, texto


import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("t2", tars.__file__)
t2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t2)
t2.catalogar_modelos = lambda forcar=False: {}

respostas = iter([R(500, "out of memory"), R(200)])
t2.ollama_request = lambda p, stream=False: next(respostas)
_, erro = t2._chamar_ollama_stream({"model": "x", "think": True, "tools": [1]})
assert erro and "out of memory" in erro and "x" not in t2._MODELOS_SEM_TOOLS, erro
print("OK: erro passageiro (falta de memória) não marca o modelo como 'sem ferramentas'")

enviados = []
respostas = iter([R(400, '"y" does not support tools'), R(200)])
t2.ollama_request = lambda p, stream=False: (enviados.append(dict(p)), next(respostas))[1]
resp, erro = t2._chamar_ollama_stream({"model": "y", "tools": [1]})
assert resp and not erro and "y" in t2._MODELOS_SEM_TOOLS and "tools" not in enviados[-1]
print("OK: 'does not support tools' → tenta de novo sem ferramentas e lembra")


# ------------------------------------------------------------------
# Visão: prefere quem enxerga E chama ferramentas
# ------------------------------------------------------------------
def modelo(tag, params, caps):
    return {"tag": tag, "parametros_b": params, "categoria": "visao",
            "vram_estimado_gb": 3.0, "capacidades": set(caps), "capacidades_conhecidas": True}


catalogo = {m["tag"]: m for m in (
    modelo("so-ve:27b", 27, {"completion", "vision"}),
    modelo("ve-e-age:12b", 12, {"completion", "vision", "tools"}),
)}
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 48.0, "vram_livre_gb": 40.0}
assert tars.construir_cadeia_fallback("visao", catalogo)[0] == "ve-e-age:12b"
print("OK: tarefa de visão escolhe modelo que enxerga E clica")


# ------------------------------------------------------------------
# Terminal: não trava esperando senha nem programa em segundo plano
# ------------------------------------------------------------------
t0 = time.time()
r = tars.executar_terminal("read resposta; echo \"lido:[$resposta]\"")
assert time.time() - t0 < 3 and "lido:[]" in r["stdout"], r
t0 = time.time()
r = tars.executar_terminal("sleep 20 &")
assert time.time() - t0 < 2 and r["sucesso"], r
print("OK: comandos que pedem entrada ou rodam em segundo plano não travam")

for cmd in ("rm -r ~/Documentos", "rm -R pasta", "find . -name '*.log' -delete", "git reset --hard HEAD~3"):
    assert tars._comando_e_perigoso(cmd), cmd
for cmd in ("rm arquivo.txt", "ls -R", "git status", "find . -name x"):
    assert not tars._comando_e_perigoso(cmd), cmd
print("OK: rm -r, find -delete e git reset --hard pedem confirmação")

# DNS não mexe mais no timeout global de sockets
import socket  # noqa: E402

antes = socket.getdefaulttimeout()
tars._dominio_existe("https://dominio-que-nao-existe-opentars.invalid")
assert socket.getdefaulttimeout() == antes
print("OK: checagem de domínio não altera o timeout de rede do processo")

# Resposta do ajudante sem <think>
tars._SESSION.post = lambda *a, **k: type("Resp", (), {
    "status_code": 200,
    "json": lambda self: {"message": {"content": "<think>hmm</think>gnome-calculator"}},
})()
assert tars._chat_unico("m", "p") == "gnome-calculator"
print("OK: raciocínio <think> não vaza na resposta do modelo ajudante")


# ------------------------------------------------------------------
# Correções da revisão independente
# ------------------------------------------------------------------
import threading  # noqa: E402

# Ordem: [print, clique] na mesma resposta -> os dois resultados logo
# após as chamadas, e a imagem só depois deles.
spec3 = importlib.util.spec_from_file_location("t3", tars.__file__)
t3 = importlib.util.module_from_spec(spec3)
spec3.loader.exec_module(t3)
t3.carregar_modelo = lambda m: True
t3.modo_modelo = "MANUAL"
t3._DISPATCH_FERRAMENTAS["take_screenshot"] = lambda a: {"sucesso": True, "mensagem": "ok", "_imagem_b64": "IMG"}
t3._DISPATCH_FERRAMENTAS["click_mouse"] = lambda a: {"sucesso": True, "mensagem": "ok"}
roteiro = iter([
    {"content": "", "tool_calls": [{"function": {"name": "take_screenshot", "arguments": {}}},
                                   {"function": {"name": "click_mouse", "arguments": {"x": 1, "y": 1}}}]},
    {"content": "feito", "tool_calls": []},
])
vistos = []
t3._executar_turno_streaming = lambda m, msgs, tools, **kw: (vistos.append([dict(x) for x in msgs]), (next(roteiro), None))[1]
t3.perguntar_modelo("m", "tarefa")
papeis = [x["role"] for x in vistos[1][-4:]]
assert papeis == ["assistant", "tool", "tool", "user"] and vistos[1][-1].get("images") == ["IMG"], papeis
print("OK: vários resultados de ferramenta ficam juntos, e o print vem depois deles")

# Parar no meio de uma sequência de ferramentas: as que não rodaram
# ganham um resultado "cancelado" (a conversa continua válida).
roteiro = iter([{"content": "", "tool_calls": [
    {"function": {"name": "click_mouse", "arguments": {}}},
    {"function": {"name": "take_screenshot", "arguments": {}}},
    {"function": {"name": "click_mouse", "arguments": {}}}]}])
t3._DISPATCH_FERRAMENTAS["click_mouse"] = lambda a: (t3.cancelar_resposta(), {"sucesso": True, "mensagem": "ok"})[1]
t3.perguntar_modelo("m", "três passos")
ultimas = t3.conversa[-5:]
assert [x["role"] for x in ultimas] == ["assistant", "tool", "tool", "tool", "assistant"], [x["role"] for x in ultimas]
assert "Cancelled" in ultimas[2]["content"] and "Cancelled" in ultimas[3]["content"]
print("OK: Parar no meio das ferramentas deixa um resultado 'cancelado' pra cada uma que não rodou")


# Parar enquanto o Ollama ainda lê a conversa (nenhuma linha chegou)
class Travada:
    status_code = 200

    def __init__(self):
        self.fechou = threading.Event()

    def iter_lines(self):
        self.fechou.wait(10)
        raise ConnectionError("fechada")

    def close(self):
        self.fechou.set()


travada = Travada()
t4 = importlib.util.module_from_spec(spec3)  # módulo novo, com o streaming de verdade
spec3.loader.exec_module(t4)
t4.carregar_modelo = lambda m: True
t4.modo_modelo = "MANUAL"
t4._chamar_ollama_stream = lambda p: (travada, None)
threading.Timer(0.5, t4.cancelar_resposta).start()
t0 = time.time()
assert t4.perguntar_modelo("m", "oi") == "(interrompido)"
assert time.time() - t0 < 3, time.time() - t0
print(f"OK: Parar funciona mesmo antes da IA começar a escrever ({time.time() - t0:.1f}s)")

# Comando de terminal longo também para
threading.Timer(0.5, tars.cancelar_resposta).start()
t0 = time.time()
r = tars.executar_terminal("sleep 20")
tars._cancelar.clear()
assert time.time() - t0 < 3 and "interrupted" in r["mensagem"], r
print("OK: Parar interrompe um comando de terminal demorado")

# DNS travado não segura o openTARS além do limite
import socket as _s  # noqa: E402
tars.socket.gethostbyname = lambda h: time.sleep(20)
t0 = time.time()
assert tars._dominio_existe("https://qualquer.com") is False
assert time.time() - t0 < tars.TIMEOUT_DNS + 1.5, time.time() - t0
tars.socket.gethostbyname = _s.gethostbyname
print("OK: DNS travado respeita o limite de tempo")

# Mais de um modelo carregado: o novo só convive se TODOS couberem
tars._cache_catalogo["dados"] = {"a": {"vram_estimado_gb": 5.0}, "b": {"vram_estimado_gb": 5.0}, "c": {"vram_estimado_gb": 12.0}}
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 24.0, "vram_livre_gb": 20.0}
tars.contexto_ia = lambda: 16384
assert tars._cabem_juntos(["a"], "b") and not tars._cabem_juntos(["a", "b"], "c")
print("OK: descarrega os modelos antigos quando o novo não cabe com todos eles")

print("\nTUDO OK.")
