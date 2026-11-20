"""
The service against a real broker: every test starts its own mosquitto on a free port.

A fake of the MQTT library would only repeat what the code under test assumes about it. Without a
mosquitto binary (a developer's Windows machine) these tests are skipped; the package build has it.
"""

import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional

import pytest

from wb.mqtt_dmx.mqtt import report_config_error, serve
from wb.mqtt_dmx.service import DRIVER, SERVICE_DEVICE, DmxService
from wb.mqtt_dmx.wb_mqtt_lib import (  # pylint: disable=import-error,no-name-in-module
    Client,
    ControlErrorFlags,
    ControlType,
)

from .fakes import PORT_PATH, make_service

MOSQUITTO = shutil.which("mosquitto") or shutil.which("mosquitto", path="/usr/sbin:/usr/local/sbin")
pytestmark = pytest.mark.skipif(MOSQUITTO is None, reason="needs the mosquitto broker")

WAIT_S = 5.0
SPAN_AND_LOGO: list[dict[str, Any]] = [
    {"name": "Пролёт", "layout": "rgbw", "count": 2},
    {"name": "Логотип", "layout": "d"},
]
Scenario = Callable[[Client], Awaitable[None]]


@pytest.fixture(name="broker_url")
def broker_url_fixture(tmp_path: Path) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n", encoding="utf-8")
    assert MOSQUITTO is not None
    with subprocess.Popen(
        [MOSQUITTO, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as broker:
        try:
            deadline = time.monotonic() + WAIT_S
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, "mosquitto did not start"
                    time.sleep(0.05)
            yield f"tcp://127.0.0.1:{port}"
        finally:
            broker.terminate()


def run_on_broker(service: DmxService, broker_url: str, scenario: Optional[Scenario] = None) -> list[str]:
    """
    Serve the service on the broker while the scenario looks at it as another MQTT client.

    Returns:
        The devices left in the broker after the service has stopped.
    """

    async def main() -> list[str]:
        async with Client(DRIVER, broker_url) as client, Client("wb-mqtt-dmx-test", broker_url) as observer:
            stop = asyncio.Event()
            serving = asyncio.create_task(serve(service, client, stop))
            try:
                await observer.get_device(SERVICE_DEVICE, timeout=WAIT_S)
                if scenario is not None:
                    await asyncio.wait_for(scenario(observer), 4 * WAIT_S)
            finally:
                stop.set()
                await serving
            return await observer.get_device_names()

    return asyncio.run(main())


def test_devices_appear_in_the_broker_and_go_away_at_stop(tmp_path: Path, broker_url: str) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    async def scenario(observer: Client) -> None:
        state = await observer.get_control(f"{SERVICE_DEVICE}/rs485_2_state", timeout=WAIT_S)
        await state.wait_value(2, WAIT_S)  # the port is running
        logo = await observer.get_device_snapshot("dmx_logotip")
        assert logo is not None
        assert (logo.meta.driver, logo.meta.title.ru) == (DRIVER, "Логотип")
        assert list(logo.controls) == ["dimmer", "dmx"]
        dimmer = logo.controls["dimmer"]
        assert (dimmer.meta.control_type, dimmer.meta.minimum, dimmer.meta.maximum) == (
            ControlType.RANGE,
            0,
            255,
        )
        assert (dimmer.meta.readonly, dimmer.value) == (False, 0)
        assert (logo.controls["dmx"].meta.readonly, logo.controls["dmx"].value) == (True, "DMX 9")
        assert set(await observer.get_device_names()) >= {
            SERVICE_DEVICE,
            "dmx_prolet",
            "dmx_prolet1",
            "dmx_prolet2",
            "dmx_logotip",
        }
        status = await observer.get_device_snapshot(SERVICE_DEVICE)
        assert status is not None
        assert (status.controls["status"].value, status.error) == (0, ControlErrorFlags.NONE)
        assert status.controls["rs485_2_error"].value is None
        assert status.controls["blackout"].meta.control_type == ControlType.PUSHBUTTON

    assert run_on_broker(service, broker_url, scenario) == []


def test_write_reaches_the_line_and_comes_back_as_the_value(tmp_path: Path, broker_url: str) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    async def scenario(observer: Client) -> None:
        dimmer = await observer.get_control("dmx_logotip/dimmer", timeout=WAIT_S)
        await asyncio.gather(dimmer.wait_value(200, WAIT_S), dimmer.set_value(200))
        assert service.frame(PORT_PATH)[8] == 200

        group = await observer.get_control("dmx_prolet/white", timeout=WAIT_S)
        member = await observer.get_control("dmx_prolet2/white", timeout=WAIT_S)
        await asyncio.gather(member.wait_value(17, WAIT_S), group.set_value(17))
        assert (service.frame(PORT_PATH)[3], service.frame(PORT_PATH)[7]) == (17, 17)

        await asyncio.gather(dimmer.wait_value(255, WAIT_S), dimmer.set_value(999))  # clamped

    run_on_broker(service, broker_url, scenario)


def test_blackout_button_zeroes_everything(tmp_path: Path, broker_url: str) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    async def scenario(observer: Client) -> None:
        dimmer = await observer.get_control("dmx_logotip/dimmer", timeout=WAIT_S)
        await asyncio.gather(dimmer.wait_value(200, WAIT_S), dimmer.set_value(200))
        button = await observer.get_control(f"{SERVICE_DEVICE}/blackout", timeout=WAIT_S)
        await asyncio.gather(dimmer.wait_value(0, WAIT_S), button.set_value(True))
        assert service.frame(PORT_PATH) == bytes(512)

    run_on_broker(service, broker_url, scenario)


def test_what_the_service_shows_reaches_the_broker_without_a_write(tmp_path: Path, broker_url: str) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    async def scenario(observer: Client) -> None:
        dimmer = await observer.get_control("dmx_logotip/dimmer", timeout=WAIT_S)
        service.handle_write("dmx_logotip", "dimmer", "42")  # as a console on the network changes it
        await dimmer.wait_value(42, WAIT_S)

    run_on_broker(service, broker_url, scenario)


def test_devices_of_a_port_that_does_not_open_are_marked(tmp_path: Path, broker_url: str) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path, fail_open=True)

    async def scenario(observer: Client) -> None:
        status = await observer.get_control(f"{SERVICE_DEVICE}/status", timeout=WAIT_S)
        await status.wait_value(1, WAIT_S)
        error = await observer.get_control(f"{SERVICE_DEVICE}/rs485_2_error", timeout=WAIT_S)
        await error.wait_value(lambda text: text is not None and "No such file" in text, WAIT_S)
        logo = await observer.get_device("dmx_logotip", timeout=WAIT_S)
        deadline = time.monotonic() + WAIT_S
        while logo.error != ControlErrorFlags.WRITE and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert logo.error == ControlErrorFlags.WRITE
        assert status.device.error == ControlErrorFlags.WRITE

    run_on_broker(service, broker_url, scenario)


def test_devices_left_by_a_killed_run_are_removed(tmp_path: Path, broker_url: str) -> None:
    async def leave_a_device() -> None:
        async with Client(DRIVER, broker_url) as killed:
            await killed.create_device("dmx_gone", title="Not in the settings any more")

    asyncio.run(leave_a_device())
    service = make_service(SPAN_AND_LOGO, tmp_path)

    async def scenario(observer: Client) -> None:
        await observer.get_device("dmx_logotip", timeout=WAIT_S)
        assert "dmx_gone" not in await observer.get_device_names()

    run_on_broker(service, broker_url, scenario)


def test_config_error_is_left_on_the_service_device(broker_url: str) -> None:
    report_config_error("ports: at least one port is required", broker_url)

    async def look() -> None:
        async with Client("wb-mqtt-dmx-test", broker_url) as observer:
            status = await observer.get_device_snapshot(SERVICE_DEVICE)
            assert status is not None
            assert status.error == ControlErrorFlags.READ
            assert status.controls["status"].value == 3
            assert status.controls["config_error"].value == "ports: at least one port is required"

    asyncio.run(look())
