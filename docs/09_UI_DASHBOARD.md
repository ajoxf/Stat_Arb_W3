# UI Dashboard

## Overview

The web interface provides real-time monitoring and control of the trading system. Built with Flask, Bootstrap 5, and Chart.js.

## File Locations
```
templates/
├── base.html           # Base layout (~440 lines)
├── dashboard.html      # Main trading view (~1,900 lines)
├── settings.html       # Configuration (~836 lines)
├── setup.html          # Exchange setup (~269 lines)
└── analysis.html       # Analytics (~302 lines)

static/
├── favicon.svg
└── logos/
```

---

## Page Structure

### Base Template (base.html)

```html
<!DOCTYPE html>
<html>
<head>
    <title>{% block title %}Trading{% endblock %}</title>
    <link href="bootstrap.min.css" rel="stylesheet">
    <style>
        /* Custom theme colors */
        :root {
            --primary-color: #2563eb;
            --success-color: #10b981;
            --danger-color: #ef4444;
        }
    </style>
</head>
<body>
    <!-- Navigation -->
    <nav class="navbar navbar-expand-lg">
        <a class="navbar-brand" href="/dashboard">Stat Arb</a>
        <ul class="navbar-nav">
            <li><a href="/dashboard">Dashboard</a></li>
            <li><a href="/settings">Settings</a></li>
            <li><a href="/setup">Setup</a></li>
            <li><a href="/analysis">Analysis</a></li>
        </ul>
        <!-- Trading toggle -->
        <div class="form-check form-switch">
            <input type="checkbox" id="algo-toggle">
            <label>Algo Trading</label>
        </div>
    </nav>

    <!-- Main content -->
    <main class="container-fluid">
        {% block content %}{% endblock %}
    </main>

    <script src="socket.io.js"></script>
    <script src="chart.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
```

---

## Dashboard (dashboard.html)

### Layout

```
┌─────────────────────────────────────────────────────────────────────┐
│ Navigation Bar                                    [Algo Toggle]     │
├───────────────────────────┬─────────────────────────────────────────┤
│ Account Info              │ Trading Status Banner                   │
│ - Equity                  │ [COLLECTING | READY | TRADING]          │
│ - Balance                 │                                         │
│ - Margin                  ├─────────────────────────────────────────┤
│                           │ Z-Score Chart                           │
├───────────────────────────┤                                         │
│ Market Data               │                                         │
│ - Spot Price              │                                         │
│ - Futures Price           │                                         │
│ - Spread                  ├─────────────────────────────────────────┤
│ - Z-Score                 │ Spread Chart                            │
│                           │                                         │
├───────────────────────────┤                                         │
│ Signal Generator          │                                         │
│ - Data Points             │                                         │
│ - Regime                  │                                         │
│ - Hurst                   ├─────────────────────────────────────────┤
│ - STD Filter              │ Trade History Table                     │
│                           │                                         │
├───────────────────────────┤ Entry Time | Type | P&L | Exit Time     │
│ Position Info             │                                         │
│ - Current Position        │                                         │
│ - Entry Price             │                                         │
│ - Unrealized P&L          │                                         │
└───────────────────────────┴─────────────────────────────────────────┘
```

### Account Info Section

```html
<div class="card">
    <div class="card-header">Account</div>
    <div class="card-body">
        <div class="row">
            <div class="col-4">
                <small class="text-muted">Equity</small>
                <div id="account-equity" class="h5">$0.00</div>
            </div>
            <div class="col-4">
                <small class="text-muted">Available</small>
                <div id="account-available" class="h5">$0.00</div>
            </div>
            <div class="col-4">
                <small class="text-muted">Margin Ratio</small>
                <div id="margin-ratio" class="h5">0%</div>
            </div>
        </div>
    </div>
</div>
```

### Trading Status Banner

```html
<div id="trading-status" class="alert alert-info">
    <span id="status-icon">●</span>
    <span id="status-text">Initializing...</span>
    <div class="progress">
        <div id="collection-progress" class="progress-bar" style="width: 0%"></div>
    </div>
    <small id="data-points">0/100 data points</small>
</div>
```

### Z-Score Chart

