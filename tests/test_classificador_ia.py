from _util import carregar_tars
import json
tars = carregar_tars()

class FakeResp:
    def __init__(self, status, corpo):
        self.status_code = status
        self._corpo = corpo
    def json(self):
        return self._corpo

# Caso 1: classificador NAO instalado -> nao chama nada, cai em "geral"
tars.listar_modelos_instalados = lambda forcar=False: {"qwen3:0.6b", "qwen3:8b"}
chamou = {"flag": False}
def post_falha(*a, **k):
    chamou["flag"] = True
    raise AssertionError("nao deveria chamar o Ollama se o modelo nao esta instalado")
tars._SESSION.post = post_falha
resultado = tars.detectar_tarefa("me conta uma curiosidade aleatoria sobre o universo que ninguem sabe")
print("caso 1 (nao instalado):", resultado, "| chamou Ollama?", chamou["flag"])
assert resultado == "geral"
assert chamou["flag"] is False

# Caso 2: classificador instalado, responde categoria valida
tars.listar_modelos_instalados = lambda forcar=False: {"qwen3:0.6b", "qwen2.5:0.5b"}
tars._SESSION.post = lambda url, json, timeout: FakeResp(200, {"message": {"content": "acao"}})
resultado = tars.detectar_tarefa("preciso resolver uma pendencia no meu computador urgente")
print("caso 2 (resposta valida 'acao'):", resultado)
assert resultado == "acao"

# Caso 2b: resposta forçada em JSON (Ollama atual)
tars._SESSION.post = lambda url, json, timeout: FakeResp(200, {"message": {"content": '{"categoria": "busca"}'}})
assert tars.detectar_tarefa("preciso resolver uma pendencia no meu computador urgente") == "busca"
print("caso 2b (JSON forçado):", "busca")

# Caso 3: Ollama antigo sem resposta forçada: resposta suja com a palavra dentro
tars._SESSION.post = lambda url, json, timeout: FakeResp(200, {"message": {"content": "Categoria: TECNICO."}})
resultado = tars.detectar_tarefa("me ajuda com uma parada que nao ta funcionando direito aqui")
print("caso 3 (resposta suja 'Categoria: TECNICO.'):", resultado)
assert resultado == "tecnico"

# Caso 4: erro de rede -> degrada pra "geral" sem quebrar
def post_excecao(*a, **k):
    raise ConnectionError("ollama fora do ar")
tars._SESSION.post = post_excecao
resultado = tars.detectar_tarefa("me conta uma curiosidade aleatoria sobre o universo que ninguem sabe")
print("caso 4 (erro de conexao):", resultado)
assert resultado == "geral"

# Caso 5: status != 200 -> degrada pra "geral"
tars._SESSION.post = lambda url, json, timeout: FakeResp(500, {})
resultado = tars.detectar_tarefa("me conta uma curiosidade aleatoria sobre o universo que ninguem sabe")
print("caso 5 (status 500):", resultado)
assert resultado == "geral"

# Caso 6: resposta sem nenhuma categoria reconhecivel -> degrada pra "geral"
tars._SESSION.post = lambda url, json, timeout: FakeResp(200, {"message": {"content": "nao sei classificar isso"}})
resultado = tars.detectar_tarefa("me conta uma curiosidade aleatoria sobre o universo que ninguem sabe")
print("caso 6 (resposta sem categoria):", resultado)
assert resultado == "geral"

print("\nOK: todos os casos do classificador passaram.")

# Caso extra: frases que ja batem por palavra-chave NAO devem chamar o Ollama
def post_nao_deveria_chamar(*a, **k):
    raise AssertionError("nao deveria chamar o classificador quando ha palavra-chave")
tars._SESSION.post = post_nao_deveria_chamar
assert tars.detectar_tarefa("abra o youtube") == "simples"
assert tars.detectar_tarefa("tem algum erro nesse codigo python?") == "codigo"
assert tars.detectar_tarefa("veja minha tela") == "visao"
assert tars.detectar_tarefa("oi tudo bem") == "simples"
print("OK: casos com palavra-chave nao chamam a IA classificadora (performance preservada).")
