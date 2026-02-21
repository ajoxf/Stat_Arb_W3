# Testing

## Overview

The test suite uses pytest with async support for testing the order execution system. Tests cover order placement, cancellation, status checking, leg risk scenarios, and spread order properties.

## File Locations
```
tests/
├── __init__.py
└── test_order_executor.py  # Order executor tests (~400 lines)
```

---

## Test Configuration

### Requirements

```bash
pip install pytest pytest-asyncio
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_order_executor.py -v

# Run specific test class
pytest tests/test_order_executor.py::TestLegSides -v

# Run with coverage
pytest tests/ --cov=core --cov-report=html
```

---

## Test Fixtures

### Trading Config Fixture

```python
@pytest.fixture
def trading_config():
    """Create a test trading configuration."""
    return TradingConfig(
        asset="BTC",
        spot_symbol="BTC-USDT",
        futures_symbol="BTC-USDT-SWAP",
        entry_threshold=2.0,
        exit_threshold=0.5,
        stop_loss_threshold=4.0,
        lookback_period=100,
        position_size_usd=1000.0,
        paper_trading=False,
        order_execution_mode="LIMIT",
        limit_order_timeout_sec=30,
        limit_order_price_offset_bps=2.0,
        futures_leverage=1,
    )
```

### Mock Adapter Fixtures

```python
@pytest.fixture
def mock_spot_adapter():
    """Create a mock spot adapter."""
    adapter = Mock()
    adapter.place_order = AsyncMock()
    adapter.cancel_order = AsyncMock(return_value=True)
    adapter.get_order_status = AsyncMock()
    adapter.get_tick = AsyncMock()
    return adapter

@pytest.fixture
def mock_futures_adapter():
    """Create a mock futures adapter."""
    adapter = Mock()
    adapter.place_order = AsyncMock()
    adapter.cancel_order = AsyncMock(return_value=True)
    adapter.get_order_status = AsyncMock()
    adapter.get_tick = AsyncMock()
    return adapter
```

### Market Tick Fixtures

```python
@pytest.fixture
def spot_tick():
    """Create a test spot tick."""
    return MarketTick(
        symbol="BTC-USDT",
        bid=67000.0,
        ask=67010.0,
        last=67005.0,
        volume_24h=1000000.0,
        timestamp=datetime.utcnow(),
    )

@pytest.fixture
def futures_tick():
    """Create a test futures tick."""
    return MarketTick(
        symbol="BTC-USDT-SWAP",
        bid=68000.0,
        ask=68010.0,
        last=68005.0,
        volume_24h=2000000.0,
        timestamp=datetime.utcnow(),
    )
```

---

## Test Classes

### TestOrderExecutorInit

Tests basic executor initialization.

```python
class TestOrderExecutorInit:
    """Test OrderExecutor initialization."""

    def test_init_creates_executor(self, trading_config, mock_spot_adapter, mock_futures_adapter):
        """Test that executor is created with correct config."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        assert executor.config == trading_config
        assert executor.spot_adapter == mock_spot_adapter
        assert executor.futures_adapter == mock_futures_adapter
        assert executor.active_order is None
        assert executor._executing is False
```

### TestExecutionLock

Tests the execution lock that prevents duplicate orders.

```python
class TestExecutionLock:
    """Test execution lock prevents duplicate orders."""

    @pytest.mark.asyncio
    async def test_execute_entry_returns_none_if_already_executing(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test that execute_entry returns None if already executing."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)
        executor._executing = True  # Simulate already executing

        result = await executor.execute_entry("LONG", spot_tick, futures_tick, 0.01)

        assert result is None

    @pytest.mark.asyncio
    async def test_execute_exit_returns_none_if_already_executing(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test that execute_exit returns None if already executing."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)
        executor._executing = True

        result = await executor.execute_exit("LONG", spot_tick, futures_tick, 0.01)

        assert result is None
```

### TestLegSides

Tests correct order sides for LONG and SHORT spread positions.

```python
class TestLegSides:
    """Test that leg sides are set correctly for LONG and SHORT spreads."""

    @pytest.mark.asyncio
    async def test_long_spread_entry_sides(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test LONG spread entry: Buy spot, Sell futures (short pos_side)."""
        trading_config.order_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        # Mock successful orders
        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_entry("LONG", spot_tick, futures_tick, 0.01)

        # Check spot leg: BUY
        spot_call = mock_spot_adapter.place_order.call_args
        assert spot_call.kwargs["side"] == "BUY"

        # Check futures leg: SELL with pos_side="short"
        futures_call = mock_futures_adapter.place_order.call_args
        assert futures_call.kwargs["side"] == "SELL"
        assert futures_call.kwargs["pos_side"] == "short"

    @pytest.mark.asyncio
    async def test_short_spread_entry_sides(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test SHORT spread entry: Sell spot, Buy futures (long pos_side)."""
        trading_config.order_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_entry("SHORT", spot_tick, futures_tick, 0.01)

        # Check spot leg: SELL
        spot_call = mock_spot_adapter.place_order.call_args
        assert spot_call.kwargs["side"] == "SELL"

        # Check futures leg: BUY with pos_side="long"
        futures_call = mock_futures_adapter.place_order.call_args
        assert futures_call.kwargs["side"] == "BUY"
        assert futures_call.kwargs["pos_side"] == "long"

    @pytest.mark.asyncio
    async def test_close_long_spread_sides(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test close LONG spread: Sell spot, Buy futures (same pos_side="short")."""
        trading_config.order_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_exit("LONG", spot_tick, futures_tick, 0.01)

        # Check spot leg: SELL
        spot_call = mock_spot_adapter.place_order.call_args
        assert spot_call.kwargs["side"] == "SELL"

        # Check futures leg: BUY with pos_side="short" (same as entry to close)
        futures_call = mock_futures_adapter.place_order.call_args
        assert futures_call.kwargs["side"] == "BUY"
        assert futures_call.kwargs["pos_side"] == "short"
```

