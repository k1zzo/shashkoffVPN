# Cumulative Traffic Accounting Design

**Date:** 2026-04-10
**Status:** Approved

---

## Problem

Three interrelated bugs in the current traffic accounting:

1. New users see `N/A` instead of `0 B / ∞` when `XRAY_API_ADDR` is not configured or Xray is unreachable.
2. Deleting a device reduces displayed traffic because the current query pattern (`user>>>username/`) relies on Xray's in-memory counters — which reset on Xray restart and are lost after a device is removed from config.
3. Cabinet and Happ subscription show different values in some edge cases (Happ falls back to 0, cabinet falls back to N/A).

---

## Goals

- Brand new user always sees `0 B / ∞`
- Deleting a device never reduces the user's displayed cumulative total
- Cabinet and Happ use the same accounting logic
- Simple: one cumulative total per user, no charts, no history

---

## Data Model

### New columns on `users` table

```python
# backend/models.py
traffic_up_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
traffic_down_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

- Both fields default to `0`. All new users start at zero.
- These fields store **only historical traffic from deleted/deactivated devices**.
- Active device traffic is never written here — it is always read live from Xray.
- This strict separation prevents double-counting.

### Schema migration

Add two `ALTER TABLE users ADD COLUMN ... NOT NULL DEFAULT 0` calls to `upgrade_db_schema()` in `backend/db.py`, following the existing column-existence check pattern. Safe to run on every startup. Existing users receive `0` (correct — no history was snapshotted before this feature existed).

---

## Accounting Logic

### Three new functions in `backend/xray_stats.py`

**`get_user_traffic_active(username, active_labels, xray_api_addr, timeout=3.0)`**

- Queries Xray with the existing pattern `user>>>username/`
- Filters results to only include stat names where the email prefix matches a label in `active_labels`
- `active_labels: frozenset[str]` — the set of `device.device_id[:24]` for currently active devices
- Returns `UserTrafficStats | None` (None = Xray unreachable or addr not set)
- A user with 0 active devices returns `UserTrafficStats(0, 0, 0)` if Xray is reachable

**`get_device_traffic(username, device_label, xray_api_addr, timeout=3.0)`**

- Queries Xray with pattern `user>>>username/device_label>>>`
- Returns `UserTrafficStats | None`
- Used only at deletion time to capture a device's final counter before it is forgotten

**`combined_traffic(stored_up, stored_down, live)`**

- When `live` is not None: returns `UserTrafficStats(stored_up + live.upload_bytes, stored_down + live.download_bytes, ...)`
- When `live` is None (Xray unavailable): returns `UserTrafficStats(stored_up, stored_down, stored_up + stored_down)`
- Always returns a non-None `UserTrafficStats`
- `combined_traffic(0, 0, None)` → `UserTrafficStats(0, 0, 0)` → displays as `0 B / ∞`

### Deletion snapshot in `backend/routes/devices.py`

In `remove_device`, before calling `deactivate_device()`:

```
1. Build device_label = device.device_id[:24]
2. If settings.xray_api_addr is set:
   a. stats = get_device_traffic(user.username, device_label, settings.xray_api_addr)
   b. If stats is not None:
      user.traffic_up_bytes += stats.upload_bytes
      user.traffic_down_bytes += stats.download_bytes
      db.commit()   ← snapshot committed before deactivation
3. deactivate_device(db, device)
4. apply_xray_client_changes(db, settings)
```

If Xray is unreachable at deletion time, the device's final traffic is not captured and deletion proceeds anyway. This is an honest limitation — documented, not hidden.

---

## Cabinet Behavior (`backend/routes/user_page.py`)

Replace the current stats block:

```
active_labels = frozenset(d.device_id[:24] for d in device_rows)
live = get_user_traffic_active(username, active_labels, settings.xray_api_addr)
  # None when xray_api_addr not set OR Xray unreachable
