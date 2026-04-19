# Design: Dynamic Device Titles and Platform Icons

**Date:** 2026-04-11  
**Status:** Approved

---

## Overview

Improve how devices are displayed in the user cabinet by:
1. Replacing raw `device_name` with a formatted `"<OS label> - <device name>"` title computed on the backend.
2. Replacing external CDN icon URLs with local SVG files served from `/static/icons/`.

---

## Platform Normalization Logic

Implemented in `backend/platform_utils.py`. All functions handle `None` safely and use substring matching (case-insensitive) against the raw `platform` string (which may contain OS version suffixes like `"iOS 17"` or `"Android 13"`).

### Priority order (checked in this exact sequence)

| Priority | Substrings matched | Clean label | Icon file |
|----------|-------------------|-------------|-----------|
| 1 | `ipad`, `ipados` | `iPadOS` | `ipad.svg` |
| 2 | `iphone`, `ios` | `iOS` | `ios.svg` |
| 3 | `mac`, `macos`, `darwin` | `macOS` | `macos.svg` |
| 4 | `android` | `Android` | `android.svg` |
| 5 | `windows`, `win` | `Windows` | `windows.svg` |
| 6 | `linux`, `ubuntu`, `debian`, `fedora`, `arch` | `Linux` | `linux.svg` |
| 7 | (none matched) | `platform.upper()` if non-empty, else `"Unknown"` | `unknown.svg` |

### Functions

```python
def normalize_platform(platform: str | None) -> str:
    """Return a clean OS label from a raw platform string."""

def platform_icon(platform: str | None) -> str:
    """Return the icon filename (e.g. 'ios.svg') for a raw platform string."""

def format_device_title(platform: str | None, device_name: str | None) -> str:
    """Return '<label> - <device_name>' or '<label> - Unknown device'."""
```

`format_device_title` always returns a non-empty string. If `device_name` is `None` or empty, it substitutes `"Unknown device"`.

---

## Backend Changes (`backend/routes/user_page.py`)

The devices list passed to the template gains two new fields:

```python
{
    "device_id": device.device_id,
    "device_name": device.device_name,       # unchanged — keep for backwards compat
    "platform": device.platform,             # unchanged
    "last_seen_at": _fmt_msk_datetime(...),
    "device_title": format_device_title(device.platform, device.device_name),  # NEW
    "icon": platform_icon(device.platform),                                      # NEW
}
```

No other route or API is touched.

---

## Icons (`static/icons/`)

Seven local SVG files, monochrome, designed to render cleanly at 18–22px:

| File | Platform |
|------|----------|
| `ios.svg` | iPhone / iOS |
| `ipad.svg` | iPad / iPadOS |
| `macos.svg` | Mac / macOS |
| `android.svg` | Android |
| `windows.svg` | Windows |
| `linux.svg` | Linux |
| `unknown.svg` | Fallback |

No external URLs. No CDN dependencies.

---

## Template Changes (`templates/user_page.html`)

Replace the existing Jinja2 inline icon-detection block (lines 111–126) with:

```html
<div class="device-item" data-device-row data-device-id="{{ device.device_id }}">
  <div class="device-main">
    <img class="device-icon" src="/static/icons/{{ device.icon }}" alt="{{ device.device_title }}">
    <div class="device-info">
      <span class="device-name">{{ device.device_title }}</span>
      <span class="device-activity">Последняя активность: {{ device.last_seen_at }}</span>
    </div>
  </div>
  <!-- remove button unchanged -->
</div>
```

`device.device_name` is no longer rendered directly (replaced by `device.device_title`). The raw field is still passed from the backend for any future use.

---

## CSS Changes (`static/css/user_page.css`)

Minimal additions only:

```css
.device-icon {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}
```

`.device-main` already uses `display: flex` — `align-items: center` added if not already present. No layout redesign.

---

## Tests

### New: `tests/test_platform_utils.py`

Unit tests for `backend/platform_utils.py`:

- `normalize_platform`: exact matches, version suffixes (`"iOS 17"`, `"Android 13"`), priority order (iPad before iOS), `None` input, empty string, unknown value
- `platform_icon`: same inputs, verifies correct filename returned, fallback returns `"unknown.svg"`
- `format_device_title`: normal case, `None` device_name → `"Unknown device"`, `None` platform → fallback label

### Updated: `tests/test_cabinet_ux.py`

Add smoke tests verifying:
- `device_title` field renders in the HTML response (e.g. `"iOS - "` appears for an iOS device)
- Local icon path `/static/icons/ios.svg` appears in the HTML (no Wikipedia CDN URLs)

---

## What Is Not Changed

- `POST /api/device/register` / `POST /api/device/remove` — unaffected
- Happ subscription response — unaffected
- Any existing test (other than adding to `test_cabinet_ux.py`)
- Database schema — no migrations needed
