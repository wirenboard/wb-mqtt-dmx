"""Turn /etc/wb-mqtt-dmx.conf into ports and devices laid out on DMX addresses.

What the form cannot check is checked here, with messages meant for the service status control:
repeated device names (ids are derived from names, so a name must be unique across the config),
repeated pinned ids, unknown decoder types, address overlaps and devices past the port's slots.
Disabled ports are kept (their devices are not published) so the status can list them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ids import id_from_name, unique_id
from .templates import Template

DEVICE_PREFIX = "dmx_"
AUTO_ADDRESS = 0
UNIVERSE_SIZE = 512
DEFAULT_NAME = "Device"


class ConfigError(ValueError):
    """The configuration cannot be used; the message names the offending port or device."""


@dataclass
class Device:
    id: str                          # MQTT id without the prefix
    name: str
    template: Template
    address: int                     # 1-based first DMX channel
    group: str | None = None         # id of the group device for a count > 1 entry

    @property
    def device_id(self) -> str:
        return f"{DEVICE_PREFIX}{self.id}"

    @property
    def last_address(self) -> int:
        return self.address + self.template.footprint - 1


@dataclass
class Group:
    """One count > 1 entry: a device with the same controls that drives every member."""

    id: str
    name: str
    template: Template
    members: list[Device]

    @property
    def device_id(self) -> str:
        return f"{DEVICE_PREFIX}{self.id}"

    @property
    def address(self) -> int:
        return self.members[0].address

    @property
    def last_address(self) -> int:
        return self.members[-1].last_address


@dataclass
class Port:
    path: str
    name: str
    enabled: bool
    refresh_hz: int
    on_start: str                    # restore | zero
    on_stop: str                     # hold | blackout
    slots: int
    break_us: int
    mab_us: int
    de_via_rts: bool
    devices: list[Device] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)

    @property
    def key(self) -> str:
        """rs485_2 for /dev/ttyRS485-2, mod1 for /dev/ttyMOD1: the port's control id prefix."""
        return id_from_name(self.path.rsplit("/", 1)[-1].removeprefix("tty"), "port")


def _int(entry: dict[str, Any], key: str, default: int, low: int, high: int, where: str) -> int:
    value = entry.get(key, default)
    try:
        value = int(default if value is None else value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: {key} must be a number") from e
    if not low <= value <= high:
        raise ConfigError(f"{where}: {key} must be {low}..{high}, not {value}")
    return value


def _port(entry: dict[str, Any], n: int) -> Port:
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        raise ConfigError(f"port {n}: settings are missing")
    path = str(settings.get("path") or "")
    if not path:
        raise ConfigError(f"port {n}: path is required")
    where = f"port {path}"
    adv = settings.get("advanced") or {}
    if not isinstance(adv, dict):
        raise ConfigError(f"{where}: advanced must be an object")
    on_start = str(settings.get("on_start") or "restore")
    on_stop = str(settings.get("on_stop") or "hold")
    if on_start not in ("restore", "zero"):
        raise ConfigError(f"{where}: on_start must be restore or zero")
    if on_stop not in ("hold", "blackout"):
        raise ConfigError(f"{where}: on_stop must be hold or blackout")
    de = str(adv.get("de_control") or "rts")
    if de not in ("rts", "none"):
        raise ConfigError(f"{where}: de_control must be rts or none")
    return Port(
        path=path, name=str(settings.get("name") or path), enabled=bool(settings.get("enabled", False)),
        refresh_hz=_int(settings, "refresh_hz", 40, 1, 44, where), on_start=on_start, on_stop=on_stop,
        slots=_int(adv, "slots", UNIVERSE_SIZE, 24, UNIVERSE_SIZE, where),
        break_us=_int(adv, "break_us", 176, 88, 1000, where), mab_us=_int(adv, "mab_us", 20, 8, 1000, where),
        de_via_rts=de == "rts",
    )


def load_config(config: dict[str, Any], templates: dict[str, Template]) -> list[Port]:
    """Ports with their devices placed; raises ConfigError with a message for the status control."""
    entries = config.get("ports") if isinstance(config, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigError("ports: at least one port is required")
    ports: list[Port] = []
    taken_ids: set[str] = set()
    seen_names: dict[str, str] = {}      # lowercase name -> port path, for the duplicate message
    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"port {n}: must be an object")
        port = _port(entry, n)
        if any(p.path == port.path for p in ports):
            raise ConfigError(f"port {port.path} is listed twice")
        next_address = 1
        for k, dev in enumerate(entry.get("devices") or [], start=1):
            if not isinstance(dev, dict):
                raise ConfigError(f"port {port.path}: device {k} must be an object")
            name = str(dev.get("name") or "").strip()
            if not name:
                raise ConfigError(f"port {port.path}: device {k} has no name")
            where = f"device '{name}' on {port.path}"
            key = name.casefold()
            if key in seen_names:
                raise ConfigError(f"device name '{name}' is used twice ({seen_names[key]} and {port.path}); "
                                  f"names must be unique, use Count for identical devices")
            seen_names[key] = port.path
            type_key = str(dev.get("type") or "")
            template = templates.get(type_key)
            if template is None:
                raise ConfigError(f"{where}: unknown type '{type_key}'")
            pinned = str(dev.get("id") or "").strip()
            base = pinned or id_from_name(name)
            if base in taken_ids:
                if pinned:
                    raise ConfigError(f"{where}: id '{pinned}' is already used")
                base = unique_id(base, set(taken_ids))   # a derived id that collides gets a suffix
            count = _int(dev, "count", 1, 1, UNIVERSE_SIZE, where)
            address = _int(dev, "address", AUTO_ADDRESS, 0, UNIVERSE_SIZE, where)
            if address == AUTO_ADDRESS:
                address = next_address
            if count == 1:
                taken_ids.add(base)
                members = [Device(base, name, template, address)]
            else:
                members = []
                for i in range(1, count + 1):
                    mid = f"{base}_{i}" if base[-1:].isdigit() else f"{base}{i}"
                    if mid in taken_ids:
                        raise ConfigError(f"{where}: member id '{mid}' is already used")
                    taken_ids.add(mid)
                    members.append(Device(mid, f"{name} {i}", template, address + (i - 1) * template.footprint, base))
                taken_ids.add(base)
                port.groups.append(Group(base, name, template, members))
            if members[-1].last_address > port.slots:
                raise ConfigError(f"{where}: {count} × {template.footprint} channels from address {address} end at "
                                  f"{members[-1].last_address}, past the {port.slots} channels of the port")
            port.devices.extend(members)
            next_address = members[-1].last_address + 1
        by_address = sorted(port.devices, key=lambda d: d.address)
        for a, b in zip(by_address, by_address[1:]):
            if b.address <= a.last_address:
                raise ConfigError(f"devices '{a.name}' ({a.address}..{a.last_address}) and '{b.name}' "
                                  f"({b.address}..{b.last_address}) overlap on {port.path}")
        ports.append(port)
    return ports


def address_map(port: Port) -> str:
    """'Фасад: 1-48 (12 × 4); Логотип: 49' for the status control."""
    parts = []
    grouped = {g.id for g in port.groups}
    for g in port.groups:
        parts.append((g.address, f"{g.name}: {g.address}-{g.last_address} ({len(g.members)} × {g.template.footprint})"))
    for d in port.devices:
        if d.group not in grouped:
            span = f"{d.address}" if d.template.footprint == 1 else f"{d.address}-{d.last_address}"
            parts.append((d.address, f"{d.name}: {span}"))
    return "; ".join(text for _, text in sorted(parts))
