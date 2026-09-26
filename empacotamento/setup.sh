#!/bin/bash
# Configuração do openTARS: instala o que falta pra ele rodar (Ollama,
# ambiente Python isolado com as dependências, modelo ajudante
# qwen2.5:0.5b) e NUNCA reinstala algo que já está presente no PC.
# Roda automaticamente depois do "apt install" (chamado pelo postinst)
# e pode ser rodado de novo a qualquer momento com "opentars --setup".
#
# De propósito, NUNCA sai com erro: se o dpkg visse esse script falhar,
# deixaria o pacote marcado como "quebrado" mesmo com o openTARS
# utilizável. Cada etapa arriscada (rede, permissões) avisa e segue.

set -uo pipefail

# Modelo ajudante: escolhe o modelo de conversa pra cada pedido e
# resolve nomes de apps/sites. Pequeno (~400 MB) e baixado sozinho.
MODELO_AJUDANTE="${TARS_MODELO_AJUDANTE:-${TARS_MODELO_CLASSIFICADOR:-qwen2.5:0.5b}}"
MODELO_AJUDANTE_ANTIGO="gemma3:270m"
DIR_OPENTARS="/usr/share/opentars"
VENV="$DIR_OPENTARS/venv"
DEPENDENCIAS=("requests:requests" "psutil:psutil" "pyautogui:pyautogui" "PIL:pillow")

# Textos no idioma do sistema: vêm de idiomas/*.json, pelo mesmo módulo
# que o openTARS usa (tars_i18n.py). Se algo falhar, aparecem as chaves
# em vez das frases — a instalação nunca quebra por causa disso.
eval "$(python3 "$DIR_OPENTARS/tars_i18n.py" --shell setup. 2>/dev/null)" 2>/dev/null || true

# t chave [campo=valor ...]  ->  frase do idioma, com os {campos} preenchidos
t() {
    local var="T_setup_$1" texto par
    texto="${!var:-$1}"
    shift
    for par in "$@"; do
        texto="${texto//"{${par%%=*}}"/${par#*=}}"
    done
    printf '%s' "$texto"
}

msg()   { echo "[openTARS] $*"; }
aviso() { echo "[openTARS] $(t aviso): $*"; }

echo ""
echo "$(t titulo)"
echo ""

OLLAMA_URL="http://127.0.0.1:11434"

ollama_responde() {
    curl -fsS --max-time 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1
}

# Nomes dos modelos baixados, um por linha (via API: funciona mesmo com
# o Ollama rodando em Docker, sem o comando "ollama" no PC).
modelos_instalados() {
    curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null \
        | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | cut -d'"' -f4
}

# ------------------------------------------------------------------
# 1. Ollama — só instala se ainda não existir.
#
# Um Ollama que já responde na porta padrão (instalado de outro jeito,
# em Docker, via Snap...) conta como instalado: instalar outro por cima
# criaria um segundo serviço brigando pela mesma porta.
# ------------------------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
    msg "$(t ollama_ja)"
elif ollama_responde; then
    msg "$(t ollama_existente url="$OLLAMA_URL")"
else
    msg "$(t ollama_instalando)"
    # Baixa pro disco e só então executa: com "curl | sh" o status do
    # pipe é o do "sh", que "funciona" mesmo se o download falhar.
    instalador="$(mktemp)"
    if curl -fsSL https://ollama.com/install.sh -o "$instalador" && sh "$instalador"; then
        msg "$(t ollama_ok)"
    else
        aviso "$(t ollama_falhou)"
        echo "        $(t ollama_manual)"
    fi
    rm -f "$instalador"
fi

# ------------------------------------------------------------------
# 2. Ambiente Python isolado.
#
# Um venv próprio evita o conflito do pyautogui com o setuptools do
# sistema em distros recentes (Ubuntu 24.04+, Zorin 17+).
# --system-site-packages deixa o venv enxergar o tkinter, que só existe
# como pacote do sistema (python3-tk), nunca via pip.
#
# Se o venv existe mas o Python dele não roda mais (ex: atualização da
# distro trocou a versão do Python), ele é recriado do zero.
# ------------------------------------------------------------------
# Escolhe o Python base: o "python3" padrão, a menos que ele não tenha
# tkinter e outro python3.X instalado tenha (acontece quando o python3
# do PATH foi trocado e o pacote python3-tk é da versão padrão da distro).
escolher_python() {
    local candidato
    for candidato in python3 /usr/bin/python3 $(ls -r /usr/bin/python3.[0-9]* 2>/dev/null | grep -E 'python3\.[0-9]+$'); do
        command -v "$candidato" >/dev/null 2>&1 || continue
        "$candidato" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>/dev/null || continue
        "$candidato" -c "import venv, tkinter" >/dev/null 2>&1 && { echo "$candidato"; return; }
    done
    echo python3
}

venv_ok() {
    [ -x "$VENV/bin/python3" ] && "$VENV/bin/python3" -c "pass" >/dev/null 2>&1
}

