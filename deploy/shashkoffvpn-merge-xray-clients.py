#!/usr/bin/env python3
"""Merge a ShashkoffVPN clients fragment into the base Xray config.

Usage (standalone script):
  python3 shashkoffvpn-merge-xray-clients.py \\
      /opt/shashkoffvpn/data/xray-clients.json \\
      /etc/xray/config.base.json \\
      /etc/xray/config.merged.json

Arguments:
  clients_path     Path to the clients JSON array written by the app.
  base_config_path Path to the base Xray config that contains everything
                   except the clients list (routing, inbound settings, etc).
  output_path      Path to write the merged config. This is a temp file;
                   the caller moves it into place only after validation.

Exit codes:
  0  — merged config written successfully.
  1  — input error (file not found, bad JSON, no VLESS inbound, etc).

This module is also importable for unit tests:
  from deploy.shashkoffvpn_merge_xray_clients import merge_clients_into_config

Design notes:
  - Pure functions are separated from file I/O so tests don't need temp files.
  - The merge logic touches only the first VLESS inbound's settings.clients.
  - All other parts of the base config are preserved verbatim.
  - The merged config is pretty-printed JSON (indent=2) for readability.

Workflow:
  The caller (shashkoffvpn-reload-xray.sh) always:
    1. Calls this script to produce a merged temp file.
    2. Validates the temp file with `xray run -test`.
    3. Moves the temp file to the live config path.
    4. Reloads Xray.
  If any step fails, the live config is not replaced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def find_vless_inbound(inbounds: list) -> dict | None:
    """Return the first VLESS inbound from the inbounds list, or None."""
    return next((b for b in inbounds if b.get("protocol") == "vless"), None)


def merge_clients_into_config(clients: list, config: dict) -> dict:
    """Return a new config dict with clients injected into the VLESS inbound.

    Args:
      clients: list of Xray client dicts (each has "id", "email", "flow").
      config:  the full Xray config dict (read from config.base.json).

    Returns:
      A new dict (shallow copy of config) with inbound.settings.clients set
      to the provided clients list.

    Raises:
      ValueError: if config contains no VLESS inbound.
    """
    import copy
    result = copy.deepcopy(config)
    inbounds = result.get("inbounds", [])
    vless = find_vless_inbound(inbounds)
    if vless is None:
        raise ValueError(
            "No VLESS inbound found in base config. "
            "Ensure config.base.json contains an inbound with protocol='vless'."
        )
    vless.setdefault("settings", {})["clients"] = clients
    return result


def _load_json(path: Path, label: str) -> object:
    """Read and parse a JSON file, exiting with a clear error on failure."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        print(f"ERROR: {label} not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {label} is not valid JSON ({path}): {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) != 3:
        print(
            f"Usage: {Path(sys.argv[0]).name} "
            "<clients.json> <config.base.json> <config.out.json>",
            file=sys.stderr,
        )
        sys.exit(1)

    clients_path = Path(argv[0])
    base_path = Path(argv[1])
    out_path = Path(argv[2])

    clients = _load_json(clients_path, "clients fragment")
    if not isinstance(clients, list):
        print(
            f"ERROR: {clients_path} must contain a JSON array, "
            f"got {type(clients).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)

    config = _load_json(base_path, "base Xray config")
    if not isinstance(config, dict):
        print(
            f"ERROR: {base_path} must contain a JSON object, "
            f"got {type(config).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        merged = merge_clients_into_config(clients, config)
    except ValueError as exc:
        print(f"ERROR: merge failed: {exc}", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2, ensure_ascii=False)

    print(
        f"OK: merged {len(clients)} clients into VLESS inbound "
        f"→ {out_path}"
    )


if __name__ == "__main__":
    main()
