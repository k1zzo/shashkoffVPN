# Device Titles and Platform Icons — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display devices in the cabinet as `"<OS label> - <device name>"` with local SVG platform icons instead of CDN URLs.

**Architecture:** A new pure-Python module `backend/platform_utils.py` encapsulates all normalization logic. `user_page.py` imports it and enriches the device dict with two new fields (`device_title`, `icon`). The template and CSS are updated to consume those fields. Seven local SVG icons replace all external CDN URLs.

**Tech Stack:** Python 3.10+, FastAPI/Jinja2, vanilla SVG, pytest

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `backend/platform_utils.py` | normalize_platform, platform_icon, format_device_title |
| Create | `tests/test_platform_utils.py` | unit tests for all three functions |
| Modify | `backend/routes/user_page.py:179-187` | add `device_title` + `icon` to device dict |
| Create | `static/icons/ios.svg` | iPhone icon |
| Create | `static/icons/ipad.svg` | iPad icon |
| Create | `static/icons/macos.svg` | Mac/laptop icon |
| Create | `static/icons/android.svg` | Android robot icon |
| Create | `static/icons/windows.svg` | Windows four-square icon |
| Create | `static/icons/linux.svg` | Terminal/monitor icon |
| Create | `static/icons/unknown.svg` | Question-mark-in-circle icon |
| Modify | `templates/user_page.html:111-146` | replace CDN Jinja2 block, use device.icon + device.device_title |
| Modify | `static/css/user_page.css:501-506` | update `.device-icon` size, remove border-radius |

---

## Task 1: Create `backend/platform_utils.py` (TDD)

**Files:**
- Create: `tests/test_platform_utils.py`
- Create: `backend/platform_utils.py`

- [ ] **Step 1.1 — Write the failing tests**

Create `tests/test_platform_utils.py`:

