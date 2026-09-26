"""Regressão do bug real reportado: 'abra o google chrome e pesquise
ollama' caindo no modelo técnico (por causa da palavra 'ollama') e
depois 'google-chrome' virando uma busca por '-chrome' quando o
navegador foi aberto via open_website em vez de open_application."""

from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen2.5-coder:latest": {"tag": "qwen2.5-coder:latest", "familia": "qwen2", "categoria": "tecnico", "parametros_b": 7.6, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:8b": {"tag": "qwen3:8b", "familia": "qwen3", "categoria": "geral", "parametros_b": 8, "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "familia": "qwen3", "categoria": "simples", "parametros_b": 0.6, "capacidades": {"tools"}, "capacidades_conhecidas": True},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake

# "ollama" sendo só o ASSUNTO da busca não pode sequestrar a categoria
tarefa = tars.detectar_tarefa("abra o google chrome e pesquise ollama")
assert tarefa != "tecnico", tarefa
print("OK: não vai mais pro modelo técnico só por causa da palavra 'ollama':", tarefa)

# perguntas técnicas de verdade continuam técnicas
assert tars.detectar_tarefa("tem algum erro nesse codigo python?") == "codigo"
print("OK: pergunta técnica de verdade continua técnica")

# pesquisar sem "abra" vira categoria própria "busca" (evita o modelo
# minúsculo, que não escolhe direito entre search_web/open_website)
assert tars.detectar_tarefa("pesquise sobre buracos negros") == "busca"
print("OK: 'pesquise sobre X' vira busca (categoria própria, sem modelo minúsculo)")

# fechar continua tendo prioridade mesmo citando um site conhecido
assert tars.detectar_tarefa("feche todas as abas do brave") == "acao"
print("OK: fechar continua prioritário sobre 'brave' (site)")

# "google-chrome"/"google chrome" via open_website é redirecionado pra
# abrir o APLICATIVO navegador, não uma busca por "-chrome"
tars.subprocess.Popen = lambda *a, **k: None
tars.shutil.which = lambda nome: "/usr/bin/google-chrome" if "chrome" in nome else None

for entrada in ("google-chrome", "google chrome", "chrome", "firefox"):
    r = tars.abrir_site(entrada)
    assert r.get("acao") == "open_application", (entrada, r)
print("OK: nomes de navegador via open_website são abertos como aplicativo")

# buscas de verdade continuam funcionando normalmente
tars.webbrowser.open = lambda u: True
tars._SESSION.post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("nao deveria precisar da IA"))
r = tars.abrir_site("pesquisar sobre gatos siameses")
assert r["url"] == "https://www.google.com/search?q=gatos+siameses", r
print("OK: busca de verdade continua funcionando:", r["url"])

print("\nTUDO OK.")
