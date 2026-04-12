"""Xray gRPC stats client.

Reads per-user traffic counters from the Xray StatsService.

Xray must be configured with stats enabled in config.base.json:
  - "api": {"tag": "api", "services": ["StatsService"]}
  - "stats": {}
  - "policy": {"levels": {"0": {"statsUserUplink": true, "statsUserDownlink": true}}}
  - An API inbound (dokodemo-door) listening on XRAY_API_ADDR port
  - A routing rule sending "api-in" tag to "api" outbound

Set XRAY_API_ADDR=127.0.0.1:10085 (or host-accessible address in Docker)
to enable stats. When unset, all queries return None (unavailable, not fake 0).

gRPC call: StatsService.QueryStats with pattern="user>>>{username}/"
This matches all client entries whose email starts with "{username}/" —
exactly the format written by xray_clients.build_active_xray_clients():
  {"email": "{user.username}/{device_id[:24]}", ...}

Stat names returned by Xray for a client with email "alice/my-device":
  user>>>alice/my-device>>>traffic>>>uplink
  user>>>alice/my-device>>>traffic>>>downlink
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# gRPC method path for the Xray StatsService.
_GRPC_METHOD = "/xray.app.stats.command.StatsService/QueryStats"


# ── Public data structures ────────────────────────────────────────────────────


@dataclass(frozen=True)
class UserTrafficStats:
    """Total traffic counters for a user, summed across all their devices."""

    upload_bytes: int
    download_bytes: int
    total_bytes: int


def format_bytes(n: int) -> str:
    """Format a byte count as a human-readable string (e.g. '1.23 GB')."""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.2f} KB"
    return f"{n} B"


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


# ── Minimal protobuf encoder ──────────────────────────────────────────────────


def _varint_encode(n: int) -> bytes:
    """Encode a non-negative integer as a protobuf base-128 varint."""
    result = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            result.append(bits | 0x80)
        else:
            result.append(bits)
            break
    return bytes(result)


def encode_query_stats_request(pattern: str = "", reset: bool = False) -> bytes:
    """Encode a QueryStatsRequest protobuf message.

    QueryStatsRequest {
      string pattern = 1;
      bool   reset   = 2;
    }
    """
    buf = bytearray()
    if pattern:
        encoded = pattern.encode("utf-8")
        buf += b"\x0a" + _varint_encode(len(encoded)) + encoded  # field 1, wire 2
    if reset:
        buf += b"\x10\x01"  # field 2, wire 0, value 1
    return bytes(buf)


# ── Minimal protobuf decoder ──────────────────────────────────────────────────


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint from data at pos; return (value, new_pos)."""
    result = 0
    shift = 0
    end = len(data)
    while pos < end:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    """Advance pos past a field of the given wire type."""
    if wire_type == 0:  # varint
        while pos < len(data) and (data[pos] & 0x80):
            pos += 1
        pos += 1
    elif wire_type == 1:  # 64-bit fixed
        pos += 8
    elif wire_type == 2:  # length-delimited
        length, pos = _read_varint(data, pos)
        pos += length
    elif wire_type == 5:  # 32-bit fixed
        pos += 4
    # wire types 3/4 (group start/end) are deprecated; skip without advancing
    return pos


def _decode_stat_message(data: bytes) -> tuple[str, int] | None:
    """Decode a Stat protobuf embedded message.

    Stat { string name = 1; int64 value = 2; }
    Returns (name, value) or None if name is empty.
    """
    name = ""
    value = 0
    pos = 0
    n = len(data)
    while pos < n:
        try:
            tag, pos = _read_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1 and wire_type == 2:  # name: string
            str_len, pos = _read_varint(data, pos)
            name = data[pos : pos + str_len].decode("utf-8", errors="replace")
            pos += str_len
        elif field_num == 2 and wire_type == 0:  # value: int64 (varint-encoded)
            value, pos = _read_varint(data, pos)
        else:
            pos = _skip_field(data, pos, wire_type)
    return (name, value) if name else None


def decode_query_stats_response(data: bytes) -> list[tuple[str, int]]:
    """Decode a QueryStatsResponse protobuf message.

    QueryStatsResponse { repeated Stat stat = 1; }
    Returns a list of (stat_name, value) pairs.
    """
    results: list[tuple[str, int]] = []
    pos = 0
    n = len(data)
    while pos < n:
        try:
            tag, pos = _read_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1 and wire_type == 2:  # repeated Stat message
            msg_len, pos = _read_varint(data, pos)
            stat = _decode_stat_message(data[pos : pos + msg_len])
            pos += msg_len
            if stat is not None:
                results.append(stat)
        else:
            pos = _skip_field(data, pos, wire_type)
    return results


