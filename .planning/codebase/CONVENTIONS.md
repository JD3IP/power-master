# Code Conventions

## Overview

This document describes the coding conventions, style, naming patterns, and error handling practices used throughout the Power Master codebase.

**Project Configuration:**
- Python: 3.11+
- Formatter: Ruff (100 character line length)
- Linter: Ruff with rules [E, F, I, N, W, UP]
- Type Checker: MyPy (strict mode enabled)
- Build System: Hatchling

---

## Code Style and Formatting

### Imports

- Use `from __future__ import annotations` at the top of every module for forward compatibility
- Organize imports in standard order: builtins → third-party → local imports
- Apply Ruff import sorting rules automatically

Example:
```python
"""Module docstring."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
import pydantic

from power_master.config.schema import AppConfig
from power_master.db.repository import Repository
```

### Line Length and Formatting

- Maximum line length: **100 characters**
- Use Ruff for automatic formatting
- Enable Ruff linting rules: E (pycodestyle errors), F (Pyflakes), I (isort), N (pep8-naming), W (warnings), UP (pyupgrade)

### Naming Conventions

**Modules and files:**
- Use lowercase with underscores: `solver.py`, `config_manager.py`, `anti_oscillation.py`

**Classes:**
- Use PascalCase for all classes
- Abstract base classes typically use descriptive names like `InverterAdapter`, `LoadController`
- Exception classes end with `Error` or `Exception`

Example:
```python
class OptimisationPlan:
    """Optimisation plan model."""
    pass

class ResilienceManager:
    """Manages system resilience states."""
    pass

class LoadController(Protocol):
    """Protocol for load adapters."""
    pass
```

**Functions and methods:**
- Use snake_case for all functions and methods
- Private methods prefix with single underscore: `_helper_function()`
- Async functions use the same naming convention with `async def`

Example:
```python
def solve(config: AppConfig, inputs: SolverInputs) -> OptimisationPlan:
    """Solve the optimisation problem."""
    pass

async def dispatch_command(adapter: InverterAdapter, cmd: ControlCommand) -> CommandResult:
    """Send command to inverter."""
    pass

def _determine_mode(charge_w: float, ...) -> SlotMode:
    """Internal helper for mode determination."""
    pass
```

**Constants:**
- Use UPPER_CASE for module-level constants
- Use descriptive names

Example:
```python
DEFAULT_SOLVER_TIMEOUT_SECONDS = 60
MAX_BATTERY_POWER_W = 10000
SCHEMA_VERSION = 1
```

**Variables:**
- Use snake_case for all variable names
- Use meaningful, descriptive names (avoid single letters except in mathematical contexts)
- Prefer explicit over cryptic (e.g., `slot_minutes` not `s_min`)

---

## Type Hints

### Requirements

- **All function signatures must include type hints** (enforced by MyPy strict mode)
- Use type hints for class attributes and method parameters
- Use type hints for return types

### Modern Type Hint Syntax

- Use `|` for union types (Python 3.10+): `str | None` instead of `Optional[str]`
- Use `list[T]` instead of `List[T]`
- Use `dict[K, V]` instead of `Dict[K, V]`
- Use `tuple[T, ...]` for variable-length tuples

Example:
```python
from datetime import datetime

def create_slot(
    start: datetime,
    end: datetime,
    mode: SlotMode,
    power_w: int = 0,
    error: str | None = None,
) -> PlanSlot | None:
    """Create a plan slot or return None on error."""
    if error:
        return None
    return PlanSlot(start=start, end=end, mode=mode, target_power_w=power_w)

def aggregate_data(items: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate list of items into summary."""
    pass
```

### Protocol and Runtime Checkable

- Use `@runtime_checkable` with `Protocol` for structural subtyping
- Protocols document interfaces without inheritance

Example from `src/power_master/loads/base.py`:
```python
@runtime_checkable
class LoadController(Protocol):
    """Protocol for all load control adapters."""

    @property
    def load_id(self) -> str:
        """Unique identifier for this load."""
        ...

    async def turn_on(self) -> bool:
        """Turn the load on. Returns True on success."""
        ...
```

---

## Docstrings

### Format and Style

- Use **Google-style docstrings** (three quotes on same line as def)
- Docstrings required for all public modules, classes, and functions
- One-liners acceptable for simple functions
- Multi-line docstrings follow: summary line → blank line → description → Args/Returns/Raises

### Module Docstrings

Place at the very top of the file, before imports:
```python
"""MILP solver for battery optimisation using PuLP (CBC)."""

from __future__ import annotations
```

### Function Docstrings

```python
def dampen_price_weighted(
    price_cents: float,
    threshold_cents: int,
    base_factor: float,
    slot_index: int,
    n_slots: int,
) -> float:
    """Apply less dampening to near-term slots and more to far-term slots.

    Near-term slots receive closer to factor=1 (less dampening), while
    far-term slots receive less (more dampening) to encourage conservative
    decisions for uncertain future periods.
    """
    pass
```

