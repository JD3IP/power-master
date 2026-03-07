# Testing Conventions

## Overview

This document describes the testing framework, structure, fixtures, mocking patterns, and coverage expectations for the Power Master codebase.

**Testing Stack:**
- Framework: Pytest 8.0+
- Async Support: pytest-asyncio 0.25+
- Coverage: pytest-cov 6.0+
- Time Mocking: time-machine 2.16+
- Configuration: `pyproject.toml` with `asyncio_mode = "auto"`

---

## Test Organization

### Directory Structure

Tests mirror the source code structure:
```
src/power_master/
├── config/
├── db/
├── hardware/
├── loads/
├── optimisation/
├── control/
├── accounting/
└── ...

tests/
├── test_config.py
├── test_db.py
├── test_hardware/
│   ├── __init__.py
│   └── test_foxess.py
├── test_loads/
│   ├── __init__.py
│   └── test_loads.py
├── test_optimisation/
│   ├── __init__.py
│   ├── test_solver.py
│   └── test_backtest_lab.py
├── test_control/
│   ├── __init__.py
│   └── test_control.py
├── test_accounting/
│   ├── __init__.py
│   └── test_accounting.py
└── conftest.py
```

### Test File Naming

- Test files: `test_*.py` (required by pytest discovery)
- Test classes: `Test*` (grouping related tests)
- Test functions: `test_*` (individual test cases)
- Helper functions: regular snake_case (no `test_` prefix)

Example structure from `tests/test_optimisation/test_solver.py`:
```python
def _make_inputs(...) -> SolverInputs:
    """Helper to create solver inputs."""
    ...

class TestSolverBasic:
    def test_solver_returns_plan(self) -> None:
        """Test that solver returns a valid plan."""
        ...

    def test_solver_respects_soc_limits(self) -> None:
        """Test SOC constraints are enforced."""
        ...
```

---

## Fixtures

### Fixture Locations

- **Global fixtures**: `tests/conftest.py` (shared across all test modules)
- **Module-specific fixtures**: In the test file or a local conftest.py

### Core Fixtures

All fixtures defined in `tests/conftest.py`:

#### `event_loop`

```python
@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for all tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
```

- **Scope**: Session-wide (one loop for all tests)
- **Usage**: Required for async tests to work with pytest-asyncio
- **Note**: Modern pytest-asyncio handles this automatically; fixture provided for explicit control

#### `config`

```python
@pytest.fixture
def config() -> AppConfig:
    """Provide a default test configuration."""
    return AppConfig(setup_completed=True)
```

- **Scope**: Function (new instance per test)
- **Usage**: Tests that need app configuration
- **Properties**: Default values suitable for testing; safe isolation

#### `config_manager`

```python
@pytest.fixture
def config_manager(tmp_path: Path) -> ConfigManager:
    """Provide a config manager with test paths."""
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("setup_completed: true\ndb:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr
```

- **Scope**: Function
- **Usage**: Tests that need config loading/saving behavior
- **Features**: Uses temporary paths to avoid polluting test environment

#### `db` (Async Fixture)

```python
@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provide a fresh in-memory database for each test."""
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()
```

- **Scope**: Function
- **Async**: Yes (marked with `@pytest_asyncio.fixture`)
- **Usage**: Tests that need database operations
- **Cleanup**: Automatic closure after test

#### `repo` (Async Fixture)

```python
@pytest_asyncio.fixture
async def repo(db: aiosqlite.Connection) -> Repository:
    """Provide a repository with a fresh database."""
    return Repository(db)
```

- **Scope**: Function
- **Async**: Yes
- **Dependency**: Depends on `db` fixture
- **Usage**: Tests for repository/data access operations

### Fixture Usage Example

