# Technical Debt & Concerns Analysis

**Document Date:** 2026-03-07
**Scope:** Power Master codebase analysis
**Categories:** Bugs, Security Issues, Performance Bottlenecks, Architectural Fragility

---

## 1. Error Handling & Recovery Gaps

### 1.1 Broad Exception Handling (Medium Risk)

Multiple files catch generic `Exception` without specific handling:

- `src/power_master/main.py`: 15+ bare `except Exception:` blocks in startup, reload, and shutdown paths
- `src/power_master/loads/manager.py`: 9+ exception handlers with `pass` or generic logging
- `src/power_master/control/loop.py`: Lines 156, 209, 261, 307 catch `Exception` generically
- `src/power_master/updater.py`: Lines 111, 134, 437, 644, 657 lack specific exception types
- `src/power_master/db/engine.py`: Lines 52, 79, 111, 136, 206, 216 catch broad exceptions

**Impact:** Difficult to diagnose root causes; may mask unexpected failures.

**Recommendation:**
- Replace with specific exception types (e.g., `httpx.HTTPError`, `asyncio.TimeoutError`, `aiosqlite.OperationalError`)
- Log exception details with `exc_info=True` for debugging
- Consider circuit breaker pattern for external API calls

**Example locations to fix:**
```python
# CURRENT (src/power_master/main.py:455)
except Exception:
    pass

# BETTER
except (asyncio.CancelledError, asyncio.TimeoutError) as e:
    logger.error("MQTT publish failed: %s", e)
```

### 1.2 Silent Failures in Critical Paths (Medium Risk)

Several error handlers use `pass` or suppress errors in critical functions:

- `src/power_master/main.py:455-456` — MQTT offline publish fails silently
- `src/power_master/main.py:471-472` — Provider close() failures ignored
- `src/power_master/control/loop.py:127` — CancelledError suppressed
- `src/power_master/dashboard/routes/sse.py:111, 166` — SSE send failures pass silently
- `src/power_master/updater.py:405` — Exception in import statement suppressed

**Impact:** System state inconsistencies; no audit trail of failures.

**Recommendation:**
- Log all caught exceptions at WARNING level minimum
- Track recovery attempts in structured logs
- Consider error budgets per component

---

## 2. Security Concerns

### 2.1 Password Hashing: SHA-256 with Salt (Low-Medium Risk)

**File:** `src/power_master/dashboard/auth.py:39-52`

Current implementation:
```python
def hash_password(password: str) -> str:
    salt = secrets.token_hex(SALT_LENGTH)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"
```

**Issues:**
- SHA-256 is fast, making it unsuitable for password hashing (vulnerable to brute force)
- No work factor (bcrypt, argon2, scrypt provide adaptive cost)
- Single iteration only

**Impact:** If password database leaks, attackers can perform rapid dictionary attacks.

**Recommendation:**
- Migrate to `argon2-cffi` or `bcrypt` with minimum 10+ cost factor
- Plan backward compatibility: hash new passwords with Argon2, verify old SHA-256 hashes on login, re-hash on next password change
- Add password requirements enforcement (currently 8 chars minimum)

**Example migration:**
```python
# New approach
from argon2 import PasswordHasher
ph = PasswordHasher()
hashed = ph.hash(password)
ph.verify(hashed, password)  # for verification
```

### 2.2 Session Cookie Configuration (Low Risk)

**File:** `src/power_master/dashboard/auth.py:234-241`

Session cookie is set with:
```python
response.set_cookie(
    key="pm_session",
    value=cookie_value,
    max_age=auth_config.session_max_age_seconds,
    httponly=True,
    samesite="lax",
    path="/",
)
```

**Issues:**
- Missing `secure=True` flag (should be set in production over HTTPS)
- `samesite="lax"` allows cross-site GET requests (consider `"strict"`)
- No domain restriction specified

**Recommendation:**
- Add `secure=True` for HTTPS-only transmission
- Use `samesite="strict"` unless cross-domain forms needed
- Document deployment requirement for HTTPS
- Add security headers in dashboard app (`X-Frame-Options`, `X-Content-Type-Options`, CSP)

### 2.3 API Key Exposure in Logging (Low Risk)

**File:** `src/power_master/dashboard/auth.py:479`

The CLI helper exposes API keys in console output:
```python
if "--set-password" in sys.argv:
    # ... user input for password_hash
    print(f'password_hash: "{hashed}"')
```

