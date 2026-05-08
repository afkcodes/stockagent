#!/usr/bin/env bash
#
# stockagent — Ubuntu VPS deploy script
#
# Sets up everything for a 1-month autonomous paper-trade run on a fresh Ubuntu
# server. Idempotent: safe to re-run if any step fails.
#
# Usage:
#   ./deploy.sh                    # fully interactive
#   ./deploy.sh --non-interactive  # use existing .env, fail if missing
#   ./deploy.sh --skip-backfill    # skip the 16-min historical backfill
#   ./deploy.sh --skip-cron        # don't install cron entries
#   ./deploy.sh -h                 # show help
#
# Run from the project root after cloning/copying the repo to the VPS.

set -euo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED=$'\e[0;31m'; GREEN=$'\e[0;32m'; YELLOW=$'\e[0;33m'; BLUE=$'\e[0;34m'
BOLD=$'\e[1m'; DIM=$'\e[2m'; NC=$'\e[0m'

log()  { printf '%s▸%s %s\n' "$BLUE" "$NC" "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$NC" "$*"; }
err()  { printf '%s✗%s %s\n' "$RED" "$NC" "$*" >&2; }
die()  { err "$*"; exit 1; }
section() { printf '\n%s%s═══ %s ═══%s\n' "$BOLD" "$BLUE" "$*" "$NC"; }

# ─── Args ────────────────────────────────────────────────────────────────────
NO_INTERACTIVE=false
SKIP_BACKFILL=false
SKIP_CRON=false
SKIP_TG_TEST=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive) NO_INTERACTIVE=true; shift ;;
        --skip-backfill)   SKIP_BACKFILL=true; shift ;;
        --skip-cron)       SKIP_CRON=true; shift ;;
        --skip-tg-test)    SKIP_TG_TEST=true; shift ;;
        -h|--help)
            sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "Unknown arg: $1 (use --help)" ;;
    esac
done

# ─── Preflight ───────────────────────────────────────────────────────────────
section "Preflight"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

[[ -f pyproject.toml ]] || die "pyproject.toml not found in $PROJECT_DIR"
[[ -f stockagent/__init__.py ]] || die "stockagent package missing"

if [[ "$EUID" -eq 0 ]]; then
    warn "Running as root. The project + cron will be owned by root."
    warn "Recommended: create a non-root user, copy the repo there, and run this as that user."
    if ! $NO_INTERACTIVE; then
        read -rp "Continue as root? [y/N] " yn
        [[ "${yn,,}" == "y" ]] || die "Aborted by user"
    fi
fi

if ! command -v apt-get >/dev/null 2>&1; then
    die "apt-get not found — this script targets Ubuntu/Debian"
fi

ok "Project: $PROJECT_DIR"
ok "User:    $(whoami)  (HOME=$HOME)"

# ─── 1. System packages ──────────────────────────────────────────────────────
section "1. System packages"

# Use sudo only if not already root
SUDO=""
if [[ "$EUID" -ne 0 ]]; then
    SUDO="sudo"
    if ! sudo -n true 2>/dev/null; then
        log "sudo will prompt for your password..."
    fi
fi

log "apt-get update + installing essentials..."
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
    curl ca-certificates build-essential \
    libsqlite3-dev tzdata cron \
    libxml2-dev libxslt1-dev zlib1g-dev \
    >/dev/null
ok "system packages installed"

# Ensure cron is enabled + running
$SUDO systemctl enable --now cron >/dev/null 2>&1 || true

# ─── 2. Timezone ─────────────────────────────────────────────────────────────
section "2. Timezone"

CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "unknown")
if [[ "$CURRENT_TZ" != "Asia/Kolkata" ]]; then
    log "Setting timezone Asia/Kolkata (was $CURRENT_TZ)..."
    $SUDO timedatectl set-timezone Asia/Kolkata
    ok "timezone Asia/Kolkata"
else
    ok "timezone already Asia/Kolkata"
fi
ok "current time: $(date)"

# ─── 3. uv ───────────────────────────────────────────────────────────────────
section "3. uv (Python package manager)"

if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
    export PATH="$HOME/.local/bin:$PATH"
fi