### Class Docstrings

```python
class ResilienceManager:
    """State machine managing system resilience levels.

    Evaluates provider health and determines the appropriate operating
    level (normal, degraded, offline).
    """
```

---

## Data Structures

### Dataclasses (Preferred)

- Use `@dataclass` for immutable data models
- Dataclasses used for: models, results, configurations, input/output containers

Example from `src/power_master/optimisation/solver.py`:
```python
@dataclass
class SolverInputs:
    """All inputs needed by the solver for a single optimisation run."""

    solar_forecast_w: list[float]
    load_forecast_w: list[float]
    import_rate_cents: list[float]
    export_rate_cents: list[float]
    is_spike: list[bool]

    current_soc: float
    wacb_cents: float

    storm_active: bool = False
    storm_reserve_soc: float = 0.0
    slot_start_times: list[datetime] | None = None

    @property
    def n_slots(self) -> int:
        return len(self.solar_forecast_w)
```

### Enums

- Use `Enum` for categorical values
- Use `IntEnum` when enum values map to integers (especially for database storage)

Example from `src/power_master/loads/base.py`:
```python
class LoadState(str, Enum):
    """Current state of a controllable load."""
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"
    ERROR = "error"
```

Example with IntEnum from `src/power_master/optimisation/plan.py`:
```python
class SlotMode(IntEnum):
    """Operating mode for each plan slot."""
    SELF_USE = 1
    SELF_USE_ZERO_EXPORT = 2
    FORCE_CHARGE = 3
    FORCE_DISCHARGE = 4
```

---

## Error Handling

### Exceptions

