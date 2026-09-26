from _util import carregar_tars
tars = carregar_tars()

tars.webbrowser.open = lambda u: True
tars._SESSION.post = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("nao deveria precisar da IA leve pra uma busca direta")
)

casos = {
    "pesquisar sobre buracos negros": "https://www.google.com/search?q=buracos+negros",
    "pesquisar gatos siameses no google": "https://www.google.com/search?q=gatos+siameses",
    "canal da anthropic no youtube": "https://www.youtube.com/results?search_query=canal+da+anthropic",
    "buscar receita de bolo de cenoura": "https://www.google.com/search?q=receita+de+bolo+de+cenoura",
}

for entrada, esperado in casos.items():
    r = tars.abrir_site(entrada)
    status = "OK" if r["url"] == esperado else "FALHOU"
    print(f"{status:6} {entrada!r:45} -> {r['url']}")
    assert r["url"] == esperado, (entrada, r["url"], esperado)

# domínio direto continua sem passar pela busca
r = tars.abrir_site("github.com")
assert r["url"] == "https://github.com"
print("OK: domínio direto não vira busca")

print("\nTUDO OK.")
