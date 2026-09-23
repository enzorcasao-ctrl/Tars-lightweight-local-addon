# openTARS

Assistente de IA 100% local para desktop Linux. Roda inteiramente na sua máquina via [Ollama](https://ollama.com) — sem nuvem, sem conta, sem mensalidade.

## O que faz

Você escreve em português, o openTARS executa:

- **Controla o desktop**: abre e fecha aplicativos, clica, digita, move o mouse
- **Pesquisa na web**: "pesquise rtx 5060 no google" abre a busca certa
- **Analisa a tela**: tira um print e descreve o que está vendo
- **Roda comandos**: executa no terminal e mostra o resultado
- **Escolhe o modelo sozinho**: usa, entre os modelos que você já tem no Ollama, o mais adequado para cada tarefa

Nada do que você digita ou mostra sai do seu PC.

## Instalação

Para Ubuntu, Debian, Linux Mint, Zorin OS, Pop!_OS e derivados.

Cole no terminal:

```bash
wget -O /tmp/opentars.deb https://github.com/enzorcasao-ctrl/openTars-lightweight-local-addon/raw/main/opentars_all.deb && sudo apt install -y /tmp/opentars.deb
```

O comando baixa sempre a versão mais nova e instala. O mesmo comando serve para **atualizar**.

Prefere baixar manualmente? Clique em [`opentars_all.deb`](https://github.com/enzorcasao-ctrl/openTars-lightweight-local-addon/raw/main/opentars_all.deb) para baixar e, na pasta onde ele foi salvo (normalmente `~/Downloads`):

```bash
sudo apt install ./opentars_all.deb
```

O instalador cuida do resto e pula o que você já tiver:

- instala o Ollama
- cria um ambiente Python isolado com as dependências
- baixa o modelo ajudante `gemma3:270m` (~300 MB)
- adiciona o openTARS ao menu de aplicativos

Depois, baixe pelo menos um modelo de conversa:

```bash
ollama pull qwen3:8b
```

Quem já tinha a versão 1.0.0 (pacote `tars`) pode instalar por cima: ela é substituída automaticamente e o histórico de conversas é mantido.

## Uso

| Como abrir | Comando |
|---|---|
| Interface gráfica | `opentars-gui` ou **openTARS** no menu |
| Terminal | `opentars` |
| Refazer a configuração | `opentars --setup` |

`tars` e `tars-gui` também funcionam como atalhos.

Exemplos:

```
abra o firefox
pesquise rtx 5060 no google
o que tem na minha tela?
quanto espaço livre tenho no disco?
```

No terminal, `/modelo` lista os modelos instalados, `/modelo <nome>` fixa um modelo e `/modelo auto` volta para a escolha automática. Na interface gráfica, isso fica no seletor no topo da janela.

## Problemas comuns

- **`E: Unsupported file ... given on commandline`**: o arquivo não está na pasta atual. Entre na pasta onde ele foi baixado (`cd ~/Downloads`) ou use o comando com `wget` acima.

- **Ollama não instalou** (sem internet na hora): instale em [ollama.com/download](https://ollama.com/download) e rode `opentars --setup`.
- **Interface gráfica não abre**: `sudo apt install python3-tk` e depois `opentars --setup`.
- **"Nenhum modelo encontrado"**: baixe um modelo de conversa com `ollama pull qwen3:8b`.

## Desinstalar

```bash
sudo apt remove opentars
```

O Ollama, os modelos baixados e o seu histórico (`~/.tars_sessoes.json`, `~/.tars_log/`) são mantidos.

## Para desenvolvedores

```
tars.py            núcleo: escolha de modelo, ferramentas de desktop, modo terminal
tars_gui.py        interface gráfica (Tkinter), usa o tars.py por baixo
tests/             testes automatizados
empacotamento/     tudo que vira o .deb (setup.sh, lançadores, atalhos, ícone, scripts do Debian)
```

Rodar direto do código (precisa de `requests`, `psutil`, `pyautogui`, `pillow` e `python3-tk`):

```bash
python3 tars.py        # terminal
python3 tars_gui.py    # interface gráfica
```

Rodar os testes:

```bash
python3 tests/run_all.py
```

Gerar o `.deb` (sai em `dist/`):

```bash
bash empacotamento/build.sh
```

A versão fica na variável `VERSAO` do `build.sh` e no topo de `empacotamento/doc/changelog`.

## Licença

A definir.
