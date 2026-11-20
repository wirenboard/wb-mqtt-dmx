"""
The confed form: a JSON Schema (draft-04 + json-editor extensions, as the other WB services use).

The package ships the schema as a ready file, wb-mqtt-dmx.schema.json, next to the sources. This module is
where that file comes from: after changing anything here run

    python3 -m wb.mqtt_dmx.schema > wb-mqtt-dmx.schema.json

A test fails while the file and this module disagree.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .dmx_port import MAX_SLOT_GAP_US
from .netin import ARTNET_MAX_UNIVERSE, SACN_MAX_UNIVERSE
from .templates import (
    CHANNELS_TITLE,
    CHANNELS_TYPE,
    DEFAULT_LAYOUT,
    LAYOUT_RE,
    LAYOUT_TITLE,
    LAYOUT_TYPE,
)

CONFIG_PATH = "/etc/wb-mqtt-dmx.conf"
SERVICE = "wb-mqtt-dmx"
UNIVERSE_SIZE = 512

TITLE = "DMX512 output"
DESCRIPTION = (
    "DMX512 fixtures driven from the controller's own RS-485 ports or WBE2 RS-485 modules. Each port is "
    "one DMX line with its devices. A port must be disabled in the serial device driver settings first "
    "(Settings → Configuration files → Serial device driver), the same way as for wb-mqtt-mbgate."
)
# Every caption says what the setting does, when to touch it and what the default is.
ENABLED_DESCRIPTION = (
    "Send DMX on this port. Keep it off until the port is disabled in the serial device driver "
    "settings, otherwise both drivers write to the same line. Default: off."
)
PORT_DESCRIPTION = (
    "The RS-485 port the DMX line is wired to: /dev/ttyRS485-1, /dev/ttyRS485-2 or a WBE2 module "
    "port such as /dev/ttyMOD1."
)
NAME_DESCRIPTION = "Shown on the tab of the port. Any text."
REFRESH_DESCRIPTION = (
    "How many times a second all channels are sent. Lower it to 25–30 if fixtures flicker. "
    "Default 40, the maximum for 512 channels."
)
ON_START_DESCRIPTION = (
    "Channel values right after the service starts (reboot, saving these settings). Restore: the same as "
    "before the stop. For that the values are written to the controller's storage, but only once the light "
    "has stood still for 10 seconds, so a running scene writes nothing. All channels 0: start dark; nothing "
    "is stored, and the light also goes out when these settings are saved. Default: restore."
)
ON_STOP_DESCRIPTION = (
    "What happens when the service stops. Hold: frames just stop and the fixtures keep their "
    "last values until their own timeout. Blackout: all channels are set to 0 first. "
    "Default: hold."
)
NETWORK_DESCRIPTION = (
    "A lighting console or software (QLC+, MadMapper, …) can drive this port over the network. "
    "While it sends, its frames go to the line and the device channels show its values; a few "
    "seconds after it stops, the port returns to the values set over MQTT. Both protocols are "
    "off by default: anyone on the network could drive the lights."
)
ARTNET_DESCRIPTION = "Accept Art-Net, UDP port 6454."
ARTNET_UNIVERSE_DESCRIPTION = (
    "The Art-Net universe this port listens to, 0–32767 (net·subnet·universe). QLC+ shows "
    "address 0 as Universe 1. Used while Art-Net is on. Default 0; every port needs "
    "its own number."
)
SACN_DESCRIPTION = (
    "Accept sACN (E1.31), UDP port 5568, unicast or multicast. With several senders the one with "
    "the higher priority wins."
)
SACN_UNIVERSE_DESCRIPTION = (
    "The sACN universe this port listens to, 1–63999. Used while sACN is on. Default 1; "
    "every port needs its own number."
)
ADVANCED_DESCRIPTION = (
    "Signal timing. The defaults suit most fixtures; change them only when a fixture flickers "
    "or does not respond."
)
SLOTS_DESCRIPTION = (
    "How many channels every frame carries. Fewer channels make frames shorter, but some fixtures "
    "expect all 512. Default 512."
)
BREAK_DESCRIPTION = (
    "The low level that starts every frame. Lengthen it to 250–400 if fixtures miss the start of "
    "a frame. Default 176, the standard allows 88 and more. On the line it comes out up to 80 µs "
    "longer."
)
MAB_DESCRIPTION = (
    "The pause between BREAK and the first byte. Lengthen it to 50–100 for slow fixtures. Default "
    "20, the standard allows 8 and more. On the line it comes out 40–80 µs longer."
)
SLOT_GAP_DESCRIPTION = (
    "A pause after every channel for fixtures that cannot keep up with the data and flicker. "
    "The frame gets longer, so the frame rate drops: 50 µs at 512 channels gives about 16 Hz. "
    "The real pause is about the set value or longer. Default 0: channels go back to back, as "
    "ordinary consoles send them."
)
DEVICES_DESCRIPTION = (
    "One row is one fixture or a group of identical ones. Type: Layout for ordinary fixtures, "
    "Channels for anything else. Layout: one letter per DMX channel in the fixture's order — "
    "d dimmer, r g b colour, w white, a amber, u UV, c cold white, h warm white, s strobe, "
    "m mode (program channel), _ unused; e.g. dRGBW; default rgbw. Resolution: 8 bit, or 16 bit "
    "when every channel of the fixture takes two addresses. Channels: how many plain channels "
    "the device has. Address: the first DMX address; 0, the default, places the device right "
    "after the previous one. Count: above 1 gives that many identical devices on consecutive "
    "addresses plus a group device for all of them. MQTT id: made from the name "
    "(Фасад → dmx_fasad); fill it in only to pin a specific id."
)

RU = {
    TITLE: "Выход DMX512",
    DESCRIPTION: "DMX512-приборы, управляемые с портов RS-485 контроллера или модулей WBE2 RS-485. Порт — "
    "одна "
    "линия DMX со своими приборами. Порт нужно сначала отключить в настройках драйвера последовательных "
    "устройств (Настройки → Конфигурационные файлы → Настройка драйвера serial-устройств), как для "
    "wb-mqtt-mbgate.",
    "Ports": "Порты",
    "Port": "Порт",
    "Port settings": "Настройки порта",
    "Name": "Название",
    "Enabled": "Включён",
    ENABLED_DESCRIPTION: "Выдавать DMX на этом порту. Не включайте, пока порт не отключён в настройках "
    "драйвера "
    "serial-устройств, иначе оба драйвера пишут в одну линию. По умолчанию выключен.",
    "Serial port": "Порт",
    "Frame rate, Hz": "Частота кадров, Гц",
    "On start": "При старте",
    "On stop": "При остановке",
    "Restore last values": "Восстановить последние значения",
    "All channels 0": "Все каналы в 0",
    "Hold last frame": "Оставить как есть",
    "Blackout": "Погасить всё",
    "Advanced": "Дополнительно",
    "Channels per frame": "Каналов в кадре",
    "BREAK, µs": "BREAK, мкс",
    "MAB, µs": "MAB, мкс",
    "Pause between channels, µs": "Пауза между каналами, мкс",
    "Devices": "Приборы",
    "Device id": "Идентификатор",
    "Type": "Тип",
    "Start address": "Стартовый адрес",
    "Count": "Количество",
    PORT_DESCRIPTION: "Порт RS-485, к которому подключена линия DMX: /dev/ttyRS485-1, /dev/ttyRS485-2 или "
    "порт "
    "модуля WBE2, например /dev/ttyMOD1.",
    NAME_DESCRIPTION: "Показывается на вкладке порта. Любой текст.",
    REFRESH_DESCRIPTION: "Сколько раз в секунду отправляются все каналы. Снизьте до 25–30, если приборы "
    "мерцают. "
    "По умолчанию 40, это максимум для 512 каналов.",
    ON_START_DESCRIPTION: "Значения каналов сразу после запуска сервиса (перезагрузка, сохранение этих "
    "настроек). Восстановить: те же, что были до остановки. Для этого значения записываются в память "
    "контроллера, но только когда свет 10 секунд стоит на месте, так что бегущая сцена ничего не пишет. "
    "Все каналы в 0: начать с темноты; ничего не хранится, и свет гаснет также при сохранении этих "
    "настроек. По умолчанию: восстановить.",
    ON_STOP_DESCRIPTION: "Что происходит при остановке сервиса. Оставить: кадры просто прекращаются, "
    "приборы держат "
    "последние значения до своего таймаута. Погасить: сначала все каналы ставятся в 0. "
    "По умолчанию: оставить.",
    "Network input": "Приём из сети",
    "Art-Net port address": "Адрес порта Art-Net",
    "sACN universe": "Вселенная sACN",
    NETWORK_DESCRIPTION: "Световой пульт или программа (QLC+, MadMapper, …) может управлять этим портом по "
    "сети. "
    "Пока она шлёт, на линию идут её кадры, а каналы приборов показывают её значения; через "
    "несколько секунд после остановки порт возвращается к значениям из MQTT. Оба протокола по "
    "умолчанию выключены: иначе светом может управлять любой в сети.",
    ARTNET_DESCRIPTION: "Принимать Art-Net, порт UDP 6454.",
    ARTNET_UNIVERSE_DESCRIPTION: "Вселенная Art-Net, которую слушает этот порт, 0–32767 "
    "(net·subnet·universe). "
    "В QLC+ адрес 0 называется Universe 1. Действует, пока включён Art-Net. "
    "По умолчанию 0; у каждого порта свой номер.",
    SACN_DESCRIPTION: "Принимать sACN (E1.31), порт UDP 5568, unicast или multicast. Если источников "
    "несколько, "
    "побеждает тот, у кого выше приоритет.",
    SACN_UNIVERSE_DESCRIPTION: "Вселенная sACN, которую слушает этот порт, 1–63999. Действует, пока "
    "включён sACN. "
    "По умолчанию 1; у каждого порта свой номер.",
    ADVANCED_DESCRIPTION: "Тайминги сигнала. Значения по умолчанию подходят большинству приборов; меняйте "
    "их, только "
    "если прибор мерцает или не реагирует.",
    SLOTS_DESCRIPTION: "Сколько каналов в каждом кадре. Чем меньше каналов, тем короче кадр, но некоторым "
    "приборам "
    "нужны все 512. По умолчанию 512.",
    BREAK_DESCRIPTION: "Низкий уровень, с которого начинается каждый кадр. Увеличьте до 250–400, если "
    "приборы "
    "теряют начало кадра. По умолчанию 176, стандарт допускает от 88. На линии выходит длиннее "
    "на величину до 80 мкс.",
    MAB_DESCRIPTION: "Пауза между BREAK и первым байтом. Увеличьте до 50–100 для медленных приборов. По "
    "умолчанию "
    "20, стандарт допускает от 8. На линии выходит длиннее на 40–80 мкс.",
    SLOT_GAP_DESCRIPTION: "Пауза после каждого канала для приборов, которые не успевают принимать данные и "
    "мерцают. "
    "Кадр становится длиннее, поэтому частота кадров падает: 50 мкс при 512 каналах дают около "
    "16 Гц. Настоящая пауза примерно равна заданной или длиннее. По умолчанию 0: каналы идут "
    "вплотную, как у обычных пультов.",
    DEVICES_DESCRIPTION: "Строка — один прибор или группа одинаковых. Тип: «Раскладка» для обычных приборов, "
    "«Каналы» для всего остального. Раскладка: по букве на DMX-канал в порядке прибора — "
    "d диммер, r g b цвет, w белый, a янтарный, u УФ, c холодный белый, h тёплый белый, "
    "s строб, m режим (канал программ), _ не используется; например dRGBW; по умолчанию rgbw. "
    "Разрядность: 8 бит, или 16 бит, если каждый канал прибора занимает два адреса. Каналов: "
    "сколько простых каналов у прибора. Адрес: первый адрес DMX; 0, значение по умолчанию, "
    "ставит прибор сразу после предыдущего. Количество: больше 1 даёт столько одинаковых "
    "приборов на адресах подряд плюс групповое устройство для всех сразу. MQTT-идентификатор: "
    "строится из названия (Фасад → dmx_fasad); заполняйте его, только если нужен конкретный.",
    "Layout": "Раскладка",
    "Resolution": "Разрядность",
    "8 bit": "8 бит",
    "16 bit": "16 бит",
    "MQTT id": "MQTT-идентификатор",
    "Address": "Адрес",
    "Device": "Прибор",
    "Channels": "Каналов",
}


def _int(title: str, minimum: int, maximum: int, default: int, order: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "integer",
        "title": title,
        "minimum": minimum,
        "maximum": maximum,
        "default": default,
        "propertyOrder": order,
        **extra,
    }


def port_schema() -> dict[str, Any]:
    # The form does not show a setting that the saved config lacks, and there is no way to bring it back.
    # Marking the settings "required" is no cure: confed then refuses to open such a config at all. So the
    # settings stay optional here and nothing hides them (no dependencies): once in the file, a setting
    # stays there. A setting added in a later version will need the configs in the field completed.
    advanced = {
        "type": "object",
        "title": "Advanced",
        "propertyOrder": 100,
        "_format": "grid",
        "description": ADVANCED_DESCRIPTION,
        "options": {"collapsed": True, "disable_edit_json": True, "disable_properties": True},
        "properties": {
            "slots": _int(
                "Channels per frame",
                24,
                UNIVERSE_SIZE,
                UNIVERSE_SIZE,
                1,
                description=SLOTS_DESCRIPTION,
                options={"grid_columns": 4},
            ),
            "break_us": _int(
                "BREAK, µs", 88, 1000, 176, 2, description=BREAK_DESCRIPTION, options={"grid_columns": 4}
            ),
            "mab_us": _int(
                "MAB, µs", 8, 1000, 20, 3, description=MAB_DESCRIPTION, options={"grid_columns": 4}
            ),
            "slot_gap_us": _int(
                "Pause between channels, µs",
                0,
                MAX_SLOT_GAP_US,
                0,
                4,
                description=SLOT_GAP_DESCRIPTION,
                options={"grid_columns": 12},
            ),
        },
    }
    # Always open: whether a console may take the port over is a thing to see at a glance
    network = {
        "type": "object",
        "title": "Network input",
        "propertyOrder": 50,
        "_format": "grid",
        "description": NETWORK_DESCRIPTION,
        "options": {"disable_collapse": True, "disable_edit_json": True, "disable_properties": True},
        "properties": {
            "artnet": {
                "type": "boolean",
                "title": "Art-Net",
                "default": False,
                "_format": "checkbox",
                "propertyOrder": 1,
                "description": ARTNET_DESCRIPTION,
                "options": {"grid_columns": 6},
            },
            "artnet_universe": _int(
                "Art-Net port address",
                0,
                ARTNET_MAX_UNIVERSE,
                0,
                2,
                description=ARTNET_UNIVERSE_DESCRIPTION,
                options={"grid_columns": 6},
            ),
            "sacn": {
                "type": "boolean",
                "title": "sACN",
                "default": False,
                "_format": "checkbox",
                "propertyOrder": 3,
                "description": SACN_DESCRIPTION,
                "options": {"grid_columns": 6},
            },
            "sacn_universe": _int(
                "sACN universe",
                1,
                SACN_MAX_UNIVERSE,
                1,
                4,
                description=SACN_UNIVERSE_DESCRIPTION,
                options={"grid_columns": 6},
            ),
        },
    }
    settings = {
        "type": "object",
        "title": "Port settings",
        "propertyOrder": 1,
        "_format": "grid",
        # Collapsed once the port is set up. The header is just the title: a summary of raw values
        # (path · 40 Hz · restore / hold) read as noise, and the tab already names the port.
        "options": {"collapsed": True, "disable_edit_json": True, "disable_properties": True},
        "properties": {
            "path": {
                "type": "string",
                "title": "Serial port",
                "minLength": 1,
                "default": "/dev/ttyRS485-2",
                "propertyOrder": 1,
                "description": PORT_DESCRIPTION,
                "options": {"grid_columns": 6},
            },
            "name": {
                "type": "string",
                "title": "Name",
                "default": "DMX 1",
                "propertyOrder": 2,
                "description": NAME_DESCRIPTION,
                "options": {"grid_columns": 6},
            },
            "refresh_hz": _int(
                "Frame rate, Hz", 1, 44, 40, 3, description=REFRESH_DESCRIPTION, options={"grid_columns": 4}
            ),
            "on_start": {
                "type": "string",
                "title": "On start",
                "enum": ["restore", "zero"],
                "default": "restore",
                "propertyOrder": 4,
                "description": ON_START_DESCRIPTION,
                "options": {"enum_titles": ["Restore last values", "All channels 0"], "grid_columns": 4},
            },
            "on_stop": {
                "type": "string",
                "title": "On stop",
                "enum": ["hold", "blackout"],
                "default": "hold",
                "propertyOrder": 5,
                "description": ON_STOP_DESCRIPTION,
                "options": {"enum_titles": ["Hold last frame", "Blackout"], "grid_columns": 4},
            },
            "network": network,
            "advanced": advanced,
        },
        "required": ["path"],
    }
    return {
        "type": "object",
        "title": "Port",
        "headerTemplate": "{{self.settings.name}} ({{self.settings.path}})",
        "properties": {
            # Above the collapsed settings: whether the port sends at all is the first thing to see
            "enabled": {
                "type": "boolean",
                "title": "Enabled",
                "default": False,
                "_format": "checkbox",
                "propertyOrder": 0,
                "description": ENABLED_DESCRIPTION,
            },
            "settings": settings,
            "devices": {
                "type": "array",
                "title": "Devices",
                "propertyOrder": 2,
                "_format": "table",
                "description": DEVICES_DESCRIPTION,
                # The form rejects fully identical rows; the service rejects repeated names on load.
                "uniqueItems": True,
                "items": device_schema(),
                "options": {
                    "disable_array_reorder": True,
                    "disable_collapse": True,
                    "disable_array_delete_last_row": True,
                    "disable_array_delete_all_rows": True,
                },
            },
        },
        "required": ["settings", "devices"],
        # The tab already names the port; homeui's wb.disable_title drops the repeated heading in the panel.
        "options": {
            "disable_edit_json": True,
            "disable_properties": True,
            "disable_collapse": True,
            "wb": {"disable_title": True},
        },
    }


def device_schema() -> dict[str, Any]:
    types = [LAYOUT_TYPE, CHANNELS_TYPE]
    titles = [LAYOUT_TITLE["en"], CHANNELS_TITLE["en"]]
    return {
        "type": "object",
        "title": "Device",
        "properties": {
            "name": {
                "type": "string",
                "title": "Name",
                "default": "Device",
                "minLength": 1,
                "propertyOrder": 1,
            },
            "type": {
                "type": "string",
                "title": "Type",
                "enum": types,
                "default": types[0],
                "propertyOrder": 2,
                "options": {"enum_titles": titles},
            },
            # Fields of the built-in types; json-editor shows each only for its type.
            "layout": {
                "type": "string",
                "title": "Layout",
                "pattern": LAYOUT_RE,
                "default": DEFAULT_LAYOUT,
                "propertyOrder": 3,
                "options": {"dependencies": {"type": ["layout"]}},
            },
            "bits": {
                "type": "integer",
                "title": "Resolution",
                "enum": [8, 16],
                "default": 8,
                "propertyOrder": 4,
                "options": {"enum_titles": ["8 bit", "16 bit"], "dependencies": {"type": ["layout"]}},
            },
            "channels": _int(
                "Channels", 1, UNIVERSE_SIZE, 1, 5, options={"dependencies": {"type": ["channels"]}}
            ),
            "address": _int("Address", 0, UNIVERSE_SIZE, 0, 6),
            "count": _int("Count", 1, UNIVERSE_SIZE, 1, 7),
            # Empty id = derived from the name by transliteration (facade, prolet_1); the device card
            # shows it.
            "id": {
                "type": "string",
                "title": "MQTT id",
                "pattern": "^([a-z0-9_]{1,32})?$",
                "default": "",
                "propertyOrder": 8,
            },
        },
        "required": ["name", "type"],
        "options": {"disable_edit_json": True, "disable_properties": True},
    }


def build_schema() -> dict[str, Any]:
    ru = dict(RU)
    for title in (LAYOUT_TITLE, CHANNELS_TITLE):
        ru[title["en"]] = title["ru"]
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "title": TITLE,
        "description": DESCRIPTION,
        "configFile": {"path": CONFIG_PATH, "service": SERVICE},
        "options": {"disable_edit_json": True, "disable_properties": True, "disable_collapse": True},
        "properties": {
            "ports": {
                "type": "array",
                "title": "Ports",
                "propertyOrder": 1,
                "minItems": 1,
                "_format": "tabs-top",
                "items": port_schema(),
                "options": {
                    "disable_array_reorder": True,
                    "disable_collapse": True,
                    "disable_array_delete_last_row": True,
                },
            },
        },
        "required": ["ports"],
        "translations": {"ru": ru},
    }


def schema_text() -> str:
    """
    The contents of wb-mqtt-dmx.schema.json.
    """
    return json.dumps(build_schema(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    sys.stdout.buffer.write(schema_text().encode("utf-8"))
