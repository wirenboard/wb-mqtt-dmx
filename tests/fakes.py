"""
Stand-ins for what the service touches outside the process: the serial port and the MQTT broker.
"""

import struct
from pathlib import Path
from typing import Any, Callable, Optional

from wb.mqtt_dmx import dmx_port as dp
from wb.mqtt_dmx.config import load_config
from wb.mqtt_dmx.service import DmxService

PORT_PATH = "/dev/ttyRS485-2"


class FakeOps(dp.PortOps):
    """
    A serial port that records every call and keeps its own clock, so nothing waits.
    """

    def __init__(self, fail_open: bool = False) -> None:
        self.calls: list[tuple] = []
        self.rs485 = struct.pack("I", dp.SER_RS485_ENABLED) + bytes(28)
        self.termios = struct.pack(dp.TERMIOS2_FMT, 0, 0, 0o10017, 0, 0, bytes(19), 9600, 9600)
        self.fail_open = fail_open
        self.now = 0.0

    def open(self, path: str) -> int:
        if self.fail_open:
            raise OSError(2, "No such file or directory")
        self.calls.append(("open", path))
        return 7

    def close(self, fd: int) -> None:
        self.calls.append(("close",))

    def write(self, fd: int, data: bytes) -> int:
        self.calls.append(("write", bytes(data)))
        return len(data)

    def ioctl(self, fd: int, request: int, arg: bytes = b"") -> bytes:
        self.calls.append(("ioctl", request, bytes(arg)))
        if request == dp.TCGETS2:
            return self.termios
        if request == dp.TCSETS2:
            self.termios = bytes(arg)
        elif request == dp.TIOCGRS485:
            return self.rs485
        elif request == dp.TIOCSRS485:
            self.rs485 = bytes(arg)
        elif request == dp.TIOCOUTQ:
            return struct.pack("i", 0)
        elif request == dp.TIOCSERGETLSR:
            return struct.pack("i", dp.TIOCSER_TEMT)
        return b""

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", seconds))
        self.now += seconds

    def spin_us(self, micros: int) -> None:
        self.calls.append(("spin_us", micros))
        self.now += micros / 1e6

    def monotonic(self) -> float:
        return self.now

    def tcdrain(self, fd: int) -> None:
        self.calls.append(("tcdrain",))

    def written(self) -> list[bytes]:
        """
        Everything written to the port, one item per write.
        """
        return [call[1] for call in self.calls if call[0] == "write"]

    def requests(self) -> list[int]:
        """
        The ioctl requests in the order they were made.
        """
        return [call[1] for call in self.calls if call[0] == "ioctl"]


class FakeClient:
    """
    An MQTT client that keeps what was published, by topic.
    """

    def __init__(self, _client_id: str) -> None:
        self.topics: dict[str, str] = {}
        self.on_connect: Callable[..., None] = lambda *_args: None
        self.on_message: Any = None

    def will_set(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def publish(self, topic: str, payload: str, **_kwargs: Any) -> None:
        self.topics[topic] = payload

    def start(self) -> None:
        self.on_connect(self, None, None, 0)

    def stop(self) -> None:
        pass


def port_entry(devices: list[dict[str, Any]], path: str = PORT_PATH, **settings: Any) -> dict[str, Any]:
    """
    One port of a configuration file. Extra keyword arguments override the port settings;
    `enabled` goes to the port itself.
    """
    enabled = settings.pop("enabled", True)
    base = {"path": path, "name": "Фасад", "refresh_hz": 40, "on_start": "restore", "on_stop": "hold"}
    base.update(settings)
    return {"enabled": enabled, "settings": base, "devices": devices}


def make_service(
    devices: list[dict[str, Any]],
    tmp_path: Path,
    network: Optional[dict[str, Any]] = None,
    enabled: bool = True,
) -> tuple[DmxService, dict[str, str]]:
    """
    A service with one port, connected to a fake broker.

    Returns:
        The service and the topics it has published.
    """
    clients: list[FakeClient] = []

    def factory(client_id: str) -> FakeClient:
        clients.append(FakeClient(client_id))
        return clients[-1]

    config = {"ports": [port_entry(devices, enabled=enabled, network=network or {})]}
    service = DmxService(
        load_config(config),
        client_factory=factory,
        serial_conf=tmp_path / "serial.conf",
        state_path=tmp_path / "state.json",
        realtime=False,
    )
    clients[0].start()  # publishes everything, as a real connect would
    return service, clients[0].topics
