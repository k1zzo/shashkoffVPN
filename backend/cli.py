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
    delete-user      Permanently delete a user and all their devices
    xray-state       Show DB vs Xray HandlerService comparison and drift
    xray-reconcile   Run the reconciler once and apply any drift
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
from backend.xray_clients import apply_xray_client_changes
from backend.xray_handler_api import XrayApiError, XrayApiUnavailable


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

    Useful for debugging drift: shows how many entries each side has, the
    intersection size, and the first N UUIDs that are missing on either side.
    """
    from backend.xray_clients import _build_uuid_to_email_map, build_desired_runtime_state
    from backend.xray_handler_api import list_users

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
        email_by_uuid = _build_uuid_to_email_map(db)
    email_by_uuid.update(desired)
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

    print(f"Xray-live entries  : {len(live)}")
    to_add = desired_ids - live
    to_remove = live - desired_ids
    print(f"Drift              : +{len(to_add)} -{len(to_remove)}")

    sample = 10
    if to_add:
        print(f"\nMissing in Xray (would add {min(sample, len(to_add))} of {len(to_add)}):")
        for uuid in sorted(to_add)[:sample]:
            print(f"  + {uuid}  email={desired.get(uuid, '?')}")
    if to_remove:
        print(f"\nStale in Xray (would remove {min(sample, len(to_remove))} of {len(to_remove)}):")
        for uuid in sorted(to_remove)[:sample]:
            email = email_by_uuid.get(uuid, f"orphan/{uuid[:8]}")
            print(f"  - {uuid}  email={email}")


def cmd_xray_reconcile(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Run one reconciliation pass and print the result.

    Equivalent to a single tick of the background reconciler loop. Useful
    after a known operator action (config edit, manual Xray restart) or to
    verify a fresh deployment.
    """
    from backend.xray_reconciler import run_reconciler_once

    settings = get_settings()
    result = run_reconciler_once(settings)
    print("Reconciler result:")
    for key in ("enabled", "live", "desired", "to_add", "to_remove", "added", "removed", "skipped_reason"):
        print(f"  {key:<16}: {result.get(key)}")
    if result.get("skipped_reason"):
        sys.exit(2)


def cmd_delete_user(args: argparse.Namespace) -> None:
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

        db.delete(user)
        # Flush so apply_xray_client_changes sees the deletion in the
        # build_active_xray_clients query. We commit ONLY after the runtime
        # apply succeeds, so a logical Xray failure cannot leave the system
        # in an inconsistent state (user gone from DB, UUID still live in
        # Xray with no DB row to identify it).
        db.flush()

        try:
            apply_xray_client_changes(db, get_settings())
        except XrayApiUnavailable as exc:
            # Transient: the on-disk snapshot is already written and the
            # reconciler will reap the UUID on its next pass. Commit the
            # DB delete and exit 0 — graceful degradation.
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
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
