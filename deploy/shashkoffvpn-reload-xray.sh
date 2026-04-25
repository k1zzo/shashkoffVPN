#!/usr/bin/env bash
# shashkoffvpn-reload-xray — host-side Xray reload script for ShashkoffVPN
#
# This script runs on the HOST (not inside the Docker container).
# It is the single trusted entrypoint for applying ShashkoffVPN client
# changes to the running Xray process.
#
# What it does:
#   1. Reads the clients fragment written by the app to the shared data dir.
#   2. Merges that fragment into the base Xray config via a Python helper.
#   3. Validates the merged config with `xray run -test`.
#   4. If validation passes: atomically replaces the live config, restarts Xray.
#   5. If validation fails: exits non-zero, live config is NOT replaced.
#
# Never restarts Xray with an invalid config.
# Exits non-zero on any failure so the caller can detect and log it.
#
# ── Installation ──────────────────────────────────────────────────────────────
#
#   sudo cp deploy/shashkoffvpn-reload-xray.sh /usr/local/bin/shashkoffvpn-reload-xray
#   sudo cp deploy/shashkoffvpn-merge-xray-clients.py /usr/local/lib/shashkoffvpn-merge-xray-clients.py
#   sudo chmod +x /usr/local/bin/shashkoffvpn-reload-xray
#
# ── Xray config split ─────────────────────────────────────────────────────────
#
# This script requires a "split config" setup for Xray:
#
#   /usr/local/etc/xray/config.base.json  — everything except clients
#                                          (inbound def, reality settings, routing).
#                                          Managed by the operator. Never overwritten here.
#
#   /usr/local/etc/xray/config.json       — the LIVE config Xray actually runs.
#                                          Always the OUTPUT of this script (do not edit
#                                          manually — changes will be overwritten on the
#                                          next device add/remove).
#
# Initial setup:
#   1. Copy your existing /usr/local/etc/xray/config.json → config.base.json
#   2. Remove the "clients" list from config.base.json (leave it as [])
#   3. Run this script once to regenerate config.json from the app's client list
#
# ── Paths (adjust to match your deployment) ───────────────────────────────────

# Path where the app writes the clients JSON array (inside Docker:
# /app/data/xray-clients.json, which maps to this host path via the data
# volume mount).
CLIENTS_FILE="${XRAY_CLIENTS_FILE:-/opt/shashkoffVPN/data/xray-clients.json}"

# Base Xray config — managed by the operator, never overwritten by this script.
BASE_CONFIG="${XRAY_BASE_CONFIG:-/usr/local/etc/xray/config.base.json}"

# Live Xray config — always the merged output. Do not edit manually.
LIVE_CONFIG="${XRAY_LIVE_CONFIG:-/usr/local/etc/xray/config.json}"

# Temporary merged config file written before validation.
MERGED_CONFIG="${XRAY_MERGED_CONFIG:-/usr/local/etc/xray/config.merging.json}"

# Xray binary path.
XRAY_BINARY="${XRAY_BINARY:-/usr/local/bin/xray}"

# Systemd service name for Xray.
XRAY_SERVICE="${XRAY_SERVICE:-xray}"

# Python merge helper (installed alongside this script).
MERGE_HELPER="${XRAY_MERGE_HELPER:-/usr/local/lib/shashkoffvpn-merge-xray-clients.py}"

# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] shashkoffvpn-reload-xray: $*"
}

err() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] shashkoffvpn-reload-xray: ERROR: $*" >&2
}

# ── Step 1: verify inputs ─────────────────────────────────────────────────────

if [ ! -f "$CLIENTS_FILE" ]; then
    err "Clients fragment not found: $CLIENTS_FILE"
    err "Has the app written a clients config yet? Check XRAY_CLIENTS_CONFIG_PATH in .env."
    exit 1
fi

if [ ! -f "$BASE_CONFIG" ]; then
    err "Base Xray config not found: $BASE_CONFIG"
    err "Create $BASE_CONFIG from your existing /usr/local/etc/xray/config.json"
    err "(remove the 'clients' list — it will be injected by this script)."
    exit 1
fi

if [ ! -x "$XRAY_BINARY" ]; then
    err "Xray binary not found or not executable: $XRAY_BINARY"
    err "Set XRAY_BINARY env var to the correct path."
    exit 1
fi

if [ ! -f "$MERGE_HELPER" ]; then
    err "Merge helper not found: $MERGE_HELPER"
    err "Install: sudo cp deploy/shashkoffvpn-merge-xray-clients.py $MERGE_HELPER"
    exit 1
fi

# ── Step 2: merge clients fragment into base config ───────────────────────────

log "Merging $(wc -l < "$CLIENTS_FILE") bytes from $CLIENTS_FILE into $BASE_CONFIG ..."
if ! python3 "$MERGE_HELPER" "$CLIENTS_FILE" "$BASE_CONFIG" "$MERGED_CONFIG"; then
    err "Merge failed. Xray was NOT reloaded."
    rm -f "$MERGED_CONFIG"
    exit 1
fi

# ── Step 3: validate merged config ───────────────────────────────────────────

log "Validating merged config with xray run -test ..."
if ! "$XRAY_BINARY" run -test -config "$MERGED_CONFIG" 2>&1; then
    err "Xray config validation failed. Xray was NOT reloaded."
    err "Inspect $MERGED_CONFIG to diagnose the problem."
    rm -f "$MERGED_CONFIG"
    exit 1
fi
log "Config validation passed."

# ── Step 4: atomically replace live config ───────────────────────────────────

log "Replacing live config: $LIVE_CONFIG ..."
# mv on the same filesystem is atomic — Xray never reads a partial file.
mv "$MERGED_CONFIG" "$LIVE_CONFIG"

# ── Step 5: reload Xray ──────────────────────────────────────────────────────
# Note: we use `restart` rather than `reload` because Xray does not implement
# a SIGHUP reload handler. `restart` causes a brief (~1s) interruption of all
# active VLESS Reality sessions; clients reconnect automatically.

log "Restarting Xray ($XRAY_SERVICE) ..."
if ! systemctl restart "$XRAY_SERVICE"; then
    err "systemctl restart $XRAY_SERVICE failed."
    err "Config file has been updated but Xray is running the old client set."
    err "Check: systemctl status $XRAY_SERVICE"
    exit 1
fi

log "Done. Xray reloaded successfully with updated client set."
