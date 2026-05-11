"""Xray gRPC HandlerService client.

Manages the live set of VPN clients inside a running Xray process without
restarting it. This is the runtime layer that, combined with the on-disk
xray-clients.json snapshot and the reconciler safety net, replaces the
old "rewrite config + systemctl restart" pipeline.

Why this exists
---------------
`systemctl restart xray` kills every active VLESS connection on the host
for ~1 second. With dozens of concurrent users that means a visible
hiccup for everyone every time any device is added or removed. The
HandlerService API lets us mutate Xray's in-memory client list with two
RPCs (AlterInbound + AddUserOperation / RemoveUserOperation) and the
running connections are left intact.

Wire-format source (IMPORTANT for future maintainers)
-----------------------------------------------------
The protobuf encoders/decoders in this module are HAND-WRITTEN. They are
NOT generated from .proto files at build time — there is no codegen step
in the deployment pipeline. The wire format was derived against:

    Xray-core v24.11.30  (https://github.com/XTLS/Xray-core/tree/v24.11.30)

Specifically these four proto files were mirrored:

    app/proxyman/command/command.proto      (HandlerService, AlterInbound,
                                             AddUserOperation, RemoveUserOperation,
                                             ListInboundsRequest/Response,
                                             InboundHandlerConfig)
        https://github.com/XTLS/Xray-core/blob/v24.11.30/app/proxyman/command/command.proto

    common/protocol/user.proto              (User { level, email, account })
        https://github.com/XTLS/Xray-core/blob/v24.11.30/common/protocol/user.proto

    common/serial/typed_message.proto       (TypedMessage { type, value })
        https://github.com/XTLS/Xray-core/blob/v24.11.30/common/serial/typed_message.proto

    proxy/vless/account.proto               (vless.Account { id, flow, encryption })
        https://github.com/XTLS/Xray-core/blob/v24.11.30/proxy/vless/account.proto

The field numbers, wire types, type-URL strings and the per-message
encoder/decoder pairs here mirror those files exactly. Regression
coverage that locks the wire bytes lives in
``tests/test_xray_handler_api.py::TestWireFormatGuards``.

If Xray is ever bumped past 24.x with a breaking protobuf change
(renamed fields, renumbered fields, removed services, renamed type
URLs), this module MUST be re-validated against the new proto sources.
Use ``deploy/generate_xray_protos.sh`` to regenerate stubs from the
target tag and diff them against the encoders here. Do NOT trust this
file to keep working across a major version bump.

Xray must be configured with HandlerService enabled:

    "api": { "tag": "api", "services": ["HandlerService", "StatsService"] }

The target VLESS inbound must have a stable tag (default ``vless-reality-in``,
configurable via ``XRAY_VLESS_INBOUND_TAG``). The tag is sent in every
AlterInbound call.

Design choices
--------------
- We hand-encode the small set of protobuf messages we need rather than
  depending on generated stubs. This mirrors the pattern in
  ``backend/xray_stats.py`` (same author, same Xray major version) and
  removes a build step from the deployment story.
- gRPC channel is opened per call. The call frequency is low (one per
  device add/remove plus the once-a-minute reconciler), so the cost of
  keeping a persistent channel is not worth the complexity around
  reconnection on transient failures.
- Two error classes are exposed:
    XrayApiUnavailable — the gRPC endpoint is unreachable (connection refused,
                          DNS failure, timeout). Callers should degrade gracefully.
    XrayApiError       — the call reached Xray but returned a logical failure
                          (bad tag, malformed message). Callers should treat
                          this as fatal for the current operation.
- ``remove_user`` is idempotent: "user not found" responses are swallowed.
- ``add_user`` returns success when Xray reports "already exists" — a
  benign condition during reconciliation when the live set already has
  the entry we wanted to add.

Examples
--------
>>> add_user(
...     inbound_tag="vless-reality-in",
...     device_uuid="aaaa-bbbb-cccc-dddd-eeee",
...     email="alice/iphone-15",
... )
>>> remove_user("vless-reality-in", "alice/iphone-15")
>>> live = list_users("vless-reality-in")
>>> isinstance(live, set)
True
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────


class XrayApiError(Exception):
    """Raised when the HandlerService call reached Xray but failed logically.

    Caller responsibility: treat this as a fatal error for the current
    operation. Routes should roll back the DB transaction and return 500.
    """


class XrayApiUnavailable(XrayApiError):
    """Raised when the gRPC endpoint cannot be reached.

    Caller responsibility: degrade gracefully. The on-disk snapshot is still
    written, and the reconciler will reconcile when connectivity returns.

    Inherits from XrayApiError so callers that only catch the parent class
    still see "something went wrong with Xray"; callers that want to
    distinguish (devices/profile routes, reconciler) catch this class first.
    """


# ── gRPC method paths ────────────────────────────────────────────────────────

_GRPC_ALTER_INBOUND = "/xray.app.proxyman.command.HandlerService/AlterInbound"
_GRPC_LIST_INBOUNDS = "/xray.app.proxyman.command.HandlerService/ListInbounds"

# Protobuf type strings as expected by Xray's serial.TypedMessage.
_TYPE_ADD_USER_OPERATION = "xray.app.proxyman.command.AddUserOperation"
_TYPE_REMOVE_USER_OPERATION = "xray.app.proxyman.command.RemoveUserOperation"
_TYPE_VLESS_ACCOUNT = "xray.proxy.vless.Account"


# ── Protobuf primitive codecs ────────────────────────────────────────────────
#
# We implement just enough of the protobuf wire format to encode the messages
# below and decode ListInbounds responses. This is exhaustively covered by
# unit tests in tests/test_xray_handler_api.py.


def _varint(n: int) -> bytes:
    """Encode a non-negative integer as a protobuf base-128 varint."""
    out = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            break
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint starting at ``pos``; return (value, new_pos)."""
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


