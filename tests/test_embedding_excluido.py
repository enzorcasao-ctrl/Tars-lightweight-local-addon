from _util import carregar_tars
tars = carregar_tars()

# Simula um catalogo com um modelo novo qualquer (nunca visto no codigo)
# + um modelo de embedding puro -- confirma que o modelo novo participa
# normalmente e o de embedding eh excluido.
catalogo_fake = {
    "meu-modelo-novo:latest": {
        "tag": "meu-modelo-novo:latest", "familia": "coisaqualquer",
        "parametros_b": 7.0, "quantizacao": "Q4_0", "vram_estimado_gb": 4.8,
        "capacidades": {"tools", "completion"}, "capacidades_conhecidas": True,
        "categoria": "geral", "emoji": "🤖",
    },
    "nomic-embed-text:latest": {
        "tag": "nomic-embed-text:latest", "familia": "nomic-bert",
        "parametros_b": 0.137, "quantizacao": "F16", "vram_estimado_gb": 0.9,
        "capacidades": {"embedding"}, "capacidades_conhecidas": True,
        "categoria": "embedding", "emoji": "🤖",
    },
}

cadeia = tars.construir_cadeia_fallback("geral", catalogo=catalogo_fake)
assert "meu-modelo-novo:latest" in cadeia, cadeia
assert "nomic-embed-text:latest" not in cadeia, cadeia
print("OK: modelo novo desconhecido participa, embedding é excluído:", cadeia)

ajudante = tars.modelo_ajudante(catalogo_fake)
assert ajudante == "meu-modelo-novo:latest", ajudante
print("OK: modelo_ajudante nunca escolhe o de embedding:", ajudante)

# categorização automática de um modelo de embedding via /api/show simulado
cat = tars._categorizar_modelo("algum-embed:latest", {"embedding"}, 0.1, "bert")
assert cat == "embedding", cat
print("OK: _categorizar_modelo identifica embedding puro")

cat2 = tars._categorizar_modelo("llava:13b", {"completion", "vision", "tools"}, 13, "llama")
assert cat2 == "visao", cat2
print("OK: modelo de chat normal continua categorizado certo")

print("\nTUDO OK.")
