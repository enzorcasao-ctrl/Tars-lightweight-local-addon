"""Clique pelo NOME, com a calculadora do GNOME de verdade.

A IA pede click_element("7"), ("+"), ("2"), ("=") e recebe de volta o que
o visor mostra — sem print, sem coordenada, sem modelo de visão.

Precisa de: display (Xvfb serve), D-Bus de sessão, gnome-calculator e a
biblioteca de acessibilidade (gir1.2-atspi-2.0). Sem isso, é pulado."""

import os
import shutil
import subprocess
import sys
import time

from _util import carregar_tars

if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    print("PULADO: precisa de DISPLAY e D-Bus de sessão")
    sys.exit(0)
if not shutil.which("gnome-calculator"):
    print("PULADO: gnome-calculator não instalado")
    sys.exit(0)

tars = carregar_tars()
if tars.acess.indisponivel():
    print(f"PULADO: acessibilidade indisponível ({tars.acess.indisponivel()})")
    sys.exit(0)

calc = subprocess.Popen(
    ["gnome-calculator"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    env={**os.environ, "GSK_RENDERER": "cairo", "LANGUAGE": "en", "LC_ALL": "C.UTF-8"},
)
try:
    limite = time.time() + 20
    while time.time() < limite and not any("calc" in (j["classe"] + j["titulo"]).lower() for j in tars.listar_janelas_x() or []):
        time.sleep(0.3)
    time.sleep(1.5)  # a árvore de acessibilidade termina de montar

    # 1. O que dá pra clicar e o que a janela mostra
    r = tars.executar_ferramenta("list_elements", {"window": "calculator"})
    assert r["sucesso"], r
    botoes = r["elementos"]["button"]
    assert {"7", "2", "+", "=", "C"} <= set(botoes), botoes
    print(f"OK: list_elements vê {len(botoes)} botões da calculadora ({', '.join(botoes[:6])}...)")

    # 2. 7 + 2 = pelo nome, sem print
    inicio = time.time()
    for nome in ("7", "+", "2", "="):
        r = tars.executar_ferramenta("click_element", {"name": nome, "window": "calculator"})
        assert r["sucesso"], r
    tempo = time.time() - inicio
    assert "9" in r["textos"], r
    assert "The window now shows: 9" in r["mensagem"], r["mensagem"]
    print(f"OK: 7 + 2 = clicados pelo nome em {tempo:.2f}s, e a ferramenta devolve o visor: {r['textos'][0]}")

    # 3. A IA escreve "*"; a calculadora mostra "×"
    for nome in ("C", "6", "*", "7", "="):
        r = tars.executar_ferramenta("click_element", {"name": nome, "window": "calculator"})
        assert r["sucesso"], (nome, r)
    assert "42" in r["textos"], r
    print("OK: '*' vira o botão '×' (6 × 7 = 42)")

    # 4. Campo de texto: escreve direto no visor
    r = tars.executar_ferramenta("type_in_element", {"text": "5*5", "window": "calculator"})
    assert r["sucesso"], r
    r = tars.executar_ferramenta("click_element", {"name": "=", "window": "calculator"})
    assert "25" in r["textos"], r
    print("OK: type_in_element escreve no campo e = calcula (25)")

    # 5. Nome errado: explica e sugere nomes que existem
    r = tars.executar_ferramenta("click_element", {"name": "banana", "window": "calculator"})
    assert not r["sucesso"] and "No element named 'banana'" in r["mensagem"] and "list_elements" in r["mensagem"], r
    r = tars.executar_ferramenta("click_element", {"name": "OK", "window": "programa-que-nao-existe"})
    assert not r["sucesso"] and "not open" in r["mensagem"], r
    print("OK: elemento ou app inexistente vira uma explicação útil pra IA")

    # 6. O que a IA recebe: JSON com chaves em inglês
    ia = tars._para_ia(tars.executar_ferramenta("list_elements", {"window": "calculator", "filter": "="}))
    assert ia["success"] and ia["elements"] == {"button": ["="]} and "texts" in ia, ia
    print("OK: resultado vai pra IA em inglês (success, elements, texts), com filtro")
finally:
    calc.terminate()
    try:
        calc.wait(5)
    except subprocess.TimeoutExpired:
        calc.kill()

print("\nTUDO OK.")
