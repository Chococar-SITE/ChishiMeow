#!/bin/bash
# ╔══════════════════════════════════════════════════════════════╗
# ║  ChishiMeow — 千島神社的小精靈                               ║
# ║  Pterodactyl-style install + startup script                  ║
# ╚══════════════════════════════════════════════════════════════╝
#
# 環境變數（可在 .env 或 docker -e 中設定，皆有預設值）：
#
#   GIT_ADDRESS        git repo 位址（預設：Chococar-SITE/ChishiMeow）
#   BRANCH             要 clone/pull 的分支（預設：main）
#   AUTO_UPDATE        啟動時自動 git pull（0/1，預設：1）
#   USER_UPLOAD        跳過 git，直接用現有檔案（0/1，預設：0）
#   USERNAME           git 認證帳號（私有 repo 用）
#   ACCESS_TOKEN       git 個人存取 token（私有 repo 用）
#   REQUIREMENTS_FILE  requirements 檔名（預設：requirements.txt）
#   PY_PACKAGES        額外 pip 套件，空格分隔（可留空）
#   PY_FILE            啟動的 Python 檔（預設：bot.py）
#   INSTALL_PLAYWRIGHT 是否安裝 Playwright Chromium（0/1，預設：1）
#   DATA_DIR           資料目錄，放 DB 與 cookies（預設：/data）
#   DISCORD_BOT_TOKEN  Discord bot token（必填）
#   THREADS_USERNAME   監控的 Threads 帳號（可留空）
#   THREADS_CHANNEL_ID Discord 通知頻道 ID（可留空）

set -e

# ── 載入 .env（如果存在）──────────────────────────────────────────
if [ -f ".env" ]; then
    echo "[*] 載入 .env"
    set -o allexport
    source .env
    set +o allexport
fi

# ── 預設值 ────────────────────────────────────────────────────────
GIT_ADDRESS="${GIT_ADDRESS:-https://github.com/Chococar-SITE/ChishiMeow.git}"
BRANCH="${BRANCH:-main}"
AUTO_UPDATE="${AUTO_UPDATE:-1}"
USER_UPLOAD="${USER_UPLOAD:-0}"
USERNAME="${USERNAME:-}"
ACCESS_TOKEN="${ACCESS_TOKEN:-}"
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-requirements.txt}"
PY_PACKAGES="${PY_PACKAGES:-}"
PY_FILE="${PY_FILE:-bot.py}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-1}"
DATA_DIR="${DATA_DIR:-/data}"

# ── 顏色輸出 ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ═══════════════════════════════════════════════════════════════════
# PHASE 1 — 取得原始碼
# ═══════════════════════════════════════════════════════════════════

if [ "${USER_UPLOAD}" == "1" ] || [ "${USER_UPLOAD}" == "true" ]; then
    warn "USER_UPLOAD=1，跳過 git，假設目錄中已有程式碼"
else
    # 組合帶 token 的 git 位址（private repo 用）
    if [ -z "${USERNAME}" ] && [ -z "${ACCESS_TOKEN}" ]; then
        AUTH_GIT_ADDRESS="${GIT_ADDRESS}"
    else
        AUTH_GIT_ADDRESS="https://${USERNAME}:${ACCESS_TOKEN}@$(echo "${GIT_ADDRESS}" | cut -d/ -f3-)"
    fi

    # 確保 .git 結尾
    [[ "${AUTH_GIT_ADDRESS}" != *.git ]] && AUTH_GIT_ADDRESS="${AUTH_GIT_ADDRESS}.git"

    if [ -d ".git" ]; then
        ORIGIN=$(git config --get remote.origin.url || echo "")
        if [ -n "${ORIGIN}" ]; then
            if [ "${AUTO_UPDATE}" == "1" ] || [ "${AUTO_UPDATE}" == "true" ]; then
                info "git pull（AUTO_UPDATE=1）"
                git pull
                ok "程式碼已更新"
            else
                info "AUTO_UPDATE=0，跳過 git pull"
            fi
        else
            warn "找到 .git 資料夾但沒有 remote origin，跳過更新"
        fi
    elif [ -z "$(ls -A /home/container 2>/dev/null)" ]; then
        info "目錄為空，clone ${GIT_ADDRESS}（branch: ${BRANCH}）"
        if [ -z "${BRANCH}" ]; then
            git clone "${AUTH_GIT_ADDRESS}" .
        else
            git clone --single-branch --branch "${BRANCH}" "${AUTH_GIT_ADDRESS}" .
        fi
        ok "Clone 完成"
    else
        warn "目錄非空且不是 git repo，跳過 clone（如需重新安裝請清空目錄）"
    fi
