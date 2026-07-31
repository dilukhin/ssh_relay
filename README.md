# ssh_relay

`ssh_relay.py` — локальный SSH-relay для выполнения коротких неинтерактивных команд через заранее открытую именованную SSH-сессию.

Пользователь вручную запускает daemon и проходит SSH-аутентификацию. После этого CLI-агент, в частности OpenCode, вызывает локальный relay без прямого `ssh` и повторного ввода SSH-пароля.

Текущая версия: `0.6.0`.

## Что добавлено в 0.6.0

- машиночитаемый режим `--json` для `exec` и `sudo-exec`;
- идентификаторы `transaction_id` и `receipt_id`;
- безопасный risky receipt без полного текста команды;
- `command_hash`, `receipt_hash` и цепочка `previous_receipt_hash`;
- отдельные результаты `command_failed`, `partial_success`, `not_started` и `unknown`;
- явные `--change-target` и `--change-description`;
- блокировка повторного подтверждённого `transaction_id`;
- проверка последней записи receipt до запуска изменяющей команды;
- защита receipt-файла правами `0600` и отказ от записи через конечную символическую ссылку;
- совместимость нового daemon с обычными запросами CLI 0.5.x.

Команды `inspect`, `exec-script` и `sudo-exec-script` в версию 0.6.0 не входят.

## Возможности

- именованные SSH-сессии;
- вход по паролю, приватному ключу или OpenSSH-сертификату;
- `exec` для обычных коротких команд;
- `sudo-exec` через явно включённый `daemon --enable-sudo`;
- безопасный удалённый JSONL receipt для `--risky`;
- загрузка и скачивание одного обычного файла;
- `status`, `list` и безопасный `stop`;
- фоновый запуск daemon по ключу через `--detach`;
- прослушивание только `127.0.0.1`;
- обязательная проверка host key через `known_hosts`;
- последовательное выполнение и ограничения времени, вывода и размера файлов.

## Ограничения

Relay предназначен только для коротких неинтерактивных операций.

Не поддерживаются:

- интерактивный stdin;
- интерактивный shell, редакторы, `top`, `less`, `passwd`;
- команды с запросом пароля;
- длительные процессы и команды с большим выводом;
- параллельное выполнение команд;
- рекурсивная передача каталогов;
- `sudo-download` и `sudo-upload`;
- специальные файлы и SCP-режим;
- передача сложной команды через `--command-file` или `--command-stdin`.

Псевдотерминал не создаётся. Максимальный суммарный stdout/stderr команды — 4 МиБ. Если timeout или лимит вывода сработал после запуска команды, результат считается `unknown`: удалённое состояние могло измениться.

Сложные inline-команды с несколькими уровнями quoting, особенно в PowerShell 5.1, следует разбивать на короткие команды либо предварительно загружать как отдельный проверенный файл. Текущее состояние quoting-задач описано в [`UX_FINDINGS.md`](UX_FINDINGS.md).

## Требования и установка

Локальная сторона:

- Windows с `cmd.exe` или PowerShell;
- Python 3.12 или новее;
- `paramiko`.

Удалённая сторона:

- Linux/Ubuntu;
- SSH-служба;
- POSIX shell;
- для `sudo-exec` — право пользователя выполнять требуемые команды через `sudo`.

Установка зависимости:

```cmd
py -m pip install paramiko
```

Проверка:

```cmd
py ssh_relay.py --version
py ssh_relay.py --help
py ssh_relay.py exec --help
```

Ожидаемый вывод версии:

```text
ssh_relay 0.6.0
```

## Подготовка known_hosts

Relay использует `RejectPolicy` и не принимает неизвестный host key автоматически.

### cmd.exe

```cmd
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
ssh-keyscan -H 198.51.100.42 >> "%USERPROFILE%\.ssh\known_hosts"
ssh-keygen -lf "%USERPROFILE%\.ssh\known_hosts"
```

### PowerShell

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keyscan -H 198.51.100.42 | Out-File -Append -Encoding ascii "$env:USERPROFILE\.ssh\known_hosts"
ssh-keygen -lf "$env:USERPROFILE\.ssh\known_hosts"
```

Fingerprint нужно сверить с данными, полученными по доверенному каналу. Результат `ssh-keyscan`, полученный только через проверяемую сеть, сам по себе не является подтверждением доверия.

## Основные команды

```text
py ssh_relay.py daemon [--name NAME] --host HOST --user USER [параметры]
py ssh_relay.py exec [--name NAME] [--risky] [--json] [параметры операции] "COMMAND"
py ssh_relay.py sudo-exec [--name NAME] [--risky] [--json] [параметры операции] "COMMAND"
py ssh_relay.py download [--name NAME] [--overwrite] [--create-dirs] REMOTE_PATH LOCAL_PATH
py ssh_relay.py upload [--name NAME] [--overwrite] [--create-dirs] LOCAL_PATH REMOTE_PATH
py ssh_relay.py status [--name NAME] [--all]
py ssh_relay.py stop [--name NAME] [--all]
py ssh_relay.py list
```

## Запуск daemon

Парольная SSH-аутентификация:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
```