```python
class TestAccountingEngine:
    async def test_charge_updates_wacb(self, repo: Repository) -> None:
        """Test that charging updates weighted average cost basis."""
        # Setup: repo fixture provides fresh database
        tracker = CostBasisTracker(
            capacity_wh=10000,
            initial_soc=0.5,
            initial_wacb=10.0
        )
        # Test and verify...
```

---

## Test Function Patterns

### Naming and Documentation

- Test functions should have descriptive names explaining what is tested
- Include docstrings explaining the test intent and critical assertions

Example from `tests/test_optimisation/test_solver.py`:
```python
def test_cheap_import_triggers_charge(self) -> None:
    """When import is cheap now but expensive later, SOC should rise in cheap slots.

    Use very low starting SOC and high load so the battery alone can't cover
    the expensive slots — must charge during cheap slots.
    """
```

### Async Test Functions

All async tests are automatically detected and run by pytest-asyncio (due to `asyncio_mode = "auto"`):

```python
class TestControlLoop:
    async def test_dispatch_command_sends_inverter_command(self, repo: Repository) -> None:
        """Verify control command is properly dispatched to inverter."""
        # Setup
        adapter = _make_adapter()
        command = ControlCommand(mode=OperatingMode.SELF_USE, power_w=5000)

        # Execute
        result = await dispatch_command(adapter, command)

        # Assert
        assert result.success is True
        adapter.send_command.assert_called_once()
```

### Synchronous Test Functions

```python
class TestSolverBasic:
    def test_solver_returns_plan(self) -> None:
        """Test that solver produces a valid plan."""
        config = AppConfig()
        inputs = _make_inputs()
        plan = solve(config, inputs)

        assert plan.version == 1
        assert plan.total_slots == 8
        assert plan.solver_time_ms >= 0
```

---

## Mocking Patterns

### Mock Objects (unittest.mock)

Use `AsyncMock` for async methods and `Mock` for sync methods:

Example from `tests/test_control/test_control.py`:
```python
from unittest.mock import AsyncMock

def _make_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_telemetry = AsyncMock(return_value=_make_telemetry())
    adapter.send_command = AsyncMock(return_value=CommandResult(success=True, latency_ms=10))
    adapter.is_connected = AsyncMock(return_value=True)
    return adapter
```

### Assertion on Mocks

```python
# Verify the mock was called
adapter.send_command.assert_called_once()

# Verify with specific arguments
adapter.send_command.assert_called_with(inverter_cmd)

# Check call count
assert adapter.send_command.call_count == 2
```

### Partial Mocking

For tests that need a real object with some methods mocked:
```python
adapter = _make_adapter()  # AsyncMock
# ... or patch specific methods on real objects
```

---

## Test Data Builders

### Helper Functions for Complex Objects

Create reusable builder functions instead of repeating test data:

Example from `tests/test_optimisation/test_solver.py`:
```python
def _make_inputs(
    n_slots: int = 8,
    solar: float = 0.0,
    load: float = 500.0,
    import_price: float = 20.0,
    export_price: float = 5.0,
    soc: float = 0.5,
    wacb: float = 10.0,
    spike_slots: list[int] | None = None,
    storm: bool = False,
) -> SolverInputs:
    """Build SolverInputs with customizable parameters."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    starts = [now + timedelta(minutes=30 * i) for i in range(n_slots)]
    return SolverInputs(
        solar_forecast_w=[solar] * n_slots,
        load_forecast_w=[load] * n_slots,
        import_rate_cents=[import_price] * n_slots,
        export_rate_cents=[export_price] * n_slots,
        is_spike=[i in (spike_slots or []) for i in range(n_slots)],
        current_soc=soc,
        wacb_cents=wacb,
        storm_active=storm,
        storm_reserve_soc=0.8 if storm else 0.0,
        slot_start_times=starts,
    )
```

Usage:
```python
# Default parameters
inputs = _make_inputs()

# Customized parameters
inputs = _make_inputs(
    import_price=50.0,
    export_price=25.0,
    soc=0.8,
)
```

