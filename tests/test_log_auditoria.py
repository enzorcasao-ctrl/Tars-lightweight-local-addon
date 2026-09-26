import os
import tempfile

tmpdir = tempfile.mkdtemp()
os.environ["TARS_DIR_LOG"] = tmpdir

from _util import carregar_tars
tars = carregar_tars()

tars.executar_ferramenta("get_current_directory", {})

for h in tars.LOG.handlers:
    h.flush()

conteudo = open(tars.ARQUIVO_LOG, encoding="utf-8").read()
assert "ferramenta=get_current_directory" in conteudo
assert "sucesso=True" in conteudo
print("OK: log de auditoria registrou a ferramenta executada")
print(conteudo)