```python
"""Unit tests for backend.platform_utils."""
from __future__ import annotations

import pytest

from backend.platform_utils import (
    format_device_title,
    normalize_platform,
    platform_icon,
)


class TestNormalizePlatform:
    # ── iPad must be checked before iOS ──────────────────────────────────────
    def test_ipad_exact(self):
        assert normalize_platform("iPad") == "iPadOS"

    def test_ipados_exact(self):
        assert normalize_platform("iPadOS") == "iPadOS"

    def test_ipados_with_version(self):
        assert normalize_platform("iPadOS 17.2") == "iPadOS"

    def test_ipad_beats_ios_when_both_present(self):
        # Contrived but verifies priority: "ipad" matches before "ios" check
        assert normalize_platform("ipadios") == "iPadOS"

    # ── iOS / iPhone ─────────────────────────────────────────────────────────
    def test_ios_exact_lowercase(self):
        assert normalize_platform("ios") == "iOS"

    def test_ios_with_version(self):
        assert normalize_platform("iOS 17") == "iOS"

    def test_iphone_maps_to_ios(self):
        assert normalize_platform("iPhone") == "iOS"

    def test_iphone_with_model(self):
        assert normalize_platform("iPhone OS 16.5") == "iOS"

    # ── macOS ─────────────────────────────────────────────────────────────────
    def test_macos_exact(self):
        assert normalize_platform("macOS") == "macOS"

    def test_mac_maps_to_macos(self):
        assert normalize_platform("Mac") == "macOS"

    def test_darwin_maps_to_macos(self):
        assert normalize_platform("darwin") == "macOS"

    def test_macos_with_version(self):
        assert normalize_platform("macOS 14.4") == "macOS"

    # ── Android ───────────────────────────────────────────────────────────────
    def test_android_exact(self):
        assert normalize_platform("Android") == "Android"

    def test_android_with_version(self):
        assert normalize_platform("Android 13") == "Android"

    # ── Windows ───────────────────────────────────────────────────────────────
    def test_windows_exact_lowercase(self):
        assert normalize_platform("windows") == "Windows"

    def test_win_maps_to_windows(self):
        assert normalize_platform("Win") == "Windows"

    def test_windows_with_version(self):
        assert normalize_platform("Windows 11") == "Windows"

    # ── Linux ─────────────────────────────────────────────────────────────────
    def test_linux_exact(self):
        assert normalize_platform("Linux") == "Linux"

    def test_ubuntu_maps_to_linux(self):
        assert normalize_platform("Ubuntu") == "Linux"

    def test_debian_maps_to_linux(self):
        assert normalize_platform("Debian") == "Linux"

    def test_fedora_maps_to_linux(self):
        assert normalize_platform("Fedora") == "Linux"

    def test_arch_maps_to_linux(self):
        assert normalize_platform("Arch") == "Linux"

    # ── Fallback ──────────────────────────────────────────────────────────────
    def test_unknown_platform_uppercased(self):
        assert normalize_platform("BeOS") == "BEOS"

    def test_none_returns_unknown(self):
        assert normalize_platform(None) == "Unknown"

    def test_empty_string_returns_unknown(self):
        assert normalize_platform("") == "Unknown"

    def test_whitespace_only_returns_unknown(self):
        assert normalize_platform("   ") == "Unknown"


class TestPlatformIcon:
    def test_ipad(self):
        assert platform_icon("iPad") == "ipad.svg"

    def test_ipados_with_version(self):
        assert platform_icon("iPadOS 17") == "ipad.svg"

    def test_ios(self):
        assert platform_icon("ios") == "ios.svg"

    def test_iphone(self):
        assert platform_icon("iPhone") == "ios.svg"

    def test_macos(self):
        assert platform_icon("macOS") == "macos.svg"

    def test_darwin(self):
        assert platform_icon("darwin") == "macos.svg"

    def test_android(self):
        assert platform_icon("Android 13") == "android.svg"

    def test_windows(self):
        assert platform_icon("windows") == "windows.svg"

    def test_win(self):
        assert platform_icon("Win") == "windows.svg"

    def test_linux(self):
        assert platform_icon("Linux") == "linux.svg"

    def test_ubuntu(self):
        assert platform_icon("ubuntu") == "linux.svg"

    def test_unknown_platform_returns_unknown_svg(self):
        assert platform_icon("SomeOS") == "unknown.svg"

    def test_none_returns_unknown_svg(self):
        assert platform_icon(None) == "unknown.svg"

    def test_empty_returns_unknown_svg(self):
        assert platform_icon("") == "unknown.svg"


class TestFormatDeviceTitle:
    def test_normal_ios(self):
        assert format_device_title("iOS", "iPhone 15") == "iOS - iPhone 15"

    def test_platform_with_version(self):
        assert format_device_title("iOS 17", "iPhone 15") == "iOS - iPhone 15"

    def test_ipad_priority(self):
        assert format_device_title("iPad", "iPad Air") == "iPadOS - iPad Air"

    def test_android_with_version(self):
        assert format_device_title("Android 13", "Pixel 7") == "Android - Pixel 7"

    def test_none_device_name_becomes_unknown_device(self):
        assert format_device_title("Android", None) == "Android - Unknown device"

    def test_empty_device_name_becomes_unknown_device(self):
        assert format_device_title("Android", "") == "Android - Unknown device"

    def test_whitespace_device_name_becomes_unknown_device(self):
        assert format_device_title("Android", "   ") == "Android - Unknown device"

    def test_none_platform_produces_unknown_label(self):
        assert format_device_title(None, "My Device") == "Unknown - My Device"

    def test_both_none(self):
        assert format_device_title(None, None) == "Unknown - Unknown device"

    def test_unknown_platform_uppercased_in_title(self):
        assert format_device_title("BeOS", "Box") == "BEOS - Box"
```

- [ ] **Step 1.2 — Run tests to verify they fail**

```
python3 -m pytest tests/test_platform_utils.py -v
```

Expected: `ModuleNotFoundError: No module named 'backend.platform_utils'`

- [ ] **Step 1.3 — Implement `backend/platform_utils.py`**

Create `backend/platform_utils.py`:

```python
"""Platform normalization helpers for the device cabinet."""
from __future__ import annotations

# Each entry: (substrings_to_match, clean_label, icon_filename)
# Checked in order — iPad/iPadOS must come before iOS/iPhone.
_PLATFORM_MAP: list[tuple[list[str], str, str]] = [
    (["ipad", "ipados"], "iPadOS", "ipad.svg"),
    (["iphone", "ios"], "iOS", "ios.svg"),
    (["mac", "macos", "darwin"], "macOS", "macos.svg"),
    (["android"], "Android", "android.svg"),
    (["windows", "win"], "Windows", "windows.svg"),
    (["linux", "ubuntu", "debian", "fedora", "arch"], "Linux", "linux.svg"),
]


def normalize_platform(platform: str | None) -> str:
    """Return a clean OS label from a raw platform string.

    Matching is case-insensitive substring search in priority order so
    version suffixes ("iOS 17", "Android 13") are handled automatically.
    Returns platform.upper() for unrecognised values, "Unknown" for None/empty.
    """
    if not platform or not platform.strip():
        return "Unknown"
    p = platform.lower()
    for substrings, label, _ in _PLATFORM_MAP:
        if any(s in p for s in substrings):
            return label
    return platform.upper()


def platform_icon(platform: str | None) -> str:
    """Return the icon filename (e.g. 'ios.svg') for a raw platform string.

    Returns 'unknown.svg' for None, empty, or unrecognised values.
    """
    if not platform or not platform.strip():
        return "unknown.svg"
    p = platform.lower()
    for substrings, _, icon in _PLATFORM_MAP:
        if any(s in p for s in substrings):
            return icon
    return "unknown.svg"


def format_device_title(platform: str | None, device_name: str | None) -> str:
    """Return '<OS label> - <device name>' for display in the cabinet.

    If device_name is None or blank, substitutes 'Unknown device'.
    Never returns an empty string.
    """
    label = normalize_platform(platform)
    name = device_name.strip() if device_name and device_name.strip() else "Unknown device"
    return f"{label} - {name}"
```

- [ ] **Step 1.4 — Run tests to verify they pass**

```
python3 -m pytest tests/test_platform_utils.py -v
```

Expected: all tests PASS, 0 failures.

- [ ] **Step 1.5 — Commit**

```bash
git add backend/platform_utils.py tests/test_platform_utils.py
git commit -m "feat: add platform_utils module with normalize, icon, and title helpers"
```

---

## Task 2: Enrich device dict in `user_page.py`

**Files:**
- Modify: `backend/routes/user_page.py`

- [ ] **Step 2.1 — Add import at the top of `user_page.py`**

After the existing imports at `backend/routes/user_page.py:22`, add:

```python
from backend.platform_utils import format_device_title, platform_icon
```

Full import block after the change (lines 1–23):

```python
from datetime import timedelta, timezone
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from backend import happ_limited_links
from backend.config import get_settings
from backend.db import get_db
from backend.platform_utils import format_device_title, platform_icon
from backend.queries import (
    count_active_devices,
    get_user_by_token,
    is_user_accessible,
    list_active_devices,
)
from backend.reserved import is_token_reserved
from backend.routes.profile import build_happ_subscription_response
from backend.subscription_utils import (
    build_happ_deep_link,
    build_subscription_url,
    is_happ_request,
)
from backend.xray_stats import combined_traffic, format_bytes, get_user_traffic_active
```

- [ ] **Step 2.2 — Add `device_title` and `icon` to the device dict (lines 179–187)**

Replace the `"devices"` list comprehension:

Old (lines 179–187):
```python
            "devices": [
                {
                    "device_id": device.device_id,
                    "device_name": device.device_name,
                    "platform": device.platform,
                    "last_seen_at": _fmt_msk_datetime(device.last_seen_at),
                }
                for device in device_rows
            ],
```

New:
```python
            "devices": [
                {
                    "device_id": device.device_id,
                    "device_name": device.device_name,
                    "platform": device.platform,
                    "last_seen_at": _fmt_msk_datetime(device.last_seen_at),
                    "device_title": format_device_title(device.platform, device.device_name),
                    "icon": platform_icon(device.platform),
                }
                for device in device_rows
            ],
```