**Also:**
- `src/power_master/tariff/providers/amber.py:30` passes API key in header
- Config files may contain API keys in plaintext

**Recommendation:**
- Never log or print sensitive values
- Use environment variables for API keys instead of config files
- Implement config validation to warn if secrets are hardcoded
- Use masked logging for HTTP headers

---

## 3. Concurrency & Race Conditions

### 3.1 Shared State Without Synchronization (Medium Risk)

**File:** `src/power_master/control/loop.py`

The control loop manages shared state across multiple async tasks:

```python
# Line 75: Shared command reference
self._last_dispatched_command: ControlCommand | None = None

# Lines 112-127: Multiple concurrent tasks access this
refresh_task = asyncio.create_task(self._command_refresh_loop())
```

**Potential Issues:**
- `_state` dict updated in main loop (line 92) and read in refresh loop without locking
- `_last_dispatched_command` modified during tick, re-sent in refresh loop (potential stale data)
- Multiple ticks could theoretically overlap if `_tick()` takes longer than interval

**Recommendation:**
- Add `asyncio.Lock` for shared state mutations:
```python
self._state_lock = asyncio.Lock()
# Then: async with self._state_lock: self._state.current_plan = plan
```
- Consider using `dataclass(frozen=True)` for immutable state snapshots
- Document tick timing guarantees

### 3.2 Load Manager Concurrent Command Dispatch (Medium Risk)

**File:** `src/power_master/loads/manager.py:400-440`

Multiple loads are controlled concurrently without explicit serialization:

```python
# Multiple await calls without gather/wait_for
try:
    result = await load.send_command(cmd)
except Exception:
    pass
```

**Issues:**
- Network requests happen sequentially but error handling is per-load
- If one load adapter hangs (network timeout), it blocks the whole manager
- No timeout protection on individual load commands

**Recommendation:**
- Wrap individual commands with `asyncio.wait_for()`:
```python
try:
    result = await asyncio.wait_for(
        load.send_command(cmd),
        timeout=5.0  # per-load timeout
    )
except asyncio.TimeoutError:
    logger.warning("Load %s command timeout", load.name)
```

---

## 4. Resource Management Issues

### 4.1 Database WAL Checkpoint (Low Risk)

**File:** `src/power_master/db/engine.py:196-207`

WAL checkpoint is manually called periodically:

```python
async def checkpoint_wal() -> None:
    if _db is not None:
        try:
            await _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.debug("WAL checkpoint completed")
        except Exception:
            logger.warning("WAL checkpoint failed", exc_info=True)
```

**Issues:**
- No caller tracking if checkpoint is actually invoked regularly
- PRAGMA busy_timeout (5000ms) may cause checkpoint delays during heavy load
- WAL file could grow unbounded if checkpoints fail silently

**Recommendation:**
- Verify checkpoint is called from main event loop (search for `checkpoint_wal()` calls)
- Add instrumentation to track checkpoint frequency
- Consider auto-checkpoint settings:
```python
await db.execute("PRAGMA wal_autocheckpoint=1000")  # Checkpoint every 1000 pages
```

### 4.2 MQTT Client Cleanup (Low-Medium Risk)

**File:** `src/power_master/main.py:446-457`

MQTT disconnection in shutdown has loose error handling:

```python
if self._mqtt_client:
    try:
        # publish offline status...
    except Exception:
        pass
    await self._mqtt_client.disconnect()
```

**Issues:**
- Offline status publish may fail, yet disconnect proceeds without logging
- No timeout on disconnect operation
- Reconnection logic not visible in shutdown path

**Recommendation:**
- Add explicit timeout on disconnect
- Log all MQTT shutdown operations

```python
try:
    await asyncio.wait_for(self._mqtt_client.disconnect(), timeout=5.0)
except asyncio.TimeoutError:
    logger.warning("MQTT disconnect timeout")
```

### 4.3 HTTP Client Lifecycle (Low Risk)

**File:** `src/power_master/tariff/providers/amber.py:28-32`

AsyncClient created at init but cleanup is in `close()`:

```python
self._client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {config.api_key}"},
    timeout=30.0,
)
# ... later in close()
async def close(self) -> None:
    await self._client.aclose()
```

**Issues:**
- No context manager usage; relies on explicit close()
- If provider reload fails, old client may leak

