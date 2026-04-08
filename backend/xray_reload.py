"""Xray process reload helper.

Executes an operator-configured shell command to reload the running Xray
process after the clients config file has been updated. This is the
mechanism that turns a correct-on-disk config into real VPN revocation.

Reload strategy: configurable subprocess command (XRAY_RELOAD_COMMAND).

Why a subprocess command, not a direct signal or API call:
  The FastAPI app runs inside a Docker container that has no Docker socket
  mounted and no access to host process namespaces. Xray typically runs
  outside this container — on the host via systemd, or in a sibling
  container. A configurable command is the only approach that works for
  all common deployment topologies without baking in assumptions about
  the host environment.

Deployment examples:
  Host systemd:
    XRAY_RELOAD_COMMAND=systemctl reload xray

  Docker sibling container (requires Docker socket: /var/run/docker.sock):
    XRAY_RELOAD_COMMAND=docker kill --signal=SIGHUP xray

  Custom update script:
    XRAY_RELOAD_COMMAND=/opt/scripts/apply-xray-clients.sh

Security:
  The command is split via shlex.split() and executed with shell=False.
  This prevents shell injection even if the env var value is accidentally
  set to something surprising. The command runs as the app process user.
  Treat XRAY_RELOAD_COMMAND as an admin-only credential-level setting.

Failure handling:
  All failure cases (non-zero exit, timeout, command not found, OS error)
  are caught, logged, and converted to a False return. The caller always
  continues normally — a failed reload is never fatal to the request.
  The config file is always correct on disk; only the running process
  state is stale when reload fails.

Note on XRAY_CLIENTS_CONFIG_PATH:
  That env var points to a JSON file containing only the Xray clients
  array (not a full Xray config). The admin is responsible for pointing
  their Xray config at this file and for writing a reload command that
  applies it. A typical pattern is a small wrapper script that:
    1. Merges the clients array into the full Xray config.
    2. Validates the merged config with `xray run -test -c ...`.
    3. Sends SIGHUP to the Xray process.
  See CLAUDE.md for the recommended production setup.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)

# Sane upper bound so a hung reload command doesn't block a request forever.
_DEFAULT_TIMEOUT_SECONDS = 10


def reload_xray_if_configured(
    command: str | None,
    *,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    watcher_mode: bool = False,
) -> bool:
    """Execute the Xray reload command if one is configured.

    Returns:
      True   — command executed and exited with code 0.
      False  — command not configured, or executed but failed.

    Never raises. All failure paths produce a log entry and return False.

    Args:
      command: the value of XRAY_RELOAD_COMMAND. None or empty → skip.
      timeout: maximum seconds to wait for the command. Sourced from
               XRAY_RELOAD_TIMEOUT_SECONDS (default 10).
      watcher_mode: True when XRAY_RELOAD_VIA_WATCHER is set. Suppresses
               the missing-reload WARNING and logs an INFO instead, because
               the host-side systemd inotify watcher is the intended reload
               mechanism — the absence of XRAY_RELOAD_COMMAND is deliberate.
    """
    if not command or not command.strip():
        if watcher_mode:
            logger.info(
                "xray_reload: clients config written; "
                "host-side watcher is expected to apply changes."
            )
        else:
            logger.warning(
                "xray_reload: XRAY_RELOAD_COMMAND is not set — "
                "clients config was written to disk but the running Xray "
                "process has NOT been reloaded. VPN access changes are not "
                "yet in effect. Set XRAY_RELOAD_COMMAND to enable automatic "
                "reload, or set XRAY_RELOAD_VIA_WATCHER=true if using the "
                "host-side systemd watcher."
            )
        return False

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        logger.error(
            "xray_reload: cannot parse XRAY_RELOAD_COMMAND %r: %s — "
            "Xray was NOT reloaded.",
            command,
            exc,
        )
        return False

    if not argv:
        logger.error(
            "xray_reload: XRAY_RELOAD_COMMAND parsed to empty argv "
            "(value: %r) — Xray was NOT reloaded.",
            command,
        )
        return False

    try:
        result = subprocess.run(
            argv,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "xray_reload: command timed out after %ds (cmd=%r). "
            "Xray may not have reloaded. Check the command and timeout.",
            timeout,
            command,
        )
        return False
    except FileNotFoundError:
        logger.error(
            "xray_reload: command binary not found (cmd=%r). "
            "Verify XRAY_RELOAD_COMMAND and PATH inside the container.",
            command,
        )
        return False
    except OSError as exc:
        logger.error(
            "xray_reload: OS error running command (cmd=%r): %s",
            command,
            exc,
        )
        return False

    if result.returncode == 0:
        logger.info(
            "xray_reload: Xray reloaded successfully (cmd=%r, stdout=%r).",
            command,
            result.stdout.strip() or "(no output)",
        )
        return True

    logger.error(
        "xray_reload: command exited with code %d (cmd=%r). "
        "Xray may not have reloaded. stderr=%r",
        result.returncode,
        command,
        result.stderr.strip() or "(no output)",
    )
    return False