Вход по ключу:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519"
```

Зашифрованный ключ:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519" --ask-key-passphrase
```

Режим sudo:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
```

Daemon запрашивает sudo-пароль локально, проверяет его и хранит только в памяти процесса. Пароль не записывается в session-файл и не включается в локальный протокол.

Фоновый запуск по ключу:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519" --detach
```

`--detach` несовместим с `--ask-key-passphrase` и `--enable-sudo`.

После замены `ssh_relay.py` работающий daemon обязательно нужно остановить и запустить заново. Наличие session-файла версии 0.6.0 не доказывает, что старый процесс уже использует новый код.

## Диагностика перед работой

Если relay уже запущен, агент не должен использовать прямой `ssh`.

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "hostname && whoami && pwd"
```

Для PowerShell код завершения проверяется через `$LASTEXITCODE`:

```powershell
py ssh_relay.py status --name prod
$LASTEXITCODE
```

Для `cmd.exe` код следует проверять в той же командной строке:

```cmd
py ssh_relay.py status --name prod & echo %ERRORLEVEL%
```

В Far Manager не следует выполнять `%ERRORLEVEL%` отдельной следующей командой: к этому моменту значение уже может относиться к другому процессу.

## Обычный текстовый режим

```cmd
py ssh_relay.py exec --name prod "hostname"
py ssh_relay.py exec --name prod "sh -c 'printf stdout-ok; printf stderr-ok >&2; exit 7'"
```

Без `--json` сохраняется прежняя семантика:

- удалённый stdout печатается в локальный stdout;
- удалённый stderr печатается в локальный stderr;
- вызывающему процессу возвращается удалённый exit code;
- ошибка relay возвращает код `1` с русской диагностикой.

Если основная команда выполнена, но receipt не подтверждён, текстовый режим возвращает `1` и явно сообщает о частичном успехе.

## Risky-операция и безопасный receipt

Пример обычной risky-операции:

```cmd
py ssh_relay.py exec --name prod --risky --change-target "/tmp/example" --change-description "Создание тестового каталога" "mkdir -p /tmp/example"
```

Путь receipt по умолчанию:

```text
~/.local/state/agent-safe/changes.jsonl
```

Пример системного receipt для `sudo-exec`:

```cmd
py ssh_relay.py sudo-exec --name prod --risky --receipt-path "/var/lib/agent-safe/changes.jsonl" --change-target "systemd:nginx.service" --change-description "Перезапуск службы" "systemctl restart nginx"
```

Receipt содержит:

- `transaction_id` и `receipt_id`;
- имя сессии, host, port и SSH-пользователя;
- `change_target` и `change_description`;
- `command_hash`, но не полный текст команды;
- exit code основной команды;
- `previous_receipt_hash` и `receipt_hash`;
- UTC timestamp и версию relay.

Полный stdout/stderr в удалённый receipt не записывается.

Перед изменяющей командой relay:

1. читает последнюю строку журнала;
2. проверяет её `receipt_hash` либо принимает последнюю запись 0.5.x как однократный legacy anchor;
3. ищет уже записанный `transaction_id`;
4. только после успешного preflight запускает основную команду.

Повтор подтверждённой транзакции блокируется до запуска основной команды. Повреждённая последняя запись также блокирует запуск.

При записи relay:

- использует `umask 077`;
- создаёт или переводит receipt-файл в режим `0600`;
- проверяет, что конечный путь не является символической ссылкой;
- после append повторно читает последнюю строку и проверяет `receipt_id` и self-hash.

Для системного receipt родительский каталог должен быть доверенным и недоступным для записи посторонним пользователям. Проверка конечной символической ссылки не устраняет гонки в недоверенном каталоге.

Hash-цепочка не является цифровой подписью. Владелец файла может пересчитать всю цепочку.

## Машинный режим JSON

```cmd
py ssh_relay.py exec --name prod --json --risky --transaction-id "agent-safe:tx-001" --change-target "/tmp/example" --change-description "Создание тестового каталога" "mkdir -p /tmp/example"
```

В режиме `--json` stdout CLI содержит ровно один JSON-объект и перевод строки. При штатно сформированном машинном результате локальный stderr пуст. Удалённые stdout/stderr находятся в полях JSON.

Основные значения `operation_status`:

- `succeeded` — команда успешна; для risky receipt подтверждён;
- `command_failed` — получен ненулевой удалённый exit code;
- `partial_success` — команда успешна, но receipt имеет статус `failed` или `unknown`;
- `not_started` — достоверно известно, что основная команда не запускалась;
- `unknown` — relay не может достоверно определить результат команды.

Коды процесса в JSON-режиме:

| Код | Результат |
|---:|---|
| `0` | операция успешна |
| `10` | команда достоверно не запускалась |
| `11` | удалённая команда завершилась с ненулевым кодом |
| `12` | команда успешна, receipt не подтверждён |
| `13` | результат удалённой команды неизвестен |
| `2` | ошибка синтаксиса CLI до формирования JSON |

Точный удалённый exit code находится в `command_exit_code`.

Пример частичного успеха:

```json
{
  "operation_status": "partial_success",
  "command_status": "succeeded",
  "command_exit_code": 0,
  "receipt_status": "failed",
  "partial_success": true,
  "error_code": "receipt_write_failed"
}
```

После кода `12` или `13` агент должен остановить последующие изменяющие операции и выполнить read-only verify, поиск receipt по идентификатору либо rollback по правилам вызывающей системы.

## transaction_id

Параметр:

```text
--transaction-id ID
```

Допустимы 1–128 символов: латинские буквы, цифры, точка, двоеточие, дефис и подчёркивание. Первый символ — буква или цифра.

Если ID не передан, relay создаёт UUIDv4 и возвращает `transaction_id_source=relay`. Адаптер `agent-safe` должен передавать собственный ID заранее, чтобы он был известен даже при потере ответа.

## Именованные сессии

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
py ssh_relay.py daemon --name test --host 198.51.100.43 --user donpedro
py ssh_relay.py list
py ssh_relay.py status --all
```

