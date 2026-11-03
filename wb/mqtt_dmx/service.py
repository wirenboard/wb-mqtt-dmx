"""The wb-mqtt-dmx daemon: MQTT devices on the controller, DMX frames on the enabled ports.

Devices (WB conventions, all under /devices/):

* `wb-mqtt-dmx` — the service: `status`, one text control per port (fps, frames, late, or the
  error), the address map per port, a Blackout button;
* `dmx_rs485_2` (named after the port) — raw channels ch1…chN when `raw_channels` > 0;
* `dmx_<id>` — one device per configured device with one range control per template channel;
* `dmx_<id>` for a Count > 1 entry — the group: the same controls, written to every member.

A value written to a control goes straight into the port's frame buffer; the frame thread streams
the buffer at the port's rate. Channel values are saved to STATE_PATH (at most once a second) and
restored on start when the port says so.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Optional, Union

from .config import Device, Group, Port, address_map
from .dmx_port import DmxPort, DmxPortError
from .sender import FrameSender, Universe

LOG = logging.getLogger("wb-mqtt-dmx")

DRIVER = "wb-mqtt-dmx"
SERVICE_DEVICE = "wb-mqtt-dmx"
SERVICE_TITLE = {"en": "DMX output", "ru": "Выход DMX"}
STATUS_META = {"type": "text", "readonly": True, "order": 1, "title": {"en": "Status", "ru": "Состояние"}}
WB_MQTT_SERIAL_CONF = Path("/etc/wb-mqtt-serial.conf")
STATE_PATH = Path("/var/lib/wb-mqtt-dmx/state.json")
STATE_SAVE_INTERVAL_S = 1.0
STATS_INTERVAL_S = 5.0
BLACKOUT_FRAMES = 3           # zero frames sent before closing a port with on_stop = blackout
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
CONFIG_ERROR_PUBLISH_TIMEOUT_S = 2.0

ClientFactory = Callable[[str], Any]
Target = Union[Device, Group]


def serial_port_conflict(path: str, conf_path: Path = WB_MQTT_SERIAL_CONF) -> Optional[str]:
    """Why the port cannot be used: it is still enabled in wb-mqtt-serial. None when it is free.

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
    """A DmxPort that refuses to open while wb-mqtt-serial still owns the port."""

    def __init__(self, *args: Any, serial_conf: Path = WB_MQTT_SERIAL_CONF, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._serial_conf = serial_conf

    def open(self) -> None:
        reason = serial_port_conflict(self.path, self._serial_conf)
        if reason:
            raise DmxPortError(reason)
        super().open()


def _default_client_factory(client_id: str) -> Any:
    # wb_common exists only on a controller; tests inject a fake instead.
    from wb_common.mqtt_client import MQTTClient  # pylint: disable=import-outside-toplevel
    return MQTTClient(client_id)


def publish_config_error(text: str, client_factory: ClientFactory = _default_client_factory) -> None:
    """Leave the error on the service device so a bad config saved in the web UI is visible there."""
    client = client_factory(f"{DRIVER}-config")
    try:
        client.start()
        dev = f"/devices/{SERVICE_DEVICE}"
        client.publish(f"{dev}/meta", json.dumps({"driver": DRIVER, "title": SERVICE_TITLE}, ensure_ascii=False),
                       retain=True)
        client.publish(f"{dev}/controls/status/meta", json.dumps(STATUS_META, ensure_ascii=False), retain=True)
        client.publish(f"{dev}/controls/status", f"config error: {text}", retain=True)
        info = client.publish(f"{dev}/meta/error", "r", retain=True)
        info.wait_for_publish(timeout=CONFIG_ERROR_PUBLISH_TIMEOUT_S)
        client.stop()
    except (ConnectionError, OSError) as e:
        LOG.warning("cannot report the config error over MQTT: %s", e)


class PortRuntime:
    """Everything that lives per enabled port: buffer, sender, its devices' current values."""

    def __init__(self, port: Port, sender: FrameSender) -> None:
        self.port = port
        self.universe = sender.universe
        self.sender = sender
        self.values: dict[str, dict[str, int]] = {}     # device_id -> control id -> value


class DmxService:
    def __init__(self, ports: list[Port], client_factory: ClientFactory = _default_client_factory,
                 serial_conf: Path = WB_MQTT_SERIAL_CONF, state_path: Path = STATE_PATH,
                 install_signals: bool = True, realtime: bool = True) -> None:
        self._ports = ports
        self._state_path = state_path
        self._runtimes: dict[str, PortRuntime] = {}            # port path -> runtime
        self._by_device: dict[str, tuple[PortRuntime, Target]] = {}
        self._raw: dict[str, PortRuntime] = {}                 # raw device id -> runtime
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._state_dirty = threading.Event()
        self._error_text = ""
        for i, port in enumerate(p for p in ports if p.enabled):
            universe = Universe(port.slots)
            dmx = CheckedPort(port.path, break_us=port.break_us, mab_us=port.mab_us, de_via_rts=port.de_via_rts,
                              serial_conf=serial_conf)
            sender = FrameSender(universe, dmx, port.refresh_hz, on_state=self._on_sender_state, realtime=realtime)
            rt = PortRuntime(port, sender)
            self._runtimes[port.path] = rt
            if port.raw_channels:
                self._raw[port.raw_device_id] = rt
            for target in [*port.groups, *port.devices]:
                self._by_device[target.device_id] = (rt, target)
                rt.values[target.device_id] = {c.id: c.default for c in target.template.channels}
        self._restore_state()
        self._client = client_factory(DRIVER)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.will_set(f"/devices/{SERVICE_DEVICE}/meta/error", "p", retain=True)
        if install_signals:
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
                self._pull_values_from_universe(rt, target)

    def _save_state(self) -> None:
        state = {}
        for rt in self._runtimes.values():
            snapshot = rt.universe.snapshot()
            state[rt.port.path] = {str(i + 1): v for i, v in enumerate(snapshot) if v}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as e:
            LOG.warning("cannot save the state to %s: %s", self._state_path, e)

    def _state_saver_loop(self) -> None:
        while not self._stop.is_set():
            if self._state_dirty.wait(timeout=STATS_INTERVAL_S):
                self._state_dirty.clear()
                time.sleep(STATE_SAVE_INTERVAL_S)       # coalesce a slider drag into one write
                self._save_state()
            else:
                with self._lock:
                    self._publish_port_stats()

    # ---- values <-> universe -----------------------------------------------------------------

    def _pull_values_from_universe(self, rt: PortRuntime, target: Target) -> None:
        """Device values from the buffer (after a restore); a group shows its first member."""
        dev = target.members[0] if isinstance(target, Group) else target
        for c in target.template.channels:
            rt.values[target.device_id][c.id] = rt.universe.get(dev.address + c.offset)

    def _render(self, rt: PortRuntime, device: Device) -> None:
        values = rt.values[device.device_id]
        data = bytearray(device.template.footprint)
        for c in device.template.channels:
            data[c.offset] = values[c.id]
        rt.universe.write(device.address, bytes(data))

    # ---- MQTT publishing ---------------------------------------------------------------------

    def _pub(self, topic: str, payload: Any, retain: bool = True) -> None:
        self._client.publish(topic, payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                             retain=retain)

    @staticmethod
    def _port_control(port: Port) -> str:
        return port.raw_device_id[len("dmx_"):]

    def _port_text(self, port: Port) -> str:
        rt = self._runtimes.get(port.path)
        if rt is None:
            return "disabled"
        s = rt.sender
        if s.error:
            return f"error: {s.error}"
        if not s.running:
            return "starting"
        return f"{port.refresh_hz} Hz, {s.frames} frames, {s.late_frames} late"

    def _publish_service_device(self) -> None:
        dev = f"/devices/{SERVICE_DEVICE}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": SERVICE_TITLE})
        self._pub(f"{dev}/controls/status/meta", STATUS_META)
        order = 2
        for port in self._ports:
            cid = self._port_control(port)
            self._pub(f"{dev}/controls/{cid}/meta", {"type": "text", "readonly": True, "order": order,
                                                     "title": {"en": f"{port.name} ({port.path})",
                                                               "ru": f"{port.name} ({port.path})"}})
            self._pub(f"{dev}/controls/{cid}", self._port_text(port))
            self._pub(f"{dev}/controls/{cid}_map/meta", {"type": "text", "readonly": True, "order": order + 1,
                                                         "title": {"en": f"{port.name}: DMX addresses",
                                                                   "ru": f"{port.name}: адреса DMX"}})
            self._pub(f"{dev}/controls/{cid}_map", address_map(port) or "-")
            order += 2
        self._pub(f"{dev}/controls/blackout/meta", {"type": "pushbutton", "order": order,
                                                    "title": {"en": "Blackout", "ru": "Погасить всё"}})
        self._publish_status()

    def _service_controls(self) -> list[str]:
        out = ["status"]
        for port in self._ports:
            out += [self._port_control(port), self._port_control(port) + "_map"]
        return out + ["blackout"]

    def _publish_status(self) -> None:
        errors = [f"{p.name}: {self._runtimes[p.path].sender.error}" for p in self._ports
                  if p.path in self._runtimes and self._runtimes[p.path].sender.error]
        enabled = [p for p in self._ports if p.path in self._runtimes]
        if errors:
            text, err = "; ".join(errors), "w"
        elif not enabled:
            text, err = "no port enabled: tick Enabled on a port in the settings", "w"
        else:
            text, err = "ok", ""
        self._error_text = err
        self._pub(f"/devices/{SERVICE_DEVICE}/controls/status", text)
        self._pub(f"/devices/{SERVICE_DEVICE}/meta/error", err)
        for device_id, (rt, _target) in self._by_device.items():
            self._pub(f"/devices/{device_id}/meta/error", "w" if rt.sender.error else "")
        for device_id, rt in self._raw.items():
            self._pub(f"/devices/{device_id}/meta/error", "w" if rt.sender.error else "")

    def _publish_port_stats(self) -> None:
        for port in self._ports:
            self._pub(f"/devices/{SERVICE_DEVICE}/controls/{self._port_control(port)}", self._port_text(port))

    def _on_sender_state(self, _sender: FrameSender) -> None:
        with self._lock:
            self._publish_port_stats()
            self._publish_status()

    def _publish_target(self, rt: PortRuntime, target: Target) -> None:
        dev = f"/devices/{target.device_id}"
        if isinstance(target, Group):
            title = {"en": f"{target.name} (all)", "ru": f"{target.name} (все)"}
            info = (f"{target.template.title['en']} · {len(target.members)} × {target.template.footprint} · "
                    f"DMX {target.address}–{target.last_address}")
        else:
            title = {"en": target.name, "ru": target.name}
            info = f"{target.template.title['en']} · DMX {target.address}–{target.last_address}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": title})
        self._pub(f"{dev}/meta/error", "w" if rt.sender.error else "")
        for n, c in enumerate(target.template.channels, start=1):
            self._pub(f"{dev}/controls/{c.id}/meta", {"type": "range", "order": n, "readonly": False, "min": 0,
                                                     "max": 255, "title": c.title})
            self._pub(f"{dev}/controls/{c.id}", str(rt.values[target.device_id][c.id]))
        n = len(target.template.channels) + 1
        self._pub(f"{dev}/controls/dmx/meta", {"type": "text", "readonly": True, "order": n,
                                               "title": {"en": "DMX", "ru": "DMX"}})
        self._pub(f"{dev}/controls/dmx", info)

    def _publish_raw(self, device_id: str, rt: PortRuntime) -> None:
        dev = f"/devices/{device_id}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": {"en": f"{rt.port.name}: channels",
                                                              "ru": f"{rt.port.name}: каналы"}})
        self._pub(f"{dev}/meta/error", "w" if rt.sender.error else "")
        for ch in range(1, rt.port.raw_channels + 1):
            self._pub(f"{dev}/controls/ch{ch}/meta", {"type": "range", "order": ch, "readonly": False, "min": 0,
                                                     "max": 255, "title": {"en": f"Ch {ch}", "ru": f"Канал {ch}"}})
            self._pub(f"{dev}/controls/ch{ch}", str(rt.universe.get(ch)))

    def _unpublish_all(self) -> None:
        """Clear retained topics so stopped devices do not linger in the web UI."""
        for device_id, (_rt, target) in self._by_device.items():
            dev = f"/devices/{device_id}"
            for cid in [c.id for c in target.template.channels] + ["dmx"]:
                self._pub(f"{dev}/controls/{cid}/meta", "")
                self._pub(f"{dev}/controls/{cid}", "")
            self._pub(f"{dev}/meta/error", "")
            self._pub(f"{dev}/meta", "")
        for device_id, rt in self._raw.items():
            dev = f"/devices/{device_id}"
            for ch in range(1, rt.port.raw_channels + 1):
                self._pub(f"{dev}/controls/ch{ch}/meta", "")
                self._pub(f"{dev}/controls/ch{ch}", "")
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
            for device_id, rt in self._raw.items():
                self._publish_raw(device_id, rt)
            for device_id, (rt, target) in self._by_device.items():
                self._publish_target(rt, target)
        # A wildcard spans a whole level, so every device is subscribed and the handler keeps our ids.
        self._client.subscribe("/devices/+/controls/+/on")

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        parts = msg.topic.split("/")       # ['', 'devices', <dev>, 'controls', <ctrl>, 'on']
        if len(parts) != 6:
            return
        self.handle_write(parts[2], parts[4], msg.payload.decode("utf-8", "replace"))

    def handle_write(self, device_id: str, control_id: str, payload: str) -> bool:
        """Apply a value written to /devices/<device_id>/controls/<control_id>/on. True when accepted."""
        with self._lock:
            if device_id == SERVICE_DEVICE:
                if control_id == "blackout":
                    self.blackout()
                    return True
                return False
            try:
                value = max(0, min(255, int(float(payload))))
            except ValueError:
                return False
            rt = self._raw.get(device_id)
            if rt is not None:
                if not control_id.startswith("ch"):
                    return False
                try:
                    ch = int(control_id[2:])
                except ValueError:
                    return False
                if not 1 <= ch <= rt.port.raw_channels:
                    return False
                rt.universe.write(ch, bytes([value]))
                self._pub(f"/devices/{device_id}/controls/{control_id}", str(value))
                self._state_dirty.set()
                return True
            found = self._by_device.get(device_id)
            if found is None:
                return False
            rt, target = found
            if control_id not in rt.values[device_id]:
                return False
            rt.values[device_id][control_id] = value
            self._pub(f"/devices/{device_id}/controls/{control_id}", str(value))
            members = target.members if isinstance(target, Group) else [target]
            for member in members:
                if isinstance(target, Group):
                    rt.values[member.device_id][control_id] = value
                    self._pub(f"/devices/{member.device_id}/controls/{control_id}", str(value))
                self._render(rt, member)
            self._state_dirty.set()
            return True

    def blackout(self) -> None:
        with self._lock:
            for rt in self._runtimes.values():
                rt.universe.blackout()
                for device_id, values in rt.values.items():
                    for cid in values:
                        values[cid] = 0
                        self._pub(f"/devices/{device_id}/controls/{cid}", "0")
                for ch in range(1, rt.port.raw_channels + 1):
                    self._pub(f"/devices/{rt.port.raw_device_id}/controls/ch{ch}", "0")
            self._state_dirty.set()

    def _signal_handler(self, _signum: int, _frame: Optional[FrameType]) -> None:
        LOG.info("stop requested")
        self._stop.set()

    # ---- lifecycle ---------------------------------------------------------------------------

    def run(self) -> int:
        enabled = list(self._runtimes.values())
        LOG.info("starting: %d port(s) enabled of %d, %d device(s)", len(enabled), len(self._ports),
                 len(self._by_device))
        for rt in enabled:
            LOG.info("%s: %s", rt.port.path, address_map(rt.port) or "no devices")
        try:
            self._client.start()         # synchronous: an unreachable broker makes systemd retry
        except (ConnectionError, OSError) as e:
            LOG.error("MQTT broker unavailable: %s", e)
            return EXIT_FAILURE
        for i, rt in enumerate(enabled):
            rt.sender.start()
            if i + 1 < len(enabled):
                time.sleep(rt.sender.period / len(enabled))   # stagger the grids, see docs/hardware_results.md
        saver = threading.Thread(target=self._state_saver_loop, name="dmx-state", daemon=True)
        saver.start()
        self._stop.wait()
        self._save_state()               # before a blackout, so a restore brings the light back
        for rt in enabled:
            if rt.port.on_stop == "blackout" and rt.sender.running:
                rt.universe.blackout()
                time.sleep(rt.sender.period * BLACKOUT_FRAMES)
            rt.sender.stop()
        self._unpublish_all()
        self._client.stop()
        LOG.info("stopped")
        return EXIT_SUCCESS
