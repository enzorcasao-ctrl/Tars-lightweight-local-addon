from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "familia": "qwen3", "categoria": "simples", "parametros_b": 0.6, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:8b": {"tag": "qwen3:8b", "familia": "qwen3", "categoria": "geral", "parametros_b": 8, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "gemma3:12b": {"tag": "gemma3:12b", "familia": "gemma3", "categoria": "geral", "parametros_b": 12, "capacidades": set(), "capacidades_conhecidas": True},
    "gemma3:270m": {"tag": "gemma3:270m", "familia": "gemma3", "categoria": "simples", "parametros_b": 0.27, "capacidades": set(), "capacidades_conhecidas": True},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo_fake.keys())
tars._carregar_em_segundo_plano = lambda modelo: None

def ia_nao_deveria_ser_chamada(*a, **k):
    raise AssertionError("nao deveria ter chamado a IA classificadora pra essa frase (sem sinal de troca de modelo)")
tars._SESSION.post = ia_nao_deveria_ser_chamada

# O bug relatado: frase de tarefa comum contendo "use", nao deveria trocar de modelo
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
resultado = tars.interpretar_comando_modelo("use suas ferramentas para abrir o youtube no canal da anthropic")
print("resultado:", resultado, "| modo:", tars.modo_modelo, "| modelo_manual:", tars.modelo_manual)
assert resultado is False, "nao deveria ter sido reconhecido como comando de troca de modelo"
assert tars.modo_modelo == "AUTO"
assert tars.modelo_manual is None
print("OK: 'use suas ferramentas...' nao dispara mais troca de modelo\n")

# Outras frases comuns com verbos ambiguos que tambem nao deveriam disparar
casos_normais = [
    "quero abrir o navegador e ver meus emails",
    "coloca a musica mais alta por favor",
    "manda ver se tem espaco em disco",
    "use a calculadora pra somar esses valores",
]
for frase in casos_normais:
    tars.modo_modelo = "AUTO"; tars.modelo_manual = None
    resultado = tars.interpretar_comando_modelo(frase)
    status = "OK" if resultado is False else "FALHOU"
    print(f"{status:6} {frase!r}")
    assert resultado is False, f"falso positivo em: {frase}"

print("\nOK: nenhum falso positivo nas frases comuns testadas.")
