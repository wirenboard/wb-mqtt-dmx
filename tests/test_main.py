import json
from pathlib import Path

import pytest

from wb.mqtt_dmx import main


@pytest.fixture(name="reported")
def reported_fixture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Catches the configuration errors the service would publish over MQTT.
    """
    errors: list[str] = []
    monkeypatch.setattr(main, "report_config_error", errors.append)
    return errors


def test_missing_config_returns_config_error(reported: list[str]) -> None:
    exit_code = main.main(["wb-mqtt-dmx", "-c", "/nonexistent/does-not-exist.conf"])

    assert exit_code == main.EXIT_CONFIG_ERROR
    assert "cannot read" in reported[0]


def test_config_that_is_not_json_returns_config_error(tmp_path: Path, reported: list[str]) -> None:
    config = tmp_path / "broken.conf"
    config.write_text("{not json", encoding="utf-8")

    assert main.main(["wb-mqtt-dmx", "-c", str(config)]) == main.EXIT_CONFIG_ERROR
    assert "is not valid JSON" in reported[0]


def test_unusable_config_is_reported_with_its_reason(tmp_path: Path, reported: list[str]) -> None:
    config = tmp_path / "bad.conf"
    config.write_text(json.dumps({"ports": []}), encoding="utf-8")

    assert main.main(["wb-mqtt-dmx", "-c", str(config)]) == main.EXIT_CONFIG_ERROR
    assert reported == ["ports: at least one port is required"]


@pytest.mark.parametrize(
    "pattern, second, frame",
    [
        ("blink", 0, bytes([255, 255, 255])),
        ("blink", 1, bytes([0, 0, 0])),
        ("full", 5, bytes([255, 255, 255])),
        ("ramp", 0, bytes([1, 2, 3])),
    ],
)
def test_test_port_patterns(pattern: str, second: int, frame: bytes) -> None:
    assert main.pattern_frame(pattern, 3, second) == frame


def test_unknown_pattern_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown pattern"):
        main.pattern_frame("disco", 3, 0)
