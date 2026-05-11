"""CLI management tool for shashkoffVPN.

Usage:
    python -m backend.cli <command> [options]

Commands:
    create-user      Create a new user
    list-users       List all users
    get-user         Show details for a single user
    activate-user    Set is_active=True for a user
    deactivate-user  Set is_active=False for a user
    extend-user      Extend expiry by N days from today (or from current expiry)
    set-expiry       Set exact expiry datetime (ISO 8601) or clear it
    reset-token      Replace a user's public_token
    delete-user        Permanently delete a user and all their devices
    xray-state         Show DB vs Xray HandlerService comparison and drift
    xray-reconcile     Run the reconciler once and apply any drift
    xray-purge-orphans Remove live Xray entries that have no DB row
"""

from __future__ import annotations

import argparse
import secrets
import sys
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from backend.config import get_settings
from backend.db import init_db, session_scope
from backend.models import User
from backend.queries import count_active_devices, is_user_accessible
from backend.reserved import is_token_reserved
from backend.xray_clients import apply_xray_client_changes, _client_email
from backend.xray_handler_api import (
    RemoveStatus,
    XrayApiError,
    XrayApiUnavailable,
    list_users,
    remove_user,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    # token_urlsafe(12) → exactly 16 URL-safe base64 characters
    return secrets.token_urlsafe(12)


def _generate_uuid() -> str:
    return str(uuid.uuid4())


def _parse_expires_at(raw: str) -> datetime:
    return datetime.fromisoformat(raw.strip())


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "never"


def _print_user(user: User, active_devices: int) -> None:
    accessible = is_user_accessible(user)
    print(
        f"  id           : {user.id}\n"
        f"  username     : {user.username}\n"
        f"  token        : {user.public_token}\n"
        f"  uuid         : {user.uuid}\n"
        f"  is_active    : {user.is_active}\n"
        f"  accessible   : {accessible}\n"
        f"  expires_at   : {_fmt_dt(user.expires_at)}\n"
        f"  max_devices  : {user.max_devices}\n"
        f"  active_devs  : {active_devices}\n"
        f"  created_at   : {_fmt_dt(user.created_at)}"
    )


def _find_user(db, token: str) -> User | None:
    return db.scalar(select(User).where(User.public_token == token))


def _require_user(db, token: str) -> User:
    user = _find_user(db, token)
    if user is None:
        print(f"Error: no user with token '{token}'", file=sys.stderr)
        sys.exit(1)
    return user


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def cmd_create_user(args: argparse.Namespace) -> None:
    username = args.username.strip()
    if not username:
        print("Error: --username cannot be empty", file=sys.stderr)
        sys.exit(1)

    token = (args.token or "").strip() or _generate_token()

    if is_token_reserved(token):
        print(f"Error: token '{token}' is reserved", file=sys.stderr)
        sys.exit(1)

    user_uuid = (args.uuid or "").strip() or _generate_uuid()

    if args.device_limit < 1:
        print("Error: --device-limit must be at least 1", file=sys.stderr)
        sys.exit(1)

    # Resolve expiry: --expires-at takes precedence over --expires-days
    expires_at: datetime | None = None
    if args.expires_at:
        try:
            expires_at = _parse_expires_at(args.expires_at)
        except ValueError as exc:
            print(f"Error: invalid --expires-at value: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.expires_days is not None and args.expires_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=args.expires_days)

    with session_scope() as db:
        if db.scalar(select(User).where(User.public_token == token)):
            print(f"Error: token '{token}' is already in use", file=sys.stderr)
            sys.exit(1)

        if db.scalar(select(User).where(User.username == username)):
            print(f"Error: username '{username}' is already in use", file=sys.stderr)
            sys.exit(1)

        if db.scalar(select(User).where(User.uuid == user_uuid)):
            print(f"Error: uuid '{user_uuid}' is already in use", file=sys.stderr)
            sys.exit(1)

        user = User(
            username=username,
            public_token=token,
            uuid=user_uuid,
            is_active=not args.inactive,
            max_devices=args.device_limit,
            expires_at=expires_at,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        print(f"Created user '{username}':")
        _print_user(user, 0)


def cmd_list_users(args: argparse.Namespace) -> None:  # noqa: ARG001
    with session_scope() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        if not users:
            print("No users found.")
            return
        for user in users:
            active = count_active_devices(db, user.id)
            print(f"[{user.id}] {user.username} ({user.public_token})"
                  f"  active={user.is_active}"
                  f"  devices={active}/{user.max_devices}"
                  f"  expires={_fmt_dt(user.expires_at)}")


def cmd_get_user(args: argparse.Namespace) -> None:
    with session_scope() as db:
        user = _require_user(db, args.token)
        active = count_active_devices(db, user.id)
        _print_user(user, active)


def cmd_activate_user(args: argparse.Namespace) -> None:
    with session_scope() as db:
        user = _require_user(db, args.token)
        user.is_active = True
        db.commit()
        print(f"User '{user.username}' activated.")


def cmd_deactivate_user(args: argparse.Namespace) -> None:
    with session_scope() as db:
        user = _require_user(db, args.token)
        user.is_active = False
        db.commit()
        print(f"User '{user.username}' deactivated.")


def cmd_extend_user(args: argparse.Namespace) -> None:
    with session_scope() as db:
        user = _require_user(db, args.token)
        base = (
            user.expires_at
            if user.expires_at and user.expires_at > datetime.utcnow()
            else datetime.utcnow()
        )
        user.expires_at = base + timedelta(days=args.days)
        db.commit()
        print(f"User '{user.username}' expires at {_fmt_dt(user.expires_at)}.")


def cmd_set_expiry(args: argparse.Namespace) -> None:
    with session_scope() as db:
        user = _require_user(db, args.token)
        if args.expires_at.strip().lower() in ("none", "never", ""):
            user.expires_at = None
            db.commit()
            print(f"User '{user.username}' expiry cleared (never expires).")
        else:
            try:
                user.expires_at = _parse_expires_at(args.expires_at)
            except ValueError as exc:
                print(f"Error: invalid --expires-at value: {exc}", file=sys.stderr)
                sys.exit(1)
            db.commit()
            print(f"User '{user.username}' expires at {_fmt_dt(user.expires_at)}.")


def cmd_reset_token(args: argparse.Namespace) -> None:
    new_token = (args.new_token or "").strip() or _generate_token()

    if is_token_reserved(new_token):
        print(f"Error: token '{new_token}' is reserved", file=sys.stderr)
        sys.exit(1)

    with session_scope() as db:
        user = _require_user(db, args.token)

        conflict = db.scalar(
            select(User).where(User.public_token == new_token, User.id != user.id)
        )
        if conflict is not None:
            print(f"Error: token '{new_token}' is already in use", file=sys.stderr)
            sys.exit(1)

        old_token = user.public_token
        user.public_token = new_token
        db.commit()
        print(f"User '{user.username}' token changed: {old_token} → {new_token}")


def cmd_xray_state(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Print a side-by-side comparison of DB-desired and Xray-live client sets.

    Emails for stale-in-Xray entries are pulled directly from the live
    HandlerService response (which is the only authoritative source for
    what Xray will accept on RemoveUserOperation). When the live response
    has no email for a UUID — only on a decoder glitch — the entry is
    labelled ``email=<unknown>`` literally. Synthetic ``orphan/...``
    labels are NEVER printed; the previous behaviour misled operators
    into believing Xray held an entry it never had, which was the
    starting point of the silent-drift production bug.
    """
    from backend.xray_clients import build_desired_runtime_state

    settings = get_settings()
    addr = settings.xray_api_addr
    tag = settings.xray_vless_inbound_tag
    timeout = float(settings.xray_handler_api_timeout)

    print(f"XRAY_API_ADDR          : {addr or '(unset)'}")
    print(f"XRAY_VLESS_INBOUND_TAG : {tag}")
    print(f"XRAY_USE_HANDLER_API   : {settings.xray_use_handler_api}")
    print()

    with session_scope() as db:
        desired = build_desired_runtime_state(db)
    desired_ids = set(desired.keys())

    print(f"DB-desired entries : {len(desired_ids)}")

    if not addr or not settings.xray_use_handler_api:
        print("Xray HandlerService is not configured — drift comparison skipped.")
        return

    try:
        live = list_users(addr=addr, inbound_tag=tag, timeout=timeout)
    except XrayApiUnavailable as exc:
        print(f"Xray API unavailable: {exc}")
        sys.exit(2)
    except XrayApiError as exc:
        print(f"Xray API error: {exc}")
        sys.exit(2)

    live_ids = set(live.keys())
    print(f"Xray-live entries  : {len(live)}")
    to_add = desired_ids - live_ids
    to_remove = live_ids - desired_ids
    print(f"Drift              : +{len(to_add)} -{len(to_remove)}")

    sample = 10
    if to_add:
        print(f"\nMissing in Xray (would add {min(sample, len(to_add))} of {len(to_add)}):")
        for uuid in sorted(to_add)[:sample]:
            print(f"  + {uuid}  email={desired.get(uuid, '?')}")
    if to_remove:
        print(f"\nStale in Xray (would remove {min(sample, len(to_remove))} of {len(to_remove)}):")
        for uuid in sorted(to_remove)[:sample]:
            email = live.get(uuid) or "<unknown>"
            print(f"  - {uuid}  email={email}")


def cmd_xray_reconcile(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Run one reconciliation pass and print the result.

    Equivalent to a single tick of the background reconciler loop. Useful
    after a known operator action (config edit, manual Xray restart) or to
    verify a fresh deployment.

    The ``added`` and ``removed`` counters reflect REAL state changes
    only. ``skipped_already_absent`` / ``already_present_same_uuid``
    flag idempotent no-ops that previously showed up as fake successes.
    """
    from backend.xray_reconciler import run_reconciler_once

    settings = get_settings()
    result = run_reconciler_once(settings)
    print("Reconciler result:")
    keys = (
        "enabled",
        "live",
        "desired",
        "to_add",
        "to_remove",
        "added",
        "replaced_different_uuid",
        "already_present_same_uuid",
        "removed",
        "skipped_already_absent",
        "skipped_no_email",
        "skipped_reason",
    )
    for key in keys:
        print(f"  {key:<28}: {result.get(key)}")
    if result.get("skipped_reason"):
        sys.exit(2)


def cmd_xray_purge_orphans(args: argparse.Namespace) -> None:
    """Remove UUIDs from Xray runtime that have no corresponding DB row.

    Operator-facing cleanup utility for the drifted-state case. The
    reconciler's normal mode also reaps orphans, but a sufficiently
    misconfigured deployment (e.g. the production system before this
    fix) can accumulate live entries that the reconciler cannot remove
    because it has no email for them. This command queries DB + Xray,
    diffs them, and calls remove_user(real_email) for everything in
    Xray that no longer belongs.

    Refuses to run without --yes.
    """
    from backend.xray_clients import build_desired_runtime_state

    settings = get_settings()
    addr = settings.xray_api_addr
    tag = getattr(settings, "xray_vless_inbound_tag", "vless-reality-in")
    timeout = float(getattr(settings, "xray_handler_api_timeout", 5))
    if not addr or not getattr(settings, "xray_use_handler_api", True):
        print("Xray HandlerService is not configured — nothing to purge.", file=sys.stderr)
        sys.exit(2)

    try:
        live = list_users(addr=addr, inbound_tag=tag, timeout=timeout)
    except XrayApiUnavailable as exc:
        print(f"Xray API unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
    except XrayApiError as exc:
        print(f"Xray API error: {exc}", file=sys.stderr)
        sys.exit(2)

    with session_scope() as db:
        desired = build_desired_runtime_state(db)
    desired_ids = set(desired.keys())
    orphan_uuids = sorted(set(live.keys()) - desired_ids)

    print(f"Xray live entries  : {len(live)}")
    print(f"DB-desired entries : {len(desired_ids)}")
    print(f"Orphans to purge   : {len(orphan_uuids)}")

    if not orphan_uuids:
        print("Nothing to do.")
        return

    for uuid in orphan_uuids[:20]:
        email = live.get(uuid) or "<unknown>"
        print(f"  - {uuid}  email={email}")
    if len(orphan_uuids) > 20:
        print(f"  ... and {len(orphan_uuids) - 20} more")

    if not args.yes:
        try:
            entered = input("Type 'PURGE' to confirm: ")
        except EOFError:
            entered = ""
        if entered != "PURGE":
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    removed = 0
    skipped_no_email = 0
    skipped_already_absent = 0
    for uuid in orphan_uuids:
        email = live.get(uuid, "")
        if not email:
            print(
                f"  SKIP {uuid}: Xray returned no email; manual cleanup required.",
                file=sys.stderr,
            )
            skipped_no_email += 1
            continue
        try:
            status = remove_user(
                addr=addr, inbound_tag=tag, email=email, timeout=timeout,
            )
        except XrayApiUnavailable as exc:
            print(
                f"Xray API became unavailable mid-purge after {removed} removals: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        except XrayApiError as exc:
            print(f"  ERROR {uuid} email={email}: {exc}", file=sys.stderr)
            continue
        if status is RemoveStatus.REMOVED:
            removed += 1
        elif status is RemoveStatus.NOT_PRESENT:
            skipped_already_absent += 1

    # Verify the cleanup by re-fetching the live set.
    try:
        live_after = list_users(addr=addr, inbound_tag=tag, timeout=timeout)
        after_count = len(live_after)
    except (XrayApiUnavailable, XrayApiError) as exc:
        after_count = -1
        print(f"Note: post-purge list_users failed: {exc}", file=sys.stderr)

    print(
        f"\nPurge complete: removed={removed} "
        f"skipped_already_absent={skipped_already_absent} "
        f"skipped_no_email={skipped_no_email}"
    )
    print(f"Live entries before: {len(live)}, after: {after_count}")


def cmd_delete_user(args: argparse.Namespace) -> None:
    settings = get_settings()

    with session_scope() as db:
        user = _require_user(db, args.token)
        active = count_active_devices(db, user.id)
        device_count = len(user.devices)
        username = user.username
        token = user.public_token

        print(f"About to permanently delete user '{username}':")
        _print_user(user, active)

        if not args.yes:
            try:
                entered = input("Type the username to confirm deletion: ")
            except EOFError:
                entered = ""
            if entered != username:
                print("Aborted: confirmation did not match the username.", file=sys.stderr)
                sys.exit(1)

        # Step 1 — capture the (uuid, email) pairs we will need to remove
        # from Xray's live state BEFORE the DB rows disappear. After
        # ``db.delete(user)`` and ``db.flush()`` the related device rows
        # cascade away, taking with them the data needed to construct
        # each email. Doing this work in-memory avoids the production bug
        # where the apply path fell back to synthetic ``orphan/<short>``
        # emails which Xray rejected as "not found".
        to_remove_emails: list[str] = []
        for device in user.devices:
            if not device.is_active or not device.device_uuid:
                continue
            to_remove_emails.append(_client_email(user.username, device.device_id))

        db.delete(user)
        db.flush()

        # Step 2 — apply Xray removals BEFORE commit. If any fail with
        # XrayApiError we roll back so the DB and Xray runtime never
        # diverge. The explicit per-email loop runs only when the
        # HandlerService path is configured; the snapshot/legacy path
        # is handled by apply_xray_client_changes below.
        handler_enabled = bool(
            getattr(settings, "xray_use_handler_api", True)
            and settings.xray_api_addr
        )
        addr = settings.xray_api_addr or ""
        tag = getattr(settings, "xray_vless_inbound_tag", "vless-reality-in")
        timeout = float(getattr(settings, "xray_handler_api_timeout", 5))

        try:
            if handler_enabled:
                for email in to_remove_emails:
                    remove_user(
                        addr=addr, inbound_tag=tag, email=email, timeout=timeout,
                    )
            # Always call the standard apply: it writes the on-disk
            # snapshot and, for the legacy path, triggers the reload
            # command. For the HandlerService path it is a no-op diff
            # because the explicit removes above already converged
            # runtime state — but we still want the snapshot file
            # rewritten.
            apply_xray_client_changes(db, settings)
        except XrayApiUnavailable as exc:
            # Transient: the on-disk snapshot is already written (or
            # will be by the next reconciler pass) and the reconciler
            # will reap any still-live UUIDs on its next pass. Commit
            # the DB delete and exit 0 — graceful degradation.
            db.commit()
            print(
                f"Note: user '{username}' deleted in DB. Xray API unavailable ({exc}); "
                "the reconciler will revoke active sessions on its next pass.",
                file=sys.stderr,
            )
            print(
                f"User '{username}' (token {token}) deleted. "
                f"Removed {device_count} devices."
            )
            return
        except XrayApiError as exc:
            # Logical failure (bad inbound tag, malformed message, etc.).
            # Roll back so DB and Xray runtime stay consistent. The user
            # is preserved; the operator can investigate and retry.
            db.rollback()
            print(
                f"ERROR: Xray runtime update failed: {exc}\n"
                f"User '{username}' was NOT deleted — DB transaction rolled back.\n"
                f"Investigate Xray (check `python -m backend.cli xray-state`) and retry.",
                file=sys.stderr,
            )
            sys.exit(2)
        except Exception as exc:  # noqa: BLE001 — defensive
            # Unknown error path. Treat as fatal: roll back and exit non-zero
            # so a buggy code path cannot silently delete a user.
            db.rollback()
            print(
                f"ERROR: Xray apply raised unexpectedly: {exc}\n"
                f"User '{username}' was NOT deleted — DB transaction rolled back.",
                file=sys.stderr,
            )
            sys.exit(2)

        db.commit()
        print(
            f"User '{username}' (token {token}) deleted. "
            f"Removed {device_count} devices."
        )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="shashkoffVPN user management CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # create-user
    p = sub.add_parser("create-user", help="Create a new user")
    p.add_argument("--username", required=True, help="Display name / identifier")
    p.add_argument("--token", default="", help="Public token (auto-generated if omitted)")
    p.add_argument("--uuid", default="", help="VLESS UUID (auto-generated if omitted)")
    p.add_argument("--device-limit", type=int, default=5, metavar="N", help="Max active devices (default: 5)")
    p.add_argument("--expires-days", type=int, default=30, metavar="N", help="Expire after N days from now (default: 30; 0 = never)")
    p.add_argument("--expires-at", default="", metavar="DATETIME", help="Exact expiry as ISO 8601 (overrides --expires-days)")
    p.add_argument("--inactive", action="store_true", help="Create user with is_active=False")

    # list-users
    sub.add_parser("list-users", help="List all users")

    # get-user
    p = sub.add_parser("get-user", help="Show full details for a user")
    p.add_argument("--token", required=True, help="User's public token")

    # activate-user
    p = sub.add_parser("activate-user", help="Set is_active=True")
    p.add_argument("--token", required=True, help="User's public token")

    # deactivate-user
    p = sub.add_parser("deactivate-user", help="Set is_active=False")
    p.add_argument("--token", required=True, help="User's public token")

    # extend-user
    p = sub.add_parser("extend-user", help="Extend expiry by N days")
    p.add_argument("--token", required=True, help="User's public token")
    p.add_argument("--days", type=int, default=30, metavar="N", help="Days to extend (default: 30)")

    # set-expiry
    p = sub.add_parser("set-expiry", help="Set exact expiry datetime (or 'never' to clear)")
    p.add_argument("--token", required=True, help="User's public token")
    p.add_argument("--expires-at", required=True, metavar="DATETIME",
                   help="ISO 8601 datetime, or 'never' to remove expiry")

    # reset-token
    p = sub.add_parser("reset-token", help="Replace a user's public token")
    p.add_argument("--token", required=True, help="Current public token")
    p.add_argument("--new-token", default="", help="New token value (auto-generated if omitted)")

    # delete-user
    p = sub.add_parser("delete-user", help="Permanently delete a user and all their devices")
    p.add_argument("--token", required=True, help="User's public token")
    p.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")

    # xray-state
    sub.add_parser(
        "xray-state",
        help="Show DB vs Xray HandlerService comparison and drift",
    )

    # xray-reconcile
    sub.add_parser(
        "xray-reconcile",
        help="Run reconciler once and apply any drift",
    )

    # xray-purge-orphans
    p = sub.add_parser(
        "xray-purge-orphans",
        help="Remove Xray runtime entries that have no DB row",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive PURGE confirmation prompt",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    init_db()

    handlers = {
        "create-user": cmd_create_user,
        "list-users": cmd_list_users,
        "get-user": cmd_get_user,
        "activate-user": cmd_activate_user,
        "deactivate-user": cmd_deactivate_user,
        "extend-user": cmd_extend_user,
        "set-expiry": cmd_set_expiry,
        "reset-token": cmd_reset_token,
        "delete-user": cmd_delete_user,
        "xray-state": cmd_xray_state,
        "xray-reconcile": cmd_xray_reconcile,
        "xray-purge-orphans": cmd_xray_purge_orphans,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
