"""SSH client for FreshTomato integration.

Provides the only write path for WiFi interface and radio control.
All commands run over SSH using the same credentials as the HTTP interface
password; the username is always attempted as ``root`` first (FreshTomato's
Linux system user), with fallback to the configured HTTP username.

SSH availability check
──────────────────────
Availability is determined by whether an SSH connection handshake succeeds.
No command is run — if the handshake succeeds, SSH is available.

Username resolution
───────────────────
FreshTomato's web interface uses ``admin`` as the HTTP credential, but the
underlying Linux system only has a ``root`` account for SSH.  The check
tries ``root`` first; if that raises an authentication error it falls back
to the configured HTTP username.  The first username that succeeds is stored
and reused for all subsequent commands.

Interface command  (persistent — survives reboot)
─────────────────────────────────────────────────
    nvram set wlN_radio=<0|1>; nvram commit; service restart_wireless &

The ``&`` is critical: it backgrounds the service restart so the SSH session
returns before the wireless subsystem is torn down.  Without it, the restart
can drop active SSH connections and leave sshd temporarily unavailable,
causing the next SSH call to fail.

Radio command  (runtime only — does not survive reboot)
────────────────────────────────────────────────────────
    wl -i <ifname> radio on|off

Per the FreshTomato wiki (toggle_radio), the correct arguments are the
strings ``on`` and ``off``.  Numeric 0/1 are NOT valid and are silently
ignored by the Broadcom driver.

Broadcom radio polarity
───────────────────────
``wl -i ethN radio`` (no argument) queries the state and returns a value
ending in 0 or 1, where:
    0  = radio is ON   (transmitting)
    1  = radio is OFF  (disabled)

This inverted convention applies to both the query output AND the
``wlstats[N].radio`` field in status-data.jsx that HA reads for state.
api.py corrects for this: ``wl_hw_radio[N] = (raw_value == 0)``.

After each ``wl radio on/off`` command a readback is performed and logged
so the debug log confirms whether the driver accepted the change.

It is NOT used for WET/bridge-mode interfaces — the driver silently ignores
the command in that mode.  The Radio switch entity is marked unavailable for
WET-mode interfaces at the switch layer (see switch.py).

Why not ``wl radio`` for WET interfaces
────────────────────────────────────────
In WET (Wireless Ethernet Bridge) mode the interface is associated with an
upstream AP.  The "radio" concept is inseparable from the association: you
can't turn the RF off without dropping the bridge.  The Interface switch
(nvram+restart) is the correct control for WET-mode interfaces.
"""
from __future__ import annotations

import asyncio
import logging

import asyncssh

_LOGGER = logging.getLogger(__name__)

# Timeout for the SSH connection handshake (seconds).
_CONNECT_TIMEOUT = 10
# Timeout for command execution after connection is up (seconds).
# Interface command returns almost instantly (restart is backgrounded).
# Radio command (wl) also returns instantly on a live interface.
_CMD_TIMEOUT = 10

# FreshTomato's Linux SSH user.  Tried before the configured HTTP username.
_SSH_PRIMARY_USER = "root"

# Broadcom wl exit code when the wireless driver is not yet ready.
# Seen as "wl: Not In Range" — occurs briefly after service restart_wireless.
_WL_NOT_IN_RANGE_EXIT = 254
# Retry parameters for wl radio commands that get "Not In Range".
_WL_RADIO_RETRIES = 3          # extra attempts after the first
_WL_RADIO_RETRY_DELAY = 5.0    # seconds between retries

# asyncssh connection kwargs shared across all calls.
_CONNECT_OPTS: dict = dict(
    known_hosts=None,           # FreshTomato routers have no CA
    server_host_key_algs=[      # Accept any host key type the router presents
        "ssh-rsa", "rsa-sha2-256", "rsa-sha2-512",
        "ecdsa-sha2-nistp256", "ssh-ed25519",
    ],
)


class SshError(Exception):
    """Raised when an SSH command fails or the connection cannot be established."""


