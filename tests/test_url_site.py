from _util import carregar_tars
tars = carregar_tars()

# 1. Pedido tipo "canal da anthropic no youtube" -> devia virar busca no youtube
r = tars.abrir_site("canal da anthropic no youtube")
assert "youtube.com/results" in r["url"], r
assert "anthropic" in r["url"]
print("OK: canal no youtube vira busca, não domínio inventado:", r["url"])

# 2. Domínio direto continua funcionando
tars.webbrowser.open = lambda u: True
r = tars.abrir_site("github.com")
assert r["url"] == "https://github.com", r
print("OK: dominio direto:", r["url"])

# 3. perguntar_url_site: IA responde "desconhecido" -> None (cai pra busca)
tars._consultar_modelo_leve = lambda p, formato=None: "desconhecido"
assert tars.perguntar_url_site("coisa qualquer") is None
print("OK: 'desconhecido' vira None")

# 4. perguntar_url_site: IA inventa domínio que não resolve -> None
tars._consultar_modelo_leve = lambda p, formato=None: "https://anthropic-canals.youtube.com"
tars._dominio_existe = lambda u: False  # simula DNS falhando
assert tars.perguntar_url_site("canal da anthropic") is None
print("OK: dominio inventado (sem DNS) vira None")

# 5. perguntar_url_site: URL real, resolve -> aceita
tars._consultar_modelo_leve = lambda p, formato=None: "https://www.anthropic.com"
tars._dominio_existe = lambda u: True
assert tars.perguntar_url_site("anthropic") == "https://www.anthropic.com"
print("OK: dominio real aceito")

print("\nTUDO OK.")