### TestOrderStatusChecking

Tests order status polling and detection.

```python
class TestOrderStatusChecking:
    """Test order status checking implementation."""

    @pytest.mark.asyncio
    async def test_check_order_status_updates_filled_leg(
        self, trading_config, mock_spot_adapter, mock_futures_adapter
    ):
        """Test that filled orders are detected and status updated."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol="BTC-USDT", side="BUY", quantity=0.01,
                order_id="spot123", status=LegStatus.OPEN
            ),
            futures_leg=LegOrder(
                symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01,
                order_id="fut123", status=LegStatus.OPEN, pos_side="short"
            ),
            is_entry=True,
            position_type="LONG",
        )

        # Mock filled status
        mock_spot_adapter.get_order_status.return_value = {
            "state": "filled",
            "filled_qty": 0.01,
            "filled_price": 67005.0,
        }
        mock_futures_adapter.get_order_status.return_value = {
            "state": "filled",
            "filled_qty": 0.01,
            "filled_price": 68005.0,
        }

        await executor._check_order_status(spread_order)

        assert spread_order.spot_leg.status == LegStatus.FILLED
        assert spread_order.spot_leg.filled_qty == 0.01
        assert spread_order.spot_leg.filled_price == 67005.0

        assert spread_order.futures_leg.status == LegStatus.FILLED
        assert spread_order.futures_leg.filled_qty == 0.01
        assert spread_order.futures_leg.filled_price == 68005.0

    @pytest.mark.asyncio
    async def test_check_order_status_detects_cancelled(
        self, trading_config, mock_spot_adapter, mock_futures_adapter
    ):
        """Test that externally cancelled orders are detected."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol="BTC-USDT", side="BUY", quantity=0.01,
                order_id="spot123", status=LegStatus.OPEN
            ),
            futures_leg=LegOrder(
                symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01,
                order_id="fut123", status=LegStatus.OPEN, pos_side="short"
            ),
            is_entry=True,
            position_type="LONG",
        )

        # Mock cancelled status
        mock_spot_adapter.get_order_status.return_value = {
            "state": "canceled",
            "filled_qty": 0,
            "filled_price": 0
        }
        mock_futures_adapter.get_order_status.return_value = {
            "state": "canceled",
            "filled_qty": 0,
            "filled_price": 0
        }

        await executor._check_order_status(spread_order)

        assert spread_order.spot_leg.status == LegStatus.CANCELLED
        assert spread_order.futures_leg.status == LegStatus.CANCELLED
```

### TestAmendOrders

Tests limit order amendment logic.

```python
class TestAmendOrders:
    """Test order amendment logic."""

    @pytest.mark.asyncio
    async def test_amend_only_places_new_order_if_cancel_succeeds(
        self, trading_config, mock_spot_adapter, mock_futures_adapter
    ):
        """Test that new order is only placed if cancel succeeds."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol="BTC-USDT", side="BUY", quantity=0.01,
                order_id="spot123", status=LegStatus.OPEN, target_price=67000.0
            ),
            futures_leg=LegOrder(
                symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01,
                order_id="fut123", status=LegStatus.OPEN, target_price=68000.0, pos_side="short"
            ),
            is_entry=True,
            position_type="LONG",
        )

        # Mock: status is live, cancel fails
        mock_spot_adapter.get_order_status.return_value = {
            "state": "live",
            "filled_qty": 0,
            "filled_price": 0
        }
        mock_spot_adapter.cancel_order.return_value = False  # Cancel fails
        mock_futures_adapter.get_order_status.return_value = {
            "state": "live",
            "filled_qty": 0,
            "filled_price": 0
        }
        mock_futures_adapter.cancel_order.return_value = False  # Cancel fails

        await executor._amend_limit_orders(spread_order)

        # place_order should NOT be called since cancel failed
        mock_spot_adapter.place_order.assert_not_called()
        mock_futures_adapter.place_order.assert_not_called()
```

### TestSpreadOrderProperties

Tests SpreadOrder dataclass property calculations.