```javascript
const zscoreChart = new Chart(document.getElementById('zscore-chart'), {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
            label: 'Z-Score',
            data: [],
            borderColor: '#2563eb',
            tension: 0.1,
        }]
    },
    options: {
        scales: {
            y: {
                min: -5,
                max: 5,
            }
        },
        plugins: {
            annotation: {
                annotations: {
                    entryHigh: {
                        type: 'line',
                        yMin: 2.0,
                        yMax: 2.0,
                        borderColor: 'green',
                        borderDash: [5, 5],
                        label: { content: 'Entry +2.0' }
                    },
                    entryLow: {
                        type: 'line',
                        yMin: -2.0,
                        yMax: -2.0,
                        borderColor: 'green',
                        borderDash: [5, 5],
                        label: { content: 'Entry -2.0' }
                    },
                    exitHigh: {
                        type: 'line',
                        yMin: 0.5,
                        yMax: 0.5,
                        borderColor: 'orange',
                        borderDash: [2, 2],
                    },
                    exitLow: {
                        type: 'line',
                        yMin: -0.5,
                        yMax: -0.5,
                        borderColor: 'orange',
                        borderDash: [2, 2],
                    }
                }
            }
        }
    }
});
```

### Real-Time Updates

```javascript
// WebSocket connection
const socket = io();

// Tick updates
socket.on('tick', function(data) {
    updatePrices(data.spot, data.futures);
    lastUpdateTime = Date.now();
});

// Signal updates
socket.on('signal', function(data) {
    updateSignal(data);
    updateCharts(data);
    lastUpdateTime = Date.now();
});

// Trade updates
socket.on('trade', function(data) {
    addTradeToTable(data);
    showToast(`Trade ${data.is_open ? 'opened' : 'closed'}: ${data.position_type}`, 'info');
});

// Fallback polling (if WebSocket fails)
setInterval(function() {
    if (Date.now() - lastUpdateTime > 500) {
        fetch('/api/engine/status')
            .then(response => response.json())
            .then(data => {
                if (data.signal) {
                    updateSignal(data.signal);
                    updateCharts(data.signal);
                }
            });
    }
}, 500);
```

### Update Functions

```javascript
function updatePrices(spot, futures) {
    document.getElementById('spot-bid').textContent = spot.bid.toFixed(2);
    document.getElementById('spot-ask').textContent = spot.ask.toFixed(2);
    document.getElementById('futures-bid').textContent = futures.bid.toFixed(2);
    document.getElementById('futures-ask').textContent = futures.ask.toFixed(2);

    const spread = futures.mid - spot.mid;
    document.getElementById('spread-value').textContent = spread.toFixed(2);
}

function updateSignal(data) {
    // Z-Score display
    const zscore = data.zscore || 0;
    const zscoreEl = document.getElementById('zscore-value');
    zscoreEl.textContent = zscore.toFixed(4);
    zscoreEl.className = zscore > 0 ? 'price-up' : 'price-down';

    // Data collection progress
    const dataPoints = data.data_points || 0;
    const lookback = data.lookback || 100;
    document.getElementById('data-points').textContent = `${dataPoints}/${lookback}`;

    const progress = Math.min(100, (dataPoints / lookback) * 100);
    document.getElementById('collection-progress').style.width = progress + '%';

    // Update regime indicator
    const regimeEl = document.getElementById('regime-display');
    if (data.regime === 'MEAN_REVERTING') {
        regimeEl.textContent = 'Mean Reverting';
        regimeEl.className = 'badge bg-success';
    } else if (data.regime === 'TRENDING') {
        regimeEl.textContent = 'Trending';
        regimeEl.className = 'badge bg-danger';
    } else {
        regimeEl.textContent = data.regime;
        regimeEl.className = 'badge bg-secondary';
    }

    // Filter status
    updateFilterStatus('hurst-status', data.hurst_ok, data.hurst);
    updateFilterStatus('std-status', data.std_filter_ok);
}

function updateCharts(data) {
    // Add new Z-score point
    zscoreHistory.push(data.zscore);
    if (zscoreHistory.length > 100) zscoreHistory.shift();

    zscoreChart.data.labels = Array.from({length: zscoreHistory.length}, (_, i) => i);
    zscoreChart.data.datasets[0].data = zscoreHistory;
    zscoreChart.update('none');

    // Add new spread point
    spreadHistory.push(data.spread);
    if (spreadHistory.length > 100) spreadHistory.shift();

    spreadChart.data.labels = Array.from({length: spreadHistory.length}, (_, i) => i);
    spreadChart.data.datasets[0].data = spreadHistory;
    spreadChart.update('none');
}
```

### Control Buttons

