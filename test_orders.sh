#!/bin/bash
# ============================================================
# Comprehensive Order Placement Test Script
# Tests all 10 order types × open+close = 20+ API calls
# Usage: bash test_orders.sh [APP_URL]
# Default APP_URL: http://localhost:5000
# ============================================================

APP_URL="${1:-http://localhost:5000}"
PASS=0
FAIL=0
LOG_FILE="test_orders_$(date +%Y%m%d_%H%M%S).log"

log() { echo "$@" | tee -a "$LOG_FILE"; }
log_pass() { PASS=$((PASS+1)); log "  ✓ PASS: $@"; }
log_fail() { FAIL=$((FAIL+1)); log "  ✗ FAIL: $@"; }

check_jq() {
    if ! command -v jq &>/dev/null; then
        log "WARNING: jq not found, using python3 for JSON parsing"
        JQ_CMD="python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('success','?'), d.get('error',''), d.get('positions_opened',''), d.get('pnl_usd',''))\""
    fi
}

get_json() {
    python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d))" 2>/dev/null
}

get_field() {
    local json="$1"
    local field="$2"
    echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field',''))" 2>/dev/null
}

# Wait for a position to appear and return its ID
get_positions() {
    curl -s "$APP_URL/api/test-order/status"
}

# Open a test order and return position ID(s)
open_order() {
    local order_type="$1"
    local size="${2:-100}"
    log ""
    log "  → Opening $order_type (size=$size USD)..."
    
    local resp
    resp=$(curl -s -X POST "$APP_URL/api/test-order/open" \
        -H "Content-Type: application/json" \
        -d "{\"order_type\": \"$order_type\", \"size_usd\": $size}")
    
    local success
    success=$(get_field "$resp" "success")
    local error
    error=$(get_field "$resp" "error")
    local opened
    opened=$(get_field "$resp" "positions_opened")
    
    if [ "$success" = "True" ] || [ "$success" = "true" ]; then
        log_pass "$order_type open: $opened position(s) opened"
        echo "ok"
    else
        log_fail "$order_type open: $error"
        echo "fail"
    fi
}

# Get position IDs from status endpoint
get_position_ids() {
    curl -s "$APP_URL/api/test-order/status" | \
        python3 -c "import sys,json; d=json.load(sys.stdin); [print(p['id']) for p in d.get('positions',[])]" 2>/dev/null
}

# Close a position by ID
close_position() {
    local pos_id="$1"
    local label="$2"
    log "  → Closing position $pos_id ($label)..."
    
    local resp
    resp=$(curl -s -X POST "$APP_URL/api/test-order/close" \
        -H "Content-Type: application/json" \
        -d "{\"position_id\": \"$pos_id\"}")
    
    local success
    success=$(get_field "$resp" "success")
    local error
    error=$(get_field "$resp" "error")
    local pnl
    pnl=$(get_field "$resp" "pnl_usd")
    local msg
    msg=$(get_field "$resp" "message")
    
    if [ "$success" = "True" ] || [ "$success" = "true" ]; then
        log_pass "$label close: pnl=\$$pnl ${msg}"
        return 0
    else
        log_fail "$label close: $error"
        return 1
    fi
}

# Close ALL open positions
close_all() {
    local label="$1"
    local ids
    ids=$(get_position_ids)
    if [ -z "$ids" ]; then
        log "  (No open positions to close)"
        return
    fi
    for id in $ids; do
        close_position "$id" "$label"
        sleep 2
    done
}

# Run one scenario: open and close with delay
run_scenario() {
    local scenario="$1"
    local order_type="$2"
    
    log ""
    log "========================================"
    log "SCENARIO: $scenario"
    log "========================================"
    
    local before_ids after_ids new_ids
    before_ids=$(get_position_ids | sort)
    
    result=$(open_order "$order_type" 100)
    
    if [ "$result" = "fail" ]; then
        log "  Skipping close (open failed)"
        return
    fi
    
    sleep 3
    
    after_ids=$(get_position_ids | sort)
    # Find new position IDs
    new_ids=$(comm -13 <(echo "$before_ids") <(echo "$after_ids"))
    
    if [ -z "$new_ids" ]; then
        log "  WARNING: No new positions found after open"
        return
    fi
    
    for id in $new_ids; do
        close_position "$id" "$order_type"
        sleep 2
    done
}

# ============================================================
# MAIN TEST EXECUTION
# ============================================================

log "============================================================"
log "OKX Order Placement Tests - $(date)"
log "App URL: $APP_URL"
log "============================================================"