fi

# ═══════════════════════════════════════════════════════════════════
# PHASE 2 — 安裝 Python 依賴
# ═══════════════════════════════════════════════════════════════════

# Docker 容器內用 root 跑，直接 pip install 不需要 --prefix
# 但保留 --prefix .local 相容性，讓腳本在容器外也能用
if [ "$(id -u)" == "0" ]; then
    PIP_FLAGS="--no-cache-dir"
else
    PIP_FLAGS="--no-cache-dir --prefix .local"
    PY_VER=$(python3 -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
    export PYTHONPATH=".local/lib/python${PY_VER}/site-packages:${PYTHONPATH:-}"
    export PATH=".local/bin:${PATH}"
fi

if [ -n "${PY_PACKAGES}" ]; then
    info "安裝額外套件：${PY_PACKAGES}"
    # shellcheck disable=SC2086
    pip install -U ${PIP_FLAGS} ${PY_PACKAGES}
fi

if [ -f "${REQUIREMENTS_FILE}" ]; then
    info "安裝 ${REQUIREMENTS_FILE}"
    # shellcheck disable=SC2086
    pip install -U ${PIP_FLAGS} -r "${REQUIREMENTS_FILE}"
    ok "pip 安裝完成"
else
    warn "找不到 ${REQUIREMENTS_FILE}，跳過"
fi

# ═══════════════════════════════════════════════════════════════════
# PHASE 3 — Playwright Chromium（ChishiMeow 必須）
# ═══════════════════════════════════════════════════════════════════

if [ "${INSTALL_PLAYWRIGHT}" == "1" ] || [ "${INSTALL_PLAYWRIGHT}" == "true" ]; then
    CHROMIUM_MARKER="${DATA_DIR}/.playwright_installed"
    mkdir -p "${DATA_DIR}"
    if [ ! -f "${CHROMIUM_MARKER}" ]; then
        info "安裝 Playwright Chromium（首次執行，需要幾分鐘）"
        PLAYWRIGHT_BROWSERS_PATH="${DATA_DIR}/playwright_browsers" \
            python3 -m playwright install chromium
        touch "${CHROMIUM_MARKER}"
        ok "Playwright Chromium 安裝完成"
    else
        info "Playwright Chromium 已安裝，跳過"
    fi
    export PLAYWRIGHT_BROWSERS_PATH="${DATA_DIR}/playwright_browsers"
fi

# ═══════════════════════════════════════════════════════════════════
# PHASE 4 — 建立資料目錄並設定環境變數
# ═══════════════════════════════════════════════════════════════════

mkdir -p "${DATA_DIR}"
export DB_PATH="${DATA_DIR}/track.db"
export THREADS_COOKIES_PATH="${DATA_DIR}/threads_cookies.json"

# ── 確認必要變數 ──────────────────────────────────────────────────
if [ -z "${DISCORD_BOT_TOKEN}" ]; then
    error "DISCORD_BOT_TOKEN 未設定！請在 .env 或 docker -e 中填入 token"
fi

[ -z "${THREADS_USERNAME}" ]   && warn "THREADS_USERNAME 未設定，Threads 監控功能將停用"
[ -z "${THREADS_CHANNEL_ID}" ] && warn "THREADS_CHANNEL_ID 未設定，Threads 通知將無目標頻道"

# ═══════════════════════════════════════════════════════════════════
# PHASE 5 — 啟動 Bot
# ═══════════════════════════════════════════════════════════════════

if [ ! -f "${PY_FILE}" ]; then
    error "找不到 ${PY_FILE}，請確認 PY_FILE 設定或程式碼是否已下載"
fi

ok "所有準備就緒，啟動 ${PY_FILE}"
echo "─────────────────────────────────────────────"
exec python3 "${PY_FILE}"