"""Regressão do bug real reportado: 'pesquise rtx 5060 no google' foi
pro qwen3:0.6b, que repassou só site='rtx 5060' pro open_website (sem
o 'no google' nem o verbo), quebrando a busca.

A correção NÃO foi interceptar a busca no texto cru antes da IA entrar
em ação (isso seria só trocar um hardcode por outro, e a IA continuaria
sem aprender a raciocinar direito) — foi dar pra IA uma ferramenta
('search_web') onde o próprio formato já separa 'o que pesquisar'
(query) de 'onde pesquisar' (service), então não tem como ela cortar a
frase pela metade feito fazia com o campo genérico 'site' de
open_website. Este teste garante que a ferramenta está de fato
registrada e funcionando (antes dela, 'search_web' existia só na lista
de ferramentas oferecida à IA, mas não tinha handler nenhum — ou seja,
se qualquer modelo tentasse chamá-la, ia falhar com 'Ferramenta
desconhecida')."""

from _util import carregar_tars
tars = carregar_tars()

tars.webbrowser.open = lambda u: True

# a ferramenta precisa estar de fato ligada ao dispatch (antes não
# estava — só existia na lista TOOLS, sem handler nenhum)
assert "search_web" in tars._DISPATCH_FERRAMENTAS
print("OK: search_web está registrada no dispatch de ferramentas")

# caso real reportado: query e service member, sem depender de cortar
# frase nenhuma
r = tars.executar_ferramenta("search_web", {"query": "rtx 5060", "service": "google"})
assert r["sucesso"] and r["url"] == "https://www.google.com/search?q=rtx+5060", r
print("OK: busca com serviço explícito:", r["url"])

# sem 'service' -> padrão Google
r = tars.executar_ferramenta("search_web", {"query": "buracos negros"})
assert r["url"] == "https://www.google.com/search?q=buracos+negros", r
print("OK: busca sem serviço cai no Google por padrão:", r["url"])

# serviço youtube
r = tars.executar_ferramenta("search_web", {"query": "canal da anthropic", "service": "youtube"})
assert r["url"] == "https://www.youtube.com/results?search_query=canal+da+anthropic", r
print("OK: busca no youtube:", r["url"])

# query vazia é erro claro, não um chute
r = tars.executar_ferramenta("search_web", {"query": "  "})
assert not r["sucesso"]
print("OK: query vazia retorna erro em vez de adivinhar algo")

# open_website continua funcionando como rede de segurança pra quando
# um modelo ainda assim usar ele pra uma frase de busca
tars._SESSION.post = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("nao deveria precisar de nenhum modelo pra uma busca reconhecida")
)
r = tars.abrir_site("pesquisar sobre gatos siameses")
assert r["url"] == "https://www.google.com/search?q=gatos+siameses", r
print("OK: open_website ainda reconhece frase de busca como rede de segurança:", r["url"])

print("\nTUDO OK.")
