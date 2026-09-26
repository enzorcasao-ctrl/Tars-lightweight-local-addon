"""
Instância única da janela do openTARS (só biblioteca padrão: carrega em
milissegundos).

Abrir o openTARS de novo (menu, atalho global) não cria uma segunda
janela brigando pelo mesmo histórico: o novo processo manda um recado
pra instância que já está aberta ("mostrar" ou "rapido") e sai na hora.
É isso que deixa o atalho global instantâneo.

- um cadeado (flock) diz quem é a instância: some sozinho se ela cair;
- o recado vai por um socket Unix na pasta de runtime do usuário
  ($XDG_RUNTIME_DIR, só ele acessa), um por tela (DISPLAY).
"""

import fcntl
import os
import re
import socket
import threading
from pathlib import Path

COMANDOS = ("mostrar", "rapido")


def _pasta():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        return Path(base)
    pasta = Path(f"/tmp/opentars-{os.getuid()}")
    pasta.mkdir(mode=0o700, exist_ok=True)
    return pasta


def _caminhos():
    tela = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or "sem-tela"
    tela = re.sub(r"[^A-Za-z0-9_.-]", "_", tela)
    pasta = _pasta()
    return pasta / f"opentars-{tela}.sock", pasta / f"opentars-{tela}.lock"


def enviar(comando, id_inicio=None, timeout=1.5):
    """Manda o recado pra instância aberta. True se ela respondeu."""
    caminho_socket, _ = _caminhos()
    linha = comando if not id_inicio else f"{comando}\t{id_inicio}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(caminho_socket))
            s.sendall(linha.encode("utf-8", "replace") + b"\n")
            return s.recv(8).startswith(b"ok")
    except OSError:
        return False


class Servidor:
    """Segura o cadeado e atende os recados das próximas aberturas."""

    def __init__(self):
        self._arquivo_trava = None
        self._socket = None
        self._ao_receber = None
        self.caminho_socket, self.caminho_trava = _caminhos()

    def travar(self):
        """True se este processo é a instância (ninguém mais segura o cadeado)."""
        try:
            arquivo = open(self.caminho_trava, "a+")
        except OSError:
            return True  # sem como travar: segue sem instância única
        try:
            fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            arquivo.close()
            return False
        self._arquivo_trava = arquivo
        return True

    def escutar(self, ao_receber):
        """ao_receber(comando, id_inicio) roda numa thread separada."""
        self._ao_receber = ao_receber
        try:
            self.caminho_socket.unlink()
        except OSError:
            pass
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(str(self.caminho_socket))
            os.chmod(self.caminho_socket, 0o600)
            s.listen(4)
        except OSError:
            return False
        self._socket = s
        threading.Thread(target=self._atender, daemon=True).start()
        return True

    def _atender(self):
        while self._socket is not None:
            try:
                conexao, _ = self._socket.accept()
            except OSError:
                return
            with conexao:
                try:
                    conexao.settimeout(1.0)
                    dados = b""
                    while not dados.endswith(b"\n") and len(dados) < 1024:
                        pedaco = conexao.recv(256)
                        if not pedaco:
                            break
                        dados += pedaco
                    comando, _, id_inicio = dados.decode("utf-8", "replace").strip().partition("\t")
                    if comando in COMANDOS:
                        conexao.sendall(b"ok\n")
                        if self._ao_receber:
                            self._ao_receber(comando, id_inicio or None)
                except OSError:
                    continue

    def fechar(self):
        s, self._socket = self._socket, None
        if s is not None:
            try:
                s.close()
                self.caminho_socket.unlink()
            except OSError:
                pass
        if self._arquivo_trava is not None:
            try:
                self._arquivo_trava.close()
            except OSError:
                pass
            self._arquivo_trava = None
