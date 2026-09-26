"""opentars --diagnostico / --autoteste: o relatório que o usuário roda no
PC dele. Aqui o Ollama, a IA e a calculadora são simulados; o que se testa
é que o relatório não quebra em nenhuma situação e que o teste da
calculadora confere o VISOR (não só o que a IA diz)."""

import contextlib
import io
import os
import tempfile

os.environ["TARS_ARQUIVO_AUTOTESTE"] = os.path.join(tempfile.mkdtemp(), "relatorio.txt")

from _util import carregar_tars  # noqa: E402

tars = carregar_tars()
import tars_autoteste  # noqa: E402


def rodar(completo):
    saida = io.StringIO()
    with contextlib.redirect_stdout(saida):
        codigo = tars_autoteste.executar(tars, completo=completo)
    return codigo, saida.getvalue()


# 1. Nada funcionando (sem Ollama, sem tela): relata, não quebra
tars._SESSION.get = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("recusado"))
tars.ollama_online = lambda: False
codigo, texto = rodar(completo=True)
assert codigo == 1 and "Ollama" in texto and "não está respondendo" in texto, texto
assert "sem Ollama ou sem modelo de conversa" in texto
print("OK: sem Ollama, o relatório aponta o problema (e não quebra)")


# 2. Ollama e modelos no ar; a IA "abre a calculadora e clica"
class Resp:
    status_code = 200

    def json(self):
        return {"version": "0.12.3"}


CATALOGO = {
    "qwen3:8b": {"tag": "qwen3:8b", "familia": "qwen3", "parametros_b": 8.2, "categoria": "geral",
                 "vram_estimado_gb": 5.5, "capacidades": {"tools", "thinking"}, "capacidades_conhecidas": True},
    "qwen2.5:0.5b": {"tag": "qwen2.5:0.5b", "familia": "qwen2", "parametros_b": 0.5, "categoria": "simples",
                     "vram_estimado_gb": 0.9, "capacidades": {"tools"}, "capacidades_conhecidas": True},
}
tars._SESSION.get = lambda *a, **k: Resp()
tars.catalogar_modelos = lambda forcar=False: CATALOGO
tars.listar_modelos_instalados = lambda forcar=False: set(CATALOGO)
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 6.0, "vram_livre_gb": 5.0, "fabricante": "NVIDIA"}
tars.medir_classificador = lambda frases: {"n": 34, "acertos_ia": 30, "acertos_total": 32, "ms_medio": 120.0,
                                           "por_categoria": {}, "erros": [("frase x", "busca", "geral")]}
tars.SESSAO_GRAFICA = "x11"
tars._buscar_app_no_sistema = lambda nome: {"tipo": "desktop", "nome": "Calculadora", "comando": ["gnome-calculator"]}
tars._capturar_tela = lambda: __import__("PIL.Image", fromlist=["Image"]).new("RGB", (1920, 1080), (40, 90, 160))
tars.listar_janelas_x = lambda: [{"id": 1, "titulo": "Calculadora", "classe": "gnome-calculator", "pid": 9}]
tars._termos_para_janela = lambda nome: [nome]
tars._pids_protegidos = lambda: set()
fechados = []
tars.fechar_aplicativo = lambda nome: fechados.append(nome) or {"sucesso": True}
visor = {"textos": ["9"]}
tars.acess.indisponivel = lambda: None
tars.acess.janelas = lambda: [{"app": "gnome-calculator", "titulo": "Calculadora", "pid": 9, "ativa": True, "no": None}]
tars.acess.achar_janela = lambda **k: {"titulo": "Calculadora", "no": None}
tars.acess.listar = lambda janela, filtro=None: {"elementos": {}, "textos": visor["textos"], "total": 0}

conversa_antes = list(tars.conversa)
arquivo_antes = tars.ARQUIVO_SESSOES


def ia_que_acerta(texto):
    assert "calculadora" in texto.lower() and tars.ARQUIVO_SESSOES != arquivo_antes, "o teste não usa o histórico real"
    for nome in ("7", "+", "2", "="):
        tars.executar_ferramenta("click_element", {"name": nome, "window": "calculadora"})
    return "O resultado é 9."


tars._DISPATCH_FERRAMENTAS["click_element"] = lambda a: {"sucesso": True, "mensagem": "SUCCESS"}
tars.processar_mensagem = ia_que_acerta
codigo, texto = rodar(completo=True)
assert codigo == 0, texto
assert "passou (o visor mostra 9)" in texto and "click_element×4" in texto, texto
assert "32/34 (94%)" in texto and "0.12.3" in texto and "NVIDIA 6.0 GB" in texto, texto
assert fechados == ["calculadora"] and tars.conversa == conversa_antes and tars.ARQUIVO_SESSOES == arquivo_antes
relatorio = open(os.environ["TARS_ARQUIVO_AUTOTESTE"], encoding="utf-8").read()
assert "passou" in relatorio and "\033[" not in relatorio and "frase x | busca | geral" in relatorio
print("OK: IA clicou 7 + 2 =, o visor mostra 9: passou (relatório em texto puro, histórico intacto)")

# 3. A IA diz "9", mas o visor não mostra: não passa
visor["textos"] = ["7+"]
codigo, texto = rodar(completo=True)
assert codigo == 1 and "não chegou no 9 (visor: 7+)" in texto, texto
print("OK: a IA dizer '9' não basta: o teste confere o visor")

# 4. Diagnóstico rápido não abre nada
tars.processar_mensagem = lambda texto: (_ for _ in ()).throw(AssertionError("o diagnóstico não pode chamar a IA"))
codigo, texto = rodar(completo=False)
assert "Calculadora 7 + 2" not in texto and "Clique pelo nome" in texto and "gnome-calculator" in texto, texto
print("OK: --diagnostico só confere o ambiente (não abre a calculadora)")

print("\nTUDO OK.")