# Venv criado com um Python sem tkinter enquanto existe outro com tkinter
# (ex: python3-tk instalado depois): recria pra liberar a interface gráfica.
PYTHON_BASE="$(escolher_python)"
if venv_ok && ! "$VENV/bin/python3" -c "import tkinter" >/dev/null 2>&1 \
        && "$PYTHON_BASE" -c "import tkinter" >/dev/null 2>&1; then
    msg "$(t venv_sem_tk python="$PYTHON_BASE")"
    rm -rf "$VENV"
fi

if venv_ok; then
    msg "$(t venv_ja)"
else
    [ -e "$VENV" ] && msg "$(t venv_quebrado)"
    rm -rf "$VENV"
    msg "$(t venv_criando versao="$("$PYTHON_BASE" --version 2>&1)")"
    if "$PYTHON_BASE" -m venv --system-site-packages "$VENV"; then
        "$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel \
            || aviso "$(t pip_falhou)"
        msg "$(t venv_ok)"
    else
        aviso "$(t venv_falhou)"
        rm -rf "$VENV"
    fi
fi

if [ -x "$VENV/bin/python3" ]; then
    msg "$(t deps_verificando)"

    faltando=()
    for par in "${DEPENDENCIAS[@]}"; do
        modulo="${par%%:*}"
        pacote="${par##*:}"
        # find_spec só checa se o módulo existe, sem executá-lo. Um
        # "import" real do pyautogui lê $DISPLAY na hora e dá KeyError
        # sem sessão gráfica (como durante o próprio postinst).
        if ! "$VENV/bin/python3" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$modulo') else 1)" >/dev/null 2>&1; then
            faltando+=("$pacote")
        fi
    done

    if [ "${#faltando[@]}" -eq 0 ]; then
        msg "$(t deps_ok)"
    else
        msg "$(t deps_instalando pacotes="${faltando[*]}")"
        if ! "$VENV/bin/pip" install --quiet "${faltando[@]}"; then
            aviso "$(t deps_falhou)"
        fi
    fi

    if "$VENV/bin/python3" -c "import tkinter" >/dev/null 2>&1; then
        msg "$(t tk_ok)"
    else
        aviso "$(t tk_falta)"
        echo "        $(t tk_instale)"
        echo "        $(t tk_terminal)"
    fi

    # Clicar nos botões pelo nome (árvore de acessibilidade, AT-SPI).
    if "$VENV/bin/python3" -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi" >/dev/null 2>&1; then
        msg "$(t acess_ok)"
    else
        aviso "$(t acess_falta)"
        echo "        $(t acess_instale)"
    fi

    # Ler a tela (OCR): clicar em apps sem acessibilidade (Electron, jogos).
    if command -v tesseract >/dev/null 2>&1; then
        msg "$(t ocr_ok idiomas="$(tesseract --list-langs 2>/dev/null | tail -n +2 | grep -v osd | tr '\n' ' ')")"
    else
        aviso "$(t ocr_falta)"
    fi
else
    aviso "$(t sem_venv)"
fi

# ------------------------------------------------------------------
# 3. Modelo ajudante (classificador leve) — baixa só se ainda não tiver.
# ------------------------------------------------------------------
servico_pronto=0

# Recém-instalado, o serviço leva um instante pra subir. Se o Ollama
# existe mas o serviço está parado (sem systemd, por exemplo), tenta
# iniciar.
if ! ollama_responde && command -v systemctl >/dev/null 2>&1 \
        && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
    systemctl start ollama >/dev/null 2>&1 || true
fi

for _ in $(seq 1 10); do
    if ollama_responde; then
        servico_pronto=1
        break
    fi
    sleep 2
done

if [ "$servico_pronto" -eq 0 ]; then
    aviso "$(t ollama_nao_respondeu url="$OLLAMA_URL")"
    echo "        $(t ajudante_depois modelo="$MODELO_AJUDANTE")"
elif modelos_instalados | grep -qx "$MODELO_AJUDANTE"; then
    msg "$(t ajudante_ja modelo="$MODELO_AJUDANTE")"
else
    msg "$(t ajudante_baixando modelo="$MODELO_AJUDANTE")"
    # Pelo comando, se existir (mostra progresso); senão pela API.
    if command -v ollama >/dev/null 2>&1; then
        ollama pull "$MODELO_AJUDANTE"
    else
        curl -fsS --max-time 1800 "$OLLAMA_URL/api/pull" \
            -d "{\"model\":\"$MODELO_AJUDANTE\",\"stream\":false}" >/dev/null
    fi
    if modelos_instalados | grep -qx "$MODELO_AJUDANTE"; then
        msg "$(t baixado modelo="$MODELO_AJUDANTE")"
    else
        aviso "$(t nao_baixou modelo="$MODELO_AJUDANTE")"
    fi
fi

