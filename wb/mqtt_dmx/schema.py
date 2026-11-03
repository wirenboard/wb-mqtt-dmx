"""The confed form: a JSON Schema (draft-04 + json-editor extensions, as the other WB services use).

The decoder type list comes from the templates, so the schema is generated on install and whenever
templates change (`wb-mqtt-dmx update-schema`) into /var/lib/wb-mqtt-confed/schemas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .templates import Template

CONFIG_PATH = "/etc/wb-mqtt-dmx.conf"
SERVICE = "wb-mqtt-dmx"
SCHEMA_PATH = Path("/var/lib/wb-mqtt-confed/schemas/wb-mqtt-dmx.schema.json")
UNIVERSE_SIZE = 512

TITLE = "DMX512 output"
DESCRIPTION = ("DMX512 fixtures driven from the controller's own RS-485 ports or WBE2 RS-485 modules. Each port is "
               "one DMX line with its devices. A port must be disabled in the serial device driver settings first "
               "(Settings → Configuration files → Serial device driver), the same way as for wb-mqtt-mbgate.")
PORT_DESCRIPTION = "/dev/ttyRS485-1, /dev/ttyRS485-2 or a WBE2 module port such as /dev/ttyMOD1."
REFRESH_DESCRIPTION = "Frames per second on the line. 40 is the maximum for 512 channels; LED decoders are fine with 25–40."
ON_START_DESCRIPTION = "What the line shows after the service starts (reboot, config save)."
ON_STOP_DESCRIPTION = ("What happens when the service stops. Hold: frames just stop and the decoders keep their last "
                       "values until their own timeout. Blackout: all channels are set to 0 first.")
RAW_DESCRIPTION = ("Expose the first N channels of the line as a device dmx_<port> with sliders ch1…chN "
                   "(dmx_rs485_2 for /dev/ttyRS485-2), for devices without a template and for wb-rules. 0: no such device.")
START_SCENE_DESCRIPTION = "Channel values applied at start when 'Scene' is selected above; other channels are 0."
DE_DESCRIPTION = ("RTS: the service holds the RS-485 transmitter enable through RTS (controller ports and WBE2 "
                  "modules). Kernel: leave direction control to the driver or the adapter (USB DMX adapters).")
ADDRESS_DESCRIPTION = "First DMX channel of the device. 0 places it right after the previous device on this port."
COUNT_DESCRIPTION = "Identical devices on consecutive addresses: ids <id>1…<id>N plus a group device <id> for all of them."
ID_DESCRIPTION = "Without the dmx_ prefix: facade → dmx_facade. Latin letters, digits, underscore."

RU = {
    TITLE: "Выход DMX512",
    DESCRIPTION: "DMX512-приборы, управляемые с портов RS-485 контроллера или модулей WBE2 RS-485. Порт — одна "
                 "линия DMX со своими приборами. Порт нужно сначала отключить в настройках драйвера последовательных "
                 "устройств (Настройки → Конфигурационные файлы → Настройка драйвера serial-устройств), как для "
                 "wb-mqtt-mbgate.",
    "Ports": "Порты", "Port": "Порт", "Port settings": "Настройки порта", "Device": "Прибор", "Name": "Название",
    "Serial port": "Порт", "Frame rate, Hz": "Частота кадров, Гц", "On start": "При старте",
    "On stop": "При остановке", "Restore last values": "Восстановить последние значения",
    "All channels 0": "Все каналы в 0", "Scene": "Сцена", "Hold last frame": "Оставить как есть",
    "Blackout": "Погасить всё", "Advanced": "Дополнительно", "Channels per frame": "Каналов в кадре",
    "BREAK, µs": "BREAK, мкс", "MAB, µs": "MAB, мкс", "Transmitter enable": "Включение передатчика",
    "RTS": "RTS", "Kernel": "Ядро", "Raw channels as a device": "Сырые каналы как устройство",
    "Start scene": "Стартовая сцена", "Channel": "Канал", "Value": "Значение",
    "Devices": "Приборы", "Device id": "Идентификатор", "Type": "Тип", "Start address": "Стартовый адрес",
    "Count": "Количество",
    PORT_DESCRIPTION: "/dev/ttyRS485-1, /dev/ttyRS485-2 или порт модуля WBE2, например /dev/ttyMOD1.",
    REFRESH_DESCRIPTION: "Кадров в секунду на линии. 40 — максимум для 512 каналов; светодиодным декодерам "
                         "хватает 25–40.",
    ON_START_DESCRIPTION: "Что на линии после запуска сервиса (перезагрузка, сохранение настроек).",
    ON_STOP_DESCRIPTION: "Что происходит при остановке сервиса. Оставить: кадры просто прекращаются, декодеры держат "
                         "последние значения до своего таймаута. Погасить: сначала все каналы ставятся в 0.",
    RAW_DESCRIPTION: "Опубликовать первые N каналов линии как устройство dmx_<порт> с ползунками ch1…chN "
                     "(dmx_rs485_2 для /dev/ttyRS485-2) — для приборов без шаблона и для wb-rules. 0 — устройства нет.",
    START_SCENE_DESCRIPTION: "Значения каналов при старте, если выше выбрана «Сцена»; остальные каналы — 0.",
    DE_DESCRIPTION: "RTS: сервис сам удерживает включение передатчика RS-485 через RTS (порты контроллера и модули "
                    "WBE2). Ядро: направлением управляет драйвер или адаптер (USB-адаптеры DMX).",
    ADDRESS_DESCRIPTION: "Первый DMX-канал прибора. 0 — сразу после предыдущего прибора на этом порту.",
    COUNT_DESCRIPTION: "Одинаковые приборы на адресах подряд: идентификаторы <id>1…<id>N плюс групповое устройство "
                       "<id> для всех сразу.",
    ID_DESCRIPTION: "Без префикса dmx_: facade → dmx_facade. Латинские буквы, цифры, подчёркивание.",
}


def _int(title: str, minimum: int, maximum: int, default: int, order: int, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "title": title, "minimum": minimum, "maximum": maximum, "default": default,
            "propertyOrder": order, **extra}


def port_schema(templates: dict[str, Template]) -> dict[str, Any]:
    advanced = {
        "type": "object", "title": "Advanced", "propertyOrder": 100, "_format": "grid",
        "options": {"collapsed": True, "disable_edit_json": True, "disable_properties": True},
        "properties": {
            "slots": _int("Channels per frame", 24, UNIVERSE_SIZE, UNIVERSE_SIZE, 1, options={"grid_columns": 4}),
            "break_us": _int("BREAK, µs", 88, 1000, 176, 2, options={"grid_columns": 4}),
            "mab_us": _int("MAB, µs", 8, 1000, 20, 3, options={"grid_columns": 4}),
            "de_control": {"type": "string", "title": "Transmitter enable", "enum": ["rts", "none"],
                           "default": "rts", "propertyOrder": 4, "description": DE_DESCRIPTION,
                           "options": {"enum_titles": ["RTS", "Kernel"], "grid_columns": 6}},
            "raw_channels": _int("Raw channels as a device", 0, UNIVERSE_SIZE, 0, 5, description=RAW_DESCRIPTION,
                                 options={"grid_columns": 6}),
            "start_scene": {
                "type": "array", "title": "Start scene", "propertyOrder": 6, "_format": "table",
                "description": START_SCENE_DESCRIPTION,
                "items": {
                    "type": "object",
                    "properties": {
                        "channel": _int("Channel", 1, UNIVERSE_SIZE, 1, 1),
                        "value": _int("Value", 0, 255, 0, 2),
                    },
                    "required": ["channel", "value"],
                    "options": {"disable_edit_json": True, "disable_properties": True},
                },
                "options": {"disable_array_reorder": True},
            },
        },
    }
    settings = {
        "type": "object", "title": "Port settings", "propertyOrder": 1, "_format": "grid",
        # Collapsed once the port is set up; the header keeps the essentials visible.
        "headerTemplate": "{{self.path}} · {{self.refresh_hz}} Hz · {{self.on_start}} / {{self.on_stop}}",
        "options": {"collapsed": True, "disable_edit_json": True, "disable_properties": True},
        "properties": {
            "path": {"type": "string", "title": "Serial port", "minLength": 1, "default": "/dev/ttyRS485-2",
                     "propertyOrder": 1, "description": PORT_DESCRIPTION, "options": {"grid_columns": 6}},
            "name": {"type": "string", "title": "Name", "default": "DMX 1", "propertyOrder": 2,
                     "options": {"grid_columns": 6}},
            "refresh_hz": _int("Frame rate, Hz", 1, 44, 40, 3, description=REFRESH_DESCRIPTION,
                               options={"grid_columns": 4}),
            "on_start": {"type": "string", "title": "On start", "enum": ["restore", "zero", "scene"],
                         "default": "restore", "propertyOrder": 4, "description": ON_START_DESCRIPTION,
                         "options": {"enum_titles": ["Restore last values", "All channels 0", "Scene"],
                                     "grid_columns": 4}},
            "on_stop": {"type": "string", "title": "On stop", "enum": ["hold", "blackout"], "default": "hold",
                        "propertyOrder": 5, "description": ON_STOP_DESCRIPTION,
                        "options": {"enum_titles": ["Hold last frame", "Blackout"], "grid_columns": 4}},
            "advanced": advanced,
        },
        "required": ["path"],
    }
    return {
        "type": "object",
        "title": "Port",
        "headerTemplate": "{{self.settings.name}} ({{self.settings.path}})",
        "properties": {
            "settings": settings,
            "devices": {
                "type": "array", "title": "Devices", "propertyOrder": 2, "_format": "table",
                "items": device_schema(templates),
                "options": {"disable_array_reorder": True, "disable_collapse": True,
                            "disable_array_delete_last_row": True, "disable_array_delete_all_rows": True},
            },
        },
        "required": ["settings", "devices"],
        "options": {"disable_edit_json": True, "disable_properties": True, "disable_collapse": True},
    }


# Base types in the order a user expects to see them; any other template follows alphabetically.
TYPE_ORDER = ["dimmer", "rgb", "grb", "rgbw", "grbw", "drgb", "drgbw", "cct", "rgbcct"]


def type_order(templates: dict[str, Template]) -> list[str]:
    known = [t for t in TYPE_ORDER if t in templates]
    return known + sorted(t for t in templates if t not in TYPE_ORDER)


def device_schema(templates: dict[str, Template]) -> dict[str, Any]:
    types = type_order(templates)
    titles = [templates[t].title["en"] for t in types]
    return {
        "type": "object",
        "title": "Device",
        "properties": {
            "id": {"type": "string", "title": "Device id", "pattern": "^[a-z0-9_]{1,32}$", "propertyOrder": 1,
                   "description": ID_DESCRIPTION, "options": {"grid_columns": 4}},
            "name": {"type": "string", "title": "Name", "propertyOrder": 2, "options": {"grid_columns": 4}},
            "type": {"type": "string", "title": "Type", "enum": types or ["dimmer"], "default": types[0] if types else "dimmer",
                     "propertyOrder": 3, "options": {"enum_titles": titles or ["Dimmer"], "grid_columns": 4}},
            "address": _int("Start address", 0, UNIVERSE_SIZE, 0, 4, description=ADDRESS_DESCRIPTION,
                            options={"grid_columns": 6}),
            "count": _int("Count", 1, UNIVERSE_SIZE, 1, 5, description=COUNT_DESCRIPTION, options={"grid_columns": 6}),
        },
        "required": ["id", "type"],
        "options": {"disable_edit_json": True, "disable_properties": True},
    }


def build_schema(templates: dict[str, Template]) -> dict[str, Any]:
    ru = dict(RU)
    for t in templates.values():
        ru[t.title["en"]] = t.title["ru"]
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "title": TITLE,
        "description": DESCRIPTION,
        "configFile": {"path": CONFIG_PATH, "service": SERVICE},
        "options": {"disable_edit_json": True, "disable_properties": True, "disable_collapse": True},
        "properties": {
            "ports": {
                "type": "array", "title": "Ports", "propertyOrder": 1, "minItems": 1, "_format": "tabs-top",
                "items": port_schema(templates),
                "options": {"disable_array_reorder": True, "disable_collapse": True,
                            "disable_array_delete_last_row": True},
            },
        },
        "required": ["ports"],
        "translations": {"ru": ru},
    }


def write_schema(schema: dict[str, Any], path: Path = SCHEMA_PATH) -> bool:
    """Write the schema when it differs; wb-mqtt-confed reloads a schema file whenever it changes."""
    text = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True