total = combined_traffic(user.traffic_up_bytes, user.traffic_down_bytes, live)
traffic_summary = f"{format_bytes(total.total_bytes)} / ∞"
```

| Scenario | Result |
|---|---|
| New user, API configured, 0 traffic | `0 B / ∞` |
| New user, API not configured | `0 B / ∞` |
| User with 200 MB stored, API unavailable | `200.00 MB / ∞` |
| User with 200 MB stored, 50 MB live active | `250.00 MB / ∞` |

`N/A` is removed from all user display paths. Inaccessible (inactive/expired) users also see their stored cumulative total (e.g. `0 B / ∞`), since `combined_traffic` always returns a value. Live Xray query is skipped for inaccessible users — only the stored fields are used.

---

## Happ Subscription Behavior (`backend/routes/profile.py`)

In `build_happ_subscription_response`, replace the current stats block with identical logic:

```
active_labels = frozenset(d.device_id[:24] for d in list_active_devices(db, user.id))
live = get_user_traffic_active(username, active_labels, settings.xray_api_addr)
total = combined_traffic(user.traffic_up_bytes, user.traffic_down_bytes, live)
# subscription-userinfo: upload=total.upload_bytes; download=total.download_bytes; total=0; ...
```

Cabinet and Happ now use the same `combined_traffic()` call — one source of truth.

Blocked/expired users: unchanged — they still receive `upload=0; download=0` in the blocked response before stats are consulted.

---

## Double-Counting Prevention

| Phase | What happens |
|---|---|
| Device A active | live query includes A's email label; stored = 0 |
| Device A deleted | A's traffic snapshotted into `user.traffic_up/down_bytes`; next live query filters to active labels only — A's email excluded |
| Xray restart | live resets to 0; stored preserved — total remains correct |
| A re-registered | A gets a new `device_id` (new Happ HWID) or same device_id; either way live tracking resumes cleanly |

---

## Tests

### Update (breaking change)

- `TestCabinetTrafficDisplay.test_shows_na_when_xray_api_not_configured` → renamed and updated: should assert `0 B / ∞` in the response, not `N/A`

### New unit tests in `tests/test_xray_stats.py`

- `get_user_traffic_active` filters out non-active device labels
- `get_user_traffic_active` with empty `active_labels` returns zero stats (not None) when Xray reachable
- `combined_traffic(0, 0, UserTrafficStats(0,0,0))` → total = 0
- `combined_traffic(100, 50, None)` → total = 150 (Xray unavailable preserves stored)
- `combined_traffic(100, 50, UserTrafficStats(10, 20, 30))` → upload=110, download=70, total=180

### New integration tests in `tests/test_traffic_accounting.py`

- Brand new user shows `0 B / ∞` when API configured and returns zero
- Brand new user shows `0 B / ∞` when API not configured
- Deleting a device does not reduce the displayed total (stored snapshot preserved)
- Multiple active devices aggregate into one total
- Cabinet and Happ show the same value for the same user state
- Repeated page loads do not change `traffic_up_bytes` / `traffic_down_bytes` (no double-write)
- Xray unavailable after deletion still shows the snapshotted total

---

## Files Changed

| File | Change |
|---|---|
| `backend/models.py` | Add `traffic_up_bytes`, `traffic_down_bytes` to `User` |
| `backend/db.py` | Add two `ALTER TABLE` calls in `upgrade_db_schema()` |
| `backend/xray_stats.py` | Add `get_user_traffic_active`, `get_device_traffic`, `combined_traffic` |
| `backend/routes/devices.py` | Snapshot device traffic before `deactivate_device()` in `remove_device` |
| `backend/routes/user_page.py` | Use filtered live query + `combined_traffic`; remove N/A path |
| `backend/routes/profile.py` | Use same logic in `build_happ_subscription_response` |
| `tests/test_xray_stats.py` | Update N/A test; add unit tests for new functions |
| `tests/test_traffic_accounting.py` | New file: integration tests for accounting behavior |

---

## Constraints Not Violated

- Per-device UUID logic: unchanged
- Xray watcher/reload architecture: unchanged
- Cabinet layout/templates: unchanged (only the value passed to `traffic_summary` changes)
- No daily history, no charts, no analytics