# Check engine status
log ""
log "Checking engine status..."
STATUS=$(curl -s "$APP_URL/api/engine/status")
SPOT_CONN=$(get_field "$STATUS" "spot_connected")
FUT_CONN=$(get_field "$STATUS" "futures_connected")
log "  Spot connected: $SPOT_CONN"
log "  Futures connected: $FUT_CONN"

if [ "$SPOT_CONN" != "True" ] && [ "$SPOT_CONN" != "true" ]; then
    log ""
    log "ERROR: Spot adapter not connected. Configure OKX exchange in the UI first."
    exit 1
fi

# Check order mode
ORDER_MODE=$(curl -s "$APP_URL/api/config" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('order_execution_mode','MARKET'))")
log "  Order execution mode: $ORDER_MODE"

log ""
log "============================================================"
log "PHASE 1: SINGLE LEG - BUY SPOT"
log "============================================================"

# Test 1: BUY_SPOT open + close
run_scenario "1. BUY_SPOT" "BUY_SPOT"
sleep 3

# Test 2: BUY_SPOT again
run_scenario "2. BUY_SPOT (2nd attempt)" "BUY_SPOT"
sleep 3

# Test 3: BUY_SPOT again
run_scenario "3. BUY_SPOT (3rd attempt)" "BUY_SPOT"
sleep 3

log ""
log "============================================================"
log "PHASE 2: SINGLE LEG - SELL FUTURES"
log "============================================================"

# Test 4: SELL_FUTURES
run_scenario "4. SELL_FUTURES" "SELL_FUTURES"
sleep 3

# Test 5: SELL_FUTURES again
run_scenario "5. SELL_FUTURES (2nd attempt)" "SELL_FUTURES"
sleep 3

# Test 6: SELL_FUTURES again
run_scenario "6. SELL_FUTURES (3rd attempt)" "SELL_FUTURES"
sleep 3

log ""
log "============================================================"
log "PHASE 3: SINGLE LEG - BUY FUTURES"
log "============================================================"

# Test 7: BUY_FUTURES
run_scenario "7. BUY_FUTURES" "BUY_FUTURES"
sleep 3

# Test 8: BUY_FUTURES again
run_scenario "8. BUY_FUTURES (2nd attempt)" "BUY_FUTURES"
sleep 3

log ""
log "============================================================"
log "PHASE 4: SINGLE LEG - SELL SPOT"
log "============================================================"

# Test 9: SELL_SPOT
run_scenario "9. SELL_SPOT" "SELL_SPOT"
sleep 3

# Test 10: SELL_SPOT again
run_scenario "10. SELL_SPOT (2nd attempt)" "SELL_SPOT"
sleep 3

log ""
log "============================================================"
log "PHASE 5: SPREAD - LONG (Buy Spot + Sell Futures)"
log "============================================================"

# Test 11: LONG_SPREAD
run_scenario "11. LONG_SPREAD" "LONG_SPREAD"
sleep 5

# Test 12: LONG_SPREAD again
run_scenario "12. LONG_SPREAD (2nd attempt)" "LONG_SPREAD"
sleep 5

# Test 13: LONG_SPREAD again
run_scenario "13. LONG_SPREAD (3rd attempt)" "LONG_SPREAD"
sleep 5

log ""
log "============================================================"
log "PHASE 6: SPREAD - SHORT (Sell Spot + Buy Futures)"
log "============================================================"

# Test 14: SHORT_SPREAD
run_scenario "14. SHORT_SPREAD" "SHORT_SPREAD"
sleep 5

# Test 15: SHORT_SPREAD again
run_scenario "15. SHORT_SPREAD (2nd attempt)" "SHORT_SPREAD"
sleep 5

# Test 16: SHORT_SPREAD again
run_scenario "16. SHORT_SPREAD (3rd attempt)" "SHORT_SPREAD"
sleep 5

# Clean up any remaining positions
log ""
log "Cleaning up any remaining positions..."
close_all "cleanup"

log ""
log "============================================================"
log "TEST RESULTS SUMMARY"
log "============================================================"
log "  PASSED: $PASS"
log "  FAILED: $FAIL"
log "  TOTAL:  $((PASS+FAIL))"
log ""
log "Full log saved to: $LOG_FILE"
log "============================================================"

if [ $FAIL -gt 0 ]; then
    exit 1
fi
exit 0

# NOTE: To also test LIMIT orders:
# 1. Change order_execution_mode to LIMIT in the UI Settings
# 2. Re-run this script: bash test_orders.sh