- Raise specific exceptions with descriptive messages
- Use built-in exceptions when appropriate: `ValueError`, `IOError`, `RuntimeError`, `ConnectionError`
- Preserve exception context with implicit chaining (don't use `raise X from None` casually)

Examples from codebase:
```python
# From config/manager.py
def get_config_manager() -> ConfigManager:
    """Get the active config manager instance."""
    if _config_manager is None:
        raise RuntimeError("Settings not loaded. Call load_settings() first.")
    return _config_manager

# From hardware/adapters/foxess.py (Modbus errors)
raise IOError(f"Modbus read error at holding register {address}: {result}")
raise IOError(f"Modbus write error at register {address}: {result}")
```

### Try-Except Patterns

**Broad exception handling with logging:**
```python
try:
    result = await some_operation()
except Exception:
    logger.exception("Operation failed")  # Includes traceback
    raise  # Re-raise for caller to handle
```

**Specific exception handling:**
```python
try:
    value = parse_iso_datetime(timestamp_str)
except ValueError:
    # Handle invalid format
    value = default_value
```

**Context managers for cleanup:**
```python
async with self.db.execute(query, params) as cursor:
    rows = await cursor.fetchall()
    await self.db.commit()
```

---

## Async Programming

### Async Functions

- Use `async def` for functions that perform I/O (database, network, hardware)
- Mark async fixtures with `@pytest_asyncio.fixture`
- Use `await` at call sites; never forget the `await` keyword

Example from `src/power_master/control/loop.py`:
```python
async def _read_telemetry(self) -> Telemetry | None:
    """Read telemetry from inverter adapter with timeout."""
    try:
        return await asyncio.wait_for(
            self._adapter.get_telemetry(),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Telemetry read timed out")
        return None
```

### Task Management

- Use `asyncio.create_task()` to spawn background tasks
- Store task references to prevent premature garbage collection
- Cancel tasks gracefully on shutdown

Example from `src/power_master/main.py`:
```python
async def start(self) -> None:
    """Start all application components."""
    # Start background tasks
    task = asyncio.create_task(self._control_loop.run())
    self._tasks.append(task)

    # Await critical startup tasks
    await asyncio.gather(*startup_tasks)
```

---

## Logging

### Setup

- Use Python's `logging` module throughout
- Structured logging via `structlog` (see `src/power_master/logging/structured.py`)
- One logger per module: `logger = logging.getLogger(__name__)`

### Log Levels

- **DEBUG**: Detailed information for troubleshooting (low-level flow, variable values)
- **INFO**: General informational messages (startup, config loaded, plan created)
- **WARNING**: Warning conditions (provider unavailable, constraint violations)
- **ERROR**: Error conditions that need attention (command failed, telemetry stale)
- **CRITICAL**: Critical failures requiring immediate action (inverter offline, grid loss)

### Logging Examples

```python
logger = logging.getLogger(__name__)

# Info: successful operations
logger.info(
    "Solver complete: status=%s objective=%.2f time=%dms slots=%d",
    status, objective_val, solver_time_ms, n,
)

# Warning: degraded operation
logger.warning("Solver status: %s (time: %dms)", status, solver_time_ms)

# Error: recoverable failures
logger.error("Command dispatch failed: %s", result.message)

# Exception: include traceback
try:
    risky_operation()
except Exception:
    logger.exception("Operation failed with traceback")
```

---

## Configuration Management

### Configuration Schema

- Use Pydantic v2 for configuration validation (see `src/power_master/config/schema.py`)
- Provide defaults for optional settings
- Document config options with descriptions

Example structure:
```python
class BatteryConfig:
    """Battery parameters."""
    capacity_wh: int
    max_charge_rate_w: int
    max_discharge_rate_w: int
    soc_min_hard: float = 0.1
    soc_max_hard: float = 0.95
    round_trip_efficiency: float = 0.85
```

### Runtime Access

- Load config once at startup via `load_settings()`
- Pass config as dependency to components
- Use `get_config_manager()` for reloads (rare)

---

## Database

### SQL Schema

- Schema defined in `src/power_master/db/models.py`
- Use `CREATE TABLE IF NOT EXISTS` for idempotency
- Add indexes on frequently queried columns
- Timestamp columns: use ISO 8601 string format (UTC)

### Repository Pattern

- All database operations via `Repository` class (`src/power_master/db/repository.py`)
- Methods handle SQL execution, JSON serialization, and error handling
- Use parameterized queries to prevent SQL injection

Example:
```python
async def store_telemetry(
    self,
    soc: float,
    battery_power_w: int,
    ...
) -> int:
    """Store telemetry and return row ID."""
    now = _now()  # ISO 8601 UTC timestamp
    async with self.db.execute(
        """INSERT INTO telemetry
           (recorded_at, soc, battery_power_w, ...)
           VALUES (?, ?, ?, ...)""",
        (now, soc, battery_power_w, ...),
    ) as cursor:
        row_id = cursor.lastrowid
    await self.db.commit()
    return row_id
```

---

## Comment Style

### Inline Comments

- Use inline comments sparingly (code should be self-documenting)
- Use `#` for explanations of non-obvious logic

Example from `src/power_master/optimisation/solver.py`:
```python
# ── Apply price dampening to import rates ──
dampened_import = [
    dampen_price_weighted(...) for ...
]

# Grid bounded by inverter capacity + load headroom
max_grid = config.battery.max_charge_rate_w + config.battery.max_discharge_rate_w
```

### Section Headers

- Use decorative comments to separate logical sections within functions/classes:
```python
# ── Decision variables ──
charge = [pulp.LpVariable(...) for t in range(n)]

# ── Constraints per slot ──
for t in range(n):
    ...

# ── Objective ──
build_objective(...)

# ── Solve ──
prob.solve(solver)
```

---

## Private vs Public

### Public Interface

- Public methods/attributes: no leading underscore
- Intended for external use by other modules

### Private Implementation

- Methods/attributes with single leading underscore: `_helper_method()`
- Implementation details, not part of public API
- Used within the module or closely related code

Example from `src/power_master/optimisation/solver.py`:
```python
# Public function
def solve(config: AppConfig, inputs: SolverInputs) -> OptimisationPlan:
    """Public entry point."""
    ...

# Private helpers
def _determine_mode(...) -> SlotMode:
    """Internal implementation detail."""
    ...

def _resolve_planner_timezone(config: AppConfig):
    """Internal timezone resolution."""
    ...
```

---

## Special Methods and Properties

### Properties

- Use `@property` decorator for computed attributes
- Properties should be lightweight (no heavy I/O)

Example from `src/power_master/optimisation/plan.py`:
```python
@property
def total_slots(self) -> int:
    return len(self.slots)

def get_current_slot(self) -> PlanSlot | None:
    """Get the slot covering the current time."""
    now = datetime.now(timezone.utc)
    for slot in self.slots:
        if slot.start <= now < slot.end:
            return slot
    return None
```

### Magic Methods

- Override `__init__`, `__repr__`, `__str__` as needed in classes
- Keep minimal and clear

---

## Testing Patterns

- See `TESTING.md` for comprehensive testing conventions
- Tests mirror source structure: `src/power_master/X/module.py` → `tests/test_X/test_module.py`
- Use fixtures in `tests/conftest.py` for shared test infrastructure

---

## Summary of Key Standards

| Aspect | Standard |
|--------|----------|
| Python Version | 3.11+ |
| Line Length | 100 characters |
| Import Organization | Standard order + Ruff sorting |
| Naming (modules/files) | snake_case |
| Naming (classes) | PascalCase |
| Naming (functions/methods) | snake_case |
| Naming (constants) | UPPER_CASE |
| Type Hints | Required on all public functions |
| Union Types | `X \| None` (not `Optional[X]`) |
| Docstrings | Google style, required for public API |
| Data Models | Dataclasses with `@dataclass` |
| Async | `async def` for I/O, `await` required |
| Logging | `logging.getLogger(__name__)` |
| Errors | Specific exceptions with descriptive messages |
| Private Methods | Single underscore prefix `_method()` |
| Type Checker | MyPy strict mode |
| Linter | Ruff [E, F, I, N, W, UP] |