# Re-source PATH for the current shell (uv installer modifies .bashrc but doesn't
# necessarily affect THIS shell)
[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || export PATH="$HOME/.local/bin:$PATH"

command -v uv >/dev/null 2>&1 || die "uv install failed; check ~/.local/bin"
ok "uv $(uv --version | awk '{print $2}')"

# ─── 4. Python deps ──────────────────────────────────────────────────────────
section "4. Python deps (uv sync)"

uv sync 2>&1 | tail -3
ok "venv ready at $PROJECT_DIR/.venv"

# ─── 5. .env ─────────────────────────────────────────────────────────────────
section "5. .env"

# Load any existing values
declare -A ENV_KV
if [[ -f .env ]]; then
    while IFS='=' read -r k v; do
        [[ -z "$k" || "$k" == \#* ]] && continue
        ENV_KV["$k"]="$v"
    done < .env
    ok ".env exists (will preserve existing values)"
fi

prompt_required() {
    local key="$1" prompt="$2"
    if [[ -n "${ENV_KV[$key]:-}" && "${ENV_KV[$key]}" != "" ]]; then
        return
    fi
    if $NO_INTERACTIVE; then
        die "$key not set in .env (run interactively, or set manually first)"
    fi
    read -rp "$prompt: " value
    [[ -n "$value" ]] || die "$key required"
    ENV_KV["$key"]="$value"
}

prompt_optional() {
    local key="$1" prompt="$2" default="${3:-}"
    if [[ -n "${ENV_KV[$key]:-}" ]]; then
        return
    fi
    if $NO_INTERACTIVE; then
        ENV_KV["$key"]="$default"
        return
    fi
    if [[ -n "$default" ]]; then
        read -rp "$prompt [$default]: " value
        ENV_KV["$key"]="${value:-$default}"
    else
        read -rp "$prompt (blank to skip): " value
        ENV_KV["$key"]="${value:-}"
    fi
}

prompt_required OPENROUTER_API_KEY "OpenRouter API key (sk-or-v1-...)"
prompt_optional CAPITAL_INR        "Starting capital in ₹" "500000"
prompt_optional TELEGRAM_BOT_TOKEN "Telegram bot token (optional)"
if [[ -n "${ENV_KV[TELEGRAM_BOT_TOKEN]:-}" ]]; then
    prompt_optional TELEGRAM_CHAT_ID "Telegram chat ID"
fi

# Production-recommended defaults — set if missing
[[ -z "${ENV_KV[OPENROUTER_BASE_URL]:-}" ]] && ENV_KV[OPENROUTER_BASE_URL]="https://openrouter.ai/api/v1"
[[ -z "${ENV_KV[STOCKAGENT_DB_PATH]:-}"  ]] && ENV_KV[STOCKAGENT_DB_PATH]="data/stockagent.db"

# Models — always set to the verified-working multimodal model
WORKING_MODEL="google/gemini-3-flash-preview"
for k in MODEL_TECHNICAL MODEL_FUNDAMENTAL MODEL_SENTIMENT MODEL_MACRO MODEL_COORDINATOR; do
    if [[ -z "${ENV_KV[$k]:-}" || "${ENV_KV[$k]}" == "moonshotai/kimi-k2.5" ]]; then
        ENV_KV[$k]="$WORKING_MODEL"
    fi
done

# Backup existing .env, write new
[[ -f .env ]] && cp .env .env.bak.$(date +%s)
{
    echo "# Generated by deploy.sh on $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "# Edit values manually or re-run ./deploy.sh"
    echo
    echo "CAPITAL_INR=${ENV_KV[CAPITAL_INR]}"
    echo
    echo "OPENROUTER_API_KEY=${ENV_KV[OPENROUTER_API_KEY]}"
    echo "OPENROUTER_BASE_URL=${ENV_KV[OPENROUTER_BASE_URL]}"
    echo
    echo "MODEL_TECHNICAL=${ENV_KV[MODEL_TECHNICAL]}"
    echo "MODEL_FUNDAMENTAL=${ENV_KV[MODEL_FUNDAMENTAL]}"
    echo "MODEL_SENTIMENT=${ENV_KV[MODEL_SENTIMENT]}"
    echo "MODEL_MACRO=${ENV_KV[MODEL_MACRO]}"
    echo "MODEL_COORDINATOR=${ENV_KV[MODEL_COORDINATOR]}"
    echo
    echo "STOCKAGENT_DB_PATH=${ENV_KV[STOCKAGENT_DB_PATH]}"
    echo
    echo "TELEGRAM_BOT_TOKEN=${ENV_KV[TELEGRAM_BOT_TOKEN]:-}"
    echo "TELEGRAM_CHAT_ID=${ENV_KV[TELEGRAM_CHAT_ID]:-}"
} > .env
chmod 600 .env
ok ".env written (mode 600)"

# ─── 6. OpenRouter API smoke test ────────────────────────────────────────────
section "6. OpenRouter API smoke test"

if uv run python -c "
import stockagent
from stockagent.agents.base import call_llm
r = call_llm(model='${ENV_KV[MODEL_TECHNICAL]}',
             system='Reply with one word: ok', user='ping', max_tokens=10)
print('response:', repr(r.content[:30]))
" 2>&1 | tail -2; then
    ok "OpenRouter API working"
else
    die "OpenRouter API test failed — check OPENROUTER_API_KEY"
fi

# ─── 7. Telegram smoke test ──────────────────────────────────────────────────
if [[ -n "${ENV_KV[TELEGRAM_BOT_TOKEN]:-}" && -n "${ENV_KV[TELEGRAM_CHAT_ID]:-}" ]]; then
    if ! $SKIP_TG_TEST; then
        section "7. Telegram smoke test"
        if uv run python -c "
import stockagent
from stockagent.alerts.telegram import send_telegram
ok = send_telegram('🚀 stockagent VPS deploy test — $(date \"+%Y-%m-%d %H:%M %Z\")')
print('telegram', 'sent' if ok else 'FAILED')
" 2>&1 | tail -1 | grep -q sent; then
            ok "Telegram delivery confirmed"
        else
            warn "Telegram test failed (continuing — fix TELEGRAM_* in .env later)"
        fi
    fi
else
    warn "Telegram not configured (no daily push). Edit .env to add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."
fi

# ─── 8. DB init ──────────────────────────────────────────────────────────────
section "8. Initialize DB"
uv run stockagent init-db | tail -3
ok "DB ready"

# ─── 9. Historical backfill ──────────────────────────────────────────────────
ROW_COUNT=$(uv run python -c "
import stockagent
from sqlalchemy import text
from stockagent.db.session import get_engine
with get_engine().connect() as c:
    print(c.execute(text('SELECT COUNT(*) FROM prices')).scalar())
" 2>/dev/null || echo "0")

if $SKIP_BACKFILL; then
    section "9. Historical backfill — SKIPPED (--skip-backfill)"
elif [[ "$ROW_COUNT" -gt 100000 ]]; then
    section "9. Historical backfill — already done"
    ok "DB has $ROW_COUNT price rows; skipping backfill"
else
    section "9. Historical backfill (~16 minutes)"
    log "Pulling 6 years of NSE EQ bhav (full universe)..."
    log "${DIM}This is a one-time cost. Subsequent daily-tick runs add only today's bar.${NC}"
    uv run stockagent backfill-bhav \
        --start 2020-01-01 --end "$(date +%Y-%m-%d)" --universe all 2>&1 | tail -5
    ok "backfill done"
fi

# ─── 10. Sector map + corp actions ───────────────────────────────────────────
section "10. Sector map + corporate-actions calendar"
uv run python -c "
import stockagent
from stockagent.data.sectors import refresh_sector_map
from stockagent.data.events import refresh_corporate_actions
m = refresh_sector_map()
print(f'sector map: {len(m)} symbols')
n = refresh_corporate_actions(lookahead_days=60)
print(f'corp actions: {n} events stored')
" 2>&1 | tail -4
ok "support data refreshed"

# ─── 11. Reset paper state for clean run ─────────────────────────────────────
section "11. Reset paper state"
uv run python -c "
import stockagent
from stockagent.paper_trade.ledger import reset_paper_state
reset_paper_state()
print('paper state cleared')
" 2>&1 | tail -1
ok "ledger reset; ready for fresh 1-month run"

# ─── 12. Smoke-test daily-tick ───────────────────────────────────────────────
section "12. Smoke-test daily-tick"
log "Running with --skip-bhav-refresh --skip-movers --skip-events to keep it fast..."
uv run stockagent daily-tick --skip-bhav-refresh --skip-movers --skip-events --no-telegram 2>&1 | tail -10 || warn "daily-tick smoke had issues (review above)"
ok "smoke test complete"

# ─── 13. Cron ────────────────────────────────────────────────────────────────
if $SKIP_CRON; then
    section "13. Cron — SKIPPED (--skip-cron)"
else
    section "13. Install cron entries"
    UV_BIN="$(command -v uv)"
    mkdir -p "$PROJECT_DIR/logs"

    CRON_FILE=$(mktemp)
    crontab -l 2>/dev/null > "$CRON_FILE" || true

    # Strip any existing stockagent entries
    grep -v -E "(stockagent |stockagent\.db|stockagent$)" "$CRON_FILE" > "${CRON_FILE}.new" 2>/dev/null || true
    mv "${CRON_FILE}.new" "$CRON_FILE"

    cat >> "$CRON_FILE" <<EOF

# stockagent — daily-tick, Mon-Fri 16:30 IST (NSE post-close + bhav posted)
30 16 * * 1-5 cd $PROJECT_DIR && $UV_BIN run stockagent daily-tick >> $PROJECT_DIR/logs/daily-tick.log 2>&1
# stockagent — weekly DB backup, Sunday 00:00
0 0 * * 0 mkdir -p \$HOME/backups && cp $PROJECT_DIR/data/stockagent.db \$HOME/backups/stockagent-\$(date +\%Y\%m\%d).db
EOF

    crontab "$CRON_FILE"
    rm "$CRON_FILE"
    ok "cron installed"
    crontab -l | grep -E "stockagent" | sed 's/^/   /'
fi

# ─── Summary ────────────────────────────────────────────────────────────────
echo
printf '%s%s═══════════════════════════════════════════════════════%s\n' "$BOLD" "$GREEN" "$NC"
printf '%s%s  Deploy complete%s\n' "$BOLD" "$GREEN" "$NC"
printf '%s%s═══════════════════════════════════════════════════════%s\n' "$BOLD" "$GREEN" "$NC"
echo
printf '  Project:     %s\n' "$PROJECT_DIR"
printf '  Capital:     ₹%s\n' "${ENV_KV[CAPITAL_INR]}"
printf '  Model:       %s (all 4 agents)\n' "${ENV_KV[MODEL_TECHNICAL]}"
printf '  DB:          %s/data/stockagent.db (%s rows)\n' "$PROJECT_DIR" "$ROW_COUNT"
printf '  Logs:        %s/logs/daily-tick.log\n' "$PROJECT_DIR"
if $SKIP_CRON; then
    printf '  Cron:        SKIPPED — install manually if needed\n'
else
    printf '  Cron:        Mon-Fri 16:30 IST (daily-tick) + Sun 00:00 (backup)\n'
fi
if [[ -n "${ENV_KV[TELEGRAM_BOT_TOKEN]:-}" && -n "${ENV_KV[TELEGRAM_CHAT_ID]:-}" ]]; then
    printf '  Telegram:    configured\n'
else
    printf '  Telegram:    %sNOT configured%s — daily summaries will be CLI-only\n' "$YELLOW" "$NC"
fi
echo
echo "  Next cron fire:  $(date -d 'next monday 16:30' '+%Y-%m-%d %H:%M %Z' 2>/dev/null || echo 'next Mon-Fri 16:30 IST')"
echo
echo "  Useful commands:"
echo "    uv run stockagent paper-status         # current portfolio"
echo "    uv run stockagent paper-summary        # P&L summary"
echo "    uv run stockagent symbol-profile --top 20"
echo "    tail -f logs/daily-tick.log"
echo
echo "  Read DEPLOY.md and ACTIONS.md for full reference."
echo
echo "  Wait 30 days, then: uv run stockagent paper-summary"