def _tag(field_num: int, wire_type: int) -> bytes:
    return _varint((field_num << 3) | wire_type)


def _field_string(field_num: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _tag(field_num, 2) + _varint(len(encoded)) + encoded


def _field_bytes(field_num: int, value: bytes) -> bytes:
    return _tag(field_num, 2) + _varint(len(value)) + value


def _field_uint32(field_num: int, value: int) -> bytes:
    return _tag(field_num, 0) + _varint(value)


def _field_message(field_num: int, value: bytes) -> bytes:
    """Embed an already-serialised message as a length-delimited field."""
    return _tag(field_num, 2) + _varint(len(value)) + value


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    """Advance pos past a field of the given wire type."""
    if wire_type == 0:
        while pos < len(data) and (data[pos] & 0x80):
            pos += 1
        pos += 1
    elif wire_type == 1:
        pos += 8
    elif wire_type == 2:
        length, pos = _read_varint(data, pos)
        pos += length
    elif wire_type == 5:
        pos += 4
    return pos


# ── Message encoders ─────────────────────────────────────────────────────────


def _encode_vless_account(device_uuid: str, flow: str) -> bytes:
    """xray.proxy.vless.Account { string id=1; string flow=2; string encryption=3 }.

    For client entries used by our routing, encryption must be left empty —
    Xray sets it on the inbound, not on each user.
    """
    return _field_string(1, device_uuid) + _field_string(2, flow)


def _encode_typed_message(type_url: str, value: bytes) -> bytes:
    """common.serial.TypedMessage { string type=1; bytes value=2 }."""
    return _field_string(1, type_url) + _field_bytes(2, value)


def _encode_user(email: str, account_value: bytes, level: int = 0) -> bytes:
    """common.protocol.User { uint32 level=1; string email=2; TypedMessage account=3 }."""
    body = b""
    if level:
        body += _field_uint32(1, level)
    body += _field_string(2, email)
    typed = _encode_typed_message(_TYPE_VLESS_ACCOUNT, account_value)
    body += _field_message(3, typed)
    return body


def _encode_add_user_operation(user_value: bytes) -> bytes:
    """app.proxyman.command.AddUserOperation { protocol.User user=1 }."""
    return _field_message(1, user_value)


def _encode_remove_user_operation(email: str) -> bytes:
    """app.proxyman.command.RemoveUserOperation { string email=1 }."""
    return _field_string(1, email)


def _encode_alter_inbound_request(tag: str, operation_value: bytes, op_type: str) -> bytes:
    """app.proxyman.command.AlterInboundRequest { string tag=1; TypedMessage operation=2 }."""
    op_typed = _encode_typed_message(op_type, operation_value)
    return _field_string(1, tag) + _field_message(2, op_typed)


def _encode_list_inbounds_request() -> bytes:
    """app.proxyman.command.ListInboundsRequest is empty."""
    return b""


# ── ListInbounds response decoder ────────────────────────────────────────────
#
# The full response message is:
#   ListInboundsResponse { repeated core.InboundHandlerConfig inbounds = 1 }
#   InboundHandlerConfig { string tag=1; TypedMessage receiver_settings=2;
#                          TypedMessage proxy_settings=3 }
#   TypedMessage(proxy_settings) is xray.proxy.vless.inbound.Config:
#       message Config { repeated User clients = 1; string decryption = 2;
#                        repeated Fallback fallbacks = 3 }
#   User { uint32 level=1; string email=2; TypedMessage account=3 }
#
# Our list_users() only needs the per-user device_uuid, which lives inside
# the vless.Account inside the TypedMessage inside each User. The decoder
# walks the tree just deep enough to collect each Account.id (field 1) under
# the matching inbound tag.


def _decode_vless_account_id(data: bytes) -> str:
    """Pull the ``id`` field (1, string) out of a vless.Account message."""
    pos = 0
    n = len(data)
    while pos < n:
        try:
            tag, pos = _read_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1 and wire_type == 2:
            length, pos = _read_varint(data, pos)
            return data[pos : pos + length].decode("utf-8", errors="replace")
        pos = _skip_field(data, pos, wire_type)
    return ""


def _decode_typed_message(data: bytes) -> tuple[str, bytes]:
    """Return (type_url, value) for a serial.TypedMessage."""
    type_url = ""
    value = b""
    pos = 0
    n = len(data)
    while pos < n:
        try:
            tag, pos = _read_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1 and wire_type == 2:
            length, pos = _read_varint(data, pos)
            type_url = data[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
        elif field_num == 2 and wire_type == 2:
            length, pos = _read_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
        else:
            pos = _skip_field(data, pos, wire_type)
    return type_url, value


def _decode_user_account_id(user_data: bytes) -> str:
    """Read account.TypedMessage(field 3) out of a User and return Account.id."""
    pos = 0
    n = len(user_data)
    while pos < n:
        try:
            tag, pos = _read_varint(user_data, pos)
        except (IndexError, ValueError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 3 and wire_type == 2:
            length, pos = _read_varint(user_data, pos)
            typed_bytes = user_data[pos : pos + length]
            pos += length
            type_url, value = _decode_typed_message(typed_bytes)
            if type_url == _TYPE_VLESS_ACCOUNT:
                return _decode_vless_account_id(value)
        else:
            pos = _skip_field(user_data, pos, wire_type)
    return ""


def _decode_vless_inbound_config_uuids(config_data: bytes) -> list[str]:
    """Extract Account.id values from vless.inbound.Config.clients (repeated User, field 1)."""
    uuids: list[str] = []
    pos = 0
    n = len(config_data)
    while pos < n:
        try:
            tag, pos = _read_varint(config_data, pos)
        except (IndexError, ValueError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1 and wire_type == 2:
            length, pos = _read_varint(config_data, pos)
            user_bytes = config_data[pos : pos + length]
            pos += length
            uuid = _decode_user_account_id(user_bytes)
            if uuid:
                uuids.append(uuid)
        else:
            pos = _skip_field(config_data, pos, wire_type)
    return uuids


def _decode_inbound_handler_config(data: bytes) -> tuple[str, list[str]]:
    """Return (tag, [uuids]) for one InboundHandlerConfig entry."""
    tag_value = ""
    uuids: list[str] = []
    pos = 0
    n = len(data)
    while pos < n:
        try:
            field_tag, pos = _read_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_num = field_tag >> 3
        wire_type = field_tag & 0x7
        if field_num == 1 and wire_type == 2:
            length, pos = _read_varint(data, pos)
            tag_value = data[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
        elif field_num == 3 and wire_type == 2:
            length, pos = _read_varint(data, pos)
            typed_bytes = data[pos : pos + length]
            pos += length
            _proxy_type, proxy_value = _decode_typed_message(typed_bytes)
            # The proxy_settings TypedMessage value is the wire-encoded
            # vless.inbound.Config — decode its clients directly.
            uuids = _decode_vless_inbound_config_uuids(proxy_value)
        else:
            pos = _skip_field(data, pos, wire_type)
    return tag_value, uuids


def decode_list_inbounds_response(data: bytes, target_tag: str) -> set[str]:
    """Return the set of Account.id values registered under ``target_tag``."""
    out: set[str] = set()
    pos = 0
    n = len(data)
    while pos < n:
        try:
            field_tag, pos = _read_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_num = field_tag >> 3
        wire_type = field_tag & 0x7
        if field_num == 1 and wire_type == 2:
            length, pos = _read_varint(data, pos)
            inbound_bytes = data[pos : pos + length]
            pos += length
            tag_value, uuids = _decode_inbound_handler_config(inbound_bytes)
            if tag_value == target_tag:
                out.update(uuids)
        else:
            pos = _skip_field(data, pos, wire_type)
    return out


# ── gRPC transport ───────────────────────────────────────────────────────────


def _grpc_call(
    addr: str,
    method: str,
    request_bytes: bytes,
    timeout: float,
) -> bytes:
    """Send a unary gRPC request and return the raw response bytes.

    Raises:
      XrayApiUnavailable — connection failures (UNAVAILABLE, DEADLINE_EXCEEDED,
        connection refused, DNS lookup failure, ImportError on grpcio).
      XrayApiError       — any other gRPC error or transport exception.
    """
    try:
        import grpc  # noqa: PLC0415
    except ImportError as exc:
        raise XrayApiUnavailable(
            "grpcio is not installed; install it to enable HandlerService"
        ) from exc

    try:
        channel = grpc.insecure_channel(addr)
        rpc = channel.unary_unary(
            method,
            request_serializer=lambda x: x,
            response_deserializer=lambda x: x,
        )
        return rpc(request_bytes, timeout=timeout)
    except grpc.RpcError as exc:
        code = exc.code() if callable(getattr(exc, "code", None)) else None
        details = exc.details() if callable(getattr(exc, "details", None)) else ""
        unavailable_codes = {
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.UNAUTHENTICATED,  # tunnel-level auth issues = "unreachable for us"
        }
        if code in unavailable_codes:
            raise XrayApiUnavailable(
                f"Xray API at {addr} unreachable: {code.name if code else 'unknown'} {details}"
            ) from exc
        raise XrayApiError(
            f"Xray API {method} failed: {code.name if code else 'unknown'} {details}"
        ) from exc
    except OSError as exc:
        raise XrayApiUnavailable(f"Xray API at {addr} unreachable: {exc}") from exc
    finally:
        try:
            channel.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass


# ── Idempotency markers in Xray error strings ────────────────────────────────
# Xray returns these specific phrases in the `details` of a gRPC error when a
# logical condition is benign for our use case. They are stable across Xray
# 24.x and we treat them as success.
_ALREADY_EXISTS_MARKERS = ("already exists",)
_NOT_FOUND_MARKERS = ("not found", "no such user")


def _matches_any(needle: str, haystacks: Iterable[str]) -> bool:
    needle_lower = needle.lower()
    return any(h in needle_lower for h in haystacks)


# ── Public API ───────────────────────────────────────────────────────────────


def add_user(
    addr: str,
    inbound_tag: str,
    device_uuid: str,
    email: str,
    *,
    flow: str = "xtls-rprx-vision",
    timeout: float = 5.0,
) -> None:
    """Add a VLESS client to a running Xray inbound.

    Idempotent: when Xray reports "already exists", this is logged at WARNING
    and treated as success — common during reconciliation when the live set
    already matches.

    Args:
      addr: gRPC endpoint, e.g. ``172.18.0.1:10085``.
      inbound_tag: the inbound's tag, e.g. ``vless-reality-in``.
      device_uuid: VLESS user ID (UUID4 string).
      email: label used by Xray for stats and logs.
      flow: VLESS flow string. Default matches our inbound.
      timeout: seconds.

    Raises:
      XrayApiUnavailable: API endpoint unreachable.
      XrayApiError: any other failure (bad tag, malformed message, etc.).
    """
    if not addr:
        raise XrayApiUnavailable("XRAY_API_ADDR is not configured")

    account_bytes = _encode_vless_account(device_uuid, flow)
    user_bytes = _encode_user(email=email, account_value=account_bytes)
    op_bytes = _encode_add_user_operation(user_bytes)
    request = _encode_alter_inbound_request(
        tag=inbound_tag,
        operation_value=op_bytes,
        op_type=_TYPE_ADD_USER_OPERATION,
    )
    try:
        _grpc_call(addr, _GRPC_ALTER_INBOUND, request, timeout)
    except XrayApiUnavailable:
        raise
    except XrayApiError as exc:
        if _matches_any(str(exc), _ALREADY_EXISTS_MARKERS):
            logger.warning(
                "xray_handler: add_user(email=%s) — Xray reports already exists; treating as success",
                email,
            )
            return
        raise
    logger.info("xray_handler: add_user email=%s uuid=%.8s", email, device_uuid)


def remove_user(
    addr: str,
    inbound_tag: str,
    email: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Remove a VLESS client from a running Xray inbound.

    Idempotent: when Xray reports "not found", this is logged at INFO and
    treated as success — the desired end state is already achieved.

    Raises:
      XrayApiUnavailable: API endpoint unreachable.
      XrayApiError: any other failure.
    """
    if not addr:
        raise XrayApiUnavailable("XRAY_API_ADDR is not configured")

    op_bytes = _encode_remove_user_operation(email)
    request = _encode_alter_inbound_request(
        tag=inbound_tag,
        operation_value=op_bytes,
        op_type=_TYPE_REMOVE_USER_OPERATION,
    )
    try:
        _grpc_call(addr, _GRPC_ALTER_INBOUND, request, timeout)
    except XrayApiUnavailable:
        raise
    except XrayApiError as exc:
        if _matches_any(str(exc), _NOT_FOUND_MARKERS):
            logger.info(
                "xray_handler: remove_user(email=%s) — Xray reports not found; treating as success",
                email,
            )
            return
        raise
    logger.info("xray_handler: remove_user email=%s", email)


def list_users(
    addr: str,
    inbound_tag: str,
    *,
    timeout: float = 5.0,
) -> set[str]:
    """Return the set of device UUIDs currently registered under ``inbound_tag``.

    Used by the reconciler to detect drift between DB-derived state and the
    live Xray client list.

    Raises:
      XrayApiUnavailable: API endpoint unreachable.
      XrayApiError: any other failure.
    """
    if not addr:
        raise XrayApiUnavailable("XRAY_API_ADDR is not configured")

    response_bytes = _grpc_call(
        addr,
        _GRPC_LIST_INBOUNDS,
        _encode_list_inbounds_request(),
        timeout,
    )
    return decode_list_inbounds_response(response_bytes, inbound_tag)