Example from `tests/test_control/test_control.py`:
```python
def _make_plan(n_slots: int = 4, mode: SlotMode = SlotMode.SELF_USE) -> OptimisationPlan:
    """Build a test optimisation plan."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = []
    for i in range(n_slots):
        start = now + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        slots.append(PlanSlot(
            index=i,
            start=start,
            end=end,
            mode=mode,
            target_power_w=3000 if mode in (SlotMode.FORCE_CHARGE, SlotMode.FORCE_DISCHARGE) else 0,
        ))
    return OptimisationPlan(
        version=1,
        created_at=now,
        trigger_reason="periodic",
        horizon_start=now,
        horizon_end=now + timedelta(minutes=30 * n_slots),
        slots=slots,
        objective_score=0.0,
        solver_time_ms=10,
    )
```

---

## Test Classes and Organization

### Class-Based Tests

Group related tests using test classes:

```python
class TestCostBasis:
    """Tests for weighted average cost basis tracking."""

    def test_initial_wacb(self) -> None:
        """Verify initial WACB is set correctly."""
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        assert tracker.wacb_cents == 10.0
        assert tracker.state.stored_wh == 5000

    def test_charge_updates_wacb(self) -> None:
        """Verify charging updates WACB correctly."""
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        tracker.record_charge(2000, 5.0)
        assert abs(tracker.wacb_cents - (60 / 7)) < 0.01

    def test_discharge_doesnt_change_wacb(self) -> None:
        """Verify discharging doesn't change WACB."""
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        tracker.record_discharge(1000)
        assert tracker.wacb_cents == 10.0
```

### Section Comments

Use comments to organize large test classes:

```python
class TestSolver:
    # ── Basic Functionality ──────────────────────────

    def test_solver_returns_plan(self) -> None:
        ...

    def test_solver_respects_soc_limits(self) -> None:
        ...

    # ── Arbitrage Behavior ───────────────────────────

    def test_high_export_triggers_force_discharge(self) -> None:
        ...

    # ── Storm Constraints ────────────────────────────

    def test_storm_reserve_constraint(self) -> None:
        ...
```

---

## Assertions and Validation

### Standard Assertions

Use clear, specific assertions:

```python
# Check equality
assert plan.version == 1
assert plan.total_slots == 8

# Check membership
assert plan.metrics["status"] in ("Optimal", "Not Solved")

# Check presence/absence
assert "storm_reserve" in plan.active_constraints

# Check numeric ranges
assert config.battery.soc_min_hard - 0.01 <= slot.expected_soc <= config.battery.soc_max_hard + 0.01

# Check boolean conditions
assert result.success is True
assert adapter.is_connected is True
```

### Floating Point Comparisons

For floating point numbers, use approximate equality:

```python
# Close to a value (e.g., for WACB calculations)
assert abs(tracker.wacb_cents - (60 / 7)) < 0.01

# Between bounds
assert 0.0 <= confidence <= 1.0
```

### Collection Assertions

```python
# Check length
assert len(plan.slots) == 8

# Check all elements satisfy condition
assert all(s.expected_soc >= 0.0 for s in plan.slots)

# Check any element satisfies condition
assert any(s.mode == SlotMode.FORCE_DISCHARGE for s in plan.slots)
```

---

## Time-Based Testing

### Using time-machine for Mocked Time

Use `time-machine` library for time-dependent tests:

```python
import time_machine

class TestLoading:
    def test_load_time_calculation(self) -> None:
        """Test load scheduling respects time constraints."""
        with time_machine.travel("2026-03-07 10:00:00", tick=False):
            # Time is frozen at 2026-03-07 10:00:00
            now = datetime.now(timezone.utc)
            assert now.year == 2026
            assert now.hour == 10

    async def test_async_with_mocked_time(self) -> None:
        """Test async operation with frozen time."""
        with time_machine.travel("2026-03-07", tick=False):
            # Perform time-sensitive async operations
            result = await some_time_dependent_function()
```

