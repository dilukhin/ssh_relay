# Протокол краткой ручной проверки ssh_relay 0.6.0

Дата: 2026-07-31.
Среда: Windows PowerShell 5.1, Python 3.13, Python 3.12+ (по README).
Тестовый сервер: стенд `4BSDownloader2`, Debian GNU/Linux 11, `172.18.24.2:60019`, пользователь `d.ilyhin`, sudo включён.

## Что проверено

1. Версия:
   - `py ssh_relay.py --version` → `ssh_relay 0.6.0` (совпадает с README).
2. Синтаксис:
   - `py -m py_compile ssh_relay.py` — exit 0.
3. Справка всех подкоманд:
   - `--help`, `daemon --help`, `exec --help`, `sudo-exec --help`, `download --help`, `upload --help`, `status --help`, `stop --help`, `list --help` — все exit 0.
4. Runtime-цикл с тестовым SSH-сервером:
   - остановка старого daemon (`stop` → exit 0);
   - запуск нового daemon `--enable-sudo` (порт 53479);
   - `status` → активна, версия 0.6.0, sudo вкл.;
   - `exec "hostname && whoami && pwd"` → `4BSDownloader2 / d.ilyhin / /home/d.ilyhin`;
   - `sudo-exec "whoami"` → `root`;
   - `stop` → daemon остановлен.
5. Коды возврата, traceback, зависания, секреты.
6. Машинный JSON-протокол: один успешный вызов `--json`.

## Результаты

| Проверка | Ожидание | Факт | Статус |
|---|---|---|---|
| `--version` | `ssh_relay 0.6.0` | `ssh_relay 0.6.0`, exit 0 | успешно |
| `py -m py_compile` | exit 0 | exit 0 | успешно |
| `--help` и все подкоманды | exit 0, без traceback | exit 0 | успешно |
| `stop` старого daemon | exit 0 | exit 0 | успешно |
| `status` после запуска | активна, 0.6.0, sudo вкл. | совпало | успешно |
| `exec "hostname && whoami && pwd"` | вывод стенда | `4BSDownloader2 / d.ilyhin / /home/d.ilyhin`, exit 0 | успешно |
| `sudo-exec "whoami"` | `root` | `root`, exit 0 | успешно |
| `exec "sh -c 'exit 7'"` | exit 7 | exit 7 (код удалённой команды передан корректно) | успешно |
| `status` для несуществующей сессии | exit 1 | exit 1 | успешно |
| `stop` → повторный `status` | «не найдена», exit 1 | совпало после паузы | успешно |
| `exec --json "printf json-ok"` | один валидный JSON-объект | 1 строка, `ConvertFrom-Json` успешен, `operation_status=succeeded`, `command_exit_code=0`, exit 0 | успешно |

Дополнительно подтверждено: работающий daemon запущен после последнего изменения `ssh_relay.py` (файл изменён 2026-07-29, daemon запущен 2026-07-31), то есть тест выполнен на текущем коде 0.6.0.

## Автоматические тесты

| Тест | Ожидание | Факт | Статус |
|---|---|---|---|
| `py tests\test_machine_protocol.py` | exit 0 | exit 0; «Все локальные проверки пройдены», «Расширенные проверки пройдены», «Проверки сохранения session-файла пройдены», «Проверка повторного transaction_id пройдена», «Machine error повторного transaction_id пройден» | успешно |
| `py tests\test_local_tcp_protocol.py` | exit 0 | exit 0; «Локальный протокольный тест пройден» | успешно |

Покрытие (по README):

- `test_machine_protocol.py` — матрица статусов, коды завершения, отсутствие команды в receipt, self-hash, legacy anchor, параметры защиты файла, повторный `transaction_id`, сохранение session-файла при потерянном ответе.
- `test_local_tcp_protocol.py` — настоящий локальный TCP daemon с подменённым SSH-транспортом: запрос старого CLI 0.5.x, новый JSON-протокол, stdout JSON, корректный `stop`.

## Чувствительные данные

В выводе и в протоколе отсутствуют пароли, токены, приватные ключи и sudo-пароль. JSON-ответ содержит `command_hash` и метаданные, но не текст команды и не секреты.

## Найденные проблемы

Критических проблем не обнаружено.

Примечание: после отправки `stop` первый немедленный `status` всё ещё показывал сессию активной, повторный `status` после паузы корректно вернул «Сессия не найдена» (exit 1). Это соответствует описанному в README поведению сохранения session-файла при потере ответа и не является дефектом.

## Вывод

Краткая проверка завершена успешно. Можно переходить к расширенному тестированию.

Не проверено в рамках краткой проверки (см. README «Непроверенное в этом комплекте»): фактическая запись JSONL на Linux через `--risky`, сбой записи receipt, потеря SSH-соединения, `download`/`upload` на реальном сервере. В комплекте ssh_relay автоматические тесты пройдены; реальный SSH-сервер и `sudo-exec` проверены вручную в рамках данной проверки.