# ------------------------------------------------------------------
# 3b. Modelo de embeddings (~560 MB, multilíngue): classifica o pedido
#     pelo SENTIDO em ~20 ms, antes de precisar perguntar ao ajudante.
#     TARS_MODELO_EMBEDDING=<modelo> escolhe outro; =off pula.
# ------------------------------------------------------------------
MODELO_EMBEDDING="${TARS_MODELO_EMBEDDING:-granite-embedding:278m}"
if [ "$servico_pronto" -eq 1 ] && [ "$MODELO_EMBEDDING" != "off" ]; then
    if modelos_instalados | grep -qx "$MODELO_EMBEDDING"; then
        msg "$(t embedding_ja modelo="$MODELO_EMBEDDING")"
    else
        msg "$(t embedding_baixando modelo="$MODELO_EMBEDDING")"
        if command -v ollama >/dev/null 2>&1; then
            ollama pull "$MODELO_EMBEDDING" || true
        else
            curl -fsS --max-time 1800 "$OLLAMA_URL/api/pull" \
                -d "{\"model\":\"$MODELO_EMBEDDING\",\"stream\":false}" >/dev/null || true
        fi
        if modelos_instalados | grep -qx "$MODELO_EMBEDDING"; then
            msg "$(t baixado modelo="$MODELO_EMBEDDING")"
        else
            aviso "$(t nao_baixou modelo="$MODELO_EMBEDDING")"
        fi
    fi
fi

# ------------------------------------------------------------------
# 4. Modelo de conversa: sem nenhum, o openTARS abre mas não tem quem
#    responda. Baixa UM, do tamanho certo pro PC, só se não houver
#    nenhum ainda (quem já tem modelos escolheu os seus).
#    TARS_MODELO_CONVERSA=<modelo> escolhe outro; TARS_SEM_MODELO=1 pula.
# ------------------------------------------------------------------
vram_gb() {
    local total=0
    if command -v nvidia-smi >/dev/null 2>&1; then
        total=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
            | awk '{s+=$1} END {printf "%d", s/1024}')
    fi
    if [ "${total:-0}" -eq 0 ]; then
        for d in /sys/class/drm/card[0-9]*/device; do
            [ "$(cat "$d/vendor" 2>/dev/null)" = "0x1002" ] || continue
            local b; b=$(cat "$d/mem_info_vram_total" 2>/dev/null || echo 0)
            [ "$b" -ge 2147483648 ] && total=$((total + b / 1073741824))
        done
    fi
    echo "${total:-0}"
}

ram_gb() {
    awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0
}

modelo_de_conversa_sugerido() {
    local vram ram
    vram=$(vram_gb); ram=$(ram_gb)
    if [ "$vram" -ge 6 ]; then
        echo "qwen3:8b"        # ~5 GB, cabe na placa
    elif [ "$ram" -ge 12 ]; then
        echo "qwen3:4b"        # ~2.5 GB, roda bem na CPU
    else
        echo "qwen3:1.7b"      # ~1.4 GB, pra PC com pouca memória
    fi
}

if [ "$servico_pronto" -eq 1 ]; then
    outros=$(modelos_instalados | grep -vx "$MODELO_AJUDANTE" | grep -vx "$MODELO_AJUDANTE_ANTIGO" \
        | grep -vx "$MODELO_EMBEDDING" | grep -viE 'embed|minilm|bge-' | wc -l)
    if [ "$outros" -gt 0 ]; then
        msg "$(t ja_tem_modelos n="$outros")"
    elif [ "${TARS_SEM_MODELO:-0}" = "1" ]; then
        aviso "$(t sem_modelo_pedido)"
    else
        MODELO_CONVERSA="${TARS_MODELO_CONVERSA:-$(modelo_de_conversa_sugerido)}"
        msg "$(t baixando_conversa modelo="$MODELO_CONVERSA" vram="$(vram_gb)" ram="$(ram_gb)")"
        msg "$(t pode_demorar)"
        if command -v ollama >/dev/null 2>&1; then
            ollama pull "$MODELO_CONVERSA" || true
        else
            curl -fsS --max-time 7200 "$OLLAMA_URL/api/pull" \
                -d "{\"model\":\"$MODELO_CONVERSA\",\"stream\":false}" >/dev/null || true
        fi
        if modelos_instalados | grep -qx "$MODELO_CONVERSA"; then
            msg "$(t baixado modelo="$MODELO_CONVERSA")"
        else
            aviso "$(t nao_baixou modelo="$MODELO_CONVERSA")"
        fi
    fi

    # O ajudante antigo não é mais usado. Não apaga nada do usuário: só avisa.
    if [ "$MODELO_AJUDANTE" != "$MODELO_AJUDANTE_ANTIGO" ] \
            && modelos_instalados | grep -qx "$MODELO_AJUDANTE_ANTIGO"; then
        echo ""
        msg "$(t ajudante_antigo modelo="$MODELO_AJUDANTE_ANTIGO")"
        echo "        $(t ajudante_antigo_rm modelo="$MODELO_AJUDANTE_ANTIGO")"
    fi
else
    aviso "$(t sem_ollama_modelo)"
fi

echo ""
echo "$(t pronto)"
echo "$(t dica_atalho)"
echo ""

exit 0
