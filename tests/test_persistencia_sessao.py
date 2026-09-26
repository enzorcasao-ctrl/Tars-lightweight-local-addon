import os
import json
import tempfile

tmpfile = tempfile.mktemp(suffix=".json")
os.environ["TARS_ARQUIVO_SESSOES"] = tmpfile

from _util import carregar_tars
tars = carregar_tars()

# Uma conversa só, para qualquer modelo
a = tars.obter_sessao("qwen3:8b")
b = tars.obter_sessao("gemma3:4b")
assert a is b, "todos os modelos devem compartilhar a mesma conversa"
a.append({"role": "user", "content": "abra o firefox"})
a.append({"role": "assistant", "content": "Abri."})
a.append({"role": "user", "content": "[Mensagem automática] print", "images": ["BASE64ENORME..."]})
tars.salvar_sessoes()

bruto = json.loads(open(tmpfile).read())
assert bruto["versao"] == 2 and not any("images" in m for m in bruto["conversa"]), bruto
assert all(m["role"] != "system" for m in bruto["conversa"])
print("OK: conversa única salva sem imagens e sem o prompt de sistema")

carregada = tars.carregar_sessoes()
assert carregada[0]["role"] == "system" and carregada[0]["content"].startswith(tars.SYSTEM_PROMPT.split("{sair}")[0])
assert "THIS COMPUTER" in carregada[0]["content"]
assert [m["content"] for m in carregada if m["role"] == "user"][0] == "abra o firefox"
print("OK: recarregada com o prompt atual + contexto do computador")

# Formato antigo (um histórico por modelo): usa o maior
json.dump({"qwen3:8b": [{"role": "user", "content": "x"}],
           "gemma3:4b": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]},
          open(tmpfile, "w"))
migrada = tars.carregar_sessoes()
assert [m["content"] for m in migrada[1:]] == ["a", "b"], migrada
print("OK: histórico do formato antigo (1.x) é aproveitado")

# /limpar
tars.iniciar_conversa()
tars.limpar_sessao()
assert len(tars.conversa) == 1 and tars.carregar_sessoes() == []
print("OK: /limpar apaga a conversa do disco")

# Poda: nunca começa no meio de uma tarefa, encurta resultados antigos
msgs = [{"role": "system", "content": "s"}]
for i in range(20):
    msgs += [
        {"role": "user", "content": f"pedido {i}"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "execute_terminal"}}]},
        {"role": "tool", "content": "x" * 5000},
        {"role": "assistant", "content": "feito"},
    ]
tars._podar_historico(msgs)
assert len(msgs) - 1 <= tars.LIMITE_HISTORICO_MENSAGENS
assert msgs[1]["role"] == "user" and msgs[1]["content"].startswith("pedido"), msgs[1]
assert all(len(m["content"]) < 700 for m in msgs[:-4] if m["role"] == "tool")
assert len(msgs[-2]["content"]) == 5000, "resultado do último pedido fica inteiro"
print("OK: poda corta no começo de um pedido e encurta resultados antigos")

os.remove(tmpfile)
print("\nTUDO OK.")