async def _try_connect(host: str, port: int, username: str, password: str) -> bool:
    """Attempt a connection-only SSH handshake; return True on success."""
    try:
        async with asyncio.timeout(_CONNECT_TIMEOUT):
            async with asyncssh.connect(
                host, port=port,
                username=username, password=password,
                **_CONNECT_OPTS,
            ):
                return True
    except asyncssh.PermissionDenied:
        _LOGGER.debug("SSH %s@%s:%d — permission denied", username, host, port)
        return False
    except asyncssh.Error as err:
        _LOGGER.debug("SSH %s@%s:%d — error: %s", username, host, port, err)
        return False
    except (OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("SSH %s@%s:%d — connection failed: %s", username, host, port, err)
        return False


async def _run(
    host: str, port: int, username: str, password: str, cmd: str,
    retries: int = 2, retry_delay: float = 3.0,
) -> str:
    """Open a single SSH session, run one command, return stdout stripped.

    Retries up to ``retries`` times with ``retry_delay`` seconds between
    attempts.  This handles the brief window after ``service restart_wireless``
    where sshd may be temporarily unavailable.
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 2):   # retries+1 total attempts
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT + _CMD_TIMEOUT):
                async with asyncssh.connect(
                    host, port=port,
                    username=username, password=password,
                    **_CONNECT_OPTS,
                ) as conn:
                    result = await conn.run(cmd, check=False)
                    stdout = (result.stdout or "").strip()
                    stderr = (result.stderr or "").strip()
                    _LOGGER.debug(
                        "SSH %s@%s:%d  cmd=%r  exit=%d  stdout=%r  stderr=%r",
                        username, host, port, cmd, result.exit_status, stdout, stderr,
                    )
                    return stdout
        except asyncssh.Error as err:
            last_err = SshError(f"SSH error: {err}")
        except (OSError, asyncio.TimeoutError) as err:
            last_err = SshError(f"SSH connection failed: {err}")

        if attempt <= retries:
            _LOGGER.debug(
                "SSH attempt %d/%d failed (%s@%s:%d), retrying in %.1fs — %s",
                attempt, retries + 1, username, host, port, retry_delay, last_err,
            )
            await asyncio.sleep(retry_delay)

    raise last_err  # type: ignore[misc]


async def check_ssh_available(
    host: str, port: int, configured_username: str, password: str
) -> tuple[bool, str]:
    """Check SSH reachability and resolve the working username.

    Tries ``root`` first (FreshTomato default SSH user), then falls back to
    ``configured_username`` (the HTTP admin credential).

    Returns
    -------
    (available, working_username)
        available        – True if any username succeeded
        working_username – the username that worked, or ``root`` as default
                           when unavailable (so callers always get a string)
    """
    candidates: list[str] = [_SSH_PRIMARY_USER]
    if configured_username and configured_username != _SSH_PRIMARY_USER:
        candidates.append(configured_username)

    for username in candidates:
        _LOGGER.debug("SSH availability check: trying %s@%s:%d", username, host, port)
        if await _try_connect(host, port, username, password):
            _LOGGER.info(
                "SSH available at %s:%d (username: %s)", host, port, username
            )
            return True, username

    _LOGGER.info(
        "SSH not available at %s:%d — tried usernames: %s",
        host, port, candidates,
    )
    return False, _SSH_PRIMARY_USER


async def set_wifi_interface(
    host: str, port: int, username: str, password: str,
    unit: int, enable: bool,
) -> None:
    """Enable or disable a WiFi interface persistently via SSH.

    Sets the nvram bit, commits, then backgrounds the wireless service
    restart so the SSH session returns before the network is disrupted.

    This change persists across reboots.
    """
    value = "1" if enable else "0"
    # & backgrounds the restart — SSH returns before the wireless subsystem
    # goes down, preventing connection drops and sshd restart races.
    cmd = f"nvram set wl{unit}_radio={value}; nvram commit; service restart_wireless &"
    _LOGGER.debug("SSH set_wifi_interface: unit=%d enable=%s  cmd=%r", unit, enable, cmd)
    try:
        await _run(host, port, username, password, cmd)
    except SshError as err:
        raise SshError(f"Failed to set wl{unit} interface to {value}: {err}") from err


async def set_wifi_radio(
    host: str, port: int, username: str, password: str,
    unit: int, ifname: str, enable: bool,
) -> None:
    """Enable or disable the RF hardware radio via SSH (runtime, no nvram commit).

    Runs: wl -i <ifname> radio on|off

    Per the FreshTomato wiki (toggle_radio), the correct arguments are the
    strings ``on`` and ``off``.  Numeric 0/1 are NOT valid and are silently
    ignored by the Broadcom driver — this was the root cause of the previous
    "does nothing" behaviour.

    "Not In Range" (exit 254) means the wireless driver is not yet ready to
    accept the command — typically occurring in the seconds after
    ``service restart_wireless`` finishes.  The command is retried up to
    _WL_RADIO_RETRIES times with _WL_RADIO_RETRY_DELAY seconds between
    attempts to handle this transient window.

    This is a driver command that affects the hardware RF state immediately
    without modifying nvram — the change does not survive a reboot.
    Works on both AP and WET/bridge-mode interfaces.
    """
    value = "on" if enable else "off"
    cmd = f"wl -i {ifname} radio {value}"
    _LOGGER.debug(
        "SSH set_wifi_radio: unit=%d ifname=%s enable=%s  cmd=%r",
        unit, ifname, enable, cmd,
    )
    for attempt in range(1, _WL_RADIO_RETRIES + 2):
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT + _CMD_TIMEOUT):
                async with asyncssh.connect(
                    host, port=port,
                    username=username, password=password,
                    **_CONNECT_OPTS,
                ) as conn:
                    result = await conn.run(cmd, check=False)
                    stdout = (result.stdout or "").strip()
                    stderr = (result.stderr or "").strip()
                    _LOGGER.debug(
                        "SSH set_wifi_radio: unit=%d ifname=%s  exit=%d"
                        "  stdout=%r  stderr=%r",
                        unit, ifname, result.exit_status, stdout, stderr,
                    )
                    if result.exit_status == _WL_NOT_IN_RANGE_EXIT:
                        # Driver not ready yet — retry after delay.
                        if attempt <= _WL_RADIO_RETRIES:
                            _LOGGER.debug(
                                "wl radio 'Not In Range' (attempt %d/%d) on %s — "
                                "retrying in %.0fs",
                                attempt, _WL_RADIO_RETRIES + 1,
                                ifname, _WL_RADIO_RETRY_DELAY,
                            )
                            await asyncio.sleep(_WL_RADIO_RETRY_DELAY)
                            continue
                        raise SshError(
                            f"wl -i {ifname} radio {value}: driver not ready after "
                            f"{_WL_RADIO_RETRIES + 1} attempts ('Not In Range'). "
                            "The interface may still be initialising after a recent "
                            "wireless restart — wait a few seconds and try again."
                        )
                    # Any other exit code (including 0) is accepted.
                    return
        except asyncssh.Error as err:
            raise SshError(f"SSH error running wl radio: {err}") from err
        except (OSError, asyncio.TimeoutError) as err:
            raise SshError(f"SSH connection failed: {err}") from err
