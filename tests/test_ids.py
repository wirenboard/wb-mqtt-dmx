import pytest

from wb.mqtt_dmx.ids import id_from_name, unique_id


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Фасад", "fasad"),
        ("Пролёт 1", "prolet_1"),
        ("Logo (left)", "logo_left"),
        ("Щит ЖК, этаж 2", "schit_zhk_etazh_2"),
        ("  ***  ", "device"),
    ],
)
def test_id_is_the_name_transliterated_and_normalised(name: str, expected: str) -> None:
    assert id_from_name(name) == expected


def test_id_is_cut_to_32_characters() -> None:
    assert len(id_from_name("x" * 100)) == 32


def test_taken_id_gets_a_numeric_suffix() -> None:
    taken: set[str] = set()

    assert [unique_id("par", taken) for _ in range(3)] == ["par", "par_2", "par_3"]


def test_suffix_fits_into_the_length_limit() -> None:
    long = "a" * 32

    assert unique_id(long, {long}) == "a" * 30 + "_2"
