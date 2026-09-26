"""A janela recebe do núcleo EVENTOS estruturados (ferramenta, modelo
escolhido, streaming) e transforma em conversa legível — sem ler o texto
impresso, que muda com o idioma. O que o núcleo ainda imprime (listagens,
avisos) vira estilo pela cor. Não precisa de display: só as funções de
formatação."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _util import carregar_tars  # noqa: E402

sys.modules["tars"] = carregar_tars()

try:
    import tars_gui  # noqa: E402
except ImportError as e:  # sem tkinter
    print(f"PULADO: {e}")
    sys.exit(0)

core = sys.modules["tars"]
c, Cor = core.c, core.Cor
ev = tars_gui.formatar_evento

assert ev("ferramenta", {"nome": "open_application", "resumo": "calculadora"}) == [
    ("▸ ", "ferramenta_icone"), ("abrindo app", "ferramenta"), ("  calculadora", "ferramenta_detalhe")]
assert ev("ferramenta", {"nome": "click_element", "resumo": "7"})[1] == ("clicando em", "ferramenta")
assert ev("ferramenta_fim", {"nome": "open_application", "sucesso": True, "segundos": 0.42}) == [("   ✓ 0.42s\n", "ferramenta_ok")]
assert ev("ferramenta_fim", {"nome": "close_application", "sucesso": False, "segundos": 0.1}) == [("   ✗ falhou\n", "erro")]
print("OK: ferramentas viram 'abrindo app  calculadora ✓ 0.42s'")

assert ev("tarefa", {"modo": "AUTO", "tarefa": "acao", "modelo": "qwen3:8b"}) == [("\nqwen3:8b · tarefa ação\n", "meta")]
assert ev("tarefa", {"modo": "MANUAL", "tarefa": None, "modelo": "qwen3:8b"}) == [("\nqwen3:8b · escolhido por você\n", "meta")]
assert ev("tempo", {"modo": "AUTO", "modelo": "qwen3:8b", "segundos": 7.4}) == [("respondeu em 7.4s", "tempo")]
assert ev("resposta", {"texto": "Oi", "inicio": True}) == [("\nopenTARS\n", "rotulo_ia"), ("Oi", None)]
assert ev("resposta", {"texto": " tudo", "inicio": False}) == [(" tudo", None)]
assert ev("pensamento", {"texto": "hmm", "inicio": True}) == [("\npensando  ", "pensamento_rotulo"), ("hmm", "pensamento")]
assert ev("carregando", {"modelo": "qwen3:8b", "segundo_plano": False}) == [("carregando qwen3:8b…\n", "suave")]
assert ev("carregado", {"modelo": "qwen3:8b", "segundos": 1.26}) == [("qwen3:8b pronto (1.3s)\n", "suave")]
assert ev("analisando", {"modelo": "qwen3:8b"}) == [] and ev("descarregando", {"modelo": "x"}) == []
print("OK: modelo escolhido, tempo, rótulo da IA, raciocínio e carregamento")

# O mesmo evento em outro idioma: nada no formato depende do texto.
core.i18n.definir_idioma("en", salvar=False)
assert ev("ferramenta", {"nome": "open_application", "resumo": "calculator"})[1] == ("opening app", "ferramenta")
assert ev("tarefa", {"modo": "AUTO", "tarefa": "acao", "modelo": "qwen3:8b"}) == [("\nqwen3:8b · action task\n", "meta")]
assert ev("tempo", {"modo": "AUTO", "modelo": "m", "segundos": 2}) == [("answered in 2.0s", "tempo")]
core.i18n.definir_idioma("pt_BR", salvar=False)
print("OK: em inglês, os mesmos eventos saem em inglês")

f = tars_gui.formatar_saida
assert f(c("╭──────────╮", Cor.CINZA)) == []
assert f(c("│ ", Cor.CINZA) + c("⚠ COMANDO PERIGOSO".ljust(30), Cor.VERMELHO) + c(" │", Cor.CINZA)) == [
    ("  ⚠ COMANDO PERIGOSO\n", "aviso")]
assert f(c("\n✓ Histórico de conversa apagado.", Cor.VERDE)) == [("\n✓ Histórico de conversa apagado.", "ok")]
assert f(c("[openTARS] não consegui salvar", Cor.AMARELO)) == [("não consegui salvar", "aviso")]
assert f("Abri a calculadora.") == [("Abri a calculadora.", None)]
print("OK: texto impresso (listagens, avisos) vira estilo pela cor; bordas somem")

print("\nTUDO OK.")
