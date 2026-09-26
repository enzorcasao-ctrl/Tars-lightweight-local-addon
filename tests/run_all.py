#!/usr/bin/env python3
"""Roda todos os test_*.py desta pasta, um por um, num subprocesso
(cada teste carrega o tars.py do zero, então não interferem entre si).
Uso: python3 tests/run_all.py
"""

import subprocess
import sys
from pathlib import Path

PASTA = Path(__file__).parent

arquivos = sorted(PASTA.glob("test_*.py"))

falhas = []

for arquivo in arquivos:
    print(f"\n{'=' * 60}\n{arquivo.name}\n{'=' * 60}")

    resultado = subprocess.run([sys.executable, str(arquivo)], cwd=PASTA)

    if resultado.returncode != 0:
        falhas.append(arquivo.name)

print(f"\n{'=' * 60}")

if falhas:
    print(f"FALHARAM ({len(falhas)}/{len(arquivos)}): {', '.join(falhas)}")
    sys.exit(1)

print(f"TODOS OS {len(arquivos)} TESTES PASSARAM.")