```html
<div class="btn-group">
    <button class="btn btn-outline-primary" onclick="toggleAlgo()">
        <span id="algo-btn-text">Enable Algo</span>
    </button>
    <button class="btn btn-outline-warning" onclick="resetSpread()">
        Reset Spread
    </button>
    <button class="btn btn-outline-info" onclick="resetTradesOnly()">
        Reset Trades/SD
    </button>
    <button class="btn btn-outline-danger" onclick="resetAll()">
        Reset All
    </button>
</div>
```

```javascript
function toggleAlgo() {
    const enabled = !algoEnabled;
    fetch('/api/engine/toggle-algo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: enabled })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            algoEnabled = data.algo_enabled;
            updateAlgoButton();
            showToast(`Algo trading ${algoEnabled ? 'enabled' : 'disabled'}`, 'success');
        }
    });
}

function resetTradesOnly() {
    if (!confirm('Reset trades and SD touches? Spread data will be preserved.')) return;

    fetch('/api/reset-trades', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const d = data.deleted;
            showToast(`Reset: ${d.trades} trades, ${d.sd_touches} SD touches`, 'success');
        }
    });
}
```

---

## Settings Page (settings.html)

### Form Structure

```html
<form id="settings-form">
    <!-- Asset Selection -->
    <div class="mb-3">
        <label>Trading Asset</label>
        <select name="asset" class="form-select">
            {% for asset in assets %}
            <option value="{{ asset }}" {% if asset == config.asset %}selected{% endif %}>
                {{ asset }}
            </option>
            {% endfor %}
        </select>
    </div>

    <!-- Z-Score Thresholds -->
    <div class="card">
        <div class="card-header">Z-Score Thresholds</div>
        <div class="card-body">
            <div class="row">
                <div class="col-4">
                    <label>Entry</label>
                    <input type="number" name="entry_threshold"
                           value="{{ config.entry_threshold }}"
                           step="0.1" min="1.0" max="5.0">
                </div>
                <div class="col-4">
                    <label>Exit</label>
                    <input type="number" name="exit_threshold"
                           value="{{ config.exit_threshold }}"
                           step="0.1" min="0.0" max="2.0">
                </div>
                <div class="col-4">
                    <label>Stop Loss</label>
                    <input type="number" name="stop_loss_threshold"
                           value="{{ config.stop_loss_threshold }}"
                           step="0.1" min="2.0" max="10.0">
                </div>
            </div>
        </div>
    </div>

    <!-- Filters -->
    <div class="card">
        <div class="card-header">Filters</div>
        <div class="card-body">
            <div class="form-check form-switch">
                <input type="checkbox" name="hurst_enabled"
                       {% if config.hurst_enabled %}checked{% endif %}>
                <label>Hurst Exponent Filter</label>
            </div>
            <input type="number" name="hurst_threshold"
                   value="{{ config.hurst_threshold }}" step="0.05">

            <div class="form-check form-switch">
                <input type="checkbox" name="std_filter_enabled"
                       {% if config.std_filter_enabled %}checked{% endif %}>
                <label>STD Profitability Filter</label>
            </div>
            <input type="number" name="min_std_multiple"
                   value="{{ config.min_std_multiple }}" step="0.1">
        </div>
    </div>

    <!-- Position Sizing -->
    <div class="card">
        <div class="card-header">Position Sizing</div>
        <div class="card-body">
            <div class="row">
                <div class="col-6">
                    <label>Position Size (USD)</label>
                    <input type="number" name="position_size_usd"
                           value="{{ config.position_size_usd }}" step="100">
                </div>
                <div class="col-6">
                    <label>Max Position (USD)</label>
                    <input type="number" name="max_position_size_usd"
                           value="{{ config.max_position_size_usd }}" step="100">
                </div>
            </div>
        </div>
    </div>

    <!-- Fee Configuration -->
    <div class="card">
        <div class="card-header">Fee Configuration (basis points)</div>
        <div class="card-body">
            <div class="row">
                <div class="col-3">
                    <label>Spot Maker</label>
                    <input type="number" name="spot_maker_fee_bps"
                           value="{{ config.spot_maker_fee_bps }}" step="0.5">
                </div>
                <div class="col-3">
                    <label>Spot Taker</label>
                    <input type="number" name="spot_taker_fee_bps"
                           value="{{ config.spot_taker_fee_bps }}" step="0.5">
                </div>
                <div class="col-3">
                    <label>Futures Maker</label>
                    <input type="number" name="futures_maker_fee_bps"
                           value="{{ config.futures_maker_fee_bps }}" step="0.5">
                </div>
                <div class="col-3">
                    <label>Futures Taker</label>
                    <input type="number" name="futures_taker_fee_bps"
                           value="{{ config.futures_taker_fee_bps }}" step="0.5">
                </div>
            </div>
        </div>
    </div>

    <button type="submit" class="btn btn-primary">Save Settings</button>
</form>
```

