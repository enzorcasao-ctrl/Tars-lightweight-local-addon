from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "familia": "qwen3", "categoria": "simples", "parametros_b": 0.6, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:32b": {"tag": "qwen3:32b", "familia": "qwen3", "categoria": "tecnico", "parametros_b": 32, "capacidades": {"tools"}, "capacidades_conhecidas": True},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake
tars.listar_modelos_instalados = lambda forcar=False: set(catalogo_fake.keys())
tars._carregar_em_segundo_plano = lambda modelo: None
tars._SESSION.post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("nao deveria precisar da IA"))

# Via "/modelo" — aceita a frase toda, com verbos e "por favor" e
# qualquer coisa em volta (substring é seguro aqui, porque o "/modelo"
# já deixou explícito que o pedido é sobre trocar de IA).
casos_com_modelo = {
    "/modelo usa o mais leve": "qwen3:0.6b",
    "/modelo quero o modelo mais leve": "qwen3:0.6b",
    "/modelo troca pro maior modelo": "qwen3:32b",
}
for frase, esperado in casos_com_modelo.items():
    tars.modo_modelo = "AUTO"; tars.modelo_manual = None
    ok = tars.interpretar_comando_modelo(frase)
    status = "OK" if (ok and tars.modelo_manual == esperado) else "FALHOU"
    print(f"{status:6} {frase!r:35} -> {tars.modelo_manual} (esperado {esperado})")
    assert ok and tars.modelo_manual == esperado

# Sem "/modelo", só a frase EXATA do superlativo (mensagem inteira,
# sem nada em volta) ainda troca — nunca um pedaço dela dentro de uma
# frase qualquer, que é justamente o que causava falso positivo antes.
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
ok = tars.interpretar_comando_modelo("mais leve")
assert ok and tars.modelo_manual == "qwen3:0.6b"
print("OK: mensagem inteira == 'mais leve' troca sem precisar de /modelo")

# "o menor problema pode causar um erro grande no sistema" contém "o
# menor" como substring, mas não é a frase inteira "o menor" — não
# deve trocar de modelo (essa era exatamente a armadilha do bug
# original "isso foi meio burro").
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
ok = tars.interpretar_comando_modelo("o menor problema pode causar um erro grande no sistema")
print("frase-armadilha 'o menor problema...':", ok, tars.modelo_manual)
assert ok is False
assert tars.modelo_manual is None

# "usa o mais leve" SEM /modelo também não deve mais trocar — trocar a
# partir de um verbo solto foi removido de propósito.
tars.modo_modelo = "AUTO"; tars.modelo_manual = None
ok = tars.interpretar_comando_modelo("usa o mais leve")
assert ok is False
assert tars.modelo_manual is None
print("OK: 'usa o mais leve' sem /modelo não troca mais (por design)")

print("\nOK: superlativos via /modelo continuam flexíveis, e nada solto no meio de uma frase dispara troca.")