---

## Parametrized Tests

### Using pytest.mark.parametrize

Test multiple scenarios with one function:

```python
import pytest

class TestModeSelection:
    @pytest.mark.parametrize("charge_w,discharge_w,export_w,import_w,expected_mode", [
        (5000, 0, 0, 1000, SlotMode.FORCE_CHARGE),
        (0, 5000, 2000, 0, SlotMode.FORCE_DISCHARGE),
        (0, 0, 0, 0, SlotMode.SELF_USE),
    ])
    def test_determine_mode(
        self,
        charge_w: float,
        discharge_w: float,
        export_w: float,
        import_w: float,
        expected_mode: SlotMode,
    ) -> None:
        """Test mode determination with various power flows."""
        mode = _determine_mode(charge_w, discharge_w, export_w, import_w, False)
        assert mode == expected_mode
```

---

## Testing Async Code

### Async Fixtures and Tests

Async fixtures use `@pytest_asyncio.fixture`:

```python
@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provide a database connection."""
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()
```

Async tests use `async def`:

```python
class TestRepository:
    async def test_store_and_retrieve_telemetry(self, repo: Repository) -> None:
        """Test storing and retrieving telemetry."""
        row_id = await repo.store_telemetry(soc=0.5, battery_power_w=1000, ...)
        assert row_id > 0

        telemetry = await repo.get_latest_telemetry()
        assert telemetry is not None
        assert telemetry["soc"] == 0.5
```

### Testing Concurrent Operations

```python
async def test_concurrent_command_dispatch(self) -> None:
    """Test multiple commands can be dispatched concurrently."""
    adapter = _make_adapter()
    commands = [
        ControlCommand(mode=OperatingMode.FORCE_CHARGE, power_w=5000),
        ControlCommand(mode=OperatingMode.SELF_USE, power_w=0),
    ]

    results = await asyncio.gather(*[
        dispatch_command(adapter, cmd) for cmd in commands
    ])

    assert len(results) == 2
    assert all(r.success for r in results)
```

---

## Integration Testing

### Multi-Module Tests

Test interactions between modules (in `tests/test_integration/`):

Example from `tests/test_integration/test_scenarios.py`:
```python
class TestScenarios:
    async def test_full_optimisation_cycle(self, config: AppConfig) -> None:
        """Test a complete optimisation cycle from forecast to plan."""
        # Create inputs combining multiple modules
        forecast = _make_forecast()
        tariff = _make_tariff()

        # Run through the pipeline
        plan = optimize(forecast, tariff, config)

        # Verify end-to-end behavior
        assert plan is not None
        assert len(plan.slots) > 0
```

---

## Coverage and Best Practices

### Coverage Goals

- **Aim for 80%+ code coverage** on critical paths
- **100% coverage not required** but encouraged for:
  - Core algorithms (solver, accounting, control logic)
  - Error handling paths
  - Public APIs

### Running Coverage

```bash
pytest --cov=src/power_master --cov-report=html tests/
```

### What to Test

✓ **DO test:**
- Public functions and methods
- Critical business logic
- Edge cases and error conditions
- Boundary conditions
- Async operations

✗ **DON'T necessarily test:**
- Private helper functions (test through public API)
- Simple data models with no logic (unless data validation)
- Third-party library code
- UI rendering (unless custom logic)

### Test Quality

- **One assertion concept per test** (or closely related assertions)
- **Clear test names** that describe what is being tested
- **DRY principle**: Use fixtures and builders to avoid repetition
- **Arrange-Act-Assert pattern**: Setup → Execute → Verify

Example:
```python
def test_charge_from_pv_uses_feed_in_rate(self) -> None:
    """Test that PV charging uses feed-in rate as cost basis."""
    # Arrange
    tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.0, initial_wacb=0.0)

    # Act
    tracker.record_charge(5000, 7.0)

    # Assert
    assert tracker.wacb_cents == 7.0
```

