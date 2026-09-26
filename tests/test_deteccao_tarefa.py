from _util import carregar_tars
tars = carregar_tars()

casos = {
    "abra o youtube": "simples",
    "abre o chatgpt": "simples",
    "oi, tudo bem?": "simples",
    "abra o gerenciador de arquivos": "acao",
    "feche todas as abas do brave": "acao",
    "fecha o spotify": "acao",
    "rode o comando ls na pasta downloads": "acao",
    "escreva um poema sobre o outono e o significado da vida": "geral",
    "me explique como funciona a fotossintese em detalhes por favor": "geral",
    "veja minha tela e me diga o que tem nela": "visao",
    "tem algum erro nesse codigo python?": "codigo",
    "abra a calculadora": "acao",
}
falhas = 0
for texto, esperado in casos.items():
    resultado = tars.detectar_tarefa(texto)
    status = "OK" if resultado == esperado else "FALHOU"
    if status == "FALHOU":
        falhas += 1
    print(f"{status:6} {texto!r:65} -> {resultado} (esperado: {esperado})")

assert falhas == 0, f"{falhas} caso(s) falharam"

catalogo_fake = {
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "categoria": "simples", "parametros_b": 0.6, "vram_estimado_gb": 0.9, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:8b": {"tag": "qwen3:8b", "categoria": "geral", "parametros_b": 8, "vram_estimado_gb": 5.5, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:27b": {"tag": "qwen3:27b", "categoria": "geral", "parametros_b": 27, "vram_estimado_gb": 17, "capacidades": {"tools"}, "capacidades_conhecidas": True},
}
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 8, "vram_livre_gb": 6}
cadeia_acao = tars.construir_cadeia_fallback("acao", catalogo_fake)
print("\ncadeia 'acao':", cadeia_acao)
assert cadeia_acao[0] == "qwen3:8b"

cadeia_simples = tars.construir_cadeia_fallback("simples", catalogo_fake)
print("cadeia 'simples':", cadeia_simples)
assert cadeia_simples[0] == "qwen3:0.6b"

print("\nOK: todos os casos passaram.")
