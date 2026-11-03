# wb-mqtt-dmx

Выход DMX512 с контроллера Wiren Board: кадры DMX идут прямо с порта RS-485 контроллера
(`/dev/ttyRS485-N`) или модуля WBE2-I-RS485-ISO (`/dev/ttyMODx`), приборы публикуются как
MQTT-устройства. Шлюз не нужен.

**Статус: прототип.** Цель — проверить на железе модель устройств, поведение порта и UX в
веб-интерфейсе. Код простой и плоский, после проверки будет переписан. План и принятые решения —
в [docs/plan.md](docs/plan.md).

## Что есть сейчас

* Сервис `wb-mqtt-dmx`: читает `/etc/wb-mqtt-dmx.conf` (форма «DMX512 output» в разделе
  Settings → Configs), на каждом включённом порту держит поток кадров DMX и публикует MQTT-устройства:
  `wb-mqtt-dmx` (статус, статистика и карта адресов по портам, кнопка «Погасить всё»),
  `dmx_<порт>` с сырыми каналами `ch1…chN` (если включено в Advanced), `dmx_<id>` на каждый прибор
  с контролами по каналам шаблона (red, green, …), и групповое `dmx_<id>` для записей с количеством > 1.
  Id строится из названия транслитерацией («Фасад» → `dmx_fasad`), названия в конфиге должны быть
  уникальными. Значения каналов сохраняются в `/var/lib/wb-mqtt-dmx/state.json` и восстанавливаются
  при старте, если у порта выбрано «Восстановить последние значения».
* Типы декодеров — JSON-шаблоны в `templates/` (dimmer, rgb, grb, rgbw, grbw, drgb, drgbw, cct, rgbcct);
  свои кладутся в `/etc/wb-mqtt-dmx.conf.d/templates/`, затем `wb-mqtt-dmx update-schema`.
* Порт, который ещё включён в драйвере serial-устройств, сервис не открывает и пишет об этом в статус.

### Запуск прототипа из исходников

```bash
# на контроллере, исходники в /mnt/data/wb-mqtt-dmx
ln -sfn /mnt/data/wb-mqtt-dmx/templates /usr/share/wb-mqtt-dmx/templates
cp /mnt/data/wb-mqtt-dmx/wb-mqtt-dmx.conf /etc/wb-mqtt-dmx.conf
PYTHONPATH=/mnt/data/wb-mqtt-dmx python3 /mnt/data/wb-mqtt-dmx/bin/wb-mqtt-dmx update-schema
```

Юнит systemd для такого запуска: как `debian/wb-mqtt-dmx.service`, но с
`Environment=PYTHONPATH=/mnt/data/wb-mqtt-dmx` и `ExecStart=/usr/bin/python3 /mnt/data/wb-mqtt-dmx/bin/wb-mqtt-dmx`.

## Низкий уровень (этап 0)

* `wb/mqtt_dmx/dmx_port.py` — кадр DMX на серийном порту: 250 000 бод 8N2 через `TCSETS2`/`BOTHER`,
  BREAK через `TIOCSBRK`/`TIOCCBRK`, передатчик RS-485 удерживается через RTS (автоматическое
  управление направлением в ядре на время работы выключается и восстанавливается при закрытии).
* `wb/mqtt_dmx/sender.py` — буфер вселенной и поток кадров с фиксированной частотой.
* `wb-mqtt-dmx test-port` — проверка порта без MQTT и конфига.

## Проверка порта

Порт должен быть свободен от `wb-mqtt-serial`: в веб-интерфейсе контроллера
*Настройки → Конфигурационные файлы → Настройка драйвера serial-устройств* отключите нужный порт
(тот же шаг, что для wb-mqtt-mbgate). Проверить, кто держит порт: `fuser -v /dev/ttyRS485-2`.

```bash
# на контроллере, из каталога с исходниками
PYTHONPATH=. python3 bin/wb-mqtt-dmx test-port /dev/ttyRS485-2 --pattern blink --seconds 30
```

Шаблоны: `blink` — все каналы 255/0 раз в секунду (видно на любом декодере), `full` — все 255,
`ramp` — канал N = N mod 256 (удобно смотреть осциллографом). Параметры: `--hz` (1…44), `--slots`,
`--break-us`, `--mab-us`, `--no-rts` (оставить направление ядру или адаптеру).

В конце команда печатает число кадров, фактическую частоту, опоздавшие кадры и число откатов на
`tcdrain`. Что смотреть осциллографом на A/B: BREAK ≥ 88 мкс, MAB ≥ 8 мкс, период кадра, что линия
не «плавает» между кадрами (DE удерживается).

Результаты на WB 8.5 — в [docs/hardware_results.md](docs/hardware_results.md). Для Rigol DHO804 по
сети есть `scripts/dmx_measure.py` (BREAK, MAB, период, ошибки стоп-битов; CH1 на линии A, щуп 10:1)
и `scripts/scope.py` (скриншоты и SCPI-запросы), адрес прибора в переменной `SCOPE_HOST`.

## Разработка

```bash
uv run --with pytest python -m pytest -q      # или просто pytest
```

Тесты подменяют системные вызовы фейком, железо не нужно.
