# ssh_relay

`ssh_relay.py` — локальный SSH-relay для выполнения коротких неинтерактивных команд через заранее открытую именованную SSH-сессию.

Пользователь вручную запускает daemon и проходит SSH-аутентификацию. После этого CLI-агент, в частности OpenCode, вызывает локальный relay без прямого `ssh` и повторного ввода SSH-пароля.

Текущая версия: `0.7.0`.

## Что добавлено в 0.7.0

- автоматическое восстановление SSH-соединения после разрыва без перезапуска локального daemon;
- SSH keepalive и фоновый контроль состояния транспорта;
- backoff повторных подключений `1, 2, 5, 10, 30` секунд;
- ожидание reconnect перед новой `exec`, `sudo-exec`, `download` или `upload`;
- запрет автоматического повтора уже начавшейся команды или передачи файла;
- состояния SSH `connected`, `reconnecting` и `disconnected` в `status` и `list`;
- явная версия машинного протокола `operation_protocol_version=1`, не связанная с minor-версией программы;
- автоматические тесты reconnect, P0-контракта, upload/download, двух именованных сессий и реального Paramiko через локальный `sshd`.

## Что добавлено в 0.6.0

- машиночитаемый режим `--json` для `exec` и `sudo-exec`;
- идентификаторы `transaction_id` и `receipt_id`;
- безопасный risky receipt без полной команды;
- `command_hash`, `receipt_hash` и цепочка `previous_receipt_hash`;
- отдельные результаты `command_failed`, `partial_success`, `not_started` и `unknown`;
- явные `--change-target` и `--change-description`;
- защита receipt-файла правами `0600` и отказ от записи через символическую ссылку;
- совместимость нового daemon с обычными запросами CLI 0.5.x;
- распознавание старой записи receipt 0.5.x как однократного начального anchor.

Новые команды `inspect`, `exec-script` и `sudo-exec-script` в эту версию не входят.

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
- последовательное выполнение и ограничения времени, вывода и размера файлов;
- автоматическое восстановление SSH между операциями без автоматического повтора уже начатой операции.

## Ограничения

Relay предназначен только для коротких неинтерактивных операций.

Не поддерживаются:

- интерактивный stdin;
- редакторы, интерактивные shell, `top`, `less`, `passwd`;
- команды с запросом пароля;
- длительные процессы и команды с большим выводом;
- параллельное выполнение команд;
- рекурсивная передача каталогов;
- `sudo-download` и `sudo-upload`;
- специальные файлы и SCP-режим.

Псевдотерминал не создаётся. Максимальный суммарный stdout/stderr команды — 4 МиБ. Если timeout или лимит вывода сработал после запуска команды, результат считается `unknown`: удалённое состояние могло измениться.

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

Ожидаемая версия:

```text
ssh_relay 0.7.0
```

## Подготовка known_hosts

Relay использует `RejectPolicy` и не принимает неизвестный host key автоматически.

`cmd.exe`:

```cmd
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
ssh-keyscan -H 198.51.100.42 >> "%USERPROFILE%\.ssh\known_hosts"
ssh-keygen -lf "%USERPROFILE%\.ssh\known_hosts"
```

PowerShell:

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

Daemon запрашивает sudo-пароль локально, проверяет его и хранит только в памяти процесса. Пароль не записывается в session-файл и не включается в протокол.

Если для SSH используется пароль или passphrase зашифрованного ключа, значение также остаётся только в памяти daemon до его остановки: оно требуется для автоматического reconnect. На диск эти данные не записываются и в session-файл не попадают.

Фоновый запуск по ключу:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519" --detach
```

`--detach` несовместим с `--ask-key-passphrase` и `--enable-sudo`. Detached daemon использует тот же механизм keepalive и reconnect.

### Автоматическое восстановление SSH

После успешного запуска daemon включает keepalive с интервалом 30 секунд и раз в секунду проверяет состояние Paramiko transport. Если SSH-транспорт перестал быть активным, локальный TCP-server и session-файл остаются рабочими, а daemon начинает reconnect.

Интервалы после неудачных попыток: `1, 2, 5, 10, 30` секунд; затем используется интервал 30 секунд до восстановления или ручного `stop`. При каждом новом SSH-подключении снова применяется тот же `known_hosts` и `RejectPolicy`.

Новая `exec`, `sudo-exec`, `download` или `upload`, поступившая во время восстановления, ждёт рабочий SSH до 30 секунд. Если соединение восстановилось, операция запускается ровно один раз. Если не восстановилось, операция считается достоверно не начатой.

Если SSH потерян уже после начала команды или передачи файла, relay не повторяет её автоматически. Для машинной команды результат становится `unknown`; для передачи файла выводится явная диагностика о неизвестном результате. Это защищает от повторного выполнения операции с побочным эффектом.

После замены `ssh_relay.py` работающий daemon обязательно нужно остановить и запустить заново. Старый session-файл не доказывает, что уже запущенный процесс использует новую реализацию.

## Диагностика перед работой

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "hostname && whoami && pwd"
```

