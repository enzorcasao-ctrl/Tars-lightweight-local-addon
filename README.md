# openTARS

Assistente de IA 100% local para desktop Linux. Roda inteiramente na sua máquina via [Ollama](https://ollama.com) — sem nuvem, sem conta, sem mensalidade.

## O que faz

Você escreve em português, o openTARS executa:

- **Controla o desktop**: abre e fecha aplicativos, clica, digita, move o mouse
- **Enxerga as janelas**: sabe o que está aberto, traz a janela certa pra frente e olha só pra ela antes de clicar
- **Pesquisa na web**: "pesquise rtx 5060 no google" abre a busca certa
- **Analisa a tela**: tira um print e descreve o que está vendo
- **Roda comandos**: executa no terminal e mostra o resultado
- **Escolhe o modelo sozinho**: usa, entre os modelos que você já tem no Ollama, o mais adequado para cada tarefa, sem perder o fio da conversa quando troca de modelo

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

- instala o Ollama (se já houver um rodando, inclusive em Docker, usa esse)
- cria um ambiente Python isolado com as dependências
- baixa o modelo ajudante `gemma3:270m` (~300 MB)
- adiciona o openTARS ao menu de aplicativos

Depois, baixe pelo menos um modelo de conversa:

```bash
ollama pull qwen3:8b
```

Quem já tinha a versão 1.0.0 (pacote `tars`) pode instalar por cima: ela é substituída automaticamente e o histórico de conversas é mantido.

## Compatibilidade

| | Funciona | Observação |
|---|---|---|
| **Distros** | Ubuntu 22.04+, Debian 12+, Mint, Zorin, Pop!_OS e derivados | precisa do `apt` |
| **Desktop** | GNOME, KDE, Cinnamon, XFCE, MATE e outros | abre apps do menu, Snap e Flatpak, pelo nome em português ou inglês |
| **Sessão Xorg (X11)** | tudo | |
| **Sessão Wayland** | conversa, abrir apps/sites, comandos, prints | cliques e digitação simulados só chegam a alguns apps (limitação do Wayland) |
| **GPU** | NVIDIA, AMD ou só CPU | sem GPU, o modo automático evita modelos grandes demais pra CPU |
| **Ollama** | local, Docker ou em outra máquina | outro endereço: variável `OLLAMA_HOST`, igual à do Ollama |
| **Modelos** | qualquer um do Ollama | modelos sem suporte a ferramentas só conversam; os de embedding ficam de fora |

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
abra a calculadora e clique em 7, +, 2 e =
pesquise rtx 5060 no google
o que tem na minha tela?
quanto espaço livre tenho no disco?
```

Para interromper uma resposta ou tarefa: botão **Parar** (ou `Esc`) na janela, `Ctrl+C` no terminal. Na janela, as setas ↑/↓ repetem pedidos anteriores.

A conversa fica salva e continua de onde parou ao reabrir (na janela ou no terminal). `/limpar` ou **Limpar conversa** começa do zero.

No terminal, `/modelo` lista os modelos instalados, `/modelo <nome>` fixa um modelo e `/modelo auto` volta para a escolha automática. Na interface gráfica, isso fica no seletor no topo da janela.

## Problemas comuns

- **`E: Unsupported file ... given on commandline`**: o arquivo não está na pasta atual. Entre na pasta onde ele foi baixado (`cd ~/Downloads`) ou use o comando com `wget` acima.
- **Cliques e digitação não fazem nada**: você provavelmente está numa sessão Wayland. Na tela de login, clique na engrenagem e escolha a opção que tem "Xorg" no nome (no Ubuntu, "Ubuntu on Xorg").
- **"O Ollama não está respondendo"**: inicie o serviço com `sudo systemctl start ollama`. Se ele roda em Docker ou em outra máquina, defina `OLLAMA_HOST` (ex: `export OLLAMA_HOST=192.168.0.10:11434`).
- **A IA esquece o pedido no meio da tarefa ou a resposta é cortada**: falta contexto. O openTARS usa 16 mil tokens com GPU de 16 GB ou mais e 8 mil nos outros casos. Para mudar: `TARS_CONTEXTO=32768 opentars-gui`. Mais contexto usa mais VRAM.
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
tests/             testes automatizados (os de janela rodam de verdade num Xvfb + openbox, se houver)
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

A versão fica na constante `VERSAO` do `tars.py` (o `build.sh` lê de lá) e no topo de `empacotamento/doc/changelog`.

## Licença

A definir.
