#!/bin/bash
# Monta o .deb do openTARS a partir dos arquivos-fonte.
# Uso: bash empacotamento/build.sh   (gera dist/opentars_<versão>_all.deb)

set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
# Versão tem uma fonte só: a constante VERSAO do tars.py.
VERSAO="$(grep -m1 '^VERSAO = ' "$RAIZ/tars.py" | cut -d'"' -f2)"
[ -n "$VERSAO" ] || { echo "VERSAO não encontrada em tars.py" >&2; exit 1; }
FONTE="$RAIZ/empacotamento"
PKG="$RAIZ/build/opentars_${VERSAO}"
SAIDA="$RAIZ/dist"

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" \
         "$PKG/usr/bin" \
         "$PKG/usr/share/opentars" \
         "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/scalable/apps" \
         "$PKG/usr/share/doc/opentars" \
         "$SAIDA"

# Programa: os módulos tars*.py e os textos de cada idioma
for modulo in "$RAIZ"/tars*.py; do
    install -m 644 "$modulo" "$PKG/usr/share/opentars/$(basename "$modulo")"
done
install -d "$PKG/usr/share/opentars/idiomas"
install -m 644 "$RAIZ"/idiomas/*.json "$PKG/usr/share/opentars/idiomas/"
install -m 755 "$FONTE/setup.sh"     "$PKG/usr/share/opentars/setup.sh"

# Comandos (+ atalhos com o nome antigo)
install -m 755 "$FONTE/bin/opentars"     "$PKG/usr/bin/opentars"
install -m 755 "$FONTE/bin/opentars-gui" "$PKG/usr/bin/opentars-gui"
ln -s opentars     "$PKG/usr/bin/tars"
ln -s opentars-gui "$PKG/usr/bin/tars-gui"

# Menu (os atalhos saem dos modelos, com os nomes em todos os idiomas) e ícone
python3 "$FONTE/gerar_desktop.py" "$FONTE/desktop" "$PKG/usr/share/applications"
chmod 644 "$PKG"/usr/share/applications/*.desktop
install -m 644 "$FONTE/icone/opentars.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/opentars.svg"

# Documentação
install -m 644 "$FONTE/doc/copyright" "$PKG/usr/share/doc/opentars/copyright"
gzip -9n -c "$FONTE/doc/changelog" > "$PKG/usr/share/doc/opentars/changelog.gz"
chmod 644 "$PKG/usr/share/doc/opentars/changelog.gz"

# Controle
TAMANHO=$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
sed -e "s/@VERSAO@/$VERSAO/" -e "s/@TAMANHO@/$TAMANHO/" "$FONTE/debian/control" > "$PKG/DEBIAN/control"
install -m 755 "$FONTE/debian/postinst" "$PKG/DEBIAN/postinst"
install -m 755 "$FONTE/debian/postrm"   "$PKG/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$PKG" "$SAIDA/opentars_${VERSAO}_all.deb"
echo "Gerado: $SAIDA/opentars_${VERSAO}_all.deb"
