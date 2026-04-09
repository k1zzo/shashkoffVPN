# Cumulative Traffic Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist cumulative traffic per user so that device deletion never reduces the displayed total, and new users always see `0 B / ∞` instead of `N/A`.

**Architecture:** Add `traffic_up_bytes`/`traffic_down_bytes` to the `users` table (historical traffic from deleted devices only). Live Xray stats are filtered to active device labels only to prevent double-counting. At deletion time, the device's final traffic is snapshotted into the user's stored fields before deactivation. Cabinet and Happ both call `combined_traffic(stored, live)` — one source of truth.

**Tech Stack:** FastAPI, SQLAlchemy/SQLite, Xray gRPC StatsService (custom protobuf client in `backend/xray_stats.py`), pytest

---

## File Map

| File | Change |
|---|---|
| `backend/models.py` | Add `traffic_up_bytes`, `traffic_down_bytes` to `User` |
| `backend/db.py` | Add ALTER TABLE migration for both new columns |
| `backend/xray_stats.py` | Add `combined_traffic`, `get_user_traffic_active`, `get_device_traffic` |
| `backend/routes/devices.py` | Snapshot device traffic before deactivation in `remove_device` |
| `backend/routes/user_page.py` | Use `get_user_traffic_active` + `combined_traffic`; remove N/A path |
| `backend/routes/profile.py` | Same logic in `build_happ_subscription_response` |
| `tests/test_xray_stats.py` | Update N/A test; add unit tests for the three new functions |
| `tests/test_traffic_accounting.py` | New file: integration tests for cumulative accounting behavior |

---

## Task 1: Add traffic columns to User model and migration

**Files:**
- Modify: `backend/models.py`
- Modify: `backend/db.py`
- Test: `tests/test_traffic_accounting.py` (new file, first test)

- [ ] **Step 1: Create the test file with a failing test for the model fields**

Create `tests/test_traffic_accounting.py`:

```python
"""Integration tests for cumulative traffic accounting.

Verifies that:
- new users start with zero traffic fields
- device deletion snapshots traffic into user totals
- cabinet and Happ both show stored + live totals
- repeated reads do not mutate stored fields
- Xray unavailability after deletion preserves stored totals
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from backend.models import Device, User
from backend.xray_stats import UserTrafficStats


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, *, username: str, token: str, **kwargs) -> User:
    user = User(
        username=username,
        public_token=token,
        uuid=str(uuid4()),
        is_active=True,
        max_devices=3,
        expires_at=datetime.utcnow() + timedelta(days=30),
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_device(db, *, user_id: int, device_id: str, **kwargs) -> Device:
    device = Device(
        user_id=user_id,
        device_id=device_id,
        device_name=f"Device {device_id[-6:]}",
        platform="iOS",
        device_uuid=str(uuid4()),
        is_active=True,
        **kwargs,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


# ── Model field defaults ──────────────────────────────────────────────────────


class TestUserTrafficFields:
    def test_new_user_has_zero_traffic_fields(self, db):
        """A freshly created user must have traffic_up_bytes == traffic_down_bytes == 0."""
        user = _make_user(db, username="new-user", token="new-token")
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0
```

- [ ] **Step 2: Run the test to verify it fails (column does not exist yet)**

```bash
cd /Users/ruslanshashkov/Desktop/shashkoffVPN
python3 -m pytest tests/test_traffic_accounting.py::TestUserTrafficFields::test_new_user_has_zero_traffic_fields -v
```

Expected: `FAILED` — `AttributeError: 'User' object has no attribute 'traffic_up_bytes'` (or similar column error).

- [ ] **Step 3: Add columns to User model**

In `backend/models.py`, add two lines inside the `User` class, after `created_at`:

```python
    traffic_up_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    traffic_down_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

The full `User` class after the change (relevant section):

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    public_token: Mapped[str] = mapped_column(
        String(120), unique=True, index=True, nullable=False
    )
    uuid: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    traffic_up_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    traffic_down_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    devices: Mapped[list["Device"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
```

- [ ] **Step 4: Add migration in upgrade_db_schema**

In `backend/db.py`, inside `upgrade_db_schema()`, add a new block for the `users` table **after** the existing `devices` block:

```python
def upgrade_db_schema() -> None:
    """Apply additive schema changes to existing tables.
    ...
    Columns added here:
      devices.device_type  — Happ device category (phone/tablet/pc/tv/unknown)
      devices.source       — registration origin ("happ", "api", or NULL for legacy)
      devices.device_uuid  — per-device VPN UUID (UUID4); NULL for legacy rows
      users.traffic_up_bytes   — cumulative upload bytes from deleted devices (default 0)
      users.traffic_down_bytes — cumulative download bytes from deleted devices (default 0)
    """
    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(devices)")).fetchall()
        }
        if "device_type" not in existing:
            conn.execute(text("ALTER TABLE devices ADD COLUMN device_type TEXT"))
            conn.commit()
        if "source" not in existing:
            conn.execute(text("ALTER TABLE devices ADD COLUMN source TEXT"))
            conn.commit()
        if "device_uuid" not in existing:
            conn.execute(text("ALTER TABLE devices ADD COLUMN device_uuid TEXT"))
            conn.commit()

        existing_users = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()
        }
        if "traffic_up_bytes" not in existing_users:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN traffic_up_bytes INTEGER NOT NULL DEFAULT 0")
            )
            conn.commit()
        if "traffic_down_bytes" not in existing_users:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN traffic_down_bytes INTEGER NOT NULL DEFAULT 0")
            )
            conn.commit()
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m pytest tests/test_traffic_accounting.py::TestUserTrafficFields::test_new_user_has_zero_traffic_fields -v
```