Если relay уже запущен, агент не должен использовать прямой `ssh`.

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

При `partial_success` текстовый режим возвращает `1`, но явно сообщает, что основная команда уже выполнена и требуется проверка.

## Risky-операция и безопасный receipt

Пример:

```cmd
py ssh_relay.py exec --name prod --risky --change-target "/tmp/example" --change-description "Создание тестового каталога" "mkdir -p /tmp/example"
```

Путь по умолчанию:

```text
~/.local/state/agent-safe/changes.jsonl
```

Системный receipt для `sudo-exec`:

```cmd
py ssh_relay.py sudo-exec --name prod --risky --receipt-path "/var/lib/agent-safe/changes.jsonl" --change-target "systemd:nginx.service" --change-description "Перезапуск службы" "systemctl restart nginx"
```

Receipt содержит безопасные метаданные:

- `transaction_id` и `receipt_id`;
- имя сессии, host, port и SSH-пользователя;
- `change_target` и `change_description`;
- `command_hash`, но не полный текст команды;
- exit code основной команды;
- `previous_receipt_hash` и `receipt_hash`;
- UTC timestamp и версию relay.

Полный stdout/stderr в удалённый receipt не записывается.

Перед изменяющей командой relay проверяет последнюю запись журнала и отсутствие уже записанного `transaction_id`. Повтор подтверждённой транзакции блокируется до запуска основной команды. Повреждённый self-hash блокирует запуск основной команды. Последняя запись формата 0.5.x, содержащая `tool=ssh_relay`, `status=done` и старое поле `command`, принимается только как однократный legacy anchor: её канонический SHA-256 становится `previous_receipt_hash` первой записи 0.6.0.

При записи relay:

- использует `umask 077`;
- создаёт или переводит receipt-файл в режим `0600`;
- проверяет, что конечный путь не является символической ссылкой;
- после append повторно читает последнюю строку и проверяет `receipt_id` и self-hash.

Для системного receipt родительский каталог должен быть доверенным и недоступным для записи посторонним пользователям. Проверка конечной символической ссылки не устраняет гонки в недоверенном каталоге. Один daemon сериализует собственные операции, но сторонние параллельные писатели в тот же JSONL не поддерживаются и могут привести к `receipt_status=unknown`.

Полноценной криптографической подписи нет. Hash-цепочка обнаруживает часть случаев изменения или перестановки журнала, но не защищает от владельца файла, который может пересчитать всю цепочку.

## Машинный режим JSON

```cmd
py ssh_relay.py exec --name prod --json --risky --transaction-id "agent-safe:tx-001" --change-target "/tmp/example" --change-description "Создание тестового каталога" "mkdir -p /tmp/example"
```

В режиме `--json` stdout CLI содержит ровно один JSON-объект и перевод строки. Локальный stderr при штатно сформированном результате пуст. Удалённые stdout/stderr находятся в полях JSON.

Основные статусы:

- `succeeded` — команда успешна; для risky receipt подтверждён;
- `command_failed` — получен ненулевой удалённый exit code;
- `partial_success` — команда успешна, но receipt имеет `failed` или `unknown`;
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

После кода `12` или `13` агент должен остановить последующие изменяющие операции и выполнить read-only verify, поиск receipt по идентификатору либо rollback по правилам `agent-safe`.

## transaction_id

Параметр:

```text
--transaction-id ID
```

Допустимы 1–128 символов: латинские буквы, цифры, точка, двоеточие, дефис и подчёркивание. Первый символ — буква или цифра.

Если ID не передан, relay создаёт UUIDv4 и возвращает `transaction_id_source=relay`. Адаптер `agent-safe` должен передавать собственный ID заранее, чтобы он был известен даже при потере ответа.

## Совместимость машинного протокола

