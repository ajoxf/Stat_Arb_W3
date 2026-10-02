"""Default algo strategy parameters (merged under the stored algo_params).

One flat dict read by the SignalEngine and the AlgoTrader every tick, so a
settings save applies on the next tick — no restart. Fee/leverage/symbol
fields live in TradingConfig (trading_config table); everything strategy
lives here.
"""

DEFAULT_ALGO_PARAMS = {
    # ── signal window ────────────────────────────────────────────────────────
    "window_minutes": 120.0,          # rolling lookback for mean/σ/z
    "sample_interval_sec": 0.5,       # spread sample rate
    "min_signal_minutes": 90.0,       # warm-up before ANY trade (the ready gate)
    "stats_update_interval_sec": 300.0,  # recompute mean/σ every N s (stable bands)
    "hedge_ratio": 1.0,               # spread = k × spot − perp
    "max_quote_age_sec": 60.0,        # skip samples on a stalled feed
    "persist_window": True,           # resume the window across a quick restart
    "resume_max_gap_min": 10.0,
    "persist_interval_sec": 30.0,

    # ── mean & bands source ──────────────────────────────────────────────────
    "band_source": "ticks",           # ticks (rolling window) / candles (TV BB, EMA basis)
    "band_timeframe": "15m",          # 5m / 15m / 1h / 4h (candle mode)
    "band_length": 20,                # BB length N (candle mode)
    "max_hold_candles": 0,            # candle mode's time-stop; 0 = off

    # ── entry ────────────────────────────────────────────────────────────────
    "entry_zscore": 2.5,              # enter when the executable z goes beyond this
    "exit_zscore": 0.0,               # z-reversion reference (BE-gated secondary exit)
    "stop_zscore": 4.0,               # z blow-out stop
    "max_entry_zscore": 0.0,          # entry |z| cap (regime-shift guard); 0 = off
    "confirmation_ticks": 2,          # consecutive ticks beyond threshold before entry
    "trade_direction": "both",        # both / sell_only / buy_only
    "max_entry_z_divergence": 0.0,    # refuse entry if fill-time z moved; 0 = off
    "cooldown": 300.0,                # seconds flat after any exit
    "stop_cooldown": 900.0,           # longer cooldown after a stop
    "z_reset_after_stop": True,       # block same-direction re-entry until z re-enters

    # ── entry quality gates ──────────────────────────────────────────────────
    "min_edge_multiple": 1.5,         # expected capture must be ≥ this × cost
    "cost_floor_mult": 1.0,           # block if cost × this > plausible full reversion
    "half_life_min_sec": 0.0,         # reversion speed acceptance band; 0 = off
    "half_life_max_sec": 0.0,
    "enable_probability_filter": True,
    "min_win_probability": 0.60,      # OU gambler's-ruin gate
    "min_expected_value": 0.0,

    # ── exits ────────────────────────────────────────────────────────────────
    "tp_capital_pct": 0.5,            # TAKE PROFIT = break-even + this % of margin
    "profit_target_usd": 0.0,         # fixed-$ target (fallback when % unset)
    "profit_target_sigma_frac": 0.0,  # σ-fraction target (overrides % when set)
    "dollar_stop_usd": 0.0,           # fixed-$ stop (fallback)
    "stop_capital_pct": 2.0,          # stop = this % of margin (tighter of the two binds)
    "stop_rr": 0.0,                   # stop = target ÷ RR
    "reversion_require_profit": True, # the z-reversion exit never books a loss
    "reversion_gate_usd": 0.0,        # $ above BE required for the reversion exit
    "min_hold_sec": 30.0,             # suppress reversion/target exits this long
    "time_stop_half_lives": 3.0,      # max hold = this × half-life
    "hard_time_stop_mult": 3.0,       # close ANY trade past this × max-hold; 0 = off
    "max_hold_silent_when_losing": False,
    "max_hold_z_progress_min": 0.0,
    "trailing_stop_pct": 0.0,         # exit when net P&L falls this % from peak; 0 = off
    "trailing_stop_floor_pct": 0.0,   # arm only once peak ≥ this % of target
    "z_stop_exit_enabled": True,
    "max_exit_failures": 5,           # halt auto-exit retries after N failures
    "exit_retry_backoff": 10.0,       # base seconds, doubled per failure
    "exit_retry_backoff_max": 60.0,

    # ── account risk ─────────────────────────────────────────────────────────
    "daily_max_loss_usd": 0.0,        # halt entries once day P&L ≤ −this; 0 = off
    "loss_streak_pause_at": 0,        # halt entries after N consecutive losses; 0 = off
    "loss_streak_reduce_at": 0,       # shrink size after N consecutive losses; 0 = off
    "loss_streak_reduce_pct": 50.0,

    # ── regime / trend-day guard ─────────────────────────────────────────────
    "regime_enabled": False,
    "regime_halt_on_trending": True,
    "regime_trend_direction_filter": False,
    "regime_window_samples": 120,
    "regime_vr_lag": 5,
    "regime_efficiency_ratio_max": 0.6,
    "regime_min_zero_crossings": 4,

    # ── loop ─────────────────────────────────────────────────────────────────
    "tick_interval": 0.5,             # algo decision tick (s)
}