**Recommendation:**
- Use context manager or async context manager where possible
- Add try/finally in provider initialization:
```python
try:
    # async with httpx.AsyncClient(...) as client:
    #     self._client = client
except Exception:
    await self._client.aclose()
    raise
```

---

## 5. Performance & Scaling Concerns

### 5.1 Synchronous Database Operations in Async Context (Low-Medium Risk)

**File:** `src/power_master/history/loader.py` (and other data loading paths)

While using `aiosqlite`, some patterns could block the event loop:

- Large SELECT queries without pagination
- No query result streaming for large result sets
- No index analysis documented

**Recommendation:**
- Add query result limits
- Implement pagination for large datasets
- Monitor slow query logs:
```python
await db.execute("PRAGMA query_only=ON")  # for read queries
```

### 5.2 Forecast Aggregator Update Without Bounds (Low Risk)

**File:** `src/power_master/main.py:543-548`

Provider updates happen in-place:

```python
aggregator.update_providers(
    solar_provider=solar,
    weather_provider=weather,
    storm_provider=storm,
    tariff_provider=tariff,
)
```

**Issues:**
- Old provider references may still be in-flight during reload
- No backpressure if many forecasts queue up
- Forecast cache not cleared on provider change

**Recommendation:**
- Document that update_providers should be idempotent
- Clear any cached forecasts when providers change
- Consider a versioning system for forecasts

### 5.3 Historical Data Queries (Low-Medium Risk)

**File:** `src/power_master/history/` module

Prediction and pattern matching load historical data:

- `prediction.py:56` has bare `except Exception` on history fetch
- No pagination on large historical queries
- Pattern matching may load years of data into memory

**Recommendation:**
- Add query date range limits
- Implement streaming/lazy evaluation for large datasets
- Cache pattern matching results

---

## 6. Architectural Fragility

### 6.1 Configuration Reload Without Full Validation (Medium Risk)

**File:** `src/power_master/main.py:487-524`

Hot-reload of config happens with partial validation:

```python
async def reload_config(self, updates: dict, app) -> None:
    new_config = self.config_manager.save_user_config(updates)
    # ... hot-swap components
    if "providers" in changed_sections:
        await self._reload_providers(old_config, new_config, app)
```

**Issues:**
- No rollback if reload fails mid-operation
- Inconsistent state between old and new config if reload fails
- No validation that new config is compatible with running system
- Partial updates may leave database in inconsistent state

**Recommendation:**
- Implement transactional config changes:
```python
# 1. Validate new config completely
# 2. Create all new resources (test connections)
# 3. Atomically swap old ← new
# 4. Keep old state for rollback if needed
```
- Add config change validation hook
- Log before/after state checksums

### 6.2 Plan Rebuild Without Synchronization (Medium Risk)

**File:** `src/power_master/main.py` (rebuild_evaluator integration)

Control loop and rebuild evaluator update plan asynchronously:

- Control loop reads `self._state.current_plan` (line 87)
- Rebuild evaluator calls `control_loop.set_plan()` in background
- No lock between read and write of plan

**Recommendation:**
- Use `asyncio.Lock` for plan updates
- Consider versioning plans (version 1 → 2) to detect stale reads

### 6.3 Storm Reserve SOC Management (Low-Medium Risk)

**File:** `src/power_master/control/loop.py:71-72`

External storm reserve state set directly:

```python
self._storm_active: bool = False
self._storm_reserve_soc: float = 0.0
```

**Issues:**
- Caller must externally coordinate storm state updates
- No validation that reserve_soc is in [0, 1] range
- State changes not logged

**Recommendation:**
- Use setter methods with validation:
```python
def set_storm_state(self, active: bool, reserve_soc: float):
    if not 0 <= reserve_soc <= 1:
        raise ValueError(f"SOC must be in [0,1], got {reserve_soc}")
    old = (self._storm_active, self._storm_reserve_soc)
    self._storm_active = active
    self._storm_reserve_soc = reserve_soc
    if old != (active, reserve_soc):
        logger.info("Storm state: %s, reserve_soc: %.2f", active, reserve_soc)
```

---

## 7. Observability & Debugging Gaps

### 7.1 Missing Correlation IDs (Low Risk)

No request/operation tracing across async boundaries:

- Each log line has timestamp but no operation ID
- Hard to correlate related events across multiple async tasks
- Difficult to track a single "plan rebuild" through all components

