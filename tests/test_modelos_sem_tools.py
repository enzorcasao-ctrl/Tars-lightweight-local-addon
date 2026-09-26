from _util import carregar_tars
import json
tars = carregar_tars()

catalogo_fake = {
    "gemma3:270m": {"tag": "gemma3:270m", "familia": "gemma3", "categoria": "simples",
                    "parametros_b": 0.27, "capacidades": set(), "capacidades_conhecidas": True},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake

class FakeStreamResp:
    def __init__(self, status, linhas, texto_erro=""):
        self.status_code = status
        self._linhas = linhas
        self.text = texto_erro
    def iter_lines(self):
        return iter(self._linhas)

chamadas = []

def fake_ollama_request(payload, stream=False):
    chamadas.append(dict(payload))
    if "tools" in payload:
        # Simula o Ollama recusando 'tools' pra esse modelo
        return FakeStreamResp(400, [], '{"error":"registry.ollama.ai/library/gemma3:270m does not support tools"}')
    linha = json.dumps({"message": {"content": "Olá! Como posso ajudar?"}, "done": True}).encode()
    return FakeStreamResp(200, [linha])

tars.ollama_request = fake_ollama_request

# --- Teste 1: catalogo ja sabe que nao suporta tools -> nem tenta com tools ---
resposta, erro = tars._chamar_ollama_stream({
    "model": "gemma3:270m", "messages": [{"role": "user", "content": "oi"}],
    "tools": tars.TOOLS, "stream": True, "think": True,
})
print("erro:", erro)
print("numero de chamadas HTTP:", len(chamadas))
print("ultima chamada tinha 'tools'?", "tools" in chamadas[-1])
assert erro is None
assert resposta is not None
assert "tools" not in chamadas[-1], "nao deveria ter mandado 'tools' pra um modelo que o catalogo ja diz que nao suporta"
assert "gemma3:270m" in tars._MODELOS_SEM_TOOLS

print("OK: descoberta proativa via catalogo evitou o erro 400 de primeira.\n")

# --- Teste 2: sem informacao de catalogo (capacidades desconhecidas), decobre reativamente ---
chamadas.clear()
tars._MODELOS_SEM_TOOLS.clear()
tars.catalogar_modelos = lambda forcar=False: {}  # catalogo vazio/sem info
resposta, erro = tars._chamar_ollama_stream({
    "model": "gemma3:270m", "messages": [{"role": "user", "content": "oi"}],
    "tools": tars.TOOLS, "stream": True, "think": True,
})
print("numero de chamadas HTTP (descoberta reativa):", len(chamadas))
assert erro is None
assert "gemma3:270m" in tars._MODELOS_SEM_TOOLS
print("OK: descoberta reativa (sem catalogo) tambem funciona e aprende pra proxima vez.\n")

# --- Teste 3: proxima chamada no MESMO processo ja nao tenta 'tools' de novo ---
chamadas.clear()
resposta, erro = tars._chamar_ollama_stream({
    "model": "gemma3:270m", "messages": [{"role": "user", "content": "hello"}],
    "tools": tars.TOOLS, "stream": True, "think": True,
})
assert len(chamadas) == 1, f"deveria ter aprendido e ido direto sem tools, mas fez {len(chamadas)} chamadas"
assert "tools" not in chamadas[0]
print("OK: memoriza entre mensagens, nao repete erro.\n")

print("TUDO OK.")