Машинный контракт имеет собственную версию:

```text
operation_protocol_version = 1
```

Она не выводится из `__version__` программы. Поэтому повышение `ssh_relay` с 0.6.x до 0.7.x не создаёт ложной несовместимости протокола.

- daemon 0.7.0 записывает `operation_protocol_version=1` в session-файл и возвращает его в `status`;
- CLI проверяет именно это поле перед `--risky` или `--json`;
- session-файлы daemon 0.6.x, созданные до появления отдельного поля, распознаются как legacy-реализация протокола v1;
- обычный нерискованный запрос остаётся совместимым со старым daemon без машинного протокола;
- запрос CLI 0.5.x к новому daemon по-прежнему получает ответ старого формата.

## download и upload

```cmd
py ssh_relay.py download --name prod "/var/log/app.log" ".\downloads\app.log" --create-dirs
py ssh_relay.py upload --name prod ".\config.json" "/tmp/config.json" --overwrite
```

Поддерживается только один обычный файл. Каталоги и специальные файлы отклоняются. Существующий файл не перезаписывается без `--overwrite`.

Локальный файл для upload читает CLI-процесс и передаёт содержимое daemon, поэтому рабочие каталоги CLI и daemon могут отличаться. Windows-style удалённые пути нормализуются для SFTP.

## status, list и stop

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py status --all
py ssh_relay.py list
py ssh_relay.py stop --name prod
py ssh_relay.py stop --all
```

`status` отдельно показывает состояние локального daemon и SSH-транспорта. Возможные значения SSH:

- `connected` — SSH готов к новой операции;
- `reconnecting` — выполняется попытка восстановления;
- `disconnected` — последняя попытка завершилась ошибкой, следующая будет выполнена после backoff;
- `stopping` — daemon завершается.

Во время reconnect локальный daemon остаётся доступным, а session-файл не удаляется. `status` возвращает ненулевой код, если SSH сейчас не `connected`, чтобы агент не принял живой локальный daemon за готовый удалённый канал.

`stop` завершает daemon через токен и не посылает сигнал по PID из session-файла, поэтому устаревший PID не используется для завершения постороннего процесса. Если запрос уже отправлен, но ответ потерян, session-файл сохраняется: relay не объявляет daemon остановленным или неактивным без подтверждения.

## Session-файлы

Расположение:

```text
Windows: %LOCALAPPDATA%\ssh_relay\sessions\<name>.json
Linux:   ${XDG_STATE_HOME:-~/.local/state}/ssh_relay/sessions/<name>.json
```

Session-файл содержит локальный токен доступа. Он не содержит SSH-пароль, sudo-пароль, passphrase или приватный ключ.

На POSIX каталоги создаются с правами `0700`, session-файлы — `0600`. Session-файлы нельзя добавлять в Git или передавать недоверенному процессу.

## Инструкция для OpenCode

```text
Удалённый сервер prod доступен через уже запущенный локальный SSH relay.
Не используй прямой ssh и не запрашивай пароль.

Перед работой выполни:
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "hostname && whoami && pwd"

Если status сообщает `reconnecting` или `disconnected`, не используй прямой ssh: daemon продолжает автоматическое восстановление. Если relay сообщает `unknown`, не повторяй изменяющую команду автоматически — сначала проверь фактическое состояние.

Обычная команда:
py ssh_relay.py exec --name prod "<remote-command>"

Изменяющая команда:
py ssh_relay.py exec --name prod --json --risky --transaction-id "<transaction-id>" --change-target "<target>" --change-description "<description>" "<remote-command>"

Изменяющая root-команда:
py ssh_relay.py sudo-exec --name prod --json --risky --receipt-path "/var/lib/agent-safe/changes.jsonl" --transaction-id "<transaction-id>" --change-target "<target>" --change-description "<description>" "<remote-command>"

