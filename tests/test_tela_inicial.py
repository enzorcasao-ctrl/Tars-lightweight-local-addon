import os
import tempfile

os.environ["TARS_ARQUIVO_SESSOES"] = tempfile.mktemp(suffix=".json")
os.environ["TARS_DIR_LOG"] = tempfile.mkdtemp()

from _util import carregar_tars
tars = carregar_tars()

catalogo_fake = {
    "qwen3:0.6b": {"tag": "qwen3:0.6b", "familia": "qwen3", "categoria": "simples", "parametros_b": 0.6, "quantizacao": "Q4_0", "vram_estimado_gb": 1.0, "capacidades": {"tools"}, "capacidades_conhecidas": True, "emoji": "⚡"},
    "qwen3:8b": {"tag": "qwen3:8b", "familia": "qwen3", "categoria": "geral", "parametros_b": 8, "quantizacao": "Q4_0", "vram_estimado_gb": 5.0, "capacidades": {"tools"}, "capacidades_conhecidas": True, "emoji": "🧠"},
    "nomic-embed-text:latest": {"tag": "nomic-embed-text:latest", "familia": "nomic-bert", "categoria": "embedding", "parametros_b": 0.137, "quantizacao": "F16", "vram_estimado_gb": 0.9, "capacidades": {"embedding"}, "capacidades_conhecidas": True, "emoji": "🤖"},
}
tars.catalogar_modelos = lambda forcar=False: catalogo_fake
tars.detectar_gpu = lambda forcar=False: {"vram_total_gb": 6.0, "vram_livre_gb": 4.2}
tars._carregar_em_segundo_plano = lambda modelo: None

# Só confirma que roda sem quebrar e sem duplicar a linha de GPU
import io
import contextlib

buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    tars.tela_inicial()

saida = buffer.getvalue()
assert saida.count("GPU detectada") == 1, "linha de GPU duplicada na tela inicial"
assert "modelo(s) de chat prontos pra uso" in saida
assert "de embedding fora da escolha" in saida
assert "/limpar" in saida
assert str(tars.ARQUIVO_SESSOES) in saida
assert str(tars.ARQUIVO_LOG) in saida
print("OK: tela inicial roda sem duplicar informação e mostra os novos comandos/arquivos")

print("\nTUDO OK.")
