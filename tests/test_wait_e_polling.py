import time

from _util import carregar_tars
tars = carregar_tars()

# wait_seconds respeita o teto (LIMITE_ESPERA_SEG) e não trava além disso
inicio = time.time()
r = tars.esperar(999)
decorrido = time.time() - inicio
assert r["sucesso"]
assert decorrido <= tars.LIMITE_ESPERA_SEG + 0.5, decorrido
print(f"OK: wait_seconds(999) foi limitado a ~{decorrido:.1f}s (teto {tars.LIMITE_ESPERA_SEG}s)")

# a ferramenta está registrada e chamável via dispatch
resultado = tars.executar_ferramenta("wait_seconds", {"seconds": 0.05})
assert resultado["sucesso"]
print("OK: wait_seconds acessível via executar_ferramenta:", resultado["mensagem"])

# abrir_aplicativo confirma por polling (processo aparece rápido = retorno rápido)
tars.psutil.process_iter = lambda *a, **k: iter([
    type("P", (), {"info": {"name": "minhaapp", "cmdline": ["minhaapp"]}})()
])
tars.shutil.which = lambda nome: "/usr/bin/minhaapp" if nome == "minhaapp" else None
tars.subprocess.Popen = lambda *a, **k: None

inicio = time.time()
r = tars.abrir_aplicativo("minhaapp")
decorrido = time.time() - inicio
assert r["sucesso"]
assert decorrido < tars.TIMEOUT_POLL_PROCESSO_SEG
print(f"OK: app confirmado rápido via polling ({decorrido:.2f}s, bem abaixo do teto de {tars.TIMEOUT_POLL_PROCESSO_SEG}s)")

print("\nTUDO OK.")
