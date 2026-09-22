<div align="center">

# 🤖 openTARS

### Um assistente de IA que roda 100% no seu PC — sem nuvem, sem mensalidade, sem enviar nada pra fora.

*"Não sou seu assistente de nuvem. Sou o computador falando com você."*

[![Licença](https://img.shields.io/badge/licença-a%20definir-lightgrey)]()
[![Plataforma](https://img.shields.io/badge/plataforma-Linux-blue?logo=linux)]()
[![Powered by Ollama](https://img.shields.io/badge/powered%20by-Ollama-black)]()
[![100%25 Local](https://img.shields.io/badge/100%25-local-success)]()

</div>

---

## O que é

openTARS é um assistente de IA para desktop Linux que entende comandos em
**português** e controla o seu computador de verdade: abre e fecha
programas, navega e pesquisa na web, digita, clica, move o mouse,
analisa o que está na tela e executa comandos de terminal — tudo isso
rodando **inteiramente na sua máquina**, via [Ollama](https://ollama.com).

Nada do que você digita ou mostra pra ele sai do seu PC. Sem conta,
sem API paga, sem depender da internet ficar de pé pra funcionar (só
pra baixar os modelos na primeira vez).

```
Você › abra o spotify e toca minha playlist
Você › pesquise as notícias de hoje sobre IA
Você › veja minha tela e me diz o que tem de errado nesse código
Você › feche todas as abas do navegador
```

## Por que openTARS existe

A maioria dos assistentes de IA hoje depende de mandar seus dados pra
um servidor de alguma empresa. TARS nasceu da ideia oposta: **e se o
cérebro inteiro do assistente morasse dentro do seu próprio
computador?** Com modelos locais via Ollama, isso já é possível — só
faltava alguém amarrar tudo numa experiência que realmente controla o
desktop, não só conversa.

## ✨ Principais recursos

- 🧠 **Escolha automática de modelo** — o TARS olha pra tarefa (uma
  busca simples, uma pergunta técnica, uma ação composta no desktop,
  analisar a tela...) e escolhe sozinho qual das suas IAs instaladas
  no Ollama é a mais indicada, sem você precisar trocar nada na mão.
- 🔌 **Se adapta ao que você já tem instalado** — baixou uma IA nova
  no Ollama? O TARS já enxerga e já sabe usar, sem editar uma linha
  de código.
- 🖥️ **Controle real de desktop** — abrir/fechar aplicativos, digitar,
  clicar, mover o mouse, tirar e analisar screenshots.
- 🌐 **Navegação e busca inteligentes** — abre sites, pesquisa no
  Google/YouTube, e nunca "inventa" um link que não existe (checagem
  de domínio antes de abrir).
- 🔗 **Tarefas compostas** — "abra a calculadora e some 25+17", "abra
  o navegador e pesquise sobre X": o TARS encadeia as ações sozinho.
- ⚠️ **Confirmação em comandos perigosos** — nada de `rm -rf` sem você
  aprovar antes.
- 💾 **Sessões persistentes** — sua conversa com cada modelo continua
  de onde parou, mesmo depois de fechar o terminal.
- 📝 **Log de auditoria** — todo comando executado fica registrado,
  pra você sempre poder conferir o que o TARS fez.

## 🚀 Instalação

```bash
# baixe o .deb da última release e instale:
sudo apt install ./tars_1.0.0.deb
```

Isso já resolve tudo sozinho: instala o [Ollama](https://ollama.com)
se ainda não estiver presente, prepara um ambiente Python isolado com
as dependências certas, baixa o modelo ajudante leve (`gemma3:270m`)
e registra o comando `tars` no terminal e um atalho no menu de
aplicativos.

Depois é só baixar pelo menos um modelo de conversa e começar:

```bash
ollama pull qwen3:8b
tars
```

## 🛠️ Como funciona por baixo

```
      seu comando em português
               │
               ▼
     detecção de tipo de tarefa
   (visão, técnico, ação, busca, simples, geral)
               │
               ▼
   escolha automática do modelo instalado
     mais adequado pra essa tarefa
               │
               ▼
   IA decide QUAL ferramenta chamar
 (abrir app, abrir site, pesquisar, clicar,
  digitar, tirar print, rodar comando...)
               │
               ▼
        ação executada de verdade
          no seu desktop Linux
```

Modelos pequenos (ex: 0.6B parâmetros) cuidam do que é rápido e óbvio;
tarefas que exigem mais raciocínio — como decidir entre duas
ferramentas parecidas, ou compor várias ações em sequência — sobem
automaticamente pra um modelo maior. Você nunca escolhe isso na mão,
mas pode fixar um modelo manualmente se quiser.

## 📋 Requisitos

- Linux (testado em bases Ubuntu/Zorin OS)
- [Ollama](https://ollama.com) — instalado automaticamente pelo `.deb`
  se ainda não estiver no sistema
- Pelo menos um modelo de conversa baixado no Ollama (o TARS te avisa
  se não tiver nenhum)

## 🗺️ Roadmap

- [ ] Suporte a mais distros (empacotar pra Arch/Fedora)
- [ ] Interface gráfica opcional, além do terminal
- [ ] Plugins de terceiros

## 🤝 Contribuindo

Contribuições, issues e sugestões são bem-vindas. Esse projeto é feito
pra evoluir com a comunidade — se você tem uma IA local rodando no seu
Ollama, o TARS já deveria funcionar com ela.

## 📄 Licença

A definir antes do lançamento oficial.

---

<div align="center">

Feito com 🧠 local, sem depender de nuvem nenhuma.

</div>