- [ ] **Step 2.3 — Run full test suite to verify no regression**

```
python3 -m pytest tests/ -v
```

Expected: all previously passing tests still PASS.

- [ ] **Step 2.4 — Commit**

```bash
git add backend/routes/user_page.py
git commit -m "feat: enrich device dict with device_title and icon fields"
```

---

## Task 3: Create local SVG icons

**Files:**
- Create: `static/icons/ios.svg`
- Create: `static/icons/ipad.svg`
- Create: `static/icons/macos.svg`
- Create: `static/icons/android.svg`
- Create: `static/icons/windows.svg`
- Create: `static/icons/linux.svg`
- Create: `static/icons/unknown.svg`

All icons use `fill="currentColor"` so they inherit the surrounding text color automatically. All are designed to render cleanly at 18–20px.

- [ ] **Step 3.1 — Create `static/icons/ios.svg`** (vertical phone with home-button dot)

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M15.5 1h-8A2.5 2.5 0 005 3.5v17A2.5 2.5 0 007.5 23h8a2.5 2.5 0 002.5-2.5v-17A2.5 2.5 0 0015.5 1zm-4 21a1.5 1.5 0 110-3 1.5 1.5 0 010 3zm4.5-4h-9V4h9v14z"/>
</svg>
```

- [ ] **Step 3.2 — Create `static/icons/ipad.svg`** (wider tablet with home-button dot, distinct from phone)

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M18.5 0h-13A2.5 2.5 0 003 2.5v19A2.5 2.5 0 005.5 24h13a2.5 2.5 0 002.5-2.5v-19A2.5 2.5 0 0018.5 0zM12 23a1.5 1.5 0 110-3 1.5 1.5 0 010 3zm6.5-5h-13V3h13v15z"/>
</svg>
```

- [ ] **Step 3.3 — Create `static/icons/macos.svg`** (laptop outline)

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M20 17V6a2 2 0 00-2-2H6a2 2 0 00-2 2v11H1v2h22v-2h-3zM6 6h12v11H6V6z"/>
</svg>
```

- [ ] **Step 3.4 — Create `static/icons/android.svg`** (Android robot — Material Icons path)

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M6 18c0 .55.45 1 1 1h1v3.5c0 .83.67 1.5 1.5 1.5S11 23.33 11 22.5V19h2v3.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V19h1c.55 0 1-.45 1-1v-9H6v9zm-2.5-9C2.67 9 2 9.67 2 10.5v5c0 .83.67 1.5 1.5 1.5S5 16.33 5 15.5v-5C5 9.67 4.33 9 3.5 9zm17 0c-.83 0-1.5.67-1.5 1.5v5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5v-5c0-.83-.67-1.5-1.5-1.5zm-4.97-5.84l1.3-1.3c.2-.2.2-.51 0-.71-.2-.2-.51-.2-.71 0l-1.48 1.48A5.84 5.84 0 0012 2c-.96 0-1.86.23-2.66.63L7.85 1.15c-.2-.2-.51-.2-.71 0-.2.2-.2.51 0 .71l1.31 1.31A5.983 5.983 0 006 8h12c0-1.99-.97-3.75-2.47-4.84zM10 6H9V5h1v1zm5 0h-1V5h1v1z"/>
</svg>
```

- [ ] **Step 3.5 — Create `static/icons/windows.svg`** (four-square grid — Windows logo)

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M3 3h9v9H3V3zm10 0h8v9h-8V3zM3 13h9v9H3v-9zm10 0h8v9h-8v-9z"/>
</svg>
```

- [ ] **Step 3.6 — Create `static/icons/linux.svg`** (monitor frame with `>` prompt triangle and cursor bar)

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path fill-rule="evenodd" d="M2 6a2 2 0 012-2h16a2 2 0 012 2v12a2 2 0 01-2 2H4a2 2 0 01-2-2V6zm2 0v12h16V6H4z"/>
  <path d="M5.5 9.5L9.5 12l-4 2.5z"/>
  <path d="M11 14.5h6v1.5h-6z"/>
</svg>
```

