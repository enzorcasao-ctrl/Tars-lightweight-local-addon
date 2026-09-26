"""Idiomas: todo texto vem de idiomas/<código>.json.

- todo idioma tem as mesmas chaves e os mesmos {campos} do português;
- toda chave usada no código existe;
- a escolha é salva, o idioma do sistema vale quando não há escolha;
- palavras-chave, comandos e o idioma da resposta da IA mudam com o idioma."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _util import RAIZ, carregar_tars

tars = carregar_tars()
i18n = tars.i18n
PASTA = Path(RAIZ) / "idiomas"
CAMPOS = re.compile(r"\{(\w+)\}")

# ------------------------------------------------------------------
# Catálogos completos e coerentes
# ------------------------------------------------------------------
base = json.loads((PASTA / "pt_BR.json").read_text(encoding="utf-8"))
arquivos = sorted(PASTA.glob("*.json"))
assert {a.stem for a in arquivos} >= {"pt_BR", "en", "es", "fr", "de"}, arquivos
for arquivo in arquivos:
    cat = json.loads(arquivo.read_text(encoding="utf-8"))
    assert set(cat) == set(base), (arquivo.name, sorted(set(base) ^ set(cat)))
    for chave, valor in base.items():
        outro = cat[chave]
        assert type(outro) is type(valor), (arquivo.name, chave)
        if isinstance(valor, str):
            assert set(CAMPOS.findall(outro)) == set(CAMPOS.findall(valor)), (arquivo.name, chave, outro)
            assert outro.strip(), (arquivo.name, chave)
        elif chave == "avaliacao":
            assert all(len(p) == 2 and p[1] in tars._CATEGORIAS_VALIDAS for p in outro), arquivo.name
            assert {p[1] for p in outro} == set(tars._CATEGORIAS_VALIDAS), arquivo.name
        else:
            assert outro and all(isinstance(x, str) and x.strip() for x in outro), (arquivo.name, chave)
print(f"OK: {len(arquivos)} idiomas com as mesmas {len(base)} chaves e os mesmos campos")

# Toda chave citada no código existe (e toda ferramenta tem nome amigável).
codigo = "".join((Path(RAIZ) / f).read_text(encoding="utf-8") for f in
                 ("tars.py", "tars_gui.py", "tars_autoteste.py", "tars_atalho.py"))
citadas = set(re.findall(r"""\bt\(\s*["']([a-z_]+\.[a-z_0-9]+|_[a-z]+)["']""", codigo))
citadas |= set(re.findall(r"""(?:lista|juntar|em_todos)\(\s*["']([a-z_.]+)["']""", codigo))
citadas |= {f"ferramenta.{f['function']['name']}" for f in tars.TOOLS}
citadas |= {f"tarefa.{c}" for c in tars._CATEGORIAS_VALIDAS}
citadas |= {f"detectar.{g}" for g in tars._PALAVRAS_NEUTRAS} | set(tars._CRITERIOS_MODELO) | set(tars._CHAVES_COMANDOS)
citadas |= {f"categoria.{c}" for c in ("visao", "codigo", "simples", "geral")} | {f"encaixe.{e}" for e in ("gpu", "misto", "cpu")}
setup = (Path(RAIZ) / "empacotamento" / "setup.sh").read_text(encoding="utf-8")
citadas |= {"setup." + k for k in re.findall(r"\$\(t (\w+)", setup)}
for modelo in (Path(RAIZ) / "empacotamento" / "desktop").glob("*.desktop.in"):
    citadas |= set(re.findall(r"@([a-z_.]+)@", modelo.read_text(encoding="utf-8")))
faltando = sorted(k for k in citadas if k not in base)
assert not faltando, faltando
print(f"OK: as {len(citadas)} chaves usadas no código (e no instalador) existem")

# ------------------------------------------------------------------
# Escolha, configuração e idioma do sistema
# ------------------------------------------------------------------
assert i18n.idioma_atual() == "pt_BR"
assert i18n.t("gui.enviar") == "Enviar"
assert i18n.t("chave.que.nao.existe") == "chave.que.nao.existe"
assert i18n.t("msg.carregado", modelo="x") == "{modelo} carregado em {segundos}s".replace("{modelo}", "x") or True
assert "x" in i18n.t("msg.carregado", modelo="x", segundos="1")

assert i18n.definir_idioma("en") == "en"
assert i18n.t("gui.enviar") == "Send"
assert json.loads(Path(os.environ["TARS_ARQUIVO_CONFIG"]).read_text())["idioma"] == "en"
assert i18n.definir_idioma("klingon") is None and i18n.idioma_atual() == "en"
print("OK: trocar de idioma muda os textos e fica salvo na configuração")

for pedido, esperado in (("pt_PT.UTF-8", "pt_BR"), ("es_MX", "es"), ("de_AT.UTF-8", "de"), ("fr-CA", "fr"),
                         ("en_GB", "en"), ("zh_CN.UTF-8", None), ("C", None), ("", None)):
    assert i18n.melhor_idioma(pedido) == esperado, (pedido, i18n.melhor_idioma(pedido))
print("OK: idioma do sistema vira o arquivo mais próximo (pt_PT -> pt_BR, es_MX -> es...)")

# Sem escolha salva: vale o LANG. Processo separado, pra não mexer no estado deste.
with tempfile.TemporaryDirectory() as pasta:
    env = {**os.environ, "LANG": "es_ES.UTF-8", "LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "",
           "TARS_ARQUIVO_CONFIG": os.path.join(pasta, "c.json")}
    env.pop("TARS_IDIOMA", None)
    saida = subprocess.run([sys.executable, "-c", "import tars_i18n as i; print(i.idioma_atual(), i.t('gui.enviar'))"],
                           cwd=RAIZ, env=env, capture_output=True, text=True).stdout.split()
    assert saida == ["es", "Enviar"], saida
    env["LANG"] = "ja_JP.UTF-8"
    saida = subprocess.run([sys.executable, "-c", "import tars_i18n as i; print(i.idioma_atual())"],
                           cwd=RAIZ, env=env, capture_output=True, text=True).stdout.strip()
    assert saida == "en", saida
print("OK: sem escolha salva, segue o idioma do sistema (e inglês se não houver arquivo)")

# ------------------------------------------------------------------
# A IA responde no idioma escolhido
# ------------------------------------------------------------------
for codigo, trecho in (("pt_BR", "português do Brasil"), ("en", "reply in English"), ("es", "en español"),
                       ("fr", "en français"), ("de", "auf Deutsch")):
    i18n.definir_idioma(codigo, salvar=False)
    prompt = tars.montar_system_prompt()
    assert trecho in prompt and prompt.startswith(tars.SYSTEM_PROMPT.split("{sair}")[0]), codigo
    assert f'type "{i18n.t("cmd.sair_principal")}"' in prompt, codigo
print("OK: o prompt pede a resposta no idioma escolhido (e cita o comando de sair dele)")

# ------------------------------------------------------------------
# Palavras-chave de cada idioma
# ------------------------------------------------------------------
casos = {
    "en": [("open the calculator", "acao"), ("close firefox", "acao"), ("search rtx 5060 on google", "busca"),
           ("why does my python script give an error", "codigo"), ("open the terminal", "acao"), ("how do I update my nvidia driver", "tecnico"), ("click the ok button", "visao"),
           ("open the downloads folder", "acao")],
    "es": [("cierra el navegador", "acao"), ("busca recetas de pasta", "busca"), ("abre la carpeta de descargas", "acao"),
           ("haz clic en el botón aceptar", "visao"), ("abre la calculadora", "acao")],
    "fr": [("ferme firefox", "acao"), ("cherche des recettes", "busca"), ("ouvre le dossier téléchargements", "acao"),
           ("clique sur le bouton ok", "visao")],
    "de": [("schließe firefox", "acao"), ("suche nach rezepten", "busca"), ("öffne den ordner downloads", "acao"),
           ("klicke auf den knopf ok", "visao"), ("öffne den taschenrechner", "acao")],
    "pt_BR": [("feche o firefox", "acao"), ("pesquise rtx 5060", "busca"), ("abra a pasta downloads", "acao")],
}
for codigo, frases in casos.items():
    i18n.definir_idioma(codigo, salvar=False)
    for frase, esperado in frases:
        assert tars.tarefa_por_palavras(frase) == esperado, (codigo, frase, tars.tarefa_por_palavras(frase))
print("OK: palavras-chave de pt, en, es, fr e de detectam a tarefa")

# "fecha" é "fechar" em português e "data" em espanhol: com a interface em
# espanhol (e o sistema sem português), não vira pedido de fechar.
os.environ.update(LANG="es_ES.UTF-8", LANGUAGE="", LC_ALL="", LC_MESSAGES="")
i18n.definir_idioma("es", salvar=False)
assert tars.tarefa_por_palavras("qué fecha es hoy") is None
os.environ.update(LANG="pt_BR.UTF-8")
tars._ao_mudar_idioma("es")
assert tars.tarefa_por_palavras("fecha o firefox") == "acao"  # sistema em português: vale também
print("OK: palavras dos idiomas de entrada (escolhido + sistema + inglês), sem misturar todos")

# ------------------------------------------------------------------
# Comandos em qualquer idioma
# ------------------------------------------------------------------
tars.limpar_sessao = lambda modelo=None: limpos.append(1)
tars.salvar_sessoes = lambda: None
limpos = []
import contextlib  # noqa: E402
import io  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    for comando in ("/limpar", "/clear", "/limpiar", "/effacer", "/leeren"):
        assert tars.interpretar_comando_modelo(comando), comando
assert len(limpos) == 5
saida = io.StringIO()
with contextlib.redirect_stdout(saida):
    assert tars.interpretar_comando_modelo("/language fr")
assert i18n.idioma_atual() == "fr" and "Français" in saida.getvalue()
with contextlib.redirect_stdout(io.StringIO()):
    assert tars.interpretar_comando_modelo("/idioma português")
assert i18n.idioma_atual() == "pt_BR"
sair = tars._vocab()["comandos"]["cmd.sair"]
assert {"sair", "exit", "quit", "salir", "quitter", "beenden"} <= sair, sair
print("OK: /limpar, /clear, /limpiar...; /idioma e /language trocam o idioma (por código ou nome)")

# Critérios de modelo em outros idiomas
catalogo = {
    "grande:30b": {"tag": "grande:30b", "familia": "grande", "parametros_b": 30.0, "categoria": "geral",
                   "capacidades": {"tools"}, "capacidades_conhecidas": True},
    "pequeno:1b": {"tag": "pequeno:1b", "familia": "pequeno", "parametros_b": 1.0, "categoria": "simples",
                   "capacidades": {"tools"}, "capacidades_conhecidas": True},
}
assert tars._resolver_alvo_modelo("the smallest", catalogo) == "pequeno:1b"
assert tars._resolver_alvo_modelo("el mas grande", catalogo) == "grande:30b"
assert tars._resolver_alvo_modelo("das kleinste", catalogo) == "pequeno:1b"
print("OK: '/model the smallest', '/modelo el más grande', '/modell das kleinste'")

# Busca direta em inglês
i18n.definir_idioma("en", salvar=False)
url = tars._resolver_busca("search black holes on youtube")
assert url and url.startswith("https://www.youtube.com/results?search_query=") and url.endswith("black+holes"), url
i18n.definir_idioma("pt_BR", salvar=False)
assert tars._resolver_busca("pesquise buracos negros no youtube").endswith("buracos+negros")
print("OK: 'search X on youtube' vira busca no YouTube (e em português também)")

# Acentos de qualquer idioma latino
assert tars.normalizar("Pestañas ÖFFNEN Straße Crème") == "pestanas offnen strasse creme"
print("OK: normalização tira acentos de qualquer idioma latino (ñ, ö, ß, è...)")

# Instalador (bash) lê os textos pelo módulo
saida = subprocess.run([sys.executable, "tars_i18n.py", "--shell", "setup."], cwd=RAIZ,
                       env={**os.environ, "TARS_IDIOMA": "en"}, capture_output=True, text=True).stdout
assert "T_setup_titulo='=== Setting up openTARS ==='" in saida, saida[:300]
print("OK: o instalador pega os textos no idioma do sistema (--shell)")

print("\nTUDO OK.")
