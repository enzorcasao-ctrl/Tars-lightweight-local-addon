# openTARS

[![Licença: MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-green.svg)](LICENSE)

Assistente de IA 100% local para desktop Linux. Você pede em português e ele faz: abre programas, clica, digita, pesquisa, olha a tela e roda comandos. Tudo roda na sua máquina via [Ollama](https://ollama.com): sem nuvem, sem conta, sem mensalidade.

**Versão atual: 2.0**

## O que faz

- **Controla o desktop**: abre e fecha aplicativos, clica, digita, usa atalhos de teclado
- **Enxerga as janelas**: sabe o que está aberto, traz a janela certa pra frente e tira print só dela antes de clicar
- **Analisa a tela**: descreve o que está aparecendo, lê textos e avisos
- **Pesquisa na web**: "pesquise rtx 5060 no google" abre a busca certa
- **Roda comandos**: executa no terminal e mostra o resultado, pedindo confirmação antes de qualquer coisa perigosa
- **Escolhe a IA sozinho**: pra cada pedido, usa o modelo mais adequado entre os que você tem no Ollama

Nada do que você digita ou mostra sai do seu PC.

## Como ele escolhe a IA

O openTARS usa dois tipos de modelo:

- **O ajudante (`qwen2.5:0.5b`)**: um modelo minúsculo, baixado pelo instalador, que trabalha nos bastidores. Ele lê cada pedido e diz de que tipo é (ver a tela, técnico, ação no PC, busca, conversa simples ou geral), além de descobrir nomes de programas e sites que a busca normal não achou. Ele não conversa com você.
- **Os modelos de conversa**: os que você baixar (ex: `qwen3:8b`). O openTARS escolhe entre eles conforme o tipo do pedido e o que cabe na sua placa de vídeo. Um "bom dia" vai pro menor e mais rápido; "abra a calculadora e clique nos números" vai pra um modelo que enxerga a tela.

Todos os modelos compartilham a mesma conversa, então trocar de modelo no meio não faz a IA esquecer o que você pediu antes.

## Instalação

Para Ubuntu, Debian, Linux Mint, Zorin OS, Pop!_OS e derivados. Cole no terminal:

```bash
wget -O /tmp/opentars.deb https://github.com/enzorcasao-ctrl/openTars-lightweight-local-addon/raw/main/opentars_all.deb && sudo apt install -y --reinstall /tmp/opentars.deb
```

O mesmo comando instala e **atualiza**. O histórico de conversas é mantido.

Prefere baixar manualmente? Clique em [`opentars_all.deb`](https://github.com/enzorcasao-ctrl/openTars-lightweight-local-addon/raw/main/opentars_all.deb) e, na pasta onde ele foi salvo (normalmente `~/Downloads`):

```bash
sudo apt install -y --reinstall ./opentars_all.deb
```

O instalador faz o resto e pula o que você já tiver:

- instala o Ollama (se já houver um rodando, inclusive em Docker, usa esse)
- cria um ambiente Python isolado com as dependências
- baixa o modelo ajudante `qwen2.5:0.5b` (~400 MB)
- adiciona o openTARS ao menu de aplicativos

### Primeiro uso: baixe um modelo de conversa

O ajudante não conversa, então você precisa de pelo menos um modelo de conversa:

```bash
ollama pull qwen3:8b
```

Sugestões pelo seu hardware:

| Seu PC | Sugestão |
|---|---|
| Sem placa de vídeo, 16 GB de RAM | `qwen3:4b` |
| Placa com 8 GB ou mais | `qwen3:8b` |
| Pra ele ver a tela e clicar em botões | um modelo com visão **e** ferramentas, como `qwen3-vl:8b` |

Quanto mais modelos diferentes você tiver, mais a escolha automática consegue adaptar a IA ao pedido.

## Uso

| Como abrir | Comando |
|---|---|
| Interface gráfica | **openTARS** no menu, ou `opentars-gui` |
| Terminal | `opentars` |
| Refazer a configuração | `opentars --setup` |
| Ver a versão | `opentars --version` |
| Medir o ajudante no seu PC | `opentars --avaliar-classificador` |

`tars` e `tars-gui` também funcionam.

Exemplos de pedidos:

```
abra o firefox
abra a calculadora e clique em 7, +, 2 e =
o que tem na minha tela?
feche o spotify e abra o discord
pesquise rtx 5060 no google
quanto espaço livre tenho no disco?
```

- **Parar**: botão **Parar** (ou `Esc`) na janela, `Ctrl+C` no terminal. Interrompe a resposta ou a tarefa na hora.
- **Repetir um pedido**: setas ↑/↓ na caixa de texto da janela.
- **Conversa salva**: continua de onde parou ao reabrir. **Limpar conversa** (ou `/limpar`) começa do zero.
- **Escolher o modelo na mão**: seletor no topo da janela. No terminal, `/modelo` lista os modelos, `/modelo <nome>` fixa um, `/modelo auto` volta pra escolha automática. Também aceita descrições como `/modelo o maior` ou `/modelo o de visão`.

## Compatibilidade

| | Funciona | Observação |
|---|---|---|
| **Distros** | Ubuntu 22.04+, Debian 12+, Mint, Zorin, Pop!_OS e derivados | precisa do `apt` |
| **Desktop** | GNOME, KDE, Cinnamon, XFCE, MATE e outros | abre apps do menu, Snap e Flatpak, pelo nome em português ou inglês |
| **Sessão Xorg (X11)** | tudo | |
| **Sessão Wayland** | conversa, abrir apps/sites, comandos, prints | cliques e digitação simulados só chegam a alguns apps (limitação do Wayland) |
| **GPU** | NVIDIA, AMD ou só CPU | sem GPU, o modo automático evita modelos grandes demais pra CPU |
| **Ollama** | local, Docker ou em outra máquina | outro endereço: variável `OLLAMA_HOST` |
| **Modelos** | qualquer um do Ollama | modelos sem suporte a ferramentas só conversam; os de embedding ficam de fora |

## Segurança

- Comandos que apagam dados ou mexem no sistema (`rm -r`, `mkfs`, `dd`, `git reset --hard`, desligar o PC...) só rodam depois de você confirmar.
- Comandos que pedem senha (`sudo`) não travam: falham na hora, e a IA mostra o comando pra você rodar.
- Ao fechar um programa, o openTARS faz como clicar no X. Se o programa perguntar "salvar alterações?", ele não força o fechamento.
- Ele nunca fecha a si mesmo nem o terminal onde está rodando.
- Tudo que ele executa fica registrado em `~/.tars_log/tars.log`.

## Configuração (opcional)

Variáveis de ambiente, pra quem quiser ajustar:

| Variável | Pra quê | Padrão |
|---|---|---|
| `OLLAMA_HOST` | endereço do Ollama | `127.0.0.1:11434` |
| `TARS_MODELO_AJUDANTE` | trocar o modelo ajudante | `qwen2.5:0.5b` |
| `TARS_CONTEXTO` | tamanho da memória da IA, em tokens | 16384 com GPU de 16 GB+, senão 8192 |
| `TARS_LARGURA_SCREENSHOT` | largura máxima do print enviado à IA | 1280 |
| `TARS_SEM_CONFIRMACAO_PERIGOSOS=1` | não pedir confirmação de comandos perigosos (por sua conta e risco) | desligado |

Exemplo: `TARS_CONTEXTO=32768 opentars-gui`

## Problemas comuns

- **`E: Unsupported file ... given on commandline`**: o arquivo não está na pasta atual. Entre na pasta onde ele foi baixado (`cd ~/Downloads`) ou use o comando com `wget` acima.
- **"Ajudante qwen2.5:0.5b não instalado"**: a instalação ficou sem internet na hora. Rode `ollama pull qwen2.5:0.5b`.
- **Cliques e digitação não fazem nada**: provavelmente a sessão é Wayland. Na tela de login, clique na engrenagem e escolha a opção com "Xorg" no nome (no Ubuntu, "Ubuntu on Xorg").
- **"O Ollama não está respondendo"**: inicie o serviço com `sudo systemctl start ollama`. Se ele roda em Docker ou em outra máquina, defina `OLLAMA_HOST` (ex: `export OLLAMA_HOST=192.168.0.10:11434`).
- **A IA diz que "não consegue" abrir ou fechar um programa**: o openTARS já lembra ela das ferramentas automaticamente. Se continuar, use um modelo de conversa geral (ex: `qwen3:8b`); modelos só de programação, como o `qwen2.5-coder`, são ruins pra controlar o desktop e o modo automático já evita eles nessas tarefas.
- **A IA escolhe um modelo estranho pro pedido**: rode `opentars --avaliar-classificador` pra ver quanto o ajudante acerta no seu PC, ou fixe um modelo no seletor da janela.
- **A IA esquece o pedido no meio da tarefa ou a resposta é cortada**: falta memória de contexto. Aumente com `TARS_CONTEXTO` (usa mais VRAM).
- **Ollama não instalou** (sem internet na hora): instale em [ollama.com/download](https://ollama.com/download) e rode `opentars --setup`.
- **Interface gráfica não abre**: `sudo apt install python3-tk` e depois `opentars --setup`.
- **"Nenhum modelo de conversa"**: baixe um com `ollama pull qwen3:8b`.
- **Vindo de uma versão antiga?** O ajudante anterior (`gemma3:270m`) não é mais usado. Se não usar ele pra outra coisa, libere espaço com `ollama rm gemma3:270m`.

## Desinstalar

```bash
sudo apt remove opentars
```

O Ollama, os modelos baixados e o seu histórico (`~/.tars_sessoes.json`, `~/.tars_log/`) são mantidos.

## Para desenvolvedores

```
tars.py            núcleo: escolha de modelo, ajudante, ferramentas de desktop e janelas, modo terminal
tars_gui.py        interface gráfica (Tkinter), usa o tars.py por baixo
tests/             testes automatizados (os de janela rodam de verdade num Xvfb + openbox, se houver)
empacotamento/     tudo que vira o .deb (setup.sh, lançadores, atalhos, ícone, scripts do Debian)
```

Rodar direto do código (precisa de `requests`, `psutil`, `pyautogui`, `pillow` e `python3-tk`):

```bash
python3 tars.py        # terminal
python3 tars_gui.py    # interface gráfica
```

Testes e pacote:

```bash
python3 tests/run_all.py          # testes
bash empacotamento/build.sh       # gera o .deb em dist/
```

A versão fica na constante `VERSAO` do `tars.py` (o `build.sh` lê de lá) e no topo de `empacotamento/doc/changelog`.

## Licença

[MIT](LICENSE): você pode usar, copiar, modificar e distribuir o openTARS, inclusive em outros projetos, desde que mantenha o aviso de copyright e a licença junto. O software é fornecido "como está", sem garantia.