- [ ] **Step 3.7 — Create `static/icons/unknown.svg`** (question mark in circle — Material Icons "help")

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17h-2v-2h2v2zm2.07-7.75l-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.21 1.79-4 4-4s4 1.79 4 4c0 .88-.36 1.68-.93 2.25z"/>
</svg>
```

- [ ] **Step 3.8 — Commit**

```bash
git add static/icons/
git commit -m "feat: add local SVG platform icons (ios, ipad, macos, android, windows, linux, unknown)"
```

---

## Task 4: Update `templates/user_page.html`

**Files:**
- Modify: `templates/user_page.html:111-146`

- [ ] **Step 4.1 — Replace the CDN Jinja2 block and device-item markup**

In `templates/user_page.html`, replace this entire block (lines 110–146):

```html
            {% for device in devices %}
            {% set platform_lower = (device.platform or '')|lower %}
            {% set name_lower = (device.device_name or '')|lower %}
            {% if 'firefox' in platform_lower or 'firefox' in name_lower %}
            {% set icon_src = 'https://upload.wikimedia.org/wikipedia/commons/a/a0/Firefox_logo%2C_2019.svg' %}
            {% set icon_alt = 'Firefox' %}
            {% elif 'safari' in platform_lower or 'safari' in name_lower %}
            {% set icon_src = 'https://upload.wikimedia.org/wikipedia/commons/5/52/Safari_browser_logo.svg' %}
            {% set icon_alt = 'Safari' %}
            {% elif 'chrome' in platform_lower or 'chrome' in name_lower or 'web' in platform_lower %}
            {% set icon_src =
            'https://upload.wikimedia.org/wikipedia/commons/e/e1/Google_Chrome_icon_%28February_2022%29.svg' %}
            {% set icon_alt = 'Chrome' %}
            {% else %}
            {% set icon_src = 'https://upload.wikimedia.org/wikipedia/commons/8/87/Desktop_computer_font_awesome.svg' %}
            {% set icon_alt = 'Устройство' %}
            {% endif %}
            <div class="device-item" data-device-row data-device-id="{{ device.device_id }}">
              <div class="device-main">
                <img class="device-icon" src="{{ icon_src }}" alt="{{ icon_alt }}">
                <div class="device-info">
                  <span class="device-name">{{ device.device_name }}</span>
                  <span class="device-activity">Последняя активность: {{ device.last_seen_at }}</span>
                </div>
              </div>
              <button type="button" class="device-remove-btn" data-action-remove-device
                data-device-id="{{ device.device_id }}" aria-label="Удалить устройство {{ device.device_name }}">
```

With this replacement:

```html
            {% for device in devices %}
            <div class="device-item" data-device-row data-device-id="{{ device.device_id }}">
              <div class="device-main">
                <img class="device-icon" src="/static/icons/{{ device.icon }}" alt="{{ device.device_title }}">
                <div class="device-info">
                  <span class="device-name">{{ device.device_title }}</span>
                  <span class="device-activity">Последняя активность: {{ device.last_seen_at }}</span>
                </div>
              </div>
              <button type="button" class="device-remove-btn" data-action-remove-device
                data-device-id="{{ device.device_id }}" aria-label="Удалить устройство {{ device.device_title }}">
```

The closing SVG inside the button, `</button>`, `</div>`, `{% endfor %}` are unchanged — leave them in place.

- [ ] **Step 4.2 — Commit**

```bash
git add templates/user_page.html
git commit -m "feat: replace CDN icon logic with backend device_title and local icon path"
```

---

## Task 5: Update CSS

**Files:**
- Modify: `static/css/user_page.css:501-506` (base `.device-icon`)
- Modify: `static/css/user_page.css:786-789` (responsive `.device-icon` override)

- [ ] **Step 5.1 — Update base `.device-icon` rule (line 501)**

Old (lines 501–506):
```css
.device-icon {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 50%;
}
```

New:
```css
.device-icon {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}
```

Changes: size 32 → 20px; `border-radius: 50%` removed (was for circular clipping of CDN raster images; not needed for monochrome SVGs).

- [ ] **Step 5.2 — Update responsive `.device-icon` override (line 786)**

Old (lines 786–789):
```css
  .device-icon {
    width: 28px;
    height: 28px;
  }