Session-файлы содержат локальные токены доступа к daemon. Они хранятся в пользовательском каталоге состояния, должны иметь ограниченные права и не должны попадать в Git.

## Передача файлов

Скачать файл:

```cmd
py ssh_relay.py download --name prod /tmp/result.txt .\result.txt
```

Загрузить файл:

```cmd
py ssh_relay.py upload --name prod .\config.json /tmp/config.json
```

Перезапись требует явного `--overwrite`. Создание отсутствующих каталогов требует `--create-dirs`. Передаются только одиночные обычные файлы с учётом настроенных лимитов размера и времени.

## Остановка daemon

```cmd
py ssh_relay.py stop --name prod
py ssh_relay.py stop --all
```

`stop` отправляется только после аутентифицированного обращения к локальному daemon. Процесс не завершается по одному PID из session-файла.

Если запрос мог быть отправлен, но подтверждение не получено, session-файл сохраняется до повторной проверки `status`. Это предотвращает потерю доступа к daemon при неопределённом результате.

## Совместимость с 0.5.x

- daemon 0.6.0 принимает обычные запросы CLI 0.5.x и возвращает ответ старого формата;
- новый CLI допускает обычную нерискованную команду через старый daemon;
- `--risky` и `--json` требуют daemon с поддержкой протокола 0.6.0;
- после обновления работающий daemon нужно перезапустить.

## Проверки

Минимальные локальные проверки:

```cmd
py -m py_compile ssh_relay.py
py tests\test_machine_protocol.py
py tests\test_local_tcp_protocol.py
py ssh_relay.py --version
```

Ручной smoke-тест:

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "hostname && whoami && pwd"
py ssh_relay.py exec --name prod --json "printf json-ok"
```

Перед ручной проверкой изменённого daemon его обязательно нужно остановить и запустить заново.

Расширенная приёмка Issue #2 дополнительно требует реальных risky-сценариев на Linux, ошибок и неопределённого статуса receipt, сетевого разрыва после отправки команды и двух именованных сессий без смешения target.

## Документы проекта

- [`RISKY_OPERATION_CONTRACT.md`](RISKY_OPERATION_CONTRACT.md) — фактический машинный контракт версии 1;
- [`SSH_RELAY_CHANGE_PLAN.md`](SSH_RELAY_CHANGE_PLAN.md) — состояние этапов реализации и дальнейшие работы;
- [`AGENT_SAFE_INTEGRATION_FINDINGS.md`](AGENT_SAFE_INTEGRATION_FINDINGS.md) — выводы интеграции с `agent-safe`;
- [`UX_FINDINGS.md`](UX_FINDINGS.md) — состояние quoting- и UX-задач;
- [`IMPLEMENTATION_REPORT.md`](IMPLEMENTATION_REPORT.md) — отчёт о реализации P0;
- [`MANUAL_TEST_REPORT_0.6.0.md`](MANUAL_TEST_REPORT_0.6.0.md) — протокол краткой ручной проверки;
- [`GITHUB_CONNECTOR_WORKFLOW.md`](GITHUB_CONNECTOR_WORKFLOW.md) — регламент публикации через GitHub-коннектор.

## Безопасность

- SSH- и sudo-пароли не сохраняются на диск;
- токены session-файлов не выводятся пользователю;
- приватные ключи и passphrase не включаются в логи;
- host key проверяется через `known_hosts`;
- локальный сервер слушает только `127.0.0.1`;
- полный текст risky-команды не записывается в receipt;
- stdout и stderr могут содержать данные, которые вывела сама удалённая команда, поэтому команды с секретами в выводе недопустимы;
- receipt и session-файлы не должны попадать в Git;
- неподдерживаемые интерактивные сценарии следует явно отклонять, а не пытаться автоматизировать обходом ограничений.
