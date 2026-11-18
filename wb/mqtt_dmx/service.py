"""
The wb-mqtt-dmx daemon: MQTT devices on the controller, DMX frames on the enabled ports.

Devices (WB conventions, all under /devices/):

* `wb-mqtt-dmx` — the service: `status` (enum), per port a state enum, frame rate, late frame count,
  the error text when there is one and the address map, plus a Blackout button;
* `dmx_<id>` — one device per configured device with one range control per template channel;
* `dmx_<id>` for a Count > 1 entry — the group: the same controls, written to every member.

Values are numbers and enums with translated titles, so the web UI shows them in its own language.
A value written to a control goes straight into the port's frame buffer; the frame thread streams
the buffer at the port's rate. Channel values are saved to STATE_PATH (at most once a second) and
restored on start when the port says so.

A port can also take its universe from the network (Art-Net / sACN, see netin.py): while a console
sends, its frames are output, the controls follow them a few times a second and the port's `source`
control names the console; when it stops, the MQTT values are back.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Optional, Union

from wb_common.mqtt_client import MQTTClient

from .config import Device, Group, Port, address_map
from .controls import Control, build_controls, render
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
}
STATUS_META = {
    "type": "value",
    "readonly": True,
    "order": 1,
    "title": {"en": "Status", "ru": "Состояние"},
    "enum": STATUS_ENUM,
}
STATUS_OK, STATUS_ERROR, STATUS_NO_PORTS = "0", "1", "2"
PORT_STATE_ENUM = {
    "0": {"en": "Disabled", "ru": "Выключен"},
    "1": {"en": "Starting", "ru": "Запускается"},
    "2": {"en": "Running", "ru": "Работает"},
    "3": {"en": "Error", "ru": "Ошибка"},
}
PORT_DISABLED, PORT_STARTING, PORT_RUNNING, PORT_ERROR = "0", "1", "2", "3"
WB_MQTT_SERIAL_CONF = Path("/etc/wb-mqtt-serial.conf")
STATE_PATH = Path("/var/lib/wb-mqtt-dmx/state.json")
# The values go to the root filesystem (eMMC). A rule or a scene that changes the light all the time
# must not turn into a write every second around the clock, so changes are gathered for this long.
# After a power loss "restore last values" may therefore be up to this much behind.
STATE_SAVE_INTERVAL_S = 10.0
STATS_INTERVAL_S = 5.0
NET_PUBLISH_INTERVAL_S = 0.25  # how often control values follow a network source (40 fps would flood MQTT)
NET_SOURCE_MQTT = "MQTT"
BLACKOUT_FRAMES = 3  # zero frames sent before closing a port with on_stop = blackout
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
CONFIG_ERROR_PUBLISH_TIMEOUT_S = 2.0

ClientFactory = Callable[[str], Any]
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


def _default_client_factory(client_id: str) -> Any:
    return MQTTClient(client_id)


def publish_config_error(text: str, client_factory: ClientFactory = _default_client_factory) -> None:
    """
    Leave the error on the service device so a bad config saved in the web UI is visible there.
    """
    client = client_factory(f"{DRIVER}-config")
    try:
        client.start()
        dev = f"/devices/{SERVICE_DEVICE}"
        client.publish(
            f"{dev}/meta",
            json.dumps({"driver": DRIVER, "title": SERVICE_TITLE}, ensure_ascii=False),
            retain=True,
        )
        client.publish(
            f"{dev}/controls/status/meta", json.dumps(STATUS_META, ensure_ascii=False), retain=True
        )
        client.publish(f"{dev}/controls/status", f"config error: {text}", retain=True)
        info = client.publish(f"{dev}/meta/error", "r", retain=True)
        info.wait_for_publish(timeout=CONFIG_ERROR_PUBLISH_TIMEOUT_S)
        client.stop()
    except (ConnectionError, OSError) as e:
        LOG.warning("cannot report the config error over MQTT: %s", e)


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
    # values published while a network source is in charge
    shown: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def universe(self) -> Universe:
        return self.sender.universe


# The service owns everything that runs: the ports with their senders, the MQTT client, the network
# receiver and the events and lock that tie their threads together.
class DmxService:  # pylint: disable=too-many-instance-attributes
    """
    Publishes the devices of the enabled ports over MQTT and keeps their frames going.
    """

    def __init__(
        self,
        ports: list[Port],
        client_factory: ClientFactory = _default_client_factory,
        state_path: Path = STATE_PATH,
        realtime: bool = True,
    ) -> None:
        self._ports = ports
        self._state_path = state_path
        self._runtimes: dict[str, PortRuntime] = {}  # port path -> runtime
        self._by_device: dict[str, tuple[PortRuntime, Target]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._state_dirty = threading.Event()
        self._error_text = ""
        for port in (p for p in ports if p.enabled):
            universe = Universe(port.slots)
            dmx = CheckedPort(
                port.path, break_us=port.break_us, mab_us=port.mab_us, slot_gap_us=port.slot_gap_us
            )
            sender = FrameSender(universe, dmx, port.refresh_hz, realtime=realtime)
            sender.on_state = self._on_sender_state
            rt = PortRuntime(port, sender)
            self._runtimes[port.path] = rt
            for target in [*port.groups, *port.devices]:
                self._by_device[target.device_id] = (rt, target)
                rt.controls[target.device_id] = build_controls(target.template)
                rt.values[target.device_id] = {c.id: c.default for c in rt.controls[target.device_id]}
        self._restore_state()
        bindings = [
            Binding(rt.port.path, rt.port.artnet_universe, rt.port.sacn_universe)
            for rt in self._runtimes.values()
            if rt.port.artnet_universe is not None or rt.port.sacn_universe is not None
        ]
        self.net = NetReceiver(bindings, self._on_net_frame, self._on_net_source) if bindings else None
        self._client = client_factory(DRIVER)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.will_set(f"/devices/{SERVICE_DEVICE}/meta/error", "p", retain=True)

    def frame(self, path: str) -> bytes:
        """
        The frame that is being sent to a port right now.

        Args:
            path: Path of an enabled port.
        """
        return self._runtimes[path].universe.snapshot()

    def install_signal_handlers(self) -> None:
        """
        Stop the service on SIGINT and SIGTERM. Call from the main thread before run().
        """
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    # ---- state on disk -----------------------------------------------------------------------

    def _restore_state(self) -> None:
        try:
            saved = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
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
        state = {}
        for rt in self._runtimes.values():
            snapshot = bytearray(rt.universe.snapshot())
            if rt.net_source:
                # A console is drawing: keep the MQTT values, not the console's frame
                for device in rt.port.devices:
                    data = render(
                        rt.controls[device.device_id], rt.values[device.device_id], device.template.footprint
                    )
                    snapshot[device.address - 1 : device.address - 1 + len(data)] = data
            state[rt.port.path] = {str(i + 1): v for i, v in enumerate(snapshot) if v}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as e:
            LOG.warning("cannot save the state to %s: %s", self._state_path, e)

    def _state_saver_loop(self) -> None:
        last_stats = time.monotonic()
        while not self._stop.is_set():
            if self._state_dirty.wait(timeout=1.0):
                self._state_dirty.clear()
                if self._stop.wait(STATE_SAVE_INTERVAL_S):  # gather the changes; run() saves on stop
                    return
                self.save_state()
            # On its own clock: a rule writing every few seconds must not starve the stats
            if time.monotonic() - last_stats >= STATS_INTERVAL_S:
                last_stats = time.monotonic()
                with self._lock:
                    self._publish_port_stats()

    # ---- values <-> universe -----------------------------------------------------------------

    def _values_from_universe(self, rt: PortRuntime, target: Target) -> dict[str, Any]:
        """
        Device values read back from the buffer (restore, network source); a group shows its first
        member.
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
                for device_id, values in rt.values.items():
                    for cid, value in values.items():
                        self._pub(f"/devices/{device_id}/controls/{cid}", str(value))
                rt.shown = {}
            self._publish_port_stats()

    def publish_net_values(self) -> None:
        """
        Let the controls follow a network source, a few times a second and only what changed.
        """
        with self._lock:
            for rt in self._runtimes.values():
                if not rt.net_source:
                    continue
                for target in [*rt.port.groups, *rt.port.devices]:
                    new = self._values_from_universe(rt, target)
                    old = rt.shown.get(target.device_id, rt.values[target.device_id])
                    for cid, value in new.items():
                        if old.get(cid) != value:
                            self._pub(f"/devices/{target.device_id}/controls/{cid}", str(value))
                    rt.shown[target.device_id] = new

    def _net_publish_loop(self) -> None:
        while not self._stop.wait(NET_PUBLISH_INTERVAL_S):
            self.publish_net_values()

    # ---- MQTT publishing ---------------------------------------------------------------------

    def _pub(self, topic: str, payload: Any, retain: bool = True) -> None:
        self._client.publish(
            topic,
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
            retain=retain,
        )

    def _port_state(self, port: Port) -> tuple[str, str]:
        """
        (state enum value, error text) of a port.
        """
        rt = self._runtimes.get(port.path)
        if rt is None:
            return PORT_DISABLED, ""
        if rt.sender.error:
            return PORT_ERROR, rt.sender.error
        return (PORT_RUNNING if rt.sender.running else PORT_STARTING), ""

    def _port_controls(self, port: Port) -> dict[str, dict[str, Any]]:
        """
        Control id -> meta of the service device's per-port block.
        """
        k, name = port.key, port.name
        return {
            f"{k}_state": {
                "type": "value",
                "readonly": True,
                "enum": PORT_STATE_ENUM,
                "title": {"en": f"{name}: state", "ru": f"{name}: состояние"},
            },
            f"{k}_fps": {
                "type": "value",
                "readonly": True,
                "units": "fps",
                "title": {"en": f"{name}: frame rate", "ru": f"{name}: кадров в секунду"},
            },
            f"{k}_late": {
                "type": "value",
                "readonly": True,
                "title": {"en": f"{name}: late frames", "ru": f"{name}: опоздавших кадров"},
            },
            f"{k}_error": {
                "type": "text",
                "readonly": True,
                "title": {"en": f"{name}: error", "ru": f"{name}: ошибка"},
            },
            f"{k}_map": {
                "type": "text",
                "readonly": True,
                "title": {"en": f"{name}: DMX addresses", "ru": f"{name}: адреса DMX"},
            },
            f"{k}_source": {
                "type": "text",
                "readonly": True,
                "title": {"en": f"{name}: source", "ru": f"{name}: источник"},
            },
        }

    def _port_values(self, port: Port) -> dict[str, str]:
        state, error = self._port_state(port)
        rt = self._runtimes.get(port.path)
        fps = rt.sender.measured_fps if rt is not None and rt.sender.running else 0.0
        late = rt.sender.late_frames if rt is not None else 0
        source = ""
        if self.net is not None:
            source = self.net.error or (
                self.net.source_text(port.path) if rt is not None and rt.net_source else ""
            )
        return {
            f"{port.key}_state": state,
            f"{port.key}_fps": f"{fps:.1f}",
            f"{port.key}_late": str(late),
            f"{port.key}_error": error,
            f"{port.key}_map": address_map(port) or "-",
            f"{port.key}_source": source or NET_SOURCE_MQTT,
        }

    def _publish_service_device(self) -> None:
        dev = f"/devices/{SERVICE_DEVICE}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": SERVICE_TITLE})
        self._pub(f"{dev}/controls/status/meta", STATUS_META)
        order = 2
        for port in self._ports:
            for cid, meta in self._port_controls(port).items():
                self._pub(f"{dev}/controls/{cid}/meta", {**meta, "order": order})
                order += 1
            for cid, value in self._port_values(port).items():
                self._pub(f"{dev}/controls/{cid}", value)
        self._pub(
            f"{dev}/controls/blackout/meta",
            {"type": "pushbutton", "order": order, "title": {"en": "Blackout", "ru": "Погасить всё"}},
        )
        self._publish_status()

    def _service_controls(self) -> list[str]:
        out = ["status"]
        for port in self._ports:
            out += list(self._port_controls(port))
        return out + ["blackout"]

    def _publish_status(self) -> None:
        enabled = [p for p in self._ports if p.path in self._runtimes]
        failed = [p for p in enabled if self._runtimes[p.path].sender.error]
        if failed:
            status, err = STATUS_ERROR, "w"
        elif not enabled:
            status, err = STATUS_NO_PORTS, "w"
        else:
            status, err = STATUS_OK, ""
        self._error_text = err
        self._pub(f"/devices/{SERVICE_DEVICE}/controls/status", status)
        self._pub(f"/devices/{SERVICE_DEVICE}/meta/error", err)
        for device_id, (rt, _target) in self._by_device.items():
            self._pub(f"/devices/{device_id}/meta/error", "w" if rt.sender.error else "")

    def _publish_port_stats(self) -> None:
        for port in self._ports:
            for cid, value in self._port_values(port).items():
                self._pub(f"/devices/{SERVICE_DEVICE}/controls/{cid}", value)

    def _on_sender_state(self, _sender: FrameSender) -> None:
        with self._lock:
            self._publish_port_stats()
            self._publish_status()

    def _publish_target(self, rt: PortRuntime, target: Target) -> None:
        dev = f"/devices/{target.device_id}"
        span = (
            f"{target.address}–{target.last_address}"
            if target.last_address > target.address
            else f"{target.address}"
        )
        if isinstance(target, Group):
            title = {"en": f"{target.name} (all)", "ru": f"{target.name} (все)"}
            info = f"{len(target.members)} × {target.template.footprint} · DMX {span}"
        else:
            title = {"en": target.name, "ru": target.name}
            info = f"DMX {span}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": title})
        self._pub(f"{dev}/meta/error", "w" if rt.sender.error else "")
        controls = rt.controls[target.device_id]
        for n, c in enumerate(controls, start=1):
            self._pub(f"{dev}/controls/{c.id}/meta", c.meta(n))
            self._pub(f"{dev}/controls/{c.id}", str(rt.values[target.device_id][c.id]))
        n = len(controls) + 1
        # The template title is the control's title, so the web UI shows it in its own language.
        self._pub(
            f"{dev}/controls/dmx/meta",
            {"type": "text", "readonly": True, "order": n, "title": target.template.title},
        )
        self._pub(f"{dev}/controls/dmx", info)

    def _unpublish_all(self) -> None:
        """
        Clear retained topics so stopped devices do not linger in the web UI.
        """
        for device_id, (rt, _target) in self._by_device.items():
            dev = f"/devices/{device_id}"
            for cid in [c.id for c in rt.controls[device_id]] + ["dmx"]:
                self._pub(f"{dev}/controls/{cid}/meta", "")
                self._pub(f"{dev}/controls/{cid}", "")
            self._pub(f"{dev}/meta/error", "")
            self._pub(f"{dev}/meta", "")
        dev = f"/devices/{SERVICE_DEVICE}"
        for cid in self._service_controls():
            self._pub(f"{dev}/controls/{cid}/meta", "")
            self._pub(f"{dev}/controls/{cid}", "")
        self._pub(f"{dev}/meta/error", "")
        self._pub(f"{dev}/meta", "")

    # ---- MQTT callbacks ----------------------------------------------------------------------

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc != 0:
            LOG.error("MQTT connect failed, rc=%s", rc)
            return
        LOG.info("MQTT connected")
        with self._lock:
            self._publish_service_device()
            for rt, target in self._by_device.values():
                self._publish_target(rt, target)
        # A wildcard spans a whole level, so every device is subscribed and the handler keeps our ids.
        self._client.subscribe("/devices/+/controls/+/on")

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        parts = msg.topic.split("/")  # ['', 'devices', <dev>, 'controls', <ctrl>, 'on']
        if len(parts) != 6:
            return
        self.handle_write(parts[2], parts[4], msg.payload.decode("utf-8", "replace"))

    def handle_write(self, device_id: str, control_id: str, payload: str) -> bool:
        """
        Apply a value written to /devices/<device_id>/controls/<control_id>/on. True when accepted.
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
            rt.values[device_id][control_id] = value
            self._pub(f"/devices/{device_id}/controls/{control_id}", str(value))
            members = target.members if isinstance(target, Group) else [target]
            for member in members:
                if isinstance(target, Group):
                    rt.values[member.device_id][control_id] = value
                    self._pub(f"/devices/{member.device_id}/controls/{control_id}", str(value))
                if not rt.net_source:  # a console owns the line: the value waits until it lets go
                    self._render(rt, member)
            self._state_dirty.set()
            return True

    def blackout(self) -> None:
        with self._lock:
            for rt in self._runtimes.values():
                rt.universe.blackout()
                for device_id, values in rt.values.items():
                    for control in rt.controls[device_id]:
                        values[control.id] = control.zero
                        self._pub(f"/devices/{device_id}/controls/{control.id}", str(control.zero))
            self._state_dirty.set()

    def _signal_handler(self, _signum: int, _frame: Optional[FrameType]) -> None:
        LOG.info("stop requested")
        self._stop.set()

    # ---- lifecycle ---------------------------------------------------------------------------

    def run(self) -> int:
        enabled = list(self._runtimes.values())
        LOG.info(
            "starting: %d port(s) enabled of %d, %d device(s)",
            len(enabled),
            len(self._ports),
            len(self._by_device),
        )
        for rt in enabled:
            LOG.info("%s: %s", rt.port.path, address_map(rt.port) or "no devices")
        try:
            self._client.start()  # synchronous: an unreachable broker makes systemd retry
        except (ConnectionError, OSError) as e:
            LOG.error("MQTT broker unavailable: %s", e)
            return EXIT_FAILURE
        for i, rt in enumerate(enabled):
            rt.sender.start()
            if i + 1 < len(enabled):
                time.sleep(rt.sender.period / len(enabled))  # stagger the grids, see docs/hardware_results.md
        saver = threading.Thread(target=self._state_saver_loop, name="dmx-state", daemon=True)
        saver.start()
        if self.net is not None:
            self.net.start()
            threading.Thread(target=self._net_publish_loop, name="dmx-net-publish", daemon=True).start()
        self._stop.wait()
        if self.net is not None:
            self.net.stop()
        self.save_state()  # before a blackout, so a restore brings the light back
        for rt in enabled:
            if rt.port.on_stop == "blackout" and rt.sender.running:
                rt.universe.blackout()
                time.sleep(rt.sender.period * BLACKOUT_FRAMES)
            rt.sender.stop()
        self._unpublish_all()
        self._client.stop()
        LOG.info("stopped")
        return EXIT_SUCCESS
