"""Binary sensor platform for FreshTomato integration.

Binary sensors:
  • WAN Connected              – True when WAN IP is present (per WAN in multi-WAN)
  • 2.4 GHz SSID Broadcast     – True when SSID is visible (not hidden)
  • 5 GHz SSID Broadcast       – True when SSID is visible
  • Wireless Client Mode       – True when router has no WAN / acts as AP or repeater
  • Per-port Link               – True when an Ethernet port has an active link

WiFi interface state (active/inactive) is exposed as switches in the switch platform,
where they can also be toggled in AP mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import FreshTomatoCoordinator, RouterData


@dataclass(frozen=True, kw_only=True)
class FreshTomatoBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Any = None


# Non-WAN binary sensors that are always created (single + multi-WAN)
_STATIC_BINARY_SENSORS: tuple[FreshTomatoBinarySensorDescription, ...] = (
    FreshTomatoBinarySensorDescription(
        key="wl0_broadcast",
        name="2.4 GHz SSID Broadcast",
        device_class=None,
        icon="mdi:broadcast",
        value_fn=lambda d: d.nvram.get("wl0_closed", "0") == "0",
    ),
    FreshTomatoBinarySensorDescription(
        key="wl1_broadcast",
        name="5 GHz SSID Broadcast",
        device_class=None,
        icon="mdi:broadcast",
        value_fn=lambda d: d.nvram.get("wl1_closed", "0") == "0",
    ),
    FreshTomatoBinarySensorDescription(
        key="wireless_client_mode",
        name="Wireless Client Mode",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:wifi-arrow-left-right",
        value_fn=lambda d: (
            (not d.wan_ip or d.wan_ip in ("", "0.0.0.0"))
            and len(d.dhcp_leases) == 0
        ),
    ),
    FreshTomatoBinarySensorDescription(
        key="band_steering",
        name="Wireless Band Steering",
        device_class=None,
        icon="mdi:wifi-sync",
        # FreshTomato BSD daemon key is "bsd_enabled"; older builds may use
        # "wl_bsd_enabled" — check both and treat "1" as enabled.
        value_fn=lambda d: (
            d.nvram.get("bsd_enabled", d.nvram.get("wl_bsd_enabled", "0")) == "1"
        ),
    ),
)


def _make_wan_connected_desc(
    wan_idx: int,
    multi_wan: bool,
) -> FreshTomatoBinarySensorDescription:
    """Create a WAN Connected binary sensor description for a given WAN index.

    When multi_wan is False the legacy key/name is used to preserve existing
    entity IDs ("wan_connected" / "WAN Connected").
    """
    key   = f"wan{wan_idx}_connected" if multi_wan else "wan_connected"
    name  = f"WAN{wan_idx} Connected"  if multi_wan else "WAN Connected"
    _idx  = wan_idx

    def _value_fn(data: "RouterData") -> bool:
        for w in data.wan_connections:
            if w.index == _idx:
                return bool(w.ip and w.ip not in ("0.0.0.0", ""))
        # Fallback for single-WAN when wan_connections is empty
        return bool(data.wan_ip and data.wan_ip not in ("0.0.0.0", ""))

    return FreshTomatoBinarySensorDescription(
        key=key,
        name=name,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:wan",
        value_fn=_value_fn,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FreshTomatoCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    nvram = coordinator.data.nvram if coordinator.data else {}
    wan_connections = coordinator.data.wan_connections if coordinator.data else []
    try:
        _mwan_num = int(nvram.get("mwan_num", "1"))
    except ValueError:
        _mwan_num = 1
    multi_wan = _mwan_num > 1 or len(wan_connections) > 1

    # Bridge mode: wan_proto disabled but lan1 has a real IP (WET uplink)
    _bridge_mode   = (
        nvram.get("wan_proto", "") in ("disabled", "")
        and bool(nvram.get("lan1_ipaddr", ""))
        and nvram.get("lan1_ipaddr", "") not in ("", "0.0.0.0")
    )
    _bridge_ifname = nvram.get("lan1_ifname", "br1") or "br1"

    # WAN Connected sensors — one per active WAN
    if multi_wan:
        active_indices = [w.index for w in wan_connections] if wan_connections else [1]
        wan_entities = [
            FreshTomatoBinarySensor(coordinator, entry, _make_wan_connected_desc(idx, True))
            for idx in active_indices
        ]
    elif _bridge_mode:
        # In bridge mode the wan_ipaddr reflects the WET uplink address, not a
        # real WAN port.  "WAN Connected" would always show True even with the
        # WAN ethernet port unplugged, so suppress it entirely.
        wan_entities = []
    else:
        wan_entities = [FreshTomatoBinarySensor(coordinator, entry, _make_wan_connected_desc(1, False))]

    # All other static sensors
    entities: list = wan_entities + [
        FreshTomatoBinarySensor(coordinator, entry, desc)
        for desc in _STATIC_BINARY_SENSORS
    ]

    # Dynamic per-port link sensors — created from etherstates data.
    # Register the listener BEFORE calling async_add_entities so it fires
    # on the first coordinator update even if data was empty at setup time.
    known_ports: set[str] = set()

    def _add_port_entities() -> None:
        if not coordinator.data:
            return
        new: list = []
        for label in coordinator.data.eth_ports:
            if label not in known_ports:
                known_ports.add(label)
                new.append(FreshTomatoPortLinkSensor(coordinator, entry, label))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_port_entities))
    _add_port_entities()  # Try immediately with current data

    async_add_entities(entities)


class FreshTomatoBinarySensor(
    CoordinatorEntity[FreshTomatoCoordinator], BinarySensorEntity
):
    entity_description: FreshTomatoBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FreshTomatoCoordinator,
        entry: ConfigEntry,
        description: FreshTomatoBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"FreshTomato ({self._entry.data['host']})",
            manufacturer="FreshTomato Project",
            model=self.coordinator.data.nvram.get("t_model_name", "Router")
            if self.coordinator.data else "Router",
            sw_version=(self.coordinator.data.nvram.get("t_build_time") or self.coordinator.data.nvram.get("os_version"))
            if self.coordinator.data else None,
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except Exception:  # pylint: disable=broad-except
            return None


def _port_is_connected(state: str) -> bool:
    """Return True if the port state string indicates an active link."""
    return state not in ("DOWN", "disabled", "")


class FreshTomatoPortLinkSensor(
    CoordinatorEntity[FreshTomatoCoordinator], BinarySensorEntity
):
    """Binary sensor: is a specific Ethernet port connected (link up)?

    One entity per physical port. Created dynamically based on what
    etherstates reports, so works across all router models regardless
    of port count (4-port, 5-port, 8-port, etc.).
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: FreshTomatoCoordinator,
        entry: ConfigEntry,
        port_label: str,  # e.g. "WAN", "LAN0", "LAN1" ...
    ) -> None:
        super().__init__(coordinator)
        self._port_label = port_label
        self._entry = entry
        safe_key = port_label.lower().replace(" ", "_")
        self._attr_unique_id = f"{entry.entry_id}_port_{safe_key}_link"
        self._attr_name = f"{port_label} Link"
        self._attr_icon = "mdi:ethernet" if "LAN" in port_label else "mdi:wan"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"FreshTomato ({self._entry.data['host']})",
            manufacturer="FreshTomato Project",
            model=self.coordinator.data.nvram.get("t_model_name", "Router")
            if self.coordinator.data else "Router",
        )

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        state = self.coordinator.data.eth_ports.get(self._port_label)
        if state is None:
            return None
        return _port_is_connected(state)

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        raw = self.coordinator.data.eth_ports.get(self._port_label, "")
        speed, duplex = _decode_port_state(raw)
        return {
            "raw_state": raw,
            "speed_mbps": speed,
            "duplex": duplex,
        }


def _decode_port_state(state: str) -> tuple[int | None, str | None]:
    """Decode a port state string into (speed_mbps, duplex).

    Examples:
        "1000FD" → (1000, "full")
        "100HD"  → (100, "half")
        "DOWN"   → (None, None)
    """
    if not state or state in ("DOWN", "disabled", "ACTIVE"):
        return None, None
    duplex = None
    if state.endswith("FD"):
        duplex = "full"
        speed_str = state[:-2]
    elif state.endswith("HD"):
        duplex = "half"
        speed_str = state[:-2]
    else:
        speed_str = state
    try:
        return int(speed_str), duplex
    except ValueError:
        return None, duplex

