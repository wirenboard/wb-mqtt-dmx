"""
The core of wb-mqtt-dmx: the enabled ports with their frame threads, and what every device shows.

Nothing here talks to the broker. mqtt.py publishes the devices that device_specs() lists with the
values of shown_values() and passes every write to handle_write(). The devices are:

* `wb-mqtt-dmx` — the service: `status` (enum), per port a state enum, frame rate, late frame count,
  the error text when there is one and the address map, plus a Blackout button;
* `dmx_<id>` — one device per configured device with one range control per template channel;
* `dmx_<id>` for a Count > 1 entry — the group: the same controls, written to every member.

A value written to a control goes straight into the port's frame buffer; the frame thread streams
the buffer at the port's rate. Channel values are saved to STATE_PATH once the light stands still and
restored on start when the port says so.

A port can also take its universe from the network (Art-Net / sACN, see netin.py): while a console
sends, its frames are output, the controls show them and the port's `source` control names the
console; when it stops, the values written over MQTT are back.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .config import Device, Group, Port, address_map
from .controls import Control, ControlSpec, DeviceSpec, build_controls, render
from .dmx_port import DmxPort, DmxPortError
from .netin import Binding, NetReceiver
from .sender import FrameSender, Universe

LOG = logging.getLogger("wb-mqtt-dmx")

DRIVER = "wb-mqtt-dmx"
SERVICE_DEVICE = "wb-mqtt-dmx"
SERVICE_TITLE = {"en": "DMX output", "ru": "Выход DMX"}
STATUS_ENUM = {
    "0": {"en": "OK", "ru": "Работает"},
    "1": {"en": "Error", "ru": "Ошибка"},
    "2": {"en": "No port enabled", "ru": "Нет включённых портов"},
    "3": {"en": "Configuration error", "ru": "Ошибка в настройках"},
}
STATUS_TITLE = {"en": "Status", "ru": "Состояние"}
STATUS_OK, STATUS_ERROR, STATUS_NO_PORTS, STATUS_CONFIG_ERROR = 0, 1, 2, 3
PORT_STATE_ENUM = {
    "0": {"en": "Disabled", "ru": "Выключен"},
    "1": {"en": "Starting", "ru": "Запускается"},
    "2": {"en": "Running", "ru": "Работает"},
    "3": {"en": "Error", "ru": "Ошибка"},
}
PORT_DISABLED, PORT_STARTING, PORT_RUNNING, PORT_ERROR = 0, 1, 2, 3
WB_MQTT_SERIAL_CONF = Path("/etc/wb-mqtt-serial.conf")
STATE_PATH = Path("/var/lib/wb-mqtt-dmx/state.json")
# The values of a port that restores them on start go to the root filesystem (eMMC). They are written
# only once the light has stood still for this long, and only when they differ from what is in the file.
# A running scene therefore writes nothing: whoever draws it will draw it again after a restart.
STATE_SETTLE_S = 10.0
NET_SOURCE_MQTT = "MQTT"
BLACKOUT_FRAMES = 3  # zero frames sent before closing a port with on_stop = blackout

Target = Union[Device, Group]


def serial_port_conflict(path: str, conf_path: Path = WB_MQTT_SERIAL_CONF) -> Optional[str]:
    """
    Why the port cannot be used: it is still enabled in wb-mqtt-serial. None when it is free.

    The kernel lets two root processes open one UART, so the serial driver's config is the guard.
    """
    try:
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in conf.get("ports", []) if isinstance(conf, dict) else []:
        if isinstance(entry, dict) and str(entry.get("path", "")) == path and entry.get("enabled", True):
            return f"{path} is enabled in wb-mqtt-serial: disable it in the serial device driver settings"
    return None


class CheckedPort(DmxPort):
    """
    A DmxPort that refuses to open while wb-mqtt-serial still owns the port.
    """

    def __init__(self, *args: Any, serial_conf: Path = WB_MQTT_SERIAL_CONF, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._serial_conf = serial_conf

    def open(self) -> None:
        reason = serial_port_conflict(self.path, self._serial_conf)
        if reason:
            raise DmxPortError(reason)
        super().open()


def checked_port(port: Port) -> DmxPort:
    """
    The serial port of a configured port, with the wb-mqtt-serial guard.
    """
    return CheckedPort(port.path, break_us=port.break_us, mab_us=port.mab_us, slot_gap_us=port.slot_gap_us)


@dataclass
class PortRuntime:
    """
    Everything that lives per enabled port: buffer, sender, its devices' current values.
    """

    port: Port
    sender: FrameSender
    values: dict[str, dict[str, Any]] = field(default_factory=dict)  # device_id -> control id -> MQTT value
    controls: dict[str, list[Control]] = field(default_factory=dict)  # device_id -> controls
    net_source: str = ""  # who owns the universe over the network, "" = nobody

    @property
    def universe(self) -> Universe:
        return self.sender.universe


# The service owns everything that runs: the ports with their senders, the network receiver and the
# events and lock that tie their threads together.
class DmxService:  # pylint: disable=too-many-instance-attributes
    """
    Keeps the frames of the enabled ports going and knows what every device shows.
    """

    def __init__(
        self,
        ports: list[Port],
        state_path: Path = STATE_PATH,
        make_port: Callable[[Port], DmxPort] = checked_port,
    ) -> None:
        self._ports = ports
        self._state_path = state_path
        self._runtimes: dict[str, PortRuntime] = {}  # port path -> runtime
        self._by_device: dict[str, tuple[PortRuntime, Target]] = {}
        self._lock = threading.RLock()
        self._state_dirty = threading.Event()
        self._last_change = 0.0  # when a value last really changed, by self.clock
        self._saved_text = ""  # what the state file holds
        self.clock: Callable[[], float] = time.monotonic  # tests replace it
        # Called when what the devices show has changed without a write: a port started or failed, a
        # console took a port or let it go. It comes from the frame and network threads.
        self.on_change: Callable[[], None] = lambda: None
        self._started = False
        for port in (p for p in ports if p.enabled):
            universe = Universe(port.slots)
            sender = FrameSender(universe, make_port(port), port.refresh_hz)
            sender.on_state = self._on_sender_state
            rt = PortRuntime(port, sender)
            self._runtimes[port.path] = rt
            for target in [*port.groups, *port.devices]:
                self._by_device[target.device_id] = (rt, target)
                rt.controls[target.device_id] = build_controls(target.template)
                rt.values[target.device_id] = {c.id: c.default for c in rt.controls[target.device_id]}
        # These texts never change while the service runs, and shown_values() is read on every write
        self._address_texts = {
            device_id: self._address_text(target) for device_id, (_rt, target) in self._by_device.items()
        }
        self._port_maps = {port.path: address_map(port) or "-" for port in ports}
        self._restore_state()
        bindings = [
            Binding(rt.port.path, rt.port.artnet_universe, rt.port.sacn_universe)
            for rt in self._runtimes.values()
            if rt.port.artnet_universe is not None or rt.port.sacn_universe is not None
        ]
        self.net = NetReceiver(bindings, self._on_net_frame, self._on_net_source) if bindings else None

    def frame(self, path: str) -> bytes:
        """
        The frame that is being sent to a port right now.

        Args:
            path: Path of an enabled port.
        """
        return self._runtimes[path].universe.snapshot()

    # ---- state on disk -----------------------------------------------------------------------

    def _restore_state(self) -> None:
        try:
            self._saved_text = self._state_path.read_text(encoding="utf-8")
            saved = json.loads(self._saved_text)
        except (OSError, ValueError):
            self._saved_text, saved = "{}", {}
        for rt in self._runtimes.values():
            if rt.port.on_start != "restore":
                continue
            channels = saved.get(rt.port.path, {}) if isinstance(saved, dict) else {}
            if not isinstance(channels, dict):
                continue
            data = bytearray(rt.universe.slots)
            for key, value in channels.items():
                try:
                    ch, v = int(key), int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= ch <= len(data) and 0 <= v <= 255:
                    data[ch - 1] = v
            rt.universe.write(1, bytes(data))
            for target in [*rt.port.groups, *rt.port.devices]:
                rt.values[target.device_id] = self._values_from_universe(rt, target)

    def save_state(self) -> None:
        """
        Write the values of the ports that restore them on start, unless the file already holds them.
        """
        state = {}
        for rt in self._runtimes.values():
            if rt.port.on_start != "restore":
                continue  # nobody will read them back
            snapshot = bytearray(rt.universe.snapshot())
            if rt.net_source:
                # A console is drawing: keep the MQTT values, not the console's frame
                for device in rt.port.devices:
                    data = render(
                        rt.controls[device.device_id], rt.values[device.device_id], device.template.footprint
                    )
                    snapshot[device.address - 1 : device.address - 1 + len(data)] = data
            state[rt.port.path] = {str(i + 1): v for i, v in enumerate(snapshot) if v}
        text = json.dumps(state)
        if text == self._saved_text:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._state_path)
            self._saved_text = text
        except OSError as e:
            LOG.warning("cannot save the state to %s: %s", self._state_path, e)

    def save_state_when_settled(self) -> bool:
        """
        Save the values if they have changed and then stood still for STATE_SETTLE_S.

        Returns:
            True when the values were due to be saved.
        """
        if not self._state_dirty.is_set() or self.clock() - self._last_change < STATE_SETTLE_S:
            return False
        self._state_dirty.clear()
        self.save_state()
        return True

    def _values_changed(self) -> None:
        self._last_change = self.clock()
        self._state_dirty.set()

    # ---- values <-> universe -----------------------------------------------------------------

    def _values_from_universe(self, rt: PortRuntime, target: Target) -> dict[str, Any]:
        """
        Device values read back from the buffer (restore, network source); a group shows its first member.
        """
        dev = target.members[0] if isinstance(target, Group) else target
        read = {off: rt.universe.get(dev.address + off) for off in range(target.template.footprint)}
        return {control.id: control.value_from(read) for control in rt.controls[target.device_id]}

    def _render(self, rt: PortRuntime, device: Device) -> None:
        data = render(rt.controls[device.device_id], rt.values[device.device_id], device.template.footprint)
        rt.universe.write(device.address, data)

    # ---- network sources (Art-Net / sACN) ----------------------------------------------------

    def _on_net_frame(self, path: str, slots: bytes) -> None:
        rt = self._runtimes.get(path)
        if rt is not None:
            rt.universe.write(1, slots)

    def _on_net_source(self, path: str, source: str) -> None:
        """
        The source in charge of a port changed; "" means released: back to the MQTT values.
        """
        with self._lock:
            rt = self._runtimes.get(path)
            if rt is None:
                return
            rt.net_source = source
            LOG.info(
                "%s: %s",
                path,
                f"network source {source}" if source else "network source gone, MQTT values back",
            )
            if not source:
                for device in rt.port.devices:
                    self._render(rt, device)
        self.on_change()

    # ---- what the devices show ---------------------------------------------------------------

    def _port_state(self, port: Port) -> tuple[int, str]:
        """
        (state enum value, error text) of a port.
        """
        rt = self._runtimes.get(port.path)
        if rt is None:
            return PORT_DISABLED, ""
        if rt.sender.error:
            return PORT_ERROR, rt.sender.error
        return (PORT_RUNNING if rt.sender.running else PORT_STARTING), ""

    @staticmethod
    def _port_controls(port: Port) -> list[ControlSpec]:
        """
        The service device's block of one port.
        """
        k, name = port.key, port.name

        def titled(en: str, ru: str) -> dict[str, str]:
            return {"en": f"{name}: {en}", "ru": f"{name}: {ru}"}

        return [
            ControlSpec(f"{k}_state", "value", titled("state", "состояние"), enum=PORT_STATE_ENUM),
            ControlSpec(f"{k}_fps", "value", titled("frame rate", "кадров в секунду"), units="fps"),
            ControlSpec(f"{k}_late", "value", titled("late frames", "опоздавших кадров")),
            ControlSpec(f"{k}_error", "text", titled("error", "ошибка")),
            ControlSpec(f"{k}_map", "text", titled("DMX addresses", "адреса DMX")),
            ControlSpec(f"{k}_source", "text", titled("source", "источник")),
        ]

    def _port_values(self, port: Port) -> dict[str, Any]:
        state, error = self._port_state(port)
        rt = self._runtimes.get(port.path)
        fps = rt.sender.measured_fps if rt is not None and rt.sender.running else 0.0
        source = ""
        if self.net is not None:
            source = self.net.error or (
                self.net.source_text(port.path) if rt is not None and rt.net_source else ""
            )
        return {
            f"{port.key}_state": state,
            f"{port.key}_fps": round(fps, 1),
            f"{port.key}_late": rt.sender.late_frames if rt is not None else 0,
            f"{port.key}_error": error or None,  # a text control has either a text or no value
            f"{port.key}_map": self._port_maps[port.path],
            f"{port.key}_source": source or NET_SOURCE_MQTT,
        }

    def _status(self) -> int:
        enabled = list(self._runtimes.values())
        if any(rt.sender.error for rt in enabled):
            return STATUS_ERROR
        return STATUS_OK if enabled else STATUS_NO_PORTS

    @staticmethod
    def _address_text(target: Target) -> str:
        span = (
            f"{target.address}–{target.last_address}"
            if target.last_address > target.address
            else f"{target.address}"
        )
        if isinstance(target, Group):
            return f"{len(target.members)} × {target.template.footprint} · DMX {span}"
        return f"DMX {span}"

    def device_specs(self) -> list[DeviceSpec]:
        """
        Every device the service shows, the service's own first.
        """
        controls = [ControlSpec("status", "value", STATUS_TITLE, enum=STATUS_ENUM)]
        for port in self._ports:
            controls += self._port_controls(port)
        controls.append(
            ControlSpec("blackout", "pushbutton", {"en": "Blackout", "ru": "Погасить всё"}, readonly=False)
        )
        specs = [DeviceSpec(SERVICE_DEVICE, SERVICE_TITLE, controls)]
        for device_id, (rt, target) in self._by_device.items():
            tails = {"en": " (all)", "ru": " (все)"} if isinstance(target, Group) else {"en": "", "ru": ""}
            # The template title is the control's title, so the web UI shows it in its own language.
            address = ControlSpec("dmx", "text", target.template.title)
            specs.append(
                DeviceSpec(
                    device_id,
                    {lang: target.name + tail for lang, tail in tails.items()},
                    [c.spec() for c in rt.controls[device_id]] + [address],
                )
            )
        return specs

    def shown_values(self) -> dict[str, dict[str, Any]]:
        """
        What every control shows right now: device id -> control id -> value; None is a control without
        a value. While a console owns a port, its devices show the console's frame.
        """
        with self._lock:
            service: dict[str, Any] = {"status": self._status()}
            for port in self._ports:
                service.update(self._port_values(port))
            shown = {SERVICE_DEVICE: service}
            for device_id, (rt, target) in self._by_device.items():
                values = self._values_from_universe(rt, target) if rt.net_source else rt.values[device_id]
                shown[device_id] = {**values, "dmx": self._address_texts[device_id]}
            return shown

    def failed_devices(self) -> set[str]:
        """
        The devices to mark with an error: those of a port that does not work, and the service itself
        when it is not all right.
        """
        with self._lock:
            failed = {device_id for device_id, (rt, _target) in self._by_device.items() if rt.sender.error}
            if self._status() != STATUS_OK:
                failed.add(SERVICE_DEVICE)
            return failed

    # ---- writes ------------------------------------------------------------------------------

    def handle_write(self, device_id: str, control_id: str, payload: str) -> bool:
        """
        Apply a value written to a control. True when accepted.
        """
        with self._lock:
            if device_id == SERVICE_DEVICE:
                if control_id == "blackout":
                    self.blackout()
                    return True
                return False
            found = self._by_device.get(device_id)
            if found is None:
                return False
            rt, target = found
            control = next((c for c in rt.controls[device_id] if c.id == control_id), None)
            if control is None:
                return False
            value = control.parse(payload)
            if value is None:
                return False
            members = target.members if isinstance(target, Group) else [target]
            # A rule that keeps sending the same value changes nothing and must not hold the save back
            changed = any(rt.values[d.device_id][control_id] != value for d in [target, *members])
            rt.values[device_id][control_id] = value
            for member in members:
                rt.values[member.device_id][control_id] = value
                if not rt.net_source:  # a console owns the line: the value waits until it lets go
                    self._render(rt, member)
            if changed:
                self._values_changed()
            return True

    def blackout(self) -> None:
        with self._lock:
            for rt in self._runtimes.values():
                rt.universe.blackout()
                for device_id, values in rt.values.items():
                    for control in rt.controls[device_id]:
                        values[control.id] = control.zero
            self._values_changed()

    def _on_sender_state(self, _sender: FrameSender) -> None:
        self.on_change()

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the frame threads and the network receiver.
        """
        enabled = list(self._runtimes.values())
        LOG.info(
            "starting: %d port(s) enabled of %d, %d device(s)",
            len(enabled),
            len(self._ports),
            len(self._by_device),
        )
        self._started = True
        for i, rt in enumerate(enabled):
            LOG.info("%s: %s", rt.port.path, address_map(rt.port) or "no devices")
            rt.sender.start()
            if i + 1 < len(enabled):
                time.sleep(rt.sender.period / len(enabled))  # stagger the grids, see docs/hardware_results.md
        if self.net is not None:
            self.net.start()

    def stop(self) -> None:
        """
        Save the values and stop what start() has started; a port set to go dark sends zero frames first.
        """
        if not self._started:
            return
        self._started = False
        if self.net is not None:
            self.net.stop()
        self.save_state()  # before a blackout, so a restore brings the light back
        for rt in self._runtimes.values():
            if rt.port.on_stop == "blackout" and rt.sender.running:
                rt.universe.blackout()
                time.sleep(rt.sender.period * BLACKOUT_FRAMES)
            rt.sender.stop()
        LOG.info("stopped")
