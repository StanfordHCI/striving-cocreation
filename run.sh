#!/usr/bin/env bash
# One-command Tempo bootstrap + launch.
#
# What this script does:
#   1. Installs uv (if missing)
#   2. Installs Node.js + pnpm (if missing)
#   3. Installs Python deps (`uv sync --extra test`)
#   4. Installs Node deps (`pnpm install` in electron-app/)
#   5. Loads .env (if present)
#   6. Launches the Electron dev environment
#
# Re-running is safe: every install step is idempotent (uv sync / pnpm install
# return quickly when nothing has changed).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. uv ──────────────────────────────────────────────────────────────────
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then return 0; fi
  if [ -x "${HOME}/.local/bin/uv" ]; then
    export PATH="${HOME}/.local/bin:${PATH}"
    return 0
  fi
  echo ">> Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
}

# ── 2. Node + pnpm ─────────────────────────────────────────────────────────
ensure_node() {
  if command -v node >/dev/null 2>&1; then return 0; fi

  if command -v brew >/dev/null 2>&1; then
    echo ">> Installing Node.js via Homebrew"
    brew install node
    return 0
  fi

  echo ">> Installing Node.js via nvm"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
  else
    echo "ERROR: need curl to install nvm; install Node.js manually and re-run." >&2
    exit 1
  fi
  export NVM_DIR="${HOME}/.nvm"
  # shellcheck disable=SC1091
  [ -s "${NVM_DIR}/nvm.sh" ] && . "${NVM_DIR}/nvm.sh"
  nvm install --lts
  nvm use --lts
  nvm alias default 'lts/*'
}

ensure_pnpm() {
  if command -v pnpm >/dev/null 2>&1; then return 0; fi

  if command -v brew >/dev/null 2>&1; then
    echo ">> Installing pnpm via Homebrew"
    brew install pnpm
    return 0
  fi

  echo ">> Installing pnpm via corepack"
  corepack enable
  corepack prepare pnpm@latest --activate
}

# ── 3-4. Dependency installation ───────────────────────────────────────────
install_python_deps() {
  echo ">> uv sync --extra test"
  (cd "${ROOT_DIR}" && uv sync --extra test)
}

install_node_deps() {
  echo ">> pnpm install (in electron-app/)"
  (cd "${ROOT_DIR}/electron-app" && pnpm install)
}

# ── Bootstrap ──────────────────────────────────────────────────────────────
ensure_uv
ensure_node
ensure_pnpm
install_python_deps
install_node_deps

# ── 5. Activate venv for child processes ───────────────────────────────────
export PYTHON="${ROOT_DIR}/.venv/bin/python"
export VIRTUAL_ENV="${ROOT_DIR}/.venv"
export PATH="${VIRTUAL_ENV}/bin:${PATH}"

# ── Load .env ───────────────────────────────────────────────────────────────
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT_DIR}/.env"
  set +a
fi

# Suppress macOS CoreText font warnings
export OS_ACTIVITY_MODE=disable

# VS Code can export this for its Node helpers; the desktop app needs Electron.
unset ELECTRON_RUN_AS_NODE

# ── 6. Launch ──────────────────────────────────────────────────────────────
echo ">> Launching Tempo"
cd "${ROOT_DIR}/electron-app"
exec pnpm dev