---

## Setup Page (setup.html)

### Exchange Management

```html
<!-- Add Exchange Form -->
<form id="add-exchange-form">
    <select name="exchange_type" class="form-select">
        <option value="okx">OKX</option>
        <option value="binance">Binance</option>
        <option value="bybit">Bybit</option>
    </select>

    <input type="text" name="name" placeholder="Display Name">
    <input type="text" name="api_key" placeholder="API Key">
    <input type="password" name="secret_key" placeholder="Secret Key">
    <input type="password" name="passphrase" placeholder="Passphrase (OKX only)">

    <div class="form-check">
        <input type="checkbox" name="is_testnet" checked>
        <label>Testnet/Demo Mode</label>
    </div>

    <button type="submit" class="btn btn-primary">Add Exchange</button>
</form>

<!-- Exchange List -->
<table class="table">
    <thead>
        <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Mode</th>
            <th>Status</th>
            <th>Actions</th>
        </tr>
    </thead>
    <tbody id="exchanges-table">
        {% for exchange in exchanges %}
        <tr>
            <td>{{ exchange.name }}</td>
            <td>{{ exchange.exchange_type }}</td>
            <td>{{ 'Demo' if exchange.is_testnet else 'Live' }}</td>
            <td>
                <span class="badge {{ 'bg-success' if exchange.status == 'CONNECTED' else 'bg-danger' }}">
                    {{ exchange.status }}
                </span>
            </td>
            <td>
                <button onclick="testConnection({{ exchange.id }})">Test</button>
                <button onclick="setActive({{ exchange.id }})">Set Active</button>
                <button onclick="deleteExchange({{ exchange.id }})">Delete</button>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
```

---

## Analysis Page (analysis.html)

### SD Touch Events

```html
<div class="card">
    <div class="card-header">SD Touch Events</div>
    <div class="card-body">
        <table class="table table-sm">
            <thead>
                <tr>
                    <th>Time</th>
                    <th>SD Level</th>
                    <th>Direction</th>
                    <th>Z-Score</th>
                    <th>Spread</th>
                </tr>
            </thead>
            <tbody id="sd-touches-table">
                {% for event in sd_touches %}
                <tr>
                    <td>{{ event.timestamp.strftime('%H:%M:%S') }}</td>
                    <td class="{{ 'text-success' if event.sd_level > 0 else 'text-danger' }}">
                        {{ '%+.0f'|format(event.sd_level) }}σ
                    </td>
                    <td>{{ event.direction }}</td>
                    <td>{{ '%.4f'|format(event.zscore) }}</td>
                    <td>{{ '%.2f'|format(event.spread) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>

<!-- Touch Statistics -->
<div class="card">
    <div class="card-header">Touch Statistics</div>
    <div class="card-body">
        <div class="row">
            <div class="col-4">
                <small>+2σ Touches</small>
                <div class="h4 text-success">{{ stats.plus_2_touches }}</div>
            </div>
            <div class="col-4">
                <small>-2σ Touches</small>
                <div class="h4 text-danger">{{ stats.minus_2_touches }}</div>
            </div>
            <div class="col-4">
                <small>Reversion Rate</small>
                <div class="h4">{{ '%.1f'|format(stats.reversion_rate) }}%</div>
            </div>
        </div>
    </div>
</div>
```

---

## Toast Notifications

```javascript
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast align-items-center text-white bg-${type} border-0`;
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">${message}</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto"
                    data-bs-dismiss="toast"></button>
        </div>
    `;

    document.getElementById('toast-container').appendChild(toast);
    const bsToast = new bootstrap.Toast(toast);
    bsToast.show();

    // Remove after hidden
    toast.addEventListener('hidden.bs.toast', () => toast.remove());
}
```

---

## Responsive Design

```css
/* Mobile optimizations */
@media (max-width: 768px) {
    .card {
        margin-bottom: 1rem;
    }

    .chart-container {
        height: 200px;
    }

    .btn-group {
        flex-direction: column;
    }

    .table-responsive {
        overflow-x: auto;
    }
}
```
