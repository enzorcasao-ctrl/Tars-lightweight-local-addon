"""Regressão do bug real reportado: 'pesquise anthropic no google',
'pesquise cooperasis no google' e 'pesquise llms no google' foram todos
pro qwen3:0.6b, que (apesar da ferramenta search_web existir e estar
bem documentada) manteve o padrão de chamar open_website com só o
termo, ignorando o pedido explícito de busca no Google.

A causa raiz não é falta de uma ferramenta melhor (search_web já
resolve isso, ver test_search_web.py) — é que um modelo de 0.6B
parâmetros não é confiável pra escolher ENTRE DUAS ferramentas
parecidas, mesmo com uma bem descrita disponível; ele tende a repetir
o padrão da própria conversa em vez de reavaliar. A correção é a mesma
já usada pra 'acao' (abrir/fechar app): tirar esse tipo de pedido do
balde reservado ao modelo minúsculo, e deixar pra um modelo com
raciocínio melhor decidir qual ferramenta usar — a IA continua
decidindo tudo sozinha, só não é mais a menorzinha demais pra esse
tipo de escolha."""

from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "familia": "qwen3", "categoria": "simples", "parametros_b": 0.6, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:8b": {"tag": "qwen3:8b", "familia": "qwen3", "categoria": "geral", "parametros_b": 8, "capacidades": {"tools"}, "capacidades_conhecidas": True},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake
tars.detectar_gpu = lambda forcar=False: None

# os três pedidos reais do bug relatado viram categoria "busca"
for texto in (
    "pesquise anthropic no google",
    "pesquise cooperasis no google",
    "agora pesquise llms no google",
):
    tarefa = tars.detectar_tarefa(texto)
    assert tarefa == "busca", (texto, tarefa)
print("OK: pedidos de busca caem na categoria própria 'busca'")

# e a cadeia de fallback de "busca" NUNCA inclui o modelo minúsculo
# (categoria "simples" no catálogo) -- igual já acontecia com "acao"
cadeia_busca = tars.construir_cadeia_fallback("busca", catalogo_fake)
assert "qwen3:0.6b" not in cadeia_busca, cadeia_busca
assert cadeia_busca[0] == "qwen3:8b", cadeia_busca
print("OK: busca nunca cai no modelo minúsculo:", cadeia_busca)

# abrir um site já conhecido, sem nenhum verbo de busca, continua
# podendo usar o modelo minúsculo normalmente (não regrediu)
cadeia_simples = tars.construir_cadeia_fallback("simples", catalogo_fake)
assert cadeia_simples[0] == "qwen3:0.6b", cadeia_simples
print("OK: 'abrir site conhecido' sem busca continua no modelo minúsculo:", cadeia_simples)

print("\nTUDO OK.")
