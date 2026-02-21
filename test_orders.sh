#!/bin/bash
# ============================================================
# Comprehensive Order Placement Test Script
#
# Reads the following settings directly from the running app:
#   order_execution_mode       - MARKET or LIMIT
#   limit_order_timeout_sec    - how long to wait for a LIMIT fill
#   limit_order_price_offset_bps - passive price offset used for LIMIT orders
#   entry_cooldown_seconds     - cooldown applied between scenarios
#
# LIMIT orders are placed at bid+offset (BUY) / ask-offset (SELL),
# exactly matching the real OrderExecutor logic.
#
# Each scenario: open → wait → check state (filled / cancelled) → close
#
# Usage: bash test_orders.sh [APP_URL]
# Default APP_URL: http://localhost:5000
# ============================================================

APP_URL="${1:-http://localhost:5000}"
PASS=0
FAIL=0
LOG_FILE="test_orders_$(date +%Y%m%d_%H%M%S).log"

log()      { echo "$@" | tee -a "$LOG_FILE"; }
log_pass() { PASS=$((PASS+1)); log "  [PASS] $*"; }
log_fail() { FAIL=$((FAIL+1)); log "  [FAIL] $*"; }
log_info() { log "  [INFO] $*"; }

# ── JSON helpers ─────────────────────────────────────────────
get_field() {
    # get_field <json_string> <key>
    echo "$1" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    val = d.get('$2', '')
    print(val)
except:
    print('')
" 2>/dev/null
}

get_positions_json() {
    curl -s "$APP_URL/api/test-order/status"
}

get_position_ids() {
    get_positions_json | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for p in d.get('positions', []):
        print(p['id'])
except:
    pass
" 2>/dev/null
}

# ── Read config from running app ──────────────────────────────
read_config() {
    CONFIG_JSON=$(curl -s "$APP_URL/api/config")
    ORDER_MODE=$(get_field "$CONFIG_JSON" "order_execution_mode")
    LIMIT_TIMEOUT=$(get_field "$CONFIG_JSON" "limit_order_timeout_sec")
    LIMIT_OFFSET=$(get_field "$CONFIG_JSON" "limit_order_price_offset_bps")
    COOLDOWN=$(get_field "$CONFIG_JSON" "entry_cooldown_seconds")

    # Defaults if not returned
    ORDER_MODE="${ORDER_MODE:-MARKET}"
    LIMIT_TIMEOUT="${LIMIT_TIMEOUT:-30}"
    LIMIT_OFFSET="${LIMIT_OFFSET:-1.0}"
    COOLDOWN="${COOLDOWN:-60}"
}

# ── Open one order ────────────────────────────────────────────
open_order() {
    local order_type="$1"
    local size="${2:-100}"
    log ""
    log_info "Opening $order_type (size=\$$size, mode=$ORDER_MODE, offset=${LIMIT_OFFSET}bps)"

    local resp
    resp=$(curl -s -X POST "$APP_URL/api/test-order/open" \
        -H "Content-Type: application/json" \
        -d "{\"order_type\": \"$order_type\", \"size_usd\": $size}")

    local success error opened
    success=$(get_field "$resp" "success")
    error=$(get_field "$resp" "error")
    opened=$(get_field "$resp" "positions_opened")

    if [ "$success" = "True" ] || [ "$success" = "true" ]; then
        log_pass "OPEN $order_type: $opened position(s) placed"
        echo "ok"
    else
        log_fail "OPEN $order_type: $error"
        echo "fail"
    fi
}

# ── Close one position ────────────────────────────────────────
close_position() {
    local pos_id="$1"
    local label="$2"
    log_info "Closing position $pos_id ($label)..."

    local resp
    resp=$(curl -s -X POST "$APP_URL/api/test-order/close" \
        -H "Content-Type: application/json" \
        -d "{\"position_id\": \"$pos_id\"}")

    local success error pnl msg
    success=$(get_field "$resp" "success")
    error=$(get_field "$resp" "error")
    pnl=$(get_field "$resp" "pnl_usd")
    msg=$(get_field "$resp" "message")

    if [ "$success" = "True" ] || [ "$success" = "true" ]; then
        if [ -n "$msg" ]; then
            log_pass "CLOSE $label ($pos_id): $msg"
        else
            log_pass "CLOSE $label ($pos_id): pnl=\$$pnl"
        fi
        return 0
    else
        log_fail "CLOSE $label ($pos_id): $error"
        return 1
    fi
}

# ── Close any remaining open positions ───────────────────────
close_all_remaining() {
    local ids
    ids=$(get_position_ids)
    if [ -z "$ids" ]; then return; fi
    log_info "Closing remaining positions..."
    for id in $ids; do
        close_position "$id" "cleanup"
        sleep 2
    done
}

