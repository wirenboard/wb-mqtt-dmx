"""
Turn /etc/wb-mqtt-dmx.conf into ports and devices laid out on DMX addresses.

What the form cannot check is checked here, with messages meant for the service status control:
repeated device names (ids are derived from names, so a name must be unique across the config),
repeated pinned ids, unknown decoder types, address overlaps and devices past the port's slots.
Disabled ports are kept (their devices are not published) so the status can list them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dmx_port import MAX_SLOT_GAP_US
from .ids import id_from_name, unique_id
from .netin import ARTNET_MAX_UNIVERSE, SACN_MAX_UNIVERSE
from .templates import (
    CHANNELS_TYPE,
    LAYOUT_TYPE,
    Template,
    TemplateError,
    channels_template,
    layout_template,
)

DEVICE_PREFIX = "dmx_"
AUTO_ADDRESS = 0
UNIVERSE_SIZE = 512
DEFAULT_NAME = "Device"


class ConfigError(ValueError):
    """
    The configuration cannot be used; the message names the offending port or device.
    """


@dataclass
class Device:
    id: str  # MQTT id without the prefix
    name: str
    template: Template
    address: int  # 1-based first DMX channel
    group: str | None = None  # id of the group device for a count > 1 entry

    @property
    def device_id(self) -> str:
        return f"{DEVICE_PREFIX}{self.id}"

    @property
    def last_address(self) -> int:
        return self.address + self.template.footprint - 1


@dataclass
class Group:
    """
    One count > 1 entry: a device with the same controls that drives every member.
    """

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


# A plain record of one port's settings from the configuration file, one field per setting.
@dataclass
class Port:  # pylint: disable=too-many-instance-attributes
    path: str
    name: str
    enabled: bool
    refresh_hz: int
    on_start: str  # restore | zero
    on_stop: str  # hold | blackout
    slots: int
    break_us: int
    mab_us: int
    artnet_universe: int | None = None  # network input, None = off
    sacn_universe: int | None = None
    slot_gap_us: int = 0  # pause between slots for slow receivers, 0 = back to back
    devices: list[Device] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)

    @property
    def key(self) -> str:
        """
        rs485_2 for /dev/ttyRS485-2, mod1 for /dev/ttyMOD1: the port's control id prefix.
        """
        return id_from_name(self.path.rsplit("/", 1)[-1].removeprefix("tty"), "port")


def _int(entry: dict[str, Any], key: str, default: int, limits: tuple[int, int], where: str) -> int:
    value = entry.get(key, default)
    try:
        value = int(default if value is None else value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: {key} must be a number") from e
    low, high = limits
    if not low <= value <= high:
        raise ConfigError(f"{where}: {key} must be {low}..{high}, not {value}")
    return value


def _section(settings: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    section = settings.get(key) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{where}: {key} must be an object")
    return section


def _choice(settings: dict[str, Any], key: str, choices: tuple[str, str], where: str) -> str:
    value = str(settings.get(key) or choices[0])
    if value not in choices:
        raise ConfigError(f"{where}: {key} must be {choices[0]} or {choices[1]}")
    return value


def _port(entry: dict[str, Any], n: int) -> Port:
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        raise ConfigError(f"port {n}: settings are missing")
    path = str(settings.get("path") or "")
    if not path:
        raise ConfigError(f"port {n}: path is required")
    where = f"port {path}"
    adv = _section(settings, "advanced", where)
    net = _section(settings, "network", where)
    # Default universes follow the port's position: Art-Net 0, 1, …; sACN 1, 2, …
    artnet = (
        _int(net, "artnet_universe", n - 1, (0, ARTNET_MAX_UNIVERSE), where) if net.get("artnet") else None
    )
    sacn = _int(net, "sacn_universe", n, (1, SACN_MAX_UNIVERSE), where) if net.get("sacn") else None
    return Port(
        path=path,
        name=str(settings.get("name") or path),
        enabled=bool(entry.get("enabled", False)),
        refresh_hz=_int(settings, "refresh_hz", 40, (1, 44), where),
        on_start=_choice(settings, "on_start", ("restore", "zero"), where),
        on_stop=_choice(settings, "on_stop", ("hold", "blackout"), where),
        slots=_int(adv, "slots", UNIVERSE_SIZE, (24, UNIVERSE_SIZE), where),
        break_us=_int(adv, "break_us", 176, (88, 1000), where),
        mab_us=_int(adv, "mab_us", 20, (8, 1000), where),
        artnet_universe=artnet,
        sacn_universe=sacn,
        slot_gap_us=_int(adv, "slot_gap_us", 0, (0, MAX_SLOT_GAP_US), where),
    )


@dataclass
class _Taken:
    """
    What is already used across the whole configuration.
    """

    ids: set[str] = field(default_factory=set)
    names: dict[str, str] = field(default_factory=dict)  # lowercase name -> port path, for the message


def _check_network_clash(port: Port, others: list[Port]) -> None:
    for other in others:
        if port.artnet_universe is not None and port.artnet_universe == other.artnet_universe:
            raise ConfigError(
                f"ports {other.path} and {port.path} both receive Art-Net universe {port.artnet_universe}"
            )
        if port.sacn_universe is not None and port.sacn_universe == other.sacn_universe:
            raise ConfigError(
                f"ports {other.path} and {port.path} both receive sACN universe {port.sacn_universe}"
            )


def _device_name(dev: Any, k: int, port: Port, taken: _Taken) -> str:
    if not isinstance(dev, dict):
        raise ConfigError(f"port {port.path}: device {k} must be an object")
    name = str(dev.get("name") or "").strip()
    if not name:
        raise ConfigError(f"port {port.path}: device {k} has no name")
    key = name.casefold()
    if key in taken.names:
        raise ConfigError(
            f"device name '{name}' is used twice ({taken.names[key]} and {port.path}); "
            f"names must be unique, use Count for identical devices"
        )
    taken.names[key] = port.path
    return name


def _device_template(dev: dict[str, Any], where: str) -> Template:
    type_key = str(dev.get("type") or LAYOUT_TYPE)
    if type_key == LAYOUT_TYPE:
        try:
            return layout_template(str(dev.get("layout") or ""), _int(dev, "bits", 8, (8, 16), where))
        except TemplateError as e:
            raise ConfigError(f"{where}: {e}") from e
    if type_key == CHANNELS_TYPE:
        return channels_template(_int(dev, "channels", 1, (1, UNIVERSE_SIZE), where))
    raise ConfigError(f"{where}: unknown type '{type_key}'")


def _device_base_id(dev: dict[str, Any], name: str, where: str, taken: _Taken) -> str:
    pinned = str(dev.get("id") or "").strip()
    base = pinned or id_from_name(name)
    if base in taken.ids:
        if pinned:
            raise ConfigError(f"{where}: id '{pinned}' is already used")
        base = unique_id(base, set(taken.ids))  # a derived id that collides gets a suffix
    return base


def _add_device(port: Port, dev: dict[str, Any], name: str, next_address: int, taken: _Taken) -> int:
    """
    Place one device entry (one device, or a group of identical ones) on the port.

    Returns:
        The first free address after it.
    """
    where = f"device '{name}' on {port.path}"
    template = _device_template(dev, where)
    base = _device_base_id(dev, name, where, taken)
    count = _int(dev, "count", 1, (1, UNIVERSE_SIZE), where)
    address = _int(dev, "address", AUTO_ADDRESS, (0, UNIVERSE_SIZE), where)
    if address == AUTO_ADDRESS:
        address = next_address
    if count == 1:
        members = [Device(base, name, template, address)]
    else:
        members = []
        for i in range(1, count + 1):
            mid = f"{base}_{i}" if base[-1:].isdigit() else f"{base}{i}"
            if mid in taken.ids:
                raise ConfigError(f"{where}: member id '{mid}' is already used")
            taken.ids.add(mid)
            members.append(Device(mid, f"{name} {i}", template, address + (i - 1) * template.footprint, base))
        port.groups.append(Group(base, name, template, members))
    taken.ids.add(base)
    if members[-1].last_address > port.slots:
        raise ConfigError(
            f"{where}: {count} × {template.footprint} channels from address {address} end at "
            f"{members[-1].last_address}, past the {port.slots} channels of the port"
        )
    port.devices.extend(members)
    return members[-1].last_address + 1


def _check_overlaps(port: Port) -> None:
    by_address = sorted(port.devices, key=lambda d: d.address)
    for a, b in zip(by_address, by_address[1:]):
        if b.address <= a.last_address:
            raise ConfigError(
                f"devices '{a.name}' ({a.address}..{a.last_address}) and '{b.name}' "
                f"({b.address}..{b.last_address}) overlap on {port.path}"
            )


def load_config(config: dict[str, Any]) -> list[Port]:
    """
    Ports with their devices placed on DMX addresses.

    Raises:
        ConfigError: The configuration cannot be used; the message is meant for the status control.
    """
    entries = config.get("ports") if isinstance(config, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigError("ports: at least one port is required")
    ports: list[Port] = []
    taken = _Taken()
    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"port {n}: must be an object")
        port = _port(entry, n)
        if any(p.path == port.path for p in ports):
            raise ConfigError(f"port {port.path} is listed twice")
        _check_network_clash(port, ports)
        next_address = 1
        for k, dev in enumerate(entry.get("devices") or [], start=1):
            name = _device_name(dev, k, port, taken)
            next_address = _add_device(port, dev, name, next_address, taken)
        _check_overlaps(port)
        ports.append(port)
    return ports


def address_map(port: Port) -> str:
    """
    'Фасад: 1-48 (12 × 4); Логотип: 49' for the status control.
    """
    parts = []
    grouped = {g.id for g in port.groups}
    for g in port.groups:
        parts.append(
            (g.address, f"{g.name}: {g.address}-{g.last_address} ({len(g.members)} × {g.template.footprint})")
        )
    for d in port.devices:
        if d.group not in grouped:
            span = f"{d.address}" if d.template.footprint == 1 else f"{d.address}-{d.last_address}"
            parts.append((d.address, f"{d.name}: {span}"))
    return "; ".join(text for _, text in sorted(parts))
