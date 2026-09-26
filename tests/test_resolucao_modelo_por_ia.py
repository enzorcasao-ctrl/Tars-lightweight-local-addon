from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen3:32b": {"tag": "qwen3:32b", "familia": "qwen3"},
    "gemma3:270m": {"tag": "gemma3:270m", "familia": "gemma3"},
    "qwen2.5:0.5b": {"tag": "qwen2.5:0.5b", "familia": "qwen2"},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo_fake.keys())
tars._carregar_em_segundo_plano = lambda modelo: None

class FakeResp:
    def __init__(self, status, corpo):
        self.status_code = status
        self._corpo = corpo
    def json(self):
        return self._corpo

# "o modelo pequenininho do google" nao bate em nada por texto -> cai pro gemma via IA
tars._SESSION.post = lambda url, json, timeout: FakeResp(200, {"message": {"content": "gemma3:270m"}})
ok = tars.interpretar_comando_modelo("/modelo o pequenininho do google")
print("resultado:", ok, tars.modelo_manual)
assert ok is True
assert tars.modelo_manual == "gemma3:270m"
print("OK: fallback via IA funcionou pra descricao sem nenhuma palavra-chave direta.")

# IA responde "nenhum" -> nao deve mudar nada, so avisar
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
tars._SESSION.post = lambda url, json, timeout: FakeResp(200, {"message": {"content": "nenhum"}})
ok = tars.interpretar_comando_modelo("/modelo o bicho do castelo")
assert ok is True
assert tars.modelo_manual is None
print("OK: IA respondendo 'nenhum' nao força nenhuma troca indevida.")