```python
class TestSpreadOrderProperties:
    """Test SpreadOrder property calculations."""

    def test_is_complete_when_both_filled(self):
        """Test is_complete returns True when both legs filled."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FILLED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.FILLED),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.is_complete is True

    def test_is_complete_false_when_one_open(self):
        """Test is_complete returns False when one leg still open."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FILLED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.OPEN),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.is_complete is False

    def test_has_partial_fill_detects_leg_risk(self):
        """Test has_partial_fill detects when one leg filled but not other."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FILLED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.OPEN),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.has_partial_fill is True

    def test_is_failed_when_leg_fails(self):
        """Test is_failed returns True when a leg fails."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FAILED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.OPEN),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.is_failed is True
```

### TestMarketOrderExecution

Tests market order execution path.

```python
class TestMarketOrderExecution:
    """Test market order execution."""

    @pytest.mark.asyncio
    async def test_market_order_fills_both_legs(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test that market orders fill both legs simultaneously."""
        trading_config.order_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_entry("LONG", spot_tick, futures_tick, 0.01)

        assert result is not None
        assert result.is_complete is True
        assert result.spot_leg.status == LegStatus.FILLED
        assert result.futures_leg.status == LegStatus.FILLED
```

---

## Test Patterns

### Async Test Pattern

```python
@pytest.mark.asyncio
async def test_async_operation(self, fixture1, fixture2):
    """Test async operations."""
    result = await some_async_function()
    assert result is not None
```

### Mock Return Value Pattern

```python
# For sync methods
mock_adapter.some_method.return_value = expected_value

# For async methods
mock_adapter.async_method.return_value = expected_value  # AsyncMock handles awaiting
```

### Verifying Call Arguments

```python
# Check call was made
mock_adapter.place_order.assert_called_once()

# Check specific arguments
call_args = mock_adapter.place_order.call_args
assert call_args.kwargs["side"] == "BUY"
assert call_args.kwargs["quantity"] == 0.01

# Check call not made
mock_adapter.place_order.assert_not_called()
```

---

## Additional Test Scenarios (To Implement)

### Signal Generator Tests

```python
# tests/test_signal_generator.py

class TestZScoreCalculation:
    """Test Z-score calculation."""

    def test_zscore_zero_at_mean(self):
        """Test Z-score is 0 when spread equals mean."""
        sg = SignalGenerator(config)
        # Add data at constant spread
        for _ in range(100):
            sg.add_tick(spot_tick, futures_tick)

        state = sg.get_state()
        assert abs(state['zscore']) < 0.1


class TestHurstExponent:
    """Test Hurst exponent calculation."""

    def test_hurst_trending_series(self):
        """Test Hurst > 0.5 for trending series."""
        # Create trending data
        # ...

    def test_hurst_mean_reverting_series(self):
        """Test Hurst < 0.5 for mean-reverting series."""
        # Create mean-reverting data
        # ...
```

### Database Tests

```python
# tests/test_database.py

class TestDatabaseManager:
    """Test database operations."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create temporary database."""
        db_path = tmp_path / "test.db"
        return DatabaseManager(str(db_path))

    def test_save_and_load_config(self, temp_db):
        """Test config persistence."""
        config = TradingConfig(asset="ETH", entry_threshold=2.5)
        temp_db.save_config(config)

        loaded = temp_db.get_config()
        assert loaded.asset == "ETH"
        assert loaded.entry_threshold == 2.5

    def test_trade_lifecycle(self, temp_db):
        """Test trade save/update/close."""
        trade = Trade(asset="BTC", position_type="LONG", ...)
        trade.id = temp_db.save_trade(trade)

        # Update with exit
        trade.exit_time = datetime.utcnow()
        trade.pnl_usd = 10.0
        trade.is_open = False
        temp_db.save_trade(trade)

        # Verify
        trades = temp_db.get_trades(open_only=False)
        assert len(trades) == 1
        assert trades[0].pnl_usd == 10.0
```

### Exchange Adapter Tests

```python
# tests/test_exchange_adapters.py

class TestOKXAdapter:
    """Test OKX adapter."""

    @pytest.mark.asyncio
    async def test_signature_generation(self):
        """Test API signature is correctly generated."""
        # ...

    @pytest.mark.asyncio
    async def test_order_placement(self, mock_http_client):
        """Test order placement request format."""
        # ...

    @pytest.mark.asyncio
    async def test_error_handling(self, mock_http_client):
        """Test error responses are handled correctly."""
        mock_http_client.post.return_value = {"code": "1", "msg": "Insufficient balance"}
        # ...
```

---

## Running Tests in CI

### pytest.ini

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
filterwarnings =
    ignore::DeprecationWarning
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    integration: marks tests as integration tests
```

### GitHub Actions Example

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio pytest-cov
      - name: Run tests
        run: pytest tests/ -v --cov=core --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

---

## Test Coverage Goals

| Module | Target Coverage |
|--------|-----------------|
| `core/order_executor.py` | 90%+ |
| `core/signal_generator.py` | 85%+ |
| `core/trading_engine.py` | 80%+ |
| `database/manager.py` | 85%+ |
| `exchanges/okx_adapter.py` | 70%+ |

### Measuring Coverage

```bash
# Generate coverage report
pytest tests/ --cov=core --cov=database --cov-report=html

# View report
open htmlcov/index.html
```
