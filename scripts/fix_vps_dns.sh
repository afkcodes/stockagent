#!/usr/bin/env bash
#
# fix_vps_dns.sh — Force /etc/resolv.conf to public DNS, permanently.
#
# Background: WARP, Tailscale, and other VPN clients hijack /etc/resolv.conf
# and point it at internal stub resolvers (127.0.2.2, 100.100.100.100, etc).
# When you uninstall the client, those entries often stay behind, breaking
# all DNS resolution on the VPS. This script:
#
#   1. Removes any immutable flag (so we can rewrite if previously locked)
#   2. Breaks any symlink (so resolvconf/systemd-resolved can't manage the file)
#   3. Writes Cloudflare + Google public DNS servers
#   4. Locks the file with chattr +i so nothing can rewrite it
#   5. Verifies DNS and HTTPS connectivity
#
# Idempotent — safe to re-run anytime.
# To temporarily allow edits: sudo chattr -i /etc/resolv.conf
#
# Usage:
#   sudo ./scripts/fix_vps_dns.sh
#   ./scripts/fix_vps_dns.sh   (will prompt for sudo)

set -euo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED=$'\e[0;31m'; GREEN=$'\e[0;32m'; YELLOW=$'\e[0;33m'; BLUE=$'\e[0;34m'
BOLD=$'\e[1m'; NC=$'\e[0m'

log()  { printf '%s▸%s %s\n' "$BLUE"  "$NC" "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$NC" "$*"; }
err()  { printf '%s✗%s %s\n' "$RED"   "$NC" "$*" >&2; }
section() { printf '\n%s%s═══ %s ═══%s\n' "$BOLD" "$BLUE" "$*" "$NC"; }

# Use sudo only if not already root
if [[ "$EUID" -ne 0 ]]; then
    SUDO="sudo"
    if ! sudo -n true 2>/dev/null; then
        log "sudo will prompt for your password..."
    fi
else
    SUDO=""
fi

# ─── 1. Show the broken state we're fixing ──────────────────────────────────
section "Current /etc/resolv.conf"
if [[ -e /etc/resolv.conf ]]; then
    cat /etc/resolv.conf
    echo
    if [[ -L /etc/resolv.conf ]]; then
        warn "It's a symlink — resolvconf/systemd-resolved is managing it. We'll break that."
    fi
    if lsattr /etc/resolv.conf 2>/dev/null | head -1 | grep -q "i"; then
        warn "File is immutable (chattr +i). We'll temporarily unlock to rewrite."
    fi
else
    warn "/etc/resolv.conf doesn't exist — will create it."
fi

# ─── 2. Remove immutable flag if set ─────────────────────────────────────────
if [[ -e /etc/resolv.conf ]] && lsattr /etc/resolv.conf 2>/dev/null | head -1 | grep -q "i"; then
    log "Removing immutable flag..."
    $SUDO chattr -i /etc/resolv.conf
fi

# ─── 3. Break symlink if present ─────────────────────────────────────────────
if [[ -L /etc/resolv.conf ]]; then
    log "Breaking symlink..."
    $SUDO unlink /etc/resolv.conf
fi

# ─── 4. Write clean DNS config ───────────────────────────────────────────────
section "Writing clean DNS config"
$SUDO tee /etc/resolv.conf > /dev/null <<EOF
# Managed by stockagent/scripts/fix_vps_dns.sh
# To edit:  sudo chattr -i /etc/resolv.conf
# To re-lock after edit: sudo chattr +i /etc/resolv.conf
nameserver 1.1.1.1
nameserver 1.0.0.1
nameserver 8.8.8.8
options timeout:2 attempts:2 rotate
EOF
ok "wrote /etc/resolv.conf"

# ─── 5. Lock the file so nothing can regenerate it ───────────────────────────
log "Locking file with chattr +i..."
$SUDO chattr +i /etc/resolv.conf
ok "file locked (immutable)"

cat /etc/resolv.conf
echo

# ─── 6. Verify DNS resolution ────────────────────────────────────────────────
section "Verifying DNS"
all_dns_ok=true
for hostname in google.com cloudflare.com nseindia.com nsearchives.nseindia.com; do
    if result=$(host "$hostname" 2>&1) && echo "$result" | grep -qE "address|alias"; then
        ip=$(echo "$result" | grep -oE "address [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | head -1 | awk '{print $2}')
        ok "$hostname → ${ip:-resolves}"
    else
        err "$hostname → FAILED to resolve"
        echo "    $result"
        all_dns_ok=false
    fi
done

# ─── 7. Verify HTTPS connectivity ────────────────────────────────────────────
section "Verifying HTTPS"
all_http_ok=true
for url in https://www.google.com https://nseindia.com; do
    if elapsed=$(curl -o /dev/null -s -w "%{time_total}" --max-time 8 "$url" 2>/dev/null); then
        if (( $(echo "$elapsed < 3" | bc -l 2>/dev/null || echo 1) )); then
            ok "$url → ${elapsed}s"
        else
            warn "$url → ${elapsed}s (slow but works)"
        fi
    else
        err "$url → timeout / connection failed"
        all_http_ok=false
    fi
done

# ─── Summary ─────────────────────────────────────────────────────────────────
echo
if $all_dns_ok && $all_http_ok; then
    printf '%s%s═══════════════════════════════════════════════════════%s\n' "$BOLD" "$GREEN" "$NC"
    printf '%s%s  DNS is healthy. VPS is ready.%s\n' "$BOLD" "$GREEN" "$NC"
    printf '%s%s═══════════════════════════════════════════════════════%s\n' "$BOLD" "$GREEN" "$NC"
    echo
    echo "  /etc/resolv.conf is locked (immutable). To edit later:"
    echo "    sudo chattr -i /etc/resolv.conf"
    echo "    sudo nano /etc/resolv.conf"
    echo "    sudo chattr +i /etc/resolv.conf"
    echo
elif $all_dns_ok; then
    warn "DNS works but HTTPS is slow/failing. May be transient — retry in a minute."
else
    err "DNS still broken. Check for active VPN/proxy services:"
    echo "    sudo systemctl list-units --type=service --state=running | grep -iE 'warp|tailscale|wireguard|openvpn'"
    echo "    sudo ss -tlnp | grep ':53'    # any local DNS resolver listening?"
    exit 1
fi
