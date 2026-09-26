from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen3:32b": {"tag": "qwen3:32b", "familia": "qwen3", "categoria": "tecnico", "parametros_b": 32, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3.8:27b": {"tag": "qwen3.8:27b", "familia": "qwen3", "categoria": "visao", "parametros_b": 27, "capacidades": {"tools", "vision"}, "capacidades_conhecidas": True},
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "familia": "qwen3", "categoria": "simples", "parametros_b": 0.6, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:8b": {"tag": "qwen3:8b", "familia": "qwen3", "categoria": "geral", "parametros_b": 8, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "llama3.1:latest": {"tag": "llama3.1:latest", "familia": "llama", "categoria": "geral", "parametros_b": 8, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "gemma3:270m": {"tag": "gemma3:270m", "familia": "gemma3", "categoria": "simples", "parametros_b": 0.27, "capacidades": set(), "capacidades_conhecidas": True},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo_fake.keys())
tars._carregar_em_segundo_plano = lambda modelo: None

def sem_chamada_ia(*a, **k):
    raise AssertionError("nao deveria precisar da IA classificadora pra esse caso (deveria casar por texto)")
tars._SESSION.post = sem_chamada_ia

# Caso do bug relatado: "/modelo gemma 270m"
assert tars.interpretar_comando_modelo("/modelo gemma 270m") is True
assert tars.modelo_manual == "gemma3:270m", tars.modelo_manual
assert tars.modo_modelo == "MANUAL"
print("OK: '/modelo gemma 270m' agora resolve para gemma3:270m")

# Reset
tars.modo_modelo = "AUTO"; tars.modelo_manual = None

# Variações de digitação — sempre via "/modelo", que é agora a ÚNICA
# forma de trocar por texto livre/aproximado. Trocar a partir de um
# verbo solto numa frase qualquer ("usar o gemma", "troca pra qwen")
# foi removido de propósito (ver test_sem_linguagem_natural.py).
for entrada, esperado in [
    ("/modelo gemma", "gemma3:270m"),
    ("/modelo qwen 8b", "qwen3:8b"),
    ("/modelo llama", "llama3.1:latest"),
    ("/modelo qwen 32b", "qwen3:32b"),
    ("/modelo usar o gemma 270m", "gemma3:270m"),
    ("/modelo troca pra qwen 8b por favor", "qwen3:8b"),
]:
    tars.modo_modelo = "AUTO"; tars.modelo_manual = None
    ok = tars.interpretar_comando_modelo(entrada)
    status = "OK" if (ok and tars.modelo_manual == esperado) else "FALHOU"
    print(f"{status:6} {entrada!r:35} -> {tars.modelo_manual} (esperado {esperado})")
    assert ok and tars.modelo_manual == esperado

# Mensagem inteira igual a uma tag/família/alias continua funcionando
# SEM precisar de "/modelo" (é a frase toda, sem ambiguidade nenhuma).
# "llama" é a família reportada (e única) desse modelo no catálogo.
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
assert tars.interpretar_comando_modelo("llama") is True
assert tars.modelo_manual == "llama3.1:latest"
print("OK: mensagem inteira == alias (família) continua trocando sem /modelo")

# Caso: pedido que não bate em nada -> avisa, nao muda o modo, nao quebra
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
ok = tars.interpretar_comando_modelo("/modelo bagulho-que-nao-existe-999")
assert ok is True  # comando reconhecido (é /modelo), so nao achou correspondencia
assert tars.modo_modelo == "AUTO"  # nao mudou nada
print("OK: modelo inexistente avisa sem travar nem mudar o estado")

print("\nTUDO OK.")
