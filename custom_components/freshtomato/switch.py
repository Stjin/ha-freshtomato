"""Switch platform for FreshTomato integration — WiFi control via SSH.

All WiFi write operations use SSH (same host/credentials as HTTP).

Entity model
────────────
For each physical wireless band (wl0 = 2.4 GHz, wl1 = 5 GHz):

  ┌──────────────────────────────────────────────────────────────────────┐
  │ N GHz Interface                                                      │
  │   State source  : wlN_radio nvram key ("1" enabled, "0" disabled)  │
  │   SSH command   : nvram set wlN_radio=N; nvram commit;              │
  │                   service restart_wireless &                         │
  │   AP mode       : writable when SSH is available                    │
  │   WET / STA     : read-only (toggle raises HomeAssistantError)      │
  │   Cooldown      : 30 s between successive toggles — prevents rapid  │
  │                   restarts that can destabilise the wireless stack   │
  │   On failure    : optimistic state cleared → reverts to nvram       │
  ├──────────────────────────────────────────────────────────────────────┤
  │ N GHz Radio                                                          │
  │   State source  : wlstats[N].radio from status-data.jsx             │
  │                   (live Broadcom driver RF state, not nvram)        │
  │                   Falls back to nvram on first cycle before         │
  │                   wlstats is populated.                             │
  │   SSH command   : wl -i <ifname> radio <0|1>  (runtime, no nvram)  │
  │   Available     : only when Interface is enabled AND mode is AP     │
  │                   (WET/bridge mode: unavailable — wl radio ignored) │
  │   No cooldown   : wl command is instant and non-disruptive          │
  └──────────────────────────────────────────────────────────────────────┘

Interface cooldown
──────────────────
``service restart_wireless`` takes 3–8 seconds and briefly disrupts the
wireless stack.  Rapid successive restarts can leave the router in an
unstable state.  After each Interface toggle, further Interface toggles
on the same band are blocked for IFACE_COOLDOWN_SECS seconds.  The entity
shows the remaining cooldown time in its attributes and raises a clear
error if toggled during the window.  Radio toggles are unaffected.

Radio state source
──────────────────
The Radio switch reads wlstats[N].radio (live hardware RF state) rather
than the wlN_radio nvram bit.  This means:
  • Turning the Interface on/off does not affect what Radio displays.
  • The wl radio command result is reflected after the next nvram-refresh
    cycle (~30 s at default scan interval).
  • If wlstats is not yet populated (first cycle), falls back to nvram.

Radio in WET mode
─────────────────
``wl -i ethN radio`` is silently ignored in WET/Wireless Ethernet Bridge
mode — the Broadcom driver discards the command.  The Radio switch entity
is therefore marked unavailable for WET-mode interfaces.  The Interface
switch remains the only control for WET-mode interfaces (though it too is
read-only when in WET mode, since the association state is managed by the
upstream AP).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import FreshTomatoCoordinator
from .ssh import SshError, set_wifi_interface, set_wifi_radio

_LOGGER = logging.getLogger(__name__)

# Wireless modes where Interface control is blocked (firmware manages state).
_CLIENT_MODES: frozenset[str] = frozenset({"wet", "sta", "psta", "wet-bridge", "apsta"})

_WIFI_MODE_LABELS: dict[str, str] = {
    "ap":    "Access Point",
    "sta":   "Wireless Client",
    "wet":   "Wireless Ethernet Bridge",
    "wds":   "WDS",
    "psta":  "Media Bridge",
    "apsta": "AP + Client",
}

# Minimum time (seconds) between successive Interface toggles on the same band.
# Prevents rapid restart_wireless calls from destabilising the wireless stack.
IFACE_COOLDOWN_SECS: int = 30

# Optimistic state guard for Interface switch — covers the nvram commit +
# backgrounded wireless service restart window.
_IFACE_GUARD_CYCLES = 2


# ── Helpers ────────────────────────────────────────────────────────────────────

def _device_info(coordinator: FreshTomatoCoordinator, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"FreshTomato ({entry.data['host']})",
        manufacturer="FreshTomato Project",
        model=coordinator.data.nvram.get("t_model_name", "Router")
        if coordinator.data else "Router",
        sw_version=(
            coordinator.data.nvram.get("t_build_time")
            or coordinator.data.nvram.get("os_version")
        ) if coordinator.data else None,
    )


def _nvram(coordinator: FreshTomatoCoordinator, key: str) -> str:
    if not coordinator.data:
        return ""
    return coordinator.data.nvram.get(key, "").strip()


def _wl_ifname(coordinator: FreshTomatoCoordinator, unit: int) -> str:
    return _nvram(coordinator, f"wl{unit}_ifname")


def _wl_mode(coordinator: FreshTomatoCoordinator, unit: int) -> str:
    return _nvram(coordinator, f"wl{unit}_mode") or "ap"


def _wl_iface_enabled(coordinator: FreshTomatoCoordinator, unit: int) -> bool:
    """Return True when the interface nvram bit says enabled."""
    return _nvram(coordinator, f"wl{unit}_radio") == "1"


def _ssh_params(
    entry: ConfigEntry, coordinator: FreshTomatoCoordinator
) -> tuple[str, int, str, str]:
    return (
        entry.data["host"],
        int(entry.data.get("ssh_port", 22)),
        coordinator.ssh_username,
        entry.data.get("password", ""),
    )


def _cooldown_remaining(coordinator: FreshTomatoCoordinator, unit: int) -> float:
    """Return seconds remaining in Interface cooldown, or 0 if none."""
    until = coordinator.iface_cooldown_until.get(unit, 0.0)
    remaining = until - time.monotonic()
    return max(0.0, remaining)


# ── Platform setup ─────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FreshTomatoCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    entities: list[SwitchEntity] = []
    for unit, band_label in ((0, "2.4 GHz"), (1, "5 GHz")):
        entities.append(FreshTomatoIfaceSwitch(coordinator, entry, unit, band_label))
        entities.append(FreshTomatoRadioSwitch(coordinator, entry, unit, band_label))
    async_add_entities(entities)


# ── Interface switch ───────────────────────────────────────────────────────────

class FreshTomatoIfaceSwitch(CoordinatorEntity[FreshTomatoCoordinator], SwitchEntity):
    """Enable / disable a WiFi interface persistently (nvram + service restart).

    State source : wlN_radio nvram key.
    SSH command  : nvram set wlN_radio=<0|1>; nvram commit; service restart_wireless &

    A 30-second cooldown is enforced between successive toggles to prevent
    rapid wireless service restarts from destabilising the router.

    AP mode  : writable (SSH required).
    WET / STA: read-only regardless of SSH state.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi-settings"

    def __init__(
        self,
        coordinator: FreshTomatoCoordinator,
        entry: ConfigEntry,
        unit: int,
        band_label: str,
    ) -> None:
        super().__init__(coordinator)
        self._unit = unit
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_wl{unit}_iface"
        self._attr_name = f"{band_label} Interface"
        self._optimistic_on: bool | None = None
        self._optimistic_cycles: int = 0

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self.coordinator, self._entry)

    @property
    def available(self) -> bool:
        if not super().available or not self.coordinator.data:
            return False
        return bool(_wl_ifname(self.coordinator, self._unit))

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_cycles > 0 and self._optimistic_on is not None:
            return self._optimistic_on
        val = _nvram(self.coordinator, f"wl{self._unit}_radio")
        if not val:
            return None
        return val == "1"

    def _handle_coordinator_update(self) -> None:
        if self._optimistic_cycles > 0:
            self._optimistic_cycles -= 1
            if self._optimistic_cycles == 0:
                self._optimistic_on = None
        super()._handle_coordinator_update()

    def _check_writable(self) -> None:
        """Raise HomeAssistantError if toggle should be blocked."""
        mode = _wl_mode(self.coordinator, self._unit)
        if mode in _CLIENT_MODES:
            label = _WIFI_MODE_LABELS.get(mode, mode)
            raise HomeAssistantError(
                f"Interface wl{self._unit} is in {label} mode — "
                "radio state is managed by the upstream AP association "
                "and cannot be changed here."
            )
        if not self.coordinator.ssh_available:
            raise HomeAssistantError(
                "SSH is not enabled on this router. "
                "Go to Administration → Admin Access and enable SSH "
                "to control WiFi interfaces from Home Assistant."
            )
        remaining = _cooldown_remaining(self.coordinator, self._unit)
        if remaining > 0:
            raise HomeAssistantError(
                f"WiFi interface wl{self._unit} was recently toggled. "
                f"Please wait {remaining:.0f} more seconds before toggling again "
                f"(cooldown: {IFACE_COOLDOWN_SECS} s)."
            )

    async def _do_toggle(self, enable: bool) -> None:
        self._check_writable()
        # Arm the cooldown BEFORE sending the SSH command so that even if
        # the command fails we still protect against a rapid retry storm.
        self.coordinator.iface_cooldown_until[self._unit] = (
            time.monotonic() + IFACE_COOLDOWN_SECS
        )
        host, port, user, pwd = _ssh_params(self._entry, self.coordinator)
        try:
            await set_wifi_interface(host, port, user, pwd, self._unit, enable)
        except SshError as err:
            # SSH failed — clear optimistic state so UI reverts to nvram truth.
            self._optimistic_on = None
            self._optimistic_cycles = 0
            raise HomeAssistantError(str(err)) from err
        self._optimistic_on = enable
        self._optimistic_cycles = _IFACE_GUARD_CYCLES
        _LOGGER.debug("wl%d Interface set to %s via SSH", self._unit, enable)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._do_toggle(True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._do_toggle(False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        nvram = self.coordinator.data.nvram
        mode_raw = _wl_mode(self.coordinator, self._unit)
        remaining = _cooldown_remaining(self.coordinator, self._unit)
        return {
            "mode": _WIFI_MODE_LABELS.get(mode_raw, mode_raw),
            "ifname": _wl_ifname(self.coordinator, self._unit),
            "ssid": nvram.get(f"wl{self._unit}_ssid", ""),
            "read_only": mode_raw in _CLIENT_MODES or not self.coordinator.ssh_available,
            "ssh_available": self.coordinator.ssh_available,
            "cooldown_remaining_sec": int(remaining) if remaining > 0 else None,
        }


# ── Radio switch ───────────────────────────────────────────────────────────────

class FreshTomatoRadioSwitch(CoordinatorEntity[FreshTomatoCoordinator], SwitchEntity):
    """Enable / disable the RF hardware radio (runtime, no nvram commit).

    State source  : wlstats[N].radio from status-data.jsx (live Broadcom RF
                    hardware state, 1=on / 0=off).
                    Falls back to wlN_radio nvram before first wlstats refresh.

    SSH command   : wl -i <ifname> radio on|off
                    Must use the strings "on" / "off" — numeric 0/1 are silently
                    ignored by the Broadcom driver.

    Available     : only when Interface is enabled (wlN_radio nvram = "1").
                    Works on both AP and WET/bridge-mode interfaces.

    Retry         : if the driver returns "Not In Range" (exit 254) — which
                    can happen briefly after a wireless service restart — the
                    command is retried up to 3 times with 5 s delay.

    No cooldown   : wl radio does not restart any service.
    No persistent : change does not survive a reboot — use Interface for that.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi"

    def __init__(
        self,
        coordinator: FreshTomatoCoordinator,
        entry: ConfigEntry,
        unit: int,
        band_label: str,
    ) -> None:
        super().__init__(coordinator)
        self._unit = unit
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_wl{unit}_radio"
        self._attr_name = f"{band_label} Radio"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self.coordinator, self._entry)

    @property
    def available(self) -> bool:
        if not super().available or not self.coordinator.data:
            return False
        if not _wl_ifname(self.coordinator, self._unit):
            return False
        # Interface must be enabled (nvram bit) — radio is meaningless when
        # the interface itself is disabled.
        if not _wl_iface_enabled(self.coordinator, self._unit):
            return False
        return True

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        # Primary: live hardware state from wlstats (populated on nvram refresh cycles).
        hw = self.coordinator.data.wl_hw_radio
        if self._unit in hw:
            return hw[self._unit]
        # Fallback: nvram bit (used on first cycle before wlstats is available).
        val = _nvram(self.coordinator, f"wl{self._unit}_radio")
        if not val:
            return None
        return val == "1"

    def _check_writable(self) -> None:
        if not self.coordinator.ssh_available:
            raise HomeAssistantError(
                "SSH is not enabled on this router. "
                "Go to Administration → Admin Access and enable SSH "
                "to control WiFi radios from Home Assistant."
            )

    async def _do_toggle(self, enable: bool) -> None:
        self._check_writable()
        host, port, user, pwd = _ssh_params(self._entry, self.coordinator)
        ifname = _wl_ifname(self.coordinator, self._unit)
        if not ifname:
            raise HomeAssistantError(
                f"wl{self._unit} interface name is not available yet — try again."
            )
        try:
            await set_wifi_radio(host, port, user, pwd, self._unit, ifname, enable)
        except SshError as err:
            raise HomeAssistantError(str(err)) from err
        # Write the new state directly into the coordinator's hw_radio cache so
        # it survives the next poll cycle.  Without this, data.wl_hw_radio is
        # populated from the stale _wl_hw_radio cache (only refreshed every
        # NVRAM_REFRESH_INTERVAL cycles, ~5 min), causing the switch to revert.
        self.coordinator._wl_hw_radio[self._unit] = enable
        _LOGGER.debug(
            "wl%d Radio (%s) set to %s via SSH; coordinator cache updated",
            self._unit, ifname, enable,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._do_toggle(True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._do_toggle(False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        mode_raw = _wl_mode(self.coordinator, self._unit)
        hw = self.coordinator.data.wl_hw_radio
        return {
            "mode": _WIFI_MODE_LABELS.get(mode_raw, mode_raw),
            "ifname": _wl_ifname(self.coordinator, self._unit),
            "hw_radio_source": "wlstats" if self._unit in hw else "nvram_fallback",
            "read_only": not self.coordinator.ssh_available,
            "ssh_available": self.coordinator.ssh_available,
        }