Expected: `PASSED`

- [ ] **Step 6: Run full suite to verify no regressions**

```bash
python3 -m pytest tests/ -v
```

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/models.py backend/db.py tests/test_traffic_accounting.py
git commit -m "feat: add cumulative traffic fields to User model"
```

---

## Task 2: Add `combined_traffic` to xray_stats

**Files:**
- Modify: `backend/xray_stats.py`
- Modify: `tests/test_xray_stats.py`

- [ ] **Step 1: Write failing tests for `combined_traffic`**

Add a new test class at the bottom of `tests/test_xray_stats.py`:

```python
# ── combined_traffic ──────────────────────────────────────────────────────────


class TestCombinedTraffic:
    def test_zero_stored_zero_live(self):
        live = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        result = combined_traffic(stored_up=0, stored_down=0, live=live)
        assert result.upload_bytes == 0
        assert result.download_bytes == 0
        assert result.total_bytes == 0

    def test_stored_only_when_live_is_none(self):
        result = combined_traffic(stored_up=100, stored_down=50, live=None)
        assert result.upload_bytes == 100
        assert result.download_bytes == 50
        assert result.total_bytes == 150

    def test_live_only_when_stored_is_zero(self):
        live = UserTrafficStats(
            upload_bytes=1_000_000, download_bytes=9_000_000, total_bytes=10_000_000
        )
        result = combined_traffic(stored_up=0, stored_down=0, live=live)
        assert result.upload_bytes == 1_000_000
        assert result.download_bytes == 9_000_000
        assert result.total_bytes == 10_000_000

    def test_stored_plus_live(self):
        live = UserTrafficStats(upload_bytes=10, download_bytes=20, total_bytes=30)
        result = combined_traffic(stored_up=100, stored_down=50, live=live)
        assert result.upload_bytes == 110
        assert result.download_bytes == 70
        assert result.total_bytes == 180

    def test_never_returns_none(self):
        result = combined_traffic(stored_up=0, stored_down=0, live=None)
        assert result is not None
        assert isinstance(result, UserTrafficStats)

    def test_xray_unavailable_preserves_stored(self):
        """When live=None (Xray down), return stored totals unchanged."""
        result = combined_traffic(stored_up=500_000_000, stored_down=2_000_000_000, live=None)
        assert result.total_bytes == 2_500_000_000
