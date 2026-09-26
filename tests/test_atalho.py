"""Atalho global: o openTARS cadastra "opentars-gui --rapido" como atalho
personalizado do ambiente (GNOME/Zorin, Cinnamon, MATE, XFCE), sem pisar
num atalho que já existe. Aqui o gsettings/xfconf são simulados."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _util  # noqa: E402,F401  (idioma pt_BR e config temporária)

import tars_atalho as at  # noqa: E402
import tars_i18n as i18n  # noqa: E402

# ------------------------------------------------------------------
# Combinações
# ------------------------------------------------------------------
assert at.normalizar_combinacao("Ctrl+Alt+Space") == "<Primary><Alt>space"
assert at.normalizar_combinacao("<Control><Alt>space") == "<Primary><Alt>space"
assert at.normalizar_combinacao("super+t") == "<Super>t"
assert at.normalizar_combinacao("ctrl+alt+espaço") == "<Primary><Alt>space"
assert at.normalizar_combinacao("alt+F2") == "<Alt>F2"
assert at.normalizar_combinacao("banana") is None and at.normalizar_combinacao("ctrl+") is None
assert at.legivel("<Primary><Alt>space") == "Ctrl+Alt+Espaço"
i18n.definir_idioma("en", salvar=False)
assert at.legivel("<Primary><Alt>space") == "Ctrl+Alt+Space"
i18n.definir_idioma("pt_BR", salvar=False)
print("OK: 'Ctrl+Alt+Space', '<Control><Alt>space', 'ctrl+alt+espaço' -> <Primary><Alt>space")


# ------------------------------------------------------------------
# gsettings / xfconf de mentira
# ------------------------------------------------------------------
class Sistema:
    def __init__(self, esquemas, recursivos=None):
        self.esquemas = esquemas
        self.valores = {}
        self.recursivos = recursivos or {}
        self.xfconf = {}

    def padrao(self, esquema, chave):
        if chave in ("custom-keybindings", "custom-list") or (chave == "binding" and "cinnamon" in esquema):
            return "@as []"
        return "''"

    def rodar(self, *cmd):
        if cmd[0] == "gsettings":
            acao = cmd[1]
            if acao == "list-schemas":
                return 0, "\n".join(self.esquemas)
            if acao == "get":
                return 0, self.valores.get((cmd[2], cmd[3]), self.padrao(cmd[2], cmd[3]))
            if acao == "set":
                self.valores[(cmd[2], cmd[3])] = cmd[4]
                return 0, ""
            if acao == "reset":
                self.valores.pop((cmd[2], cmd[3]), None)
                return 0, ""
            if acao == "reset-recursively":
                for k in [k for k in self.valores if k[0] == cmd[2]]:
                    del self.valores[k]
                return 0, ""
            if acao == "list-recursively":
                linhas = self.recursivos.get(cmd[2])
                return (0, linhas) if linhas is not None else (1, "")
        if cmd[0] == "xfconf-query":
            args = list(cmd[3:])
            if args[:2] == ["-l", "-v"]:
                return 0, "\n".join(f"{k}  {v}" for k, v in self.xfconf.items())
            caminho = args[1]
            if "-s" in args:
                self.xfconf[caminho] = args[args.index("-s") + 1]
                return 0, ""
            if "-r" in args:
                self.xfconf.pop(caminho, None)
                return 0, ""
            return (0, self.xfconf[caminho]) if caminho in self.xfconf else (1, "")
        return 1, ""


def lista(texto):
    return re.findall(r"'([^']*)'", texto)


at.shutil.which = lambda nome: f"/usr/bin/{nome}"

# ------------------------------------------------------------------
# GNOME / Zorin
# ------------------------------------------------------------------
os.environ["XDG_CURRENT_DESKTOP"] = "zorin:GNOME"
assert at.ambiente() == "gnome"
gnome = Sistema(
    [at.GNOME_SCHEMA, "org.gnome.desktop.wm.keybindings"],
    {"org.gnome.desktop.wm.keybindings":
         "org.gnome.desktop.wm.keybindings switch-input-source ['<Super>space', 'XF86Keyboard']\n"
         "org.gnome.desktop.wm.keybindings activate-window-menu ['<Alt>space']"},
)
# o usuário já tem um atalho dele
gnome.valores[(at.GNOME_SCHEMA, "custom-keybindings")] = f"['{at.GNOME_CAMINHO}custom0/']"
seu = f"{at.GNOME_SCHEMA}.custom-keybinding:{at.GNOME_CAMINHO}custom0/"
gnome.valores[(seu, "command")] = "'flameshot gui'"
gnome.valores[(seu, "binding")] = "'Print'"
at._rodar = gnome.rodar

ok, mensagem = at.registrar()
assert ok, mensagem
nosso = f"{at.GNOME_SCHEMA}.custom-keybinding:{at.GNOME_CAMINHO}custom1/"
assert lista(gnome.valores[(at.GNOME_SCHEMA, "custom-keybindings")]) == [f"{at.GNOME_CAMINHO}custom0/", f"{at.GNOME_CAMINHO}custom1/"]
assert "--rapido" in gnome.valores[(nosso, "command")] and gnome.valores[(nosso, "binding")] == "'<Primary><Alt>space'"
assert gnome.valores[(seu, "command")] == "'flameshot gui'", "o atalho do usuário continua lá"
assert "Ctrl+Alt+Espaço" in mensagem and at.estado()["registrado"]
print("OK: GNOME/Zorin: cadastra Ctrl+Alt+Espaço num atalho novo, sem mexer nos do usuário")

ok, mensagem = at.registrar("super+space")
assert not ok and "switch-input-source" in mensagem, mensagem
print("OK: Super+Espaço é recusado: no GNOME já troca o layout do teclado")

gnome.valores[(seu, "binding")] = "'<Primary><Alt>t'"
ok, mensagem = at.registrar("ctrl+alt+t")
assert not ok and "flameshot" in mensagem, mensagem
ok, _ = at.registrar("ctrl+alt+o")
assert ok and gnome.valores[(nosso, "binding")] == "'<Primary><Alt>o'"
assert len(lista(gnome.valores[(at.GNOME_SCHEMA, "custom-keybindings")])) == 2, "trocar não duplica"
print("OK: combinação de outro atalho é recusada; trocar a nossa não cria outra entrada")

ok, _ = at.remover()
assert ok and lista(gnome.valores[(at.GNOME_SCHEMA, "custom-keybindings")]) == [f"{at.GNOME_CAMINHO}custom0/"]
assert (nosso, "command") not in gnome.valores and at.garantir_registrado() is None
print("OK: --atalho off remove só o nosso, e a janela não cadastra de novo sozinha")

# ------------------------------------------------------------------
# Primeira abertura da janela: cadastra uma vez só
# ------------------------------------------------------------------
os.environ.pop("TARS_SEM_ATALHO", None)
i18n.salvar_config(atalho_tentado=False, atalho_desligado=False, atalho=None)
gnome.valores = {}
assert at.garantir_registrado()[0] is True
assert at.garantir_registrado() is None
print("OK: na primeira abertura a janela cadastra o atalho; depois não tenta de novo")

# ------------------------------------------------------------------
# Cinnamon, XFCE e KDE
# ------------------------------------------------------------------
os.environ["XDG_CURRENT_DESKTOP"] = "X-Cinnamon"
cinnamon = Sistema([at.CINNAMON_SCHEMA])
at._rodar = cinnamon.rodar
ok, _ = at.registrar("ctrl+alt+space")
nosso = f"{at.CINNAMON_SCHEMA}.custom-keybinding:{at.CINNAMON_CAMINHO}custom0/"
assert ok and lista(cinnamon.valores[(at.CINNAMON_SCHEMA, "custom-list")]) == ["custom0"]
assert lista(cinnamon.valores[(nosso, "binding")]) == ["<Primary><Alt>space"]
print("OK: Cinnamon (Linux Mint)")

os.environ["XDG_CURRENT_DESKTOP"] = "XFCE"
xfce = Sistema([])
at._rodar = xfce.rodar
xfce.xfconf["/commands/custom/<Primary><Alt>t"] = "xfce4-terminal"
ok, mensagem = at.registrar("ctrl+alt+t")
assert not ok and "xfce4-terminal" in mensagem
ok, _ = at.registrar("ctrl+alt+space")
assert ok and "--rapido" in xfce.xfconf["/commands/custom/<Primary><Alt>space"]
at.remover()
assert "/commands/custom/<Primary><Alt>space" not in xfce.xfconf and "/commands/custom/<Primary><Alt>t" in xfce.xfconf
print("OK: XFCE (respeita o Ctrl+Alt+T do terminal)")

os.environ["XDG_CURRENT_DESKTOP"] = "KDE"
ok, mensagem = at.registrar()
assert not ok and "opentars-gui --rapido" in mensagem and "KDE" in mensagem
print("OK: KDE e outros: explica o comando pra cadastrar à mão")

print("\nTUDO OK.")