Не запускай интерактивные команды, запросы пароля, длительные процессы и команды с большим выводом.
После process code 12 или 13 не продолжай изменяющие операции без verify или rollback.
```

## Безопасность

- SSH-пароль, passphrase и sudo-пароль не сохраняются на диск;
- токен session-файла не выводится в штатной диагностике;
- daemon слушает только `127.0.0.1`;
- каждый запрос проверяет токен;
- неизвестный host key не принимается автоматически;
- полная risky-команда не сохраняется в receipt;
- секреты всё равно нельзя передавать в командной строке: их может раскрыть локальный список процессов, shell history, удалённый процесс или перебор низкоэнтропийного значения по `command_hash`;
- сам удалённый stdout/stderr может содержать секрет по воле команды, поэтому агент не должен запускать команды, выводящие секреты;
- `change-target` и `change-description` считаются безопасными метаданными и не должны содержать секреты;
- session-файл sudo-сессии фактически даёт доступ к разрешённому `sudo-exec` через daemon и требует усиленной защиты;
- возможность произвольного удалённого выполнения нельзя расширять без оценки угроз.

## Автоматические проверки без внешнего SSH-сервера

Из корня репозитория:

```cmd
py -m py_compile ssh_relay.py tests\test_machine_protocol.py tests\test_local_tcp_protocol.py tests\test_reconnect_protocol.py tests\test_transfer_protocol.py tests\test_multi_session_protocol.py tests\test_real_ssh_reconnect.py
py tests\test_machine_protocol.py
py tests\test_local_tcp_protocol.py
py tests\test_reconnect_protocol.py
py tests\test_transfer_protocol.py
py tests\test_multi_session_protocol.py
```

Покрытие:

- `test_machine_protocol.py` — матрица P0-статусов, коды процесса, безопасный receipt, self-hash, legacy anchor, duplicate `transaction_id` и независимая версия машинного протокола;
- `test_local_tcp_protocol.py` — настоящий локальный TCP daemon с подменённым SSH-транспортом, legacy-запрос, JSON-протокол и `stop`;
- `test_reconnect_protocol.py` — reconnect до операции, timeout ожидания, обрыв во время команды, границы reconnect × risky receipt, отсутствие автоматического retry и обрывы upload/download;
- `test_transfer_protocol.py` — overwrite файла того же размера, SHA-256 содержимого, Windows/POSIX paths, создание каталогов и cleanup временных файлов;
- `test_multi_session_protocol.py` — две именованные сессии, независимый reconnect, target и lifecycle.

Эти тесты не требуют `paramiko` и не используют внешний сервер: транспорт моделируется, а локальный TCP-протокол daemon остаётся настоящим.

## Автоматическая проверка с реальным Paramiko

`.github/workflows/tests.yml` запускает отдельный Ubuntu job с локальным `sshd` и временным TCP fault-proxy. Внешний VPS, пароль пользователя или GitHub secret для этого не требуются.

`test_real_ssh_reconnect.py` автоматически проверяет:

- реальный Paramiko и `known_hosts`;
- обычную команду и ожидание reconnect;
- запрет повторного запуска уже начатой команды после обрыва;
- реальный risky receipt, duplicate `transaction_id` и повреждённый журнал;
- реальный `partial_success` при ошибке записи receipt;
- `sudo-exec --risky` через тестового локального пользователя;
- реальный SFTP upload/download.

Без переменной `SSH_RELAY_REAL_SSH_TEST=1` этот тест завершается как пропущенный, поэтому обычный локальный прогон не требует настройки `sshd`.

## Минимальная проверка с сервером

После замены файла сначала перезапустите daemon.

```cmd
py ssh_relay.py stop --name prod
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod --json "printf protocol-ok"
py ssh_relay.py exec --name prod --json "sh -c 'printf stdout-ok; printf stderr-ok >&2; exit 7'"
py ssh_relay.py exec --name prod --json --risky --transaction-id "manual:test-001" --change-target "/tmp/ssh-relay-risky-test.txt" --change-description "Создание тестового файла" "touch /tmp/ssh-relay-risky-test.txt"
py ssh_relay.py exec --name prod "tail -n 1 ~/.local/state/agent-safe/changes.jsonl"
```

Для `cmd.exe` проверяйте exit code в той же строке:

```cmd
cmd /V:ON /C "py ssh_relay.py exec --name prod --json ^"sh -c 'exit 7'^" & echo Exit code: !ERRORLEVEL!"
```

PowerShell использует `$LASTEXITCODE`.

В Far Manager не проверяйте `%ERRORLEVEL%` отдельной следующей командой: она может показать код уже другого процесса.

## Что остаётся вне автоматического покрытия

Автоматический CI не моделирует длительные сетевые деградации, изменение host key в эксплуатации, реальные ограничения конкретного `sudoers` и работу с внешним VPS. Эти сценарии не должны становиться обязательной ручной процедурой для каждого изменения; при необходимости они выполняются как отдельные стендовые проверки.
