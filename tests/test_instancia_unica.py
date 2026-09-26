"""Instância única: abrir o openTARS de novo (menu, atalho global) só
manda um recado pra janela que já está aberta — é o que deixa a barra
rápida instantânea e evita duas janelas brigando pelo mesmo histórico."""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="opentars-runtime-")
os.environ["DISPLAY"] = os.environ.get("DISPLAY") or ":99"

import tars_instancia  # noqa: E402

assert not tars_instancia.enviar("rapido"), "ninguém aberto: o recado não pode 'dar certo'"

primeira = tars_instancia.Servidor()
assert primeira.travar()
segunda = tars_instancia.Servidor()
assert not segunda.travar(), "a segunda abertura tem que perceber que já existe uma instância"
print("OK: o cadeado diz quem é a instância (a segunda abertura percebe)")

recebidos = []
chegou = threading.Event()
primeira.escutar(lambda comando, id_inicio: (recebidos.append((comando, id_inicio)), chegou.set()))
inicio = time.time()
assert tars_instancia.enviar("rapido", "gnome-shell-42-_TIME123456")
chegou.wait(2)
assert recebidos == [("rapido", "gnome-shell-42-_TIME123456")], recebidos
print(f"OK: o recado chega em {1000 * (time.time() - inicio):.1f} ms, com o id de inicialização do atalho")

assert not tars_instancia.enviar("rm -rf /"), "só comandos conhecidos"
assert tars_instancia.enviar("mostrar")
time.sleep(0.2)
assert recebidos[-1] == ("mostrar", None) and len(recebidos) == 2
modo = os.stat(primeira.caminho_socket).st_mode & 0o777
assert modo == 0o600, oct(modo)
print("OK: só 'mostrar' e 'rapido' são aceitos; o socket é só do usuário (600)")

primeira.fechar()
assert not tars_instancia.enviar("rapido")
terceira = tars_instancia.Servidor()
assert terceira.travar(), "fechou: a próxima abertura vira a instância"
terceira.fechar()
print("OK: ao fechar, o cadeado e o socket somem")

print("\nTUDO OK.")