**Recommendation:**
- Add context var for correlation IDs:
```python
import contextvars
operation_id = contextvars.ContextVar('operation_id')

# In main paths:
op_id = str(uuid.uuid4())[:8]
operation_id.set(op_id)
logger.info("Starting operation %s", operation_id.get())
```

### 7.2 Silent Telemetry Read Failures (Low-Medium Risk)

**File:** `src/power_master/control/loop.py:145-149`

If telemetry read fails, tick is skipped:

```python
telemetry = await self._read_telemetry()
if telemetry is None:
    logger.warning("Tick %d: failed to read telemetry, skipping", self._state.tick_count)
    return None
```

**Issues:**
- No tracking of failure frequency
- Could mask persistent hardware issues
- No alerting mechanism for degradation

**Recommendation:**
- Track failure count/rate:
```python
self._telemetry_failures = 0
if telemetry is None:
    self._telemetry_failures += 1
    if self._telemetry_failures > 5:
        logger.error("Telemetry read failing consistently (%d failures)",
                     self._telemetry_failures)
```

---

## 8. Known Limitations & TODOs

### 8.1 Password Length Inconsistency (Low Risk)

**Files:**
- `src/power_master/dashboard/auth.py:299` — 8 character minimum for user password change
- `src/power_master/dashboard/auth.py:492-493` — CLI warns about 12 character minimum for external access

**Recommendation:** Standardize and enforce 12 character minimum consistently

### 8.2 API Input Validation (Low-Medium Risk)

**File:** `src/power_master/dashboard/routes/api.py:125-150`

Mode control accepts power_w without range validation:

```python
power_w = body.power_w
if power_w == 0 and mode in (OperatingMode.FORCE_CHARGE, ...):
    # Apply default
```

**Missing validation:**
- No bounds check (negative, > max charge rate)
- No type checking (relies on Pydantic BaseModel)

**Recommendation:**
- Add Pydantic validators:
```python
class ModeRequest(BaseModel):
    mode: int
    power_w: int = Field(ge=0, le=50000)  # Add bounds
```

---

## 9. Testing & Coverage Gaps

### 9.1 No Explicit Tests for Error Paths (Medium Risk)

Most error handling code (`except Exception: pass`) has no corresponding test:

- Reconnection logic after network failures
- Provider recovery after API errors
- Database corruption recovery (code exists but untested)

**Recommendation:**
- Add integration tests for fault scenarios:
  - Network timeouts
  - Provider unavailability
  - Database corruption (use `src/power_master/db/engine.py:57-117` as reference)

---

## 10. Configuration & Defaults

### 10.1 Magic Numbers Without Constants (Low Risk)

Hardcoded values scattered throughout:

- `src/power_master/control/loop.py:180` — Timeout set to `5000` (5 seconds)
- `src/power_master/db/engine.py:180` — Busy timeout set to `5000` (5 seconds)
- Various intervals and thresholds

**Recommendation:**
- Extract to config constants in `src/power_master/config/schema.py`

---

## Summary Table

| Issue | Severity | Files | Category | Status |
|-------|----------|-------|----------|--------|
| Broad exception handling | Medium | main.py, loads/manager.py, control/loop.py | Error Handling | Needs refactoring |
| SHA-256 password hashing | Medium | auth.py | Security | Needs migration to Argon2 |
| Session cookie secure flag | Low | auth.py | Security | Missing `secure=True` |
| Shared state race conditions | Medium | control/loop.py | Concurrency | Needs locking |
| Config reload without rollback | Medium | main.py | Architecture | Needs transactional pattern |
| WAL checkpoint verification | Low | db/engine.py | Resource Mgmt | Needs instrumentation |
| Silent telemetry failures | Low-Medium | control/loop.py | Observability | Needs failure tracking |
| API input validation gaps | Low-Medium | routes/api.py | Security | Needs bounds checks |
| Correlation ID missing | Low | All modules | Observability | Add contextvars |
| Error path test coverage | Medium | Tests | Testing | Needs fault scenario tests |

---

## Recommended Priority

**Phase 1 (Critical):**
1. Add specific exception types (replaces bare `except Exception`)
2. Password hashing migration to Argon2
3. Add synchronization locks for shared state (control loop)

**Phase 2 (Important):**
4. Config reload transactions with rollback
5. API input validation with bounds
6. WAL checkpoint instrumentation

**Phase 3 (Nice-to-have):**
7. Correlation ID context vars
8. Comprehensive error path tests
9. Extract magic numbers to constants

---

