"""
The MQTT side of wb-mqtt-dmx: the devices of the service in the broker, by the Wiren Board convention.

The broker is reached through wb-mqtt-python-lib. The library is asynchronous, so this module runs in
the asyncio loop of the main thread while the frames go out from their own threads (sender.py). The
service knows nothing about MQTT: this module publishes what it shows and passes it the writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import Any

from .service import (
    DRIVER,
    SERVICE_DEVICE,
    SERVICE_TITLE,
    STATUS_CONFIG_ERROR,
    STATUS_ENUM,
    STATUS_TITLE,
    DmxService,
)

# The library is a git submodule; setup.py puts it inside this package, so in the source tree the import
# does not resolve.
from .wb_mqtt_lib import (  # pylint: disable=import-error,no-name-in-module
    DEFAULT_BROKER_URL,
    BrokerDisconnectedError,
    Client,
    Control,
    ControlErrorFlags,
    ControlType,
    Device,
    MqttLibError,
)

LOG = logging.getLogger("wb-mqtt-dmx")

# How often the controls catch up with what the service shows. A console draws 40 frames a second, and
# publishing every one of them would flood the broker.
TICK_S = 0.25
CONNECT_TIMEOUT_S = 30.0
CONFIG_ERROR_TIMEOUT_S = 5.0
CONFIG_ERROR_TITLE = {"en": "Configuration error", "ru": "Ошибка в настройках"}
# MQTT is handled in the main thread, and under a stream of writes it keeps the interpreter busy. A
# frame thread that wakes up then waits for the interpreter lock: 5 ms by default, a fifth of a frame.
SWITCH_INTERVAL_S = 0.0005
EXIT_SUCCESS = 0
EXIT_FAILURE = 1


class MqttDevices:
    """
    The devices of the service in the broker.
    """

    def __init__(self, service: DmxService) -> None:
        self._service = service
        self._devices: dict[str, Device] = {}
        self._controls: dict[tuple[str, str], Control] = {}
        self._shown: dict[str, dict[str, Any]] = {}  # what the broker holds: device id -> control id -> value
        self._failed: set[str] = set()  # the devices marked with an error in the broker
        self._sync_lock = asyncio.Lock()
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """
        Do not wait for the next tick: what the service shows has changed.
        """
        self._wake.set()

    async def create(self, client: Client) -> None:
        """
        Publish every device of the service with its controls and current values.
        """
        # What a run that was killed has left behind, a device that is not in the settings any more included
        await client.remove_driver_devices()
        shown = self._service.shown_values()
        for spec in self._service.device_specs():
            device = await client.create_device(spec.id, title=spec.title)
            device.on_control_command(self._on_command)
            self._devices[spec.id] = device
            for control in spec.controls:
                self._controls[spec.id, control.id] = await device.create_control(
                    control.id,
                    ControlType(control.type),
                    title=control.title,
                    value=shown[spec.id].get(control.id),
                    maximum=control.maximum,
                    minimum=None if control.maximum is None else 0,
                    readonly=control.readonly,
                    units=control.units,
                    enum=control.enum,
                )
        self._shown = shown
        await self.sync()

    async def _on_command(self, control: Control, value: Any) -> None:
        device_id = control.device.name
        if not self._service.handle_write(device_id, control.name, str(value)):
            return
        # A write is always answered with the value the control shows, even when it has not changed:
        # while a console owns the port, that is how a slider in the web UI goes back to the console's value.
        async with self._sync_lock:
            self._shown.get(device_id, {}).pop(control.name, None)
        await self.sync()

    async def sync(self) -> None:
        """
        Bring the values and the device errors in the broker to what the service shows now.
        """
        async with self._sync_lock:
            shown = self._service.shown_values()
            for device_id, values in shown.items():
                was = self._shown.get(device_id, {})
                if values == was:
                    continue
                for control_id, value in values.items():
                    if control_id not in was or was[control_id] != value:
                        await self._controls[device_id, control_id].set_value(value)
            self._shown = shown
            failed = self._service.failed_devices()
            for device_id in failed ^ self._failed:
                error = ControlErrorFlags.WRITE if device_id in failed else ControlErrorFlags.NONE
                await self._devices[device_id].set_error(error)
            self._failed = failed

    async def follow(self, stop: asyncio.Event) -> None:
        """
        Keep the broker up to date until stop is set. The channel values are saved from here too.
        """
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_S)
            self._wake.clear()
            self._service.save_state_when_settled()
            await self.sync()


async def serve(service: DmxService, client: Client, stop: asyncio.Event) -> None:
    """
    Run the service with its devices in the broker until stop is set, then remove the devices.

    Args:
        service: The service, not started yet.
        client: A connected client; it is left connected.
        stop: Set it to end the run.
    """
    devices = MqttDevices(service)
    loop = asyncio.get_running_loop()
    try:
        await devices.create(client)
        service.on_change = lambda: loop.call_soon_threadsafe(devices.wake)
        service.start()
        await devices.follow(stop)
    finally:
        service.stop()
        service.on_change = lambda: None
        # Stopped devices must not linger in the web UI
        with contextlib.suppress(BrokerDisconnectedError):
            await client.remove_driver_devices()


async def _run(service: DmxService, broker_url: str) -> int:
    client = Client(DRIVER, broker_url)
    try:
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_S)
        except TimeoutError:
            LOG.error("MQTT broker unavailable")  # systemd starts the service again
            return EXIT_FAILURE
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)
        await serve(service, client, stop)
        return EXIT_SUCCESS
    finally:
        await client.close()


def run_service(service: DmxService, broker_url: str = DEFAULT_BROKER_URL) -> int:
    """
    Run the service until SIGINT or SIGTERM.

    Returns:
        The exit code of the process.
    """
    sys.setswitchinterval(SWITCH_INTERVAL_S)
    return asyncio.run(_run(service, broker_url))


async def _report_config_error(text: str, broker_url: str) -> None:
    client = Client(DRIVER, broker_url)
    try:
        await asyncio.wait_for(client.connect(), CONFIG_ERROR_TIMEOUT_S)
        await client.remove_driver_devices()
        device = await client.create_device(SERVICE_DEVICE, title=SERVICE_TITLE)
        await device.set_error(ControlErrorFlags.READ)
        await device.create_control(
            "status",
            ControlType.VALUE,
            title=STATUS_TITLE,
            value=STATUS_CONFIG_ERROR,
            readonly=True,
            enum=STATUS_ENUM,
        )
        # Creating a control waits for the broker, so by the end of this call everything above has
        # reached it and the client can be closed.
        await device.create_control(
            "config_error", ControlType.TEXT, title=CONFIG_ERROR_TITLE, value=text, readonly=True
        )
    except (TimeoutError, MqttLibError, OSError) as e:
        LOG.warning("cannot report the config error over MQTT: %s", e)
    finally:
        await client.close()


def report_config_error(text: str, broker_url: str = DEFAULT_BROKER_URL) -> None:
    """
    Leave the error on the service device so a bad config saved in the web UI is visible there.
    """
    asyncio.run(_report_config_error(text, broker_url))