```

Also update the import at the top of `tests/test_xray_stats.py` — add `combined_traffic` to the import line:

```python
from backend.xray_stats import (
    UserTrafficStats,
    _call_query_stats,
    _decode_stat_message,
    _varint_encode,
    combined_traffic,
    decode_query_stats_response,
    encode_query_stats_request,
    format_bytes,
    get_user_traffic,
)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_xray_stats.py::TestCombinedTraffic -v
```

Expected: `ImportError` — `cannot import name 'combined_traffic'`

- [ ] **Step 3: Implement `combined_traffic` in xray_stats.py**

Add this function in `backend/xray_stats.py` after the `format_bytes` function (around line 57), before the protobuf encoder section:

```python
def combined_traffic(
    stored_up: int,
    stored_down: int,
    live: "UserTrafficStats | None",
) -> "UserTrafficStats":
    """Combine persisted historical traffic with live Xray stats.

    stored_up / stored_down: cumulative bytes from deleted devices
      (user.traffic_up_bytes / user.traffic_down_bytes from DB).
    live: real-time stats from Xray for currently active devices,
      or None when Xray is unreachable / not configured.

    Returns a UserTrafficStats that is always non-None. When live is None,
    only the stored values are returned — never loses already-persisted data.
    """
    if live is not None:
        up = stored_up + live.upload_bytes
        down = stored_down + live.download_bytes
    else:
        up = stored_up
        down = stored_down
    return UserTrafficStats(
        upload_bytes=up,
        download_bytes=down,
        total_bytes=up + down,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_xray_stats.py::TestCombinedTraffic -v
```

Expected: all 6 tests `PASSED`

- [ ] **Step 5: Commit**

```bash
git add backend/xray_stats.py tests/test_xray_stats.py
git commit -m "feat: add combined_traffic helper to xray_stats"
```

---

## Task 3: Add `get_user_traffic_active` to xray_stats

**Files:**
- Modify: `backend/xray_stats.py`
- Modify: `tests/test_xray_stats.py`

- [ ] **Step 1: Write failing tests for `get_user_traffic_active`**

Add a new test class in `tests/test_xray_stats.py`. Add `get_user_traffic_active` to the imports at the top:

```python
from backend.xray_stats import (
    UserTrafficStats,
    _call_query_stats,
    _decode_stat_message,
    _varint_encode,
    combined_traffic,
    decode_query_stats_response,
    encode_query_stats_request,
    format_bytes,
    get_user_traffic,
    get_user_traffic_active,
)
```

Add the test class at the bottom of the file:

```python
# ── get_user_traffic_active ───────────────────────────────────────────────────


class TestGetUserTrafficActive:
    def test_returns_none_when_addr_empty(self):
        result = get_user_traffic_active(
            username="alice", active_labels=frozenset(["dev1"]), xray_api_addr=""
        )
        assert result is None

    def test_returns_none_when_username_empty(self):
        result = get_user_traffic_active(
            username="", active_labels=frozenset(["dev1"]), xray_api_addr="127.0.0.1:10085"
        )
        assert result is None

    def test_returns_none_when_xray_unreachable(self):
        with patch("backend.xray_stats._call_query_stats", return_value=None):
            result = get_user_traffic_active(
                username="alice",
                active_labels=frozenset(["dev1"]),
                xray_api_addr="127.0.0.1:10085",
            )
        assert result is None

    def test_filters_to_active_labels_only(self):
        """Stats for deleted device labels must be excluded from the result."""
        raw = [
            ("user>>>alice/active-device>>>traffic>>>uplink", 1_000),
            ("user>>>alice/active-device>>>traffic>>>downlink", 5_000),
            ("user>>>alice/deleted-device>>>traffic>>>uplink", 9_999),   # must be excluded
            ("user>>>alice/deleted-device>>>traffic>>>downlink", 9_999), # must be excluded
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic_active(
                username="alice",
                active_labels=frozenset(["active-device"]),
                xray_api_addr="127.0.0.1:10085",
            )
        assert result is not None
        assert result.upload_bytes == 1_000
        assert result.download_bytes == 5_000
        assert result.total_bytes == 6_000

    def test_empty_active_labels_returns_zero_stats(self):
        """User with no registered devices → zero live stats, not None."""
        raw = [("user>>>alice/some-device>>>traffic>>>uplink", 1_000)]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic_active(
                username="alice",
                active_labels=frozenset(),
                xray_api_addr="127.0.0.1:10085",
            )
        assert result is not None
        assert result.upload_bytes == 0
        assert result.download_bytes == 0
        assert result.total_bytes == 0

    def test_multiple_active_devices_aggregated(self):
        raw = [
            ("user>>>bob/device-a>>>traffic>>>uplink", 100),
            ("user>>>bob/device-a>>>traffic>>>downlink", 200),
            ("user>>>bob/device-b>>>traffic>>>uplink", 50),
            ("user>>>bob/device-b>>>traffic>>>downlink", 150),
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic_active(
                username="bob",
                active_labels=frozenset(["device-a", "device-b"]),
                xray_api_addr="127.0.0.1:10085",
            )
        assert result is not None
        assert result.upload_bytes == 150   # 100 + 50
        assert result.download_bytes == 350  # 200 + 150
        assert result.total_bytes == 500

    def test_label_truncation_matches_xray_email(self):
        """Labels are device_id[:24]; a 30-char device_id truncates to first 24."""
        long_id = "a" * 30
        label = long_id[:24]  # "aaaaaaaaaaaaaaaaaaaaaaaa"
        raw = [
            (f"user>>>carol/{label}>>>traffic>>>uplink", 777),
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic_active(
                username="carol",
                active_labels=frozenset([label]),
                xray_api_addr="127.0.0.1:10085",
            )
        assert result is not None
        assert result.upload_bytes == 777
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_xray_stats.py::TestGetUserTrafficActive -v
```

Expected: `ImportError` — `cannot import name 'get_user_traffic_active'`

- [ ] **Step 3: Implement `get_user_traffic_active` in xray_stats.py**

Add this function to `backend/xray_stats.py` in the `# ── Public API ──` section, after `get_user_traffic`:

```python
def get_user_traffic_active(
    username: str,
    active_labels: "frozenset[str]",
    xray_api_addr: str | None,
    timeout: float = 3.0,
) -> UserTrafficStats | None:
    """Query Xray for traffic, filtered to currently active device labels only.

    active_labels: frozenset of device.device_id[:24] for all currently active
      devices. Stats for any other email label (e.g. deleted devices) are
      excluded from the result — this prevents double-counting when stored
      historical traffic already accounts for those deleted devices.

    Returns None when:
      - xray_api_addr is empty or None
      - username is empty
      - the Xray API is unreachable or returns an error

    Returns UserTrafficStats(0, 0, 0) when:
      - active_labels is empty (no registered devices)
      - all devices have zero traffic

    A result with all-zero counters is an honest zero, not "unavailable".
    """
    if not username or not xray_api_addr:
        return None

    pattern = f"user>>>{username}/"
    raw = _call_query_stats(addr=xray_api_addr, pattern=pattern, timeout=timeout)
    if raw is None:
        return None

    # Build a set of allowed prefixes — only stat names that start with one of
    # these are counted. Prefix format: "user>>>username/label>>>"
    # e.g. "user>>>alice/my-device>>>" matches
    #   user>>>alice/my-device>>>traffic>>>uplink
    #   user>>>alice/my-device>>>traffic>>>downlink
    active_prefixes = frozenset(
        f"user>>>{username}/{label}>>>" for label in active_labels
    )

    upload = 0
    download = 0
    for name, value in raw:
        if not any(name.startswith(p) for p in active_prefixes):
            continue
        if ">>>traffic>>>uplink" in name:
            upload += max(0, value)
        elif ">>>traffic>>>downlink" in name:
            download += max(0, value)

    return UserTrafficStats(
        upload_bytes=upload,
        download_bytes=download,
        total_bytes=upload + download,
    )
```

- [ ] **Step 4: Run to verify all pass**

```bash
python3 -m pytest tests/test_xray_stats.py::TestGetUserTrafficActive -v
```

Expected: all 7 tests `PASSED`

- [ ] **Step 5: Commit**

```bash
git add backend/xray_stats.py tests/test_xray_stats.py
git commit -m "feat: add get_user_traffic_active to xray_stats"
```

---

## Task 4: Add `get_device_traffic` to xray_stats

**Files:**
- Modify: `backend/xray_stats.py`
- Modify: `tests/test_xray_stats.py`

- [ ] **Step 1: Write failing tests for `get_device_traffic`**

Add `get_device_traffic` to the imports in `tests/test_xray_stats.py`:

```python
from backend.xray_stats import (
    UserTrafficStats,
    _call_query_stats,
    _decode_stat_message,
    _varint_encode,
    combined_traffic,
    decode_query_stats_response,
    encode_query_stats_request,
    format_bytes,
    get_device_traffic,
    get_user_traffic,
    get_user_traffic_active,
)
```

Add the test class at the bottom of `tests/test_xray_stats.py`:

```python
# ── get_device_traffic ────────────────────────────────────────────────────────


class TestGetDeviceTraffic:
    def test_returns_none_when_addr_empty(self):
        result = get_device_traffic(
            username="alice", device_label="my-device", xray_api_addr=""
        )
        assert result is None

    def test_returns_none_when_username_empty(self):
        result = get_device_traffic(
            username="", device_label="my-device", xray_api_addr="127.0.0.1:10085"
        )
        assert result is None

    def test_returns_none_when_device_label_empty(self):
        result = get_device_traffic(
            username="alice", device_label="", xray_api_addr="127.0.0.1:10085"
        )
        assert result is None

    def test_returns_none_when_xray_unreachable(self):
        with patch("backend.xray_stats._call_query_stats", return_value=None):
            result = get_device_traffic(
                username="alice", device_label="my-device", xray_api_addr="127.0.0.1:10085"
            )
        assert result is None

    def test_returns_device_stats(self):
        raw = [
            ("user>>>alice/my-device>>>traffic>>>uplink", 3_000),
            ("user>>>alice/my-device>>>traffic>>>downlink", 7_000),
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_device_traffic(
                username="alice", device_label="my-device", xray_api_addr="127.0.0.1:10085"
            )
        assert result is not None
        assert result.upload_bytes == 3_000
        assert result.download_bytes == 7_000
        assert result.total_bytes == 10_000

    def test_uses_device_specific_pattern(self):
        """The query pattern must include the device label to avoid fetching all users."""
        with patch("backend.xray_stats._call_query_stats", return_value=[]) as mock_fn:
            get_device_traffic(
                username="alice", device_label="my-device", xray_api_addr="127.0.0.1:10085"
            )
        args, kwargs = mock_fn.call_args
        pattern = kwargs.get("pattern") or args[1]
        assert "user>>>alice/my-device" in pattern

    def test_returns_zero_when_no_traffic(self):
        with patch("backend.xray_stats._call_query_stats", return_value=[]):
            result = get_device_traffic(
                username="alice", device_label="my-device", xray_api_addr="127.0.0.1:10085"
            )
        assert result is not None
        assert result.total_bytes == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_xray_stats.py::TestGetDeviceTraffic -v
```

Expected: `ImportError` — `cannot import name 'get_device_traffic'`

- [ ] **Step 3: Implement `get_device_traffic` in xray_stats.py**

Add after `get_user_traffic_active` in `backend/xray_stats.py`:

```python
def get_device_traffic(
    username: str,
    device_label: str,
    xray_api_addr: str | None,
    timeout: float = 3.0,
) -> UserTrafficStats | None:
    """Query Xray for the traffic of one specific device.

    device_label: device.device_id[:24] — the label used in the Xray email
      for this device (format: "username/device_label").

    Used at device deletion time to capture the device's final traffic counter
    before it is removed from the active client set.

    Returns None when addr is empty, username/label is empty, or Xray
    is unreachable. Returns UserTrafficStats(0, 0, 0) for a device with no
    recorded traffic (honest zero, not unavailable).
    """
    if not username or not device_label or not xray_api_addr:
        return None

    # Pattern targets exactly this device's email prefix.
    # e.g. "user>>>alice/my-device" matches:
    #   user>>>alice/my-device>>>traffic>>>uplink
    #   user>>>alice/my-device>>>traffic>>>downlink
    pattern = f"user>>>{username}/{device_label}"
    raw = _call_query_stats(addr=xray_api_addr, pattern=pattern, timeout=timeout)
    if raw is None:
        return None

    upload = 0
    download = 0
    for name, value in raw:
        if ">>>traffic>>>uplink" in name:
            upload += max(0, value)
        elif ">>>traffic>>>downlink" in name:
            download += max(0, value)

    return UserTrafficStats(
        upload_bytes=upload,
        download_bytes=download,
        total_bytes=upload + download,
    )
```

- [ ] **Step 4: Run to verify all pass**

```bash
python3 -m pytest tests/test_xray_stats.py::TestGetDeviceTraffic -v
```

Expected: all 7 tests `PASSED`

- [ ] **Step 5: Run full xray_stats test suite**

```bash
python3 -m pytest tests/test_xray_stats.py -v
```

Expected: all tests `PASSED`

- [ ] **Step 6: Commit**

```bash
git add backend/xray_stats.py tests/test_xray_stats.py
git commit -m "feat: add get_device_traffic to xray_stats"
```

---

## Task 5: Snapshot device traffic on deletion

**Files:**
- Modify: `backend/routes/devices.py`
- Modify: `tests/test_traffic_accounting.py`

- [ ] **Step 1: Write failing test for deletion snapshot**

Add this class to `tests/test_traffic_accounting.py`:

```python
# ── Deletion snapshot ─────────────────────────────────────────────────────────


class TestDeletionSnapshot:
    def test_device_traffic_snapshotted_into_user_totals(self, client, db):
        """Deleting a device must add its Xray traffic to user.traffic_up/down_bytes."""
        user = _make_user(db, username="snap-user", token="snap-token")
        _make_device(db, user_id=user.id, device_id="device-to-delete")

        device_stats = UserTrafficStats(
            upload_bytes=30_000_000,
            download_bytes=70_000_000,
            total_bytes=100_000_000,
        )

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.routes.devices.get_device_traffic", return_value=device_stats):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "snap-token", "device_id": "device-to-delete"},
                )

        assert resp.status_code == 200
        db.refresh(user)
        assert user.traffic_up_bytes == 30_000_000
        assert user.traffic_down_bytes == 70_000_000

    def test_deletion_proceeds_when_xray_unavailable(self, client, db):
        """Device deletion must succeed even if Xray stats are unreachable."""
        user = _make_user(db, username="offline-user", token="offline-token")
        _make_device(db, user_id=user.id, device_id="device-offline")

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.routes.devices.get_device_traffic", return_value=None):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "offline-token", "device_id": "device-offline"},
                )

        assert resp.status_code == 200
        # stored fields unchanged (nothing captured, but no crash)
        db.refresh(user)
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0

    def test_existing_stored_traffic_is_accumulated(self, client, db):
        """A second deletion adds to existing stored totals, not replaces them."""
        # Simulate a user who already had one deletion snapshotted
        user = _make_user(
            db,
            username="accum-user",
            token="accum-token",
            traffic_up_bytes=10_000_000,
            traffic_down_bytes=20_000_000,
        )
        _make_device(db, user_id=user.id, device_id="second-device")

        second_stats = UserTrafficStats(
            upload_bytes=5_000_000,
            download_bytes=15_000_000,
            total_bytes=20_000_000,
        )

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.routes.devices.get_device_traffic", return_value=second_stats):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "accum-token", "device_id": "second-device"},
                )

        assert resp.status_code == 200
        db.refresh(user)
        assert user.traffic_up_bytes == 15_000_000   # 10M + 5M
        assert user.traffic_down_bytes == 35_000_000  # 20M + 15M
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_traffic_accounting.py::TestDeletionSnapshot -v
```

Expected: `FAILED` — `get_device_traffic` not importable from `backend.routes.devices` (not yet imported there).

- [ ] **Step 3: Implement deletion snapshot in devices.py**

In `backend/routes/devices.py`, add the import at the top:

```python
from backend.xray_stats import get_device_traffic
```

Then in the `remove_device` function, insert the snapshot block between getting the device and calling `deactivate_device`. The relevant section becomes:

```python
    device = get_active_device(db, user.id, device_id)
    if device is None:
        return JSONResponse(status_code=404, content={"detail": "Device not found"})

    # ── Traffic snapshot ──────────────────────────────────────────────────────
    # Before deactivating, capture this device's current Xray traffic counter
    # and add it to the user's persistent stored totals.  This ensures the
    # user's cumulative total never decreases when a device is removed, even
    # after Xray resets its in-memory counters on restart.
    #
    # If Xray is unreachable, we proceed with deletion and accept that the
    # device's final traffic is not captured — honest limitation, not a crash.
    if settings.xray_api_addr:
        device_label = (device.device_id or "")[:24]
        _snapshot = get_device_traffic(
            username=user.username,
            device_label=device_label,
            xray_api_addr=settings.xray_api_addr,
        )
        if _snapshot is not None and _snapshot.total_bytes > 0:
            user.traffic_up_bytes = (user.traffic_up_bytes or 0) + _snapshot.upload_bytes
            user.traffic_down_bytes = (user.traffic_down_bytes or 0) + _snapshot.download_bytes
            db.commit()
    # ─────────────────────────────────────────────────────────────────────────

    deactivate_device(db, device)
```

- [ ] **Step 4: Run to verify all pass**

```bash
python3 -m pytest tests/test_traffic_accounting.py::TestDeletionSnapshot -v
```

Expected: all 3 tests `PASSED`

- [ ] **Step 5: Run full suite**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/routes/devices.py tests/test_traffic_accounting.py
git commit -m "feat: snapshot device traffic into user totals on device deletion"
```

---

## Task 6: Update cabinet display (user_page.py)

**Files:**
- Modify: `backend/routes/user_page.py`
- Modify: `tests/test_xray_stats.py`
- Modify: `tests/test_traffic_accounting.py`

- [ ] **Step 1: Update the broken N/A test and add new cabinet tests**

In `tests/test_xray_stats.py`, rename and update `test_shows_na_when_xray_api_not_configured` inside `TestCabinetTrafficDisplay`:

```python
class TestCabinetTrafficDisplay:
    def test_shows_zero_traffic_when_xray_api_not_configured(self, client, active_user):
        """When XRAY_API_ADDR is not set, cabinet shows stored total (0 B / ∞ for new users).
        
        Replaces the old test_shows_na_when_xray_api_not_configured.
        N/A is no longer shown — stored traffic (even 0) is always displayed.
        """
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )
        with patch.object(page_mod, "settings", patched):
            resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B / ∞" in resp.text
        assert "N/A" not in resp.text

    def test_shows_real_traffic_when_api_available(self, client, active_user):
        """Cabinet shows formatted bytes when Xray API returns stats."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(
            upload_bytes=10 * 1024 ** 2,
            download_bytes=500 * 1024 ** 2,
            total_bytes=510 * 1024 ** 2,
        )
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=stats):
                resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "510.00 MB" in resp.text

    def test_shows_zero_traffic_honestly(self, client, active_user):
        """0 bytes of traffic shows '0 B / ∞', not 'N/A'."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=stats):
                resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B / ∞" in resp.text
        assert "N/A" not in resp.text
```

Add cabinet integration tests to `tests/test_traffic_accounting.py`:

```python
# ── Cabinet display ───────────────────────────────────────────────────────────


class TestCabinetDisplay:
    def test_new_user_shows_zero_with_api_configured(self, client, active_user):
        """Brand new user (zero stored + zero live) shows '0 B / ∞'."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        zero_stats = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=zero_stats):
                resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B / ∞" in resp.text
        assert "N/A" not in resp.text

    def test_new_user_shows_zero_without_api(self, client, active_user):
        """Brand new user with no API configured still shows '0 B / ∞' (stored=0)."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )
        with patch.object(page_mod, "settings", patched):
            resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B / ∞" in resp.text
        assert "N/A" not in resp.text

    def test_stored_traffic_shown_when_xray_unavailable(self, client, db):
        """If Xray is down, stored historical totals are still shown."""
        user = _make_user(
            db,
            username="stored-user",
            token="stored-token",
            traffic_up_bytes=50_000_000,
            traffic_down_bytes=150_000_000,
        )
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=None):
                resp = client.get(f"/{user.public_token}")
        assert resp.status_code == 200
        # stored total = 50MB + 150MB = 200MB
        assert "200.00 MB" in resp.text
        assert "N/A" not in resp.text

    def test_repeated_page_loads_do_not_mutate_stored_fields(self, client, db):
        """Page views must never write to traffic_up_bytes / traffic_down_bytes."""
        user = _make_user(db, username="readonly-user", token="readonly-token")
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )
        with patch.object(page_mod, "settings", patched):
            client.get(f"/{user.public_token}")
            client.get(f"/{user.public_token}")
            client.get(f"/{user.public_token}")

        db.refresh(user)
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0
```

- [ ] **Step 2: Run tests to verify failures**

```bash
python3 -m pytest tests/test_xray_stats.py::TestCabinetTrafficDisplay tests/test_traffic_accounting.py::TestCabinetDisplay -v
```

Expected: failures due to `test_shows_zero_traffic_when_xray_api_not_configured` (still seeing N/A in page), and import errors for `get_user_traffic_active` in `user_page`.

- [ ] **Step 3: Update imports in user_page.py**

In `backend/routes/user_page.py`, replace the xray_stats import line:

```python
# Replace:
from backend.xray_stats import format_bytes, get_user_traffic
# With:
from backend.xray_stats import combined_traffic, format_bytes, get_user_traffic_active
```

- [ ] **Step 4: Replace the traffic stats block in user_page.py**

Find the traffic stats block (lines ~116–133):

```python
    # ── Traffic stats (real, from Xray) ──────────────────────────────────────
    # Only queried when XRAY_API_ADDR is configured and user is accessible.
    # Returns None when the API is unreachable — shown as "N/A" rather than
    # a fake "0 GB" to be honest about the unavailability.
    traffic_stats = None
    if settings.xray_api_addr and accessible:
        traffic_stats = get_user_traffic(
            username=user.username,
            xray_api_addr=settings.xray_api_addr,
        )

    if traffic_stats is not None:
        traffic_used = format_bytes(traffic_stats.total_bytes)
        traffic_summary = f"{traffic_used} / ∞"
    else:
        traffic_used = "N/A"
        traffic_summary = "N/A"
    # ─────────────────────────────────────────────────────────────────────────
```

Replace it with:

```python
    # ── Traffic stats: stored historical + live active devices ────────────────
    # active_labels is the set of device_id[:24] for all currently active
    # devices.  get_user_traffic_active() queries Xray and filters to those
    # labels only, so deleted devices' counters are never double-counted with
    # the stored historical totals.
    #
    # combined_traffic() always returns a UserTrafficStats (never None):
    #   - When Xray is available: stored + live.
    #   - When Xray is down or not configured: stored only.
    # Both cases produce "0 B / ∞" for a brand new user (stored=0, live=0/None).
    _active_labels = frozenset(d.device_id[:24] for d in device_rows)
    _live = None
    if accessible and settings.xray_api_addr:
        _live = get_user_traffic_active(
            username=user.username,
            active_labels=_active_labels,
            xray_api_addr=settings.xray_api_addr,
        )
    _total = combined_traffic(
        stored_up=user.traffic_up_bytes or 0,
        stored_down=user.traffic_down_bytes or 0,
        live=_live,
    )
    traffic_used = format_bytes(_total.total_bytes)
    traffic_summary = f"{traffic_used} / ∞"
    # ─────────────────────────────────────────────────────────────────────────
```

- [ ] **Step 5: Run to verify all cabinet tests pass**

```bash
python3 -m pytest tests/test_xray_stats.py::TestCabinetTrafficDisplay tests/test_traffic_accounting.py::TestCabinetDisplay -v
```

Expected: all `PASSED`

- [ ] **Step 6: Run full suite**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/routes/user_page.py tests/test_xray_stats.py tests/test_traffic_accounting.py
git commit -m "feat: cabinet uses combined stored+live traffic, remove N/A path"
```

---

## Task 7: Update Happ subscription display (profile.py)

**Files:**
- Modify: `backend/routes/profile.py`
- Modify: `tests/test_xray_stats.py`
- Modify: `tests/test_traffic_accounting.py`

- [ ] **Step 1: Update existing Happ tests and add consistency test**

In `tests/test_xray_stats.py`, update `TestHappUserinfoTraffic` — the mock target changes from `get_user_traffic` to `get_user_traffic_active`. Replace:

```python
class TestHappUserinfoTraffic:
    def test_userinfo_contains_real_upload_and_download(self, client, active_user):
        """When Xray API is available, subscription-userinfo has real byte counts."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(
            upload_bytes=1_000_000,
            download_bytes=9_000_000,
            total_bytes=10_000_000,
        )
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic_active", return_value=stats):
                resp = client.get(f"/{active_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=1000000" in userinfo
        assert "download=9000000" in userinfo
        assert "total=0" in userinfo  # quota unchanged

    def test_userinfo_uses_zero_when_api_unavailable(self, client, active_user):
        """When Xray API is unreachable, subscription-userinfo falls back to stored (0 for new user)."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic_active", return_value=None):
                resp = client.get(f"/{active_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo

    def test_userinfo_zero_bytes_is_honest_zero(self, client, active_user):
        """When API reports 0, subscription-userinfo shows 0 (not a fake value)."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic_active", return_value=stats):
                resp = client.get(f"/{active_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo

    def test_blocked_user_userinfo_unchanged(self, client, inactive_user):
        """Blocked users always get upload=0; download=0 regardless of stats config."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        with patch.object(profile_mod, "settings", patched):
            resp = client.get(f"/{inactive_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo
        assert "total=0" in userinfo
```

Add a cabinet-vs-Happ consistency test to `tests/test_traffic_accounting.py`:

```python
# ── Cabinet / Happ consistency ────────────────────────────────────────────────


_HAPP_HEADERS = {
    "user-agent": "Happ/4.2.1",
    "x-hwid": "consistency-test-hwid",
    "x-device-os": "iOS 17.0",
}


class TestCabinetHappConsistency:
    def test_happ_and_cabinet_reflect_stored_totals(self, client, db):
        """With Xray down, both cabinet and Happ show the same stored total."""
        user = _make_user(
            db,
            username="consistency-user",
            token="consistency-token",
            traffic_up_bytes=12_000_000,
            traffic_down_bytes=88_000_000,
        )

        import backend.routes.user_page as page_mod
        import backend.routes.profile as profile_mod

        page_patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )
        profile_patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f)
               for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )

        with patch.object(page_mod, "settings", page_patched):
            cabinet_resp = client.get(f"/{user.public_token}")

        with patch.object(profile_mod, "settings", profile_patched):
            happ_resp = client.get(f"/{user.public_token}", headers=_HAPP_HEADERS)

        # Cabinet: 12MB + 88MB = 100MB
        assert "100.00 MB" in cabinet_resp.text

        # Happ: upload=12000000, download=88000000
        userinfo = happ_resp.headers["subscription-userinfo"]
        assert "upload=12000000" in userinfo
        assert "download=88000000" in userinfo
```

- [ ] **Step 2: Run to verify failures**

```bash
python3 -m pytest tests/test_xray_stats.py::TestHappUserinfoTraffic tests/test_traffic_accounting.py::TestCabinetHappConsistency -v
```

Expected: `FAILED` — `get_user_traffic` mock target no longer matches (profile.py hasn't been updated yet) and `get_user_traffic_active` not importable from profile.

- [ ] **Step 3: Update imports in profile.py**

In `backend/routes/profile.py`:

1. Add `list_active_devices` to the queries import block:

```python
from backend.queries import (
    count_active_devices,
    get_device,
    get_user_by_token,
    is_user_accessible,
    list_active_devices,
)
```

2. Replace the xray_stats import:

```python
# Replace:
from backend.xray_stats import get_user_traffic
# With:
from backend.xray_stats import combined_traffic, get_user_traffic_active
```

- [ ] **Step 4: Replace the traffic stats block in profile.py**

Find the traffic stats block in `build_happ_subscription_response` (around line 585–593):

```python
    # ── Traffic stats (real, from Xray) ──────────────────────────────────────
    # upload/download in subscription-userinfo are the bytes consumed by the
    # user. total=0 means no quota (unlimited).  We only update upload and
    # download — never total — with Xray stats.
    # When XRAY_API_ADDR is not set, or the API is unreachable, upload and
    # download stay at 0 (the Happ subscription protocol has no "unknown" value
    # for these fields; 0 is the conventional "no data yet / unavailable").
    _traffic = None
    if settings.xray_api_addr:
        _traffic = get_user_traffic(
            username=user.username,
            xray_api_addr=settings.xray_api_addr,
        )
    _upload_bytes = _traffic.upload_bytes if _traffic is not None else 0
    _download_bytes = _traffic.download_bytes if _traffic is not None else 0
    # ─────────────────────────────────────────────────────────────────────────
```

Replace it with:

```python
    # ── Traffic stats: stored historical + live active devices ────────────────
    # Identical accounting to the cabinet:
    #   stored (user.traffic_up/down_bytes) + live (active devices only).
    # active_labels filters to device_id[:24] for currently active devices
    # so deleted devices' Xray counters are never double-counted.
    # combined_traffic always returns a non-None result:
    #   Xray unavailable → stored only; new user → 0.
    _active_labels = frozenset(
        d.device_id[:24] for d in list_active_devices(db, user.id)
    )
    _live = None
    if settings.xray_api_addr:
        _live = get_user_traffic_active(
            username=user.username,
            active_labels=_active_labels,
            xray_api_addr=settings.xray_api_addr,
        )
    _total = combined_traffic(
        stored_up=user.traffic_up_bytes or 0,
        stored_down=user.traffic_down_bytes or 0,
        live=_live,
    )
    _upload_bytes = _total.upload_bytes
    _download_bytes = _total.download_bytes
    # ─────────────────────────────────────────────────────────────────────────
```

- [ ] **Step 5: Run to verify all Happ and consistency tests pass**

```bash
python3 -m pytest tests/test_xray_stats.py::TestHappUserinfoTraffic tests/test_traffic_accounting.py::TestCabinetHappConsistency -v
```

Expected: all `PASSED`

- [ ] **Step 6: Run full suite**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/routes/profile.py tests/test_xray_stats.py tests/test_traffic_accounting.py
git commit -m "feat: Happ subscription uses combined stored+live traffic accounting"
```

---

## Task 8: Final integration test — end-to-end deletion flow

**Files:**
- Modify: `tests/test_traffic_accounting.py`

- [ ] **Step 1: Add the end-to-end deletion + display test**

Add this class to `tests/test_traffic_accounting.py`:

```python
# ── End-to-end: deletion does not reduce displayed total ──────────────────────


class TestEndToEndDeletionFlow:
    def test_deletion_does_not_reduce_cabinet_total(self, client, db):
        """Full flow: device had traffic → deleted → cabinet still shows that traffic."""
        user = _make_user(db, username="e2e-user", token="e2e-token")
        _make_device(db, user_id=user.id, device_id="e2e-device")

        device_stats = UserTrafficStats(
            upload_bytes=100_000_000,   # 100 MB upload
            download_bytes=400_000_000, # 400 MB download
            total_bytes=500_000_000,
        )

        # Step 1: delete the device (snapshots 500 MB into user)
        import backend.routes.devices as devices_mod
        patched_devices = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )
        with patch.object(devices_mod, "settings", patched_devices):
            with patch("backend.routes.devices.get_device_traffic", return_value=device_stats):
                del_resp = client.post(
                    "/api/device/remove",
                    json={"token": "e2e-token", "device_id": "e2e-device"},
                )
        assert del_resp.status_code == 200

        # Step 2: load cabinet — Xray has no active devices (device deleted),
        # but stored totals must still show the 500 MB
        import backend.routes.user_page as page_mod
        patched_page = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f)
               for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        zero_live = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(page_mod, "settings", patched_page):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=zero_live):
                cabinet_resp = client.get(f"/{user.public_token}")

        assert cabinet_resp.status_code == 200
        # 100MB + 400MB = 500 MB total
        assert "500.00 MB" in cabinet_resp.text
        assert "N/A" not in cabinet_resp.text