# ── gRPC transport ────────────────────────────────────────────────────────────


def _call_query_stats(
    addr: str,
    pattern: str,
    timeout: float,
    reset: bool = False,
) -> list[tuple[str, int]] | None:
    """Send a QueryStats gRPC request to Xray; return parsed stats or None.

    reset=True passes the reset flag to Xray, which atomically reads and
    clears the matched counters in a single call.

    Returns None on any error (connection refused, timeout, bad response, …).
    Imports grpcio lazily so the module loads without it installed.
    """
    try:
        import grpc  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "xray_stats: grpcio is not installed — install it to enable stats"
        )
        return None

    try:
        channel = grpc.insecure_channel(addr)
        method = channel.unary_unary(
            _GRPC_METHOD,
            request_serializer=lambda x: x,   # request is already bytes
            response_deserializer=lambda x: x,  # return raw protobuf bytes
        )
        request_bytes = encode_query_stats_request(pattern=pattern, reset=reset)
        response_bytes = method(request_bytes, timeout=timeout)
        return decode_query_stats_response(response_bytes)
    except Exception as exc:
        logger.debug(
            "xray_stats: query addr=%s pattern=%r failed — %s: %s",
            addr, pattern, type(exc).__name__, exc,
        )
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def get_user_traffic(
    username: str,
    xray_api_addr: str,
    timeout: float = 3.0,
    reset: bool = False,
) -> UserTrafficStats | None:
    """Query Xray for total traffic used by a user across all their devices.

    reset=True: atomically reads AND clears Xray's in-memory counters for
      this user in a single gRPC call.  Use before any operation that will
      trigger a Xray reload (which resets counters anyway) to ensure the
      values are captured in the DB before they disappear.  Safe to use
      regardless of whether the subsequent reload actually resets stats —
      the counter is already 0 after this call, so no double-counting can
      occur when combined_traffic() is called next.

    Returns None when:
      - xray_api_addr is empty
      - grpcio is not installed
      - the Xray API is unreachable or returns an error

    Returns UserTrafficStats with real values when stats are available.
    A result with all-zero counters means the real count is zero (honest zero),
    not "unavailable" — only None means unavailable.

    Pattern matching: Xray stat names for a user look like
      user>>>alice/my-device-1>>>traffic>>>uplink
    The pattern "user>>>alice/" matches all devices belonging to "alice".
    """
    if not username or not xray_api_addr:
        return None

    pattern = f"user>>>{username}/"
    raw = _call_query_stats(addr=xray_api_addr, pattern=pattern, timeout=timeout, reset=reset)
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


def snapshot_user_traffic_before_reload(
    db,
    user,
    xray_api_addr: str | None,
    timeout: float = 3.0,
) -> bool:
    """Atomically capture all of a user's Xray traffic and persist it to the DB.

    Queries Xray with reset=True, which reads AND clears every stat counter
    matching "user>>>{username}/" in a single gRPC call.  The returned values
    are added to user.traffic_up_bytes / traffic_down_bytes and committed.

    Call this BEFORE apply_xray_client_changes() on any path that triggers a
    Xray reload.  Xray's systemctl reload (SIGHUP) reinitialises the stats
    manager and resets all in-memory counters to zero — this function ensures
    those values are captured in the DB before the reset happens.

    Using reset=True rather than a plain read prevents double-counting: the
    counter is zeroed here, so whether or not the subsequent Xray reload also
    resets counters, the value is stored exactly once.

    Returns True if a snapshot was taken and committed, False otherwise
    (Xray unreachable, not configured, or zero traffic — all safe to ignore).
    """
    if not xray_api_addr:
        return False

    username = getattr(user, "username", None)
    if not username:
        return False

    snapshot = get_user_traffic(
        username=username,
        xray_api_addr=xray_api_addr,
        timeout=timeout,
        reset=True,
    )
    if snapshot is None or snapshot.total_bytes == 0:
        return False

    user.traffic_up_bytes = (user.traffic_up_bytes or 0) + snapshot.upload_bytes
    user.traffic_down_bytes = (user.traffic_down_bytes or 0) + snapshot.download_bytes
    db.commit()
    logger.info(
        "xray_stats: snapshotted user=%s up=%d down=%d before Xray reload",
        username, snapshot.upload_bytes, snapshot.download_bytes,
    )
    return True


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