---

## Debugging and Development

### Running Specific Tests

```bash
# Run a single test file
pytest tests/test_optimisation/test_solver.py

# Run a specific test class
pytest tests/test_optimisation/test_solver.py::TestSolverBasic

# Run a specific test function
pytest tests/test_optimisation/test_solver.py::TestSolverBasic::test_solver_returns_plan

# Run tests matching pattern
pytest -k "charge" tests/test_accounting/

# Run with verbose output
pytest -v tests/

# Run with print output
pytest -s tests/
```

### Debugging with Breakpoints

```python
def test_expensive_operation(self) -> None:
    """Debug a failing test."""
    result = complex_calculation()

    # Drop into debugger
    import pdb; pdb.set_trace()  # pragma: no cover

    assert result > 0
```

Or use pytest's built-in debugger:
```bash
pytest --pdb tests/test_optimisation/test_solver.py
```

### Temporary Skip

```python
@pytest.mark.skip(reason="WIP: under development")
def test_future_feature(self) -> None:
    """This test is skipped."""
    pass
```

---

## Common Test Patterns

### Testing Exception Handling

```python
def test_invalid_config_raises_error(self) -> None:
    """Test that invalid configuration raises ValueError."""
    with pytest.raises(ValueError, match="battery capacity must be positive"):
        AppConfig(battery=BatteryConfig(capacity_wh=-1000))
```

### Testing State Changes

```python
async def test_resilience_degradation(self) -> None:
    """Test system transitions to degraded state when providers fail."""
    manager = ResilienceManager(config, health_checker)

    # Initial state
    assert manager.level == ResilienceLevel.NORMAL

    # Simulate health check failure
    health_checker.mark_unhealthy("forecast_provider")
    changed = manager.evaluate()

    # Verify state changed
    assert changed is True
    assert manager.level == ResilienceLevel.DEGRADED
```

### Testing Callbacks and Listeners

```python
async def test_telemetry_callback_invoked(self, repo: Repository) -> None:
    """Test that telemetry callbacks are invoked."""
    loop = ControlLoop(config, adapter, repo)
    callback_invoked = False

    async def on_telemetry(telemetry: Telemetry) -> None:
        nonlocal callback_invoked
        callback_invoked = True

    loop.add_telemetry_callback(on_telemetry)
    await loop._tick()

    assert callback_invoked is True
```

---

## Test Configuration

### pytest.ini Options (in pyproject.toml)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

**Explanation:**
- `testpaths`: Directories to search for tests
- `asyncio_mode = "auto"`: Automatically detect and run async tests

### Running All Tests

```bash
pytest
pytest -v                          # Verbose output
pytest --tb=short                  # Shorter tracebacks
pytest --lf                        # Last failed
pytest --ff                        # Failed first
```

---

## Summary of Key Testing Standards

| Aspect | Standard |
|--------|----------|
| Framework | Pytest 8.0+ |
| Async Support | pytest-asyncio 0.25+ with `asyncio_mode = "auto"` |
| File Organization | Mirror `src/` structure in `tests/` |
| Test File Naming | `test_*.py` |
| Test Class Naming | `Test*` |
| Test Function Naming | `test_*` with descriptive names |
| Test Fixtures | In `conftest.py` or test files |
| Fixture Scope | Function (default) or session for heavy setup |
| Async Fixtures | `@pytest_asyncio.fixture` |
| Mocking | `unittest.mock.Mock` and `AsyncMock` |
| Time Testing | `time-machine` for frozen time |
| Assertions | Clear, specific, one concept per test |
| Float Comparisons | Approximate equality with tolerance |
| Coverage Target | 80%+ on critical paths |
| Test Data | Reusable builder functions (`_make_*`) |
| Async Tests | `async def test_*` |
| Error Testing | `pytest.raises()` context manager |

