from wb.mqtt_dmx.ids import id_from_name, unique_id


def test_ids_are_transliterated_and_normalised():
    assert id_from_name("Фасад") == "fasad"
    assert id_from_name("Пролёт 1") == "prolet_1"
    assert id_from_name("Logo (left)") == "logo_left"
    assert id_from_name("Щит ЖК, этаж 2") == "schit_zhk_etazh_2"
    assert id_from_name("  ***  ") == "device"
    assert len(id_from_name("x" * 100)) == 32


def test_duplicates_get_numeric_suffixes():
    taken: set[str] = set()
    assert [unique_id("par", taken) for _ in range(3)] == ["par", "par_2", "par_3"]
    long = "a" * 32
    assert unique_id(long, {long}) == "a" * 30 + "_2"