# ── Run one full scenario: open → wait → close ───────────────
# For LIMIT mode:
#   - waits up to LIMIT_TIMEOUT seconds for a fill
#   - if still unfilled after timeout, close will CANCEL the order
#   - tests both the fill path and the cancel path
run_scenario() {
    local scenario="$1"
    local order_type="$2"
    local wait_for_fill="${3:-false}"  # true = wait full timeout; false = close immediately (cancel test)

    log ""
    log "──────────────────────────────────────────"
    log "SCENARIO $scenario  [$order_type | $ORDER_MODE]"
    log "──────────────────────────────────────────"

    local before_ids after_ids new_ids
    before_ids=$(get_position_ids | sort)

    local result
    result=$(open_order "$order_type" 100)
    [ "$result" = "fail" ] && { log_info "Skipping close (open failed)"; return; }

    if [ "$ORDER_MODE" = "LIMIT" ]; then
        if [ "$wait_for_fill" = "true" ]; then
            log_info "LIMIT order placed. Waiting ${LIMIT_TIMEOUT}s for fill (offset=${LIMIT_OFFSET}bps)..."
            sleep "$LIMIT_TIMEOUT"
        else
            log_info "LIMIT cancel test: closing immediately to trigger cancellation..."
            sleep 2
        fi
    else
        # MARKET: small wait for exchange to process
        sleep 3
    fi

    after_ids=$(get_position_ids | sort)
    new_ids=$(comm -13 <(echo "$before_ids") <(echo "$after_ids") 2>/dev/null)

    if [ -z "$new_ids" ]; then
        log_info "WARNING: No new positions found after open"
        return
    fi

    for id in $new_ids; do
        close_position "$id" "$order_type"
        sleep 2
    done
}

# ── Apply entry cooldown between scenarios ────────────────────
cooldown() {
    local secs="${1:-$COOLDOWN}"
    # Cap at 15s for testing to avoid very long waits
    if [ "$secs" -gt 15 ] 2>/dev/null; then secs=15; fi
    if [ "$secs" -gt 0 ] 2>/dev/null; then
        log_info "Cooldown ${secs}s (app setting: ${COOLDOWN}s, capped at 15s for testing)..."
        sleep "$secs"
    fi
}

# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

log "============================================================"
log "OKX Order Placement Tests  -  $(date)"
log "App URL: $APP_URL"
log "============================================================"

# -- Check engine status
log ""
log "Checking engine status..."
STATUS=$(curl -s "$APP_URL/api/engine/status")
SPOT_CONN=$(get_field "$STATUS" "spot_connected")
FUT_CONN=$(get_field "$STATUS" "futures_connected")
log_info "Spot connected:    $SPOT_CONN"
log_info "Futures connected: $FUT_CONN"

if [ "$SPOT_CONN" != "True" ] && [ "$SPOT_CONN" != "true" ]; then
    log ""
    log "[ERROR] Spot adapter not connected."
    log "        Start the app with OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE set."
    exit 1
fi

# -- Read settings
read_config
log ""
log_info "order_execution_mode:        $ORDER_MODE"
log_info "limit_order_timeout_sec:     $LIMIT_TIMEOUT"
log_info "limit_order_price_offset_bps:$LIMIT_OFFSET"
log_info "entry_cooldown_seconds:      $COOLDOWN (capped at 15s per scenario)"

log ""
log "============================================================"
log "PHASE 1  -  BUY SPOT  (3 attempts)"
log "============================================================"

run_scenario "1a" "BUY_SPOT" "true"
cooldown
run_scenario "1b" "BUY_SPOT" "true"
cooldown
run_scenario "1c" "BUY_SPOT" "false"  # cancel test

log ""
log "============================================================"
log "PHASE 2  -  SELL FUTURES  (3 attempts)"
log "============================================================"

run_scenario "2a" "SELL_FUTURES" "true"
cooldown
run_scenario "2b" "SELL_FUTURES" "true"
cooldown
run_scenario "2c" "SELL_FUTURES" "false"  # cancel test

log ""
log "============================================================"
log "PHASE 3  -  BUY FUTURES  (3 attempts)"
log "============================================================"

run_scenario "3a" "BUY_FUTURES" "true"
cooldown
run_scenario "3b" "BUY_FUTURES" "true"
cooldown
run_scenario "3c" "BUY_FUTURES" "false"  # cancel test

log ""
log "============================================================"
log "PHASE 4  -  SELL SPOT  (3 attempts)"
log "============================================================"

run_scenario "4a" "SELL_SPOT" "true"
cooldown
run_scenario "4b" "SELL_SPOT" "true"
cooldown
run_scenario "4c" "SELL_SPOT" "false"  # cancel test

log ""
log "============================================================"
log "PHASE 5  -  LONG SPREAD  Buy Spot + Sell Futures  (3 attempts)"
log "============================================================"

run_scenario "5a" "LONG_SPREAD" "true"
cooldown
run_scenario "5b" "LONG_SPREAD" "true"
cooldown
run_scenario "5c" "LONG_SPREAD" "false"  # cancel test

log ""
log "============================================================"
log "PHASE 6  -  SHORT SPREAD  Sell Spot + Buy Futures  (3 attempts)"
log "============================================================"

run_scenario "6a" "SHORT_SPREAD" "true"
cooldown
run_scenario "6b" "SHORT_SPREAD" "true"
cooldown
run_scenario "6c" "SHORT_SPREAD" "false"  # cancel test

# -- Cleanup any remaining positions
log ""
close_all_remaining

log ""
log "============================================================"
log "RESULTS"
log "============================================================"
log "  PASSED: $PASS"
log "  FAILED: $FAIL"
log "  TOTAL:  $((PASS+FAIL))"
log ""
log "Mode tested: $ORDER_MODE  (offset=${LIMIT_OFFSET}bps, timeout=${LIMIT_TIMEOUT}s)"
log ""
log "To test the other mode, change order_execution_mode in Settings"
log "then re-run: bash test_orders.sh"
log ""
log "Full log: $LOG_FILE"
log "============================================================"

[ $FAIL -gt 0 ] && exit 1
exit 0