```

- [ ] **Step 2: Run to verify it passes**

```bash
python3 -m pytest tests/test_traffic_accounting.py::TestEndToEndDeletionFlow -v
```

Expected: `PASSED`

- [ ] **Step 3: Run the complete test suite one final time**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests pass, zero failures.

- [ ] **Step 4: Final commit**

```bash
git add tests/test_traffic_accounting.py
git commit -m "test: add end-to-end deletion flow test for cumulative traffic accounting"
```

---

## Self-Review Checklist

Spec requirements vs plan coverage:

| Requirement | Task |
|---|---|
| New user sees `0 B / ∞` | Task 6 (cabinet), Task 7 (Happ) |
| `N/A` only when truly unavailable | Task 6 removes N/A path |
| Device deletion preserves traffic | Task 5 (snapshot), Task 8 (e2e) |
| Persistent cumulative fields on User | Task 1 |
| Stored historical = deleted devices only | Tasks 2–4 (combined_traffic semantics) |
| Live filtered to active devices | Task 3 (get_user_traffic_active) |
| Cabinet and Happ same source of truth | Task 7 (TestCabinetHappConsistency) |
| Repeated reads don't double-write | Task 6 (test_repeated_page_loads_do_not_mutate_stored_fields) |
| Xray unavailable preserves stored | Task 2 (combined_traffic test), Task 6 (test_stored_traffic_shown_when_xray_unavailable) |
| Schema migration for existing DBs | Task 1 (upgrade_db_schema) |
| Per-device UUID logic unchanged | No changes to xray_clients.py, happ_devices.py |
| Xray watcher/reload unchanged | No changes to xray_reload.py, xray_clients.py |