```

New:
```css
  .device-icon {
    width: 18px;
    height: 18px;
  }
```

- [ ] **Step 5.3 — Commit**

```bash
git add static/css/user_page.css
git commit -m "style: update device-icon to 20px SVG-appropriate size, remove border-radius"
```

---

## Task 6: Add smoke tests to `test_cabinet_ux.py`

**Files:**
- Modify: `tests/test_cabinet_ux.py`

- [ ] **Step 6.1 — Append the new test class to `tests/test_cabinet_ux.py`**

At the end of the file, add:

```python
# ── Device title and icon rendering ──────────────────────────────────────────


class TestDeviceTitleAndIcon:
    def _make_ios_device(self, db, user_id):
        from uuid import uuid4
        device = Device(
            user_id=user_id,
            device_id="dev-icon-1",
            device_name="iPhone 15",
            platform="iOS",
            device_uuid=str(uuid4()),
            is_active=True,
            last_seen_at=_utc(2025, 6, 1, 12, 0),
        )
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    def test_device_title_rendered_in_html(self, client, db):
        """Cabinet must render 'iOS - iPhone 15', not bare device_name."""
        user = _make_user(db, username="icon-user", token="icon-token",
                          expires_at=_utc(2099, 12, 31))
        self._make_ios_device(db, user.id)

        resp = client.get("/icon-token")
        assert resp.status_code == 200
        assert "iOS - iPhone 15" in resp.text

    def test_local_icon_path_in_html(self, client, db):
        """Cabinet must reference the local SVG path, not a CDN URL."""
        user = _make_user(db, username="icon-path-user", token="icon-path-token",
                          expires_at=_utc(2099, 12, 31))
        self._make_ios_device(db, user.id)

        resp = client.get("/icon-path-token")
        assert resp.status_code == 200
        assert "/static/icons/ios.svg" in resp.text

    def test_no_cdn_urls_in_html(self, client, db):
        """Cabinet must not reference any Wikipedia/CDN URLs for device icons."""
        user = _make_user(db, username="no-cdn-user", token="no-cdn-token",
                          expires_at=_utc(2099, 12, 31))
        self._make_ios_device(db, user.id)

        resp = client.get("/no-cdn-token")
        assert resp.status_code == 200
        assert "wikipedia.org" not in resp.text
        assert "wikimedia.org" not in resp.text
```

The `Device` and `_make_user` / `_utc` helpers are already defined at the top of `test_cabinet_ux.py` — no new imports needed beyond what's already there.

- [ ] **Step 6.2 — Run the new tests to verify they pass**

```
python3 -m pytest tests/test_cabinet_ux.py::TestDeviceTitleAndIcon -v
```

Expected: all 3 tests PASS.

- [ ] **Step 6.3 — Run the full test suite**

```
python3 -m pytest tests/ -v
```

Expected: all tests PASS, 0 failures.

- [ ] **Step 6.4 — Commit**

```bash
git add tests/test_cabinet_ux.py
git commit -m "test: add device title and local icon smoke tests to test_cabinet_ux"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** platform normalization ✓, icon mapping ✓, backend fields ✓, local SVGs ✓, template replacement ✓, CSS update ✓, tests ✓
- [x] **No placeholders:** all code blocks are complete
- [x] **Type consistency:** `format_device_title`, `platform_icon`, `normalize_platform` names match across Task 1 implementation and Task 2 import
- [x] **Existing fields preserved:** `device_name` and `platform` remain in device dict (Task 2) — no downstream breakage
- [x] **Priority order locked in:** `_PLATFORM_MAP` has iPad before iOS, matching the agreed spec
- [x] **CSS responsive breakpoint covered:** both the base rule and the `@media` override are updated (Steps 5.1 and 5.2)
