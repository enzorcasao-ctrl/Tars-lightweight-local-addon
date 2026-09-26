#!/usr/bin/env python3
"""Gera os atalhos .desktop a partir dos modelos (*.desktop.in) com os
textos de todos os idiomas (idiomas/*.json): a linha "Chave=@texto@" vira
"Chave=<inglês>" + "Chave[pt_BR]=...", "Chave[es]=..." etc. Assim o menu
de aplicativos mostra o openTARS no idioma do sistema, e nenhum texto fica
escrito à mão no atalho.

Uso: gerar_desktop.py <pasta dos modelos> <pasta de saída>"""

import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LINHA = re.compile(r"^([A-Za-z]+)=@([a-z_.]+)@$")


def main(origem, destino):
    catalogos = {}
    for arquivo in sorted((RAIZ / "idiomas").glob("*.json")):
        catalogos[arquivo.stem] = json.loads(arquivo.read_text(encoding="utf-8"))
    base = catalogos["en"]
    destino.mkdir(parents=True, exist_ok=True)
    for modelo in sorted(origem.glob("*.desktop.in")):
        saida = []
        for linha in modelo.read_text(encoding="utf-8").splitlines():
            m = LINHA.match(linha)
            if not m:
                saida.append(linha)
                continue
            chave, texto = m.groups()
            padrao = base[texto]
            saida.append(f"{chave}={padrao}")
            for codigo, cat in catalogos.items():
                valor = cat.get(texto)
                if codigo == "en" or not valor or valor == padrao:
                    continue
                principal = cat.get("_locale", [codigo])[0]
                for local in dict.fromkeys([principal, principal.split("_")[0]]):
                    if local != "en":
                        saida.append(f"{chave}[{local}]={valor}")
        (destino / modelo.name[:-3]).write_text("\n".join(saida) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
