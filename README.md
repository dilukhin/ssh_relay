# ssh_relay

`ssh_relay.py` — локальный SSH-relay для коротких неинтерактивных команд, управляемых длительных удалённых задач и контролируемых передач файлов через заранее открытую именованную SSH-сессию.

Пользователь вручную запускает `daemon` и проходит SSH-аутентификацию. После этого CLI-агент, в частности OpenCode, работает только через локальный relay: `exec`/`sudo-exec` для коротких команд, `job` для длительных удалённых процессов и `upload`/`download` для SFTP-передач. Прямой `ssh` агенту не нужен.

Текущая версия: `0.9.0`.

Внутренняя структура:

* `ssh_relay.py` — основной CLI и фактическая версия;
* `ssh_relay_core.py` — daemon, reconnect, SFTP и базовый протокол коротких команд;
* `ssh_relay_session.py` — защита локальной регистрации живого daemon, безопасный retry `status`, подключение machine/receipt-слоёв и редукция известных relay-секретов в диагностике зависимостей;
* `ssh_relay_outcomes.py` — структурированные границы `not_started`/`unknown` и базовый JSON-контракт коротких команд;
* `ssh_relay_receipts.py` — safe risky receipt v1 и корреляция транзакций;
* `ssh_relay_p0_contract.py` — единый machine result для risky-команд, включая `partial_success`;
* `ssh_relay_jobs.py` — протокол управляемых длительных удалённых задач;
* `ssh_relay_transfers.py` — прогресс, чанки, таймауты и безопасные частичные файлы для длительных передач.

Каноническая спецификация машинного интерфейса находится в `MACHINE_CONTRACT.md`.

## Возможности

* именованные SSH-сессии с парольной или key/certificate-аутентификацией;
* обязательная проверка host key через доверенный `known_hosts`;
* `exec` для коротких команд с раздельными stdout/stderr и исходным exit code;
* `sudo-exec` при явном `daemon --enable-sudo`;
* `exec --json` и `sudo-exec --json` с машинно различимыми исходами `succeeded`, `not_started`, `command_failed`, `partial_success`, `unknown`;
* safe `--risky` receipt без полного текста команды, stdout/stderr и session token;
* `transaction_id`, заранее созданный `receipt_id`, `command_hash` и `receipt_hash` для risky-корреляции;
* `job start/status/tail/wait/stop/list` для длительных неинтерактивных процессов;
* `download` и `upload` одного обычного файла через SFTP;
* наблюдаемый прогресс длительной передачи без чтения всего файла в память;
* общий аварийный timeout и отдельный timeout отсутствия прогресса;
* безопасные `.ssh-relay.part`, которые не выглядят как готовый файл;
* SSH keepalive и автоматический reconnect с backoff `1, 2, 5, 10, 30` секунд;
* сохранение session-файла при неоднозначной локальной ошибке и восстановление исчезнувшей регистрации живым daemon;
* локальный TCP-server только на `127.0.0.1` и токен сессии;
* `status`, `status --all`, `list`, `stop`, `stop --all`.

## Ключевое различие: `job` и file transfer

`job` управляет удалённым процессом. После подтверждённого старта такой процесс может продолжать работать независимо от управляющего SSH-канала. Поэтому сборки, CTest и другие длительные процессы запускаются через `job`.

`upload`/`download` — это SFTP-передача между локальной и удалённой сторонами. Она зависит от SSH/SFTP и **не является `job`**. Для неё relay использует собственную модель прогресса, таймаутов и частичных файлов.

Не превращайте SFTP-передачу в удалённый shell-процесс ради унификации.

## Ограничения

`exec` и `sudo-exec` предназначены только для коротких неинтерактивных команд. Не поддерживаются PTY, интерактивный stdin, редакторы, shell, `top`, `less`, `passwd`, повторные запросы пароля и потенциально большой вывод. Вывод ограничен 4 МиБ, время — `--command-timeout`.

`job` поддерживает длительные **неинтерактивные** процессы на Linux/Ubuntu. Команды, которые самостоятельно daemonize и покидают process group job, не поддерживаются. Встроенный sudo-пароль daemon для long-job не используется.

`upload`/`download` работают только с обычными файлами, доступными текущему SSH-пользователю. Не поддерживаются каталоги, рекурсивная передача, специальные файлы, `sudo-upload` и `sudo-download`.

Автоматическое resume в `0.9.0` **не поддерживается**. Размер частичного файла сам по себе недостаточен для доказательства, что это префикс того же исходного файла. Relay не имитирует безопасное resume без надёжной проверки идентичности данных.

Machine JSON и safe receipt реализованы только для коротких `exec`/`sudo-exec`. Они не расширяют поддержку интерактивного stdin/PTY и не добавляют автоматический retry. `operation_status=unknown` и `partial_success` запрещено автоматически повторять.

## Требования

Локально:

* Windows, PowerShell или `cmd.exe`;
* Python 3.12+;
* `paramiko`.

Удалённо:

* Linux/Ubuntu и SSH;
* POSIX shell;
* для `job`: Linux `/proc`, `setsid`, `base64`, `date`, `stat`, `tail`, `wc`;
* для `sudo-exec`: право выполнять нужные команды через `sudo`.

Установка зависимости:

```powershell
py -m pip install paramiko
```

## known_hosts

Relay не принимает неизвестный host key автоматически. До запуска добавьте проверенный ключ сервера в `%USERPROFILE%\.ssh\known_hosts` либо передайте отдельный файл через `--known-hosts`.

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keyscan -H 198.51.100.42 | Out-File -Append -Encoding ascii "$env:USERPROFILE\.ssh\known_hosts"
ssh-keygen -lf "$env:USERPROFILE\.ssh\known_hosts"
```

Fingerprint, полученный через сеть, нужно сверить по доверенному каналу.

## Команды

```text
py .\ssh_relay.py daemon [--name NAME] --host HOST --user USER [--port PORT] [-i PATH] [--ask-key-passphrase] [--known-hosts PATH] [--command-timeout SECONDS] [--download-timeout SECONDS] [--download-max-size SIZE] [--upload-timeout SECONDS] [--upload-max-size SIZE] [--enable-sudo] [--detach] [--detach-log PATH]
py .\ssh_relay.py exec [--name NAME] [--json] [--risky] [--receipt-path REMOTE_JSONL] [--transaction-id ID] [--change-target TEXT] [--change-description TEXT] "COMMAND"
py .\ssh_relay.py sudo-exec [--name NAME] [--json] [--risky] [--receipt-path REMOTE_JSONL] [--transaction-id ID] [--change-target TEXT] [--change-description TEXT] "COMMAND"
py .\ssh_relay.py job start [--name NAME] --job JOB "COMMAND"
py .\ssh_relay.py job status [--name NAME] --job JOB
py .\ssh_relay.py job tail [--name NAME] --job JOB [--lines N] [--bytes SIZE]
py .\ssh_relay.py job wait [--name NAME] --job JOB [--poll-interval SECONDS] [--timeout SECONDS]
py .\ssh_relay.py job stop [--name NAME] --job JOB [--grace SECONDS] [--force]
py .\ssh_relay.py job list [--name NAME]
py .\ssh_relay.py download [--name NAME] [--overwrite] [--create-dirs] [--idle-timeout SECONDS] [--discard-partial] REMOTE_PATH LOCAL_PATH
py .\ssh_relay.py upload [--name NAME] [--overwrite] [--create-dirs] [--idle-timeout SECONDS] [--discard-partial] LOCAL_PATH REMOTE_PATH
py .\ssh_relay.py status [--name NAME] [--all]
py .\ssh_relay.py stop [--name NAME] [--all]
py .\ssh_relay.py list
```

## daemon и именованные сессии

Обычный запуск:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
```

По ключу:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519"
```

Эквивалент для `cmd.exe`:

```cmd
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519"
```

Для зашифрованного ключа используйте `--ask-key-passphrase`. Пароль, passphrase и sudo-пароль не записываются в session-файл. При reconnect необходимые секреты остаются только в памяти daemon до его остановки. Начиная с `0.8.2`, если SSH-библиотека включает известный relay пароль или passphrase в текст исключения, relay заменяет этот секрет на `[СКРЫТО]` перед выводом диагностики; то же правило применяется к sudo-паролю при исключениях выполнения sudo-команды.

`--detach` доступен только с `--identity-file` без интерактивного passphrase и без `--enable-sudo`.

После обновления daemon старую активную сессию нужно остановить и запустить заново. Клиент `0.9.0` откажется запускать протокол длительных `upload`/`download` через daemon старее `0.8.0`, чтобы старый daemon не интерпретировал служебные чанки как обычную передачу. Перед risky-командой клиент также требует capability safe receipt v1; старый daemon без capability получает только read-only `status`, а пользовательская risky-команда не отправляется.

## Автоматический reconnect

Daemon сохраняет локальный TCP-server и session-файл при разрыве SSH. Повторные попытки выполняются с backoff `1, 2, 5, 10, 30` секунд, затем каждые 30 секунд. Host key проверяется заново тем же `known_hosts`.

Начиная с `0.8.1`, временная недоступность локального control-plane сама по себе не удаляет session-файл. Read-only запрос `status` безопасно повторяется до трёх попыток с короткими задержками; `exec`, `sudo-exec`, `upload`, `download` и `stop` автоматически не повторяются, потому что после потери ответа результат операции может быть неизвестен.

Живой daemon контролирует собственный session-файл. Если файл отсутствует, daemon сначала полностью записывает и закрывает временный файл, затем атомарно публикует его через hard link без перезаписи существующего имени. Поэтому другой daemon с тем же именем и новым токеном не должен быть затронут.

Если SSH недоступен **до начала** новой операции, relay может ждать восстановления. Если разрыв произошёл **после начала** команды или чанка передачи, relay не считает его подтверждённым автоматически.

Для `job start` после неопределённого результата сначала проверяйте `job status`/`job list`. Для передачи сначала проверяйте фактический частичный файл и только затем решайте, как продолжать.

## exec и sudo-exec

```powershell
py .\ssh_relay.py exec --name prod "hostname && whoami && pwd"
```

Sudo-режим включается только вручную при старте daemon:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
py .\ssh_relay.py sudo-exec --name prod "whoami"
```

Для короткой изменяющей команды используется safe `--risky` receipt v1:

```powershell
py .\ssh_relay.py exec --name prod --risky --transaction-id deploy-001 --change-target "~/work/app" --change-description "создан каталог приложения" "mkdir -p ~/work/app"
```

При подтверждённом exit code `0` receipt пишется в `~/.local/state/agent-safe/changes.jsonl` либо путь из `--receipt-path`. Receipt содержит hash команды и корреляционные идентификаторы, но не содержит полный текст команды, stdout/stderr, session token, SSH/sudo-пароли и приватные ключи.

Повторный `transaction_id` не делает команду идемпотентной: writer отклоняет duplicate receipt. Если команда уже успела успешно выполниться, итог является `partial_success`, и повторять команду автоматически нельзя.

### Машинный режим 0.9

Для внешнего агента используйте `--json`:

```powershell
py .\ssh_relay.py exec --name prod --json "hostname"
py .\ssh_relay.py sudo-exec --name prod --json "whoami"
py .\ssh_relay.py exec --name prod --json --risky --transaction-id deploy-001 --change-target "/etc/app.conf" --change-description "обновлена конфигурация" "true"
```

Process exit code машинного режима:

* `0` — `succeeded`;
* `10` — `not_started`;
* `11` — `command_failed`;
* `12` — `partial_success`: команда завершилась успешно, но receipt failed/unknown;
* `13` — `unknown`: команда могла быть запущена, но достоверный результат потерян.

Remote exit code хранится отдельно в `command_exit_code`. Полный текст команды в JSON не включается. Для risky-команды `transaction_id` и `receipt_id` создаются до изменяющего запроса, поэтому они остаются доступны даже при неизвестном результате.

`operation_status=unknown` и `partial_success` нельзя автоматически retry. Полная схема, failure matrix, `receipt_status` и правила hash описаны в `MACHINE_CONTRACT.md`.

## job — длительные удалённые процессы

Типичный цикл:

```powershell
py .\ssh_relay.py job start --name prod --job build-app "cd ~/src/app && cmake --build build -j2"
py .\ssh_relay.py job status --name prod --job build-app
py .\ssh_relay.py job tail --name prod --job build-app
py .\ssh_relay.py job wait --name prod --job build-app --poll-interval 5 --timeout 7200
```

`job start` подтверждает запуск механизма job, а не успешное завершение команды. `job wait` локально опрашивает короткий status. Локальный timeout `job wait` не останавливает удалённую задачу. `job tail` возвращает только ограниченный хвост журнала.

Остановка сначала мягкая; `--force` — отдельная явная ступень:

```powershell
py .\ssh_relay.py job stop --name prod --job build-app
py .\ssh_relay.py job stop --name prod --job build-app --force
```

## Длительные download/upload

### Прогресс

Передача разбивается на последовательные чанки по 1 МиБ. После подтверждённого роста количества байтов CLI печатает прогресс не чаще примерно одного раза в секунду и всегда печатает начало/завершение:

```text
Прогресс: transferred_bytes=4194304 total_bytes=16777216 percent=25.0 elapsed=3.2s speed=1310720.0B/s
```

Поля:

* `transferred_bytes` — подтверждённое количество переданных байтов;
* `total_bytes` — известный размер файла;
* `percent` — процент от `0.0` до `100.0`;
* `elapsed` — время текущего вызова передачи;
* `speed` — средняя скорость по подтверждённым байтам.

Рост `transferred_bytes`/`percent` означает, что передача продвигается. Отсутствие текстового вывода не используется как признак зависания.

`upload` читает локальный файл чанками, а не целиком в память. В локальном JSON-запросе одновременно находится только текущий чанк.

### Два разных timeout

Сохраняется прежний смысл daemon-параметров:

* `--download-timeout` — общий аварийный предел всего download;
* `--upload-timeout` — общий аварийный предел всего upload.

Они по-прежнему задаются при старте daemon и **не меняют смысл** в `0.9.0`.

Дополнительно у `download` и `upload` есть `--idle-timeout`, по умолчанию 60 секунд. Это максимальный интервал без подтверждения одного сетевого шага/чанка. Пока чанки подтверждаются быстрее этого интервала, transfer считается живым. Общий аварийный предел при этом всё равно действует.

Пример большого download с большим общим пределом:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --download-timeout 7200 --download-max-size 4G
py .\ssh_relay.py download --name prod "/srv/archive.bin" ".\archive.bin" --idle-timeout 90
```

Пример upload:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --upload-timeout 7200 --upload-max-size 4G
py .\ssh_relay.py upload --name prod ".\archive.bin" "/srv/incoming/archive.bin" --idle-timeout 90
```

В `cmd.exe` параметры те же:

```cmd
py .\ssh_relay.py upload --name prod ".\archive.bin" "/srv/incoming/archive.bin" --idle-timeout 90
```

### Частичный download

Download пишет рядом с целевым локальным файлом детерминированный временный файл:

```text
.<имя>.ssh-relay.part
```

Готовый целевой файл не появляется до получения всех байтов, `fsync`, проверки размера и финального `os.replace`. При сбое partial сохраняется для диагностики и не выглядит как готовый результат.

Если следующий вызов видит partial, он сначала получает актуальный размер удалённого файла и сообщает оба размера. Автоматическое resume не выполняется. После проверки можно явно удалить partial и начать заново:

```powershell
py .\ssh_relay.py download --name prod "/srv/archive.bin" ".\archive.bin" --discard-partial
```

Если удалённый размер или `mtime` меняется во время одного download, финализация запрещается.

### Частичный upload

Upload использует рядом с удалённым целевым путём:

```text
.<имя>.ssh-relay.part
```

Каждый следующий чанк разрешён только если фактический размер partial совпадает с ожидаемым offset. После полной передачи размер проверяется ещё раз. Если целевого файла не было, partial переименовывается в него только после успешной проверки.

При `--overwrite` существующий готовый файл не удаляется заранее. Для его замены требуется поддержка SFTP `posix-rename`; если безопасная замена недоступна или завершается ошибкой, старый готовый файл сохраняется, а partial остаётся для диагностики.

При повторном вызове существующий partial сначала обнаруживается и его размер показывается. Автоматическое resume не выполняется. Для явного перезапуска:

```powershell
py .\ssh_relay.py upload --name prod ".\archive.bin" "/srv/incoming/archive.bin" --overwrite --discard-partial
```

`probe` не создаёт удалённые каталоги даже при `--create-dirs`; создание начинается только на подтверждённой фазе `begin`.

### Обрыв SSH/SFTP

Обрыв текущего чанка не считается успехом. Relay не повторяет изменяющий upload-чанк вслепую: сервер мог принять данные до потери подтверждения.

После reconnect следующий запуск сначала делает `probe` и сравнивает фактическое состояние готового и частичного файла. Поскольку безопасное автоматическое resume пока не доказано, найденный partial требует явного решения пользователя/агента: сохранить для проверки либо удалить через `--discard-partial` и начать заново.

`--overwrite` при этом не обходится.

## Размеры

Сохраняются лимиты `--download-max-size` и `--upload-max-size`. Размер задаётся числом байт либо с суффиксом `K`, `M`, `G`.

Большой лимит следует выставлять осознанно. Relay по-прежнему передаёт только один файл за вызов и не поддерживает рекурсивную передачу.

## agent-safe

Safe receipt v1 относится только к коротким `exec`/`sudo-exec`. `upload` также меняет удалённое состояние, но для transfer в `0.9.0` не создаётся новый несовместимый lifecycle receipt и `--risky` к `upload` не добавляется.

Это явное ограничение: если для file transfer потребуется lifecycle receipt, его нужно проектировать отдельно с состояниями начала/завершения/ошибки и стабильным transfer ID. До этого upload остаётся существующей явной изменяющей командой relay без agent-safe receipt.

`download` обычно не меняет удалённое состояние; локальный overwrite по-прежнему требует `--overwrite`.

Внешний агент должен интерпретировать machine result по полям и process exit code, а не разбирать русский `error_message`. Для `partial_success`/`unknown` повтор risky-команды запрещён до read-only проверки состояния.

## status, list, stop

```powershell
py .\ssh_relay.py status --name prod
py .\ssh_relay.py status --all
py .\ssh_relay.py list
py .\ssh_relay.py stop --name prod
py .\ssh_relay.py stop --all
```

`stop` завершает daemon только через аутентифицированный локальный запрос и токен, а не по PID из session-файла. Если ответ `stop` потерян или daemon временно не отвечает, session-файл сохраняется: отсутствие ответа не считается доказательством завершения процесса.

Session-файлы:

* Windows: `%LOCALAPPDATA%\ssh_relay\sessions\<name>.json`;
* Linux: `${XDG_STATE_HOME:-~/.local/state}/ssh_relay/sessions/<name>.json`.

На Linux каталоги состояния создаются с `0700`, session-файлы — `0600`. Токен чувствителен и не должен попадать в Git, логи или недоверенные процессы.

## Использование с OpenCode

Авторитетная инструкция находится в `opencode/skills/ssh-relay/SKILL.md`. Основной порядок:

```text
1. Не используй прямой ssh.
2. status --name <session>.
3. Короткая диагностика: exec "hostname && whoami && pwd".
4. Короткие команды — exec/sudo-exec; для машинной интеграции — --json.
5. После partial_success/unknown не повторяй risky-команду автоматически.
6. Длительные удалённые процессы — job.
7. Большие upload/download — собственный transfer-механизм, не job.
8. Рост байтов/процента означает живую передачу.
9. Idle timeout и общий timeout — разные причины остановки.
10. После обрыва не повторяй upload/download вслепую; сначала проверь partial.
11. Не раскрывай секреты в командах, выводе, логах и receipts.
```

## Безопасность

* SSH-пароль, passphrase и sudo-пароль не сохраняются на диск и не выводятся в логи; известные relay-секреты редактируются из текста исключений зависимостей перед диагностическим выводом.
* Приватный ключ не копируется в session-файл.
* Session token не выводится в CLI; защищайте session-файл.
* `known_hosts` обязателен; неизвестный host key автоматически не принимается.
* Все локальные relay-запросы идут только на `127.0.0.1` и проверяются по токену.
* Safe risky receipt не хранит полный текст команды, stdout/stderr, session token, SSH/sudo-пароль или приватный ключ.
* `transaction_id`, `change_target` и `change_description` не должны содержать секреты.
* Receipt writer использует `umask 077`, права `0600`, проверку final symlink и типа файла. Portable POSIX shell не устраняет полностью symlink TOCTOU, поэтому parent directory receipt должен быть доверенным и недоступным для записи посторонним пользователям.
* `download` не заменяет готовый локальный файл без `--overwrite`.
* `upload` не заменяет готовый удалённый файл без `--overwrite`.
* Частичный файл никогда не выдаётся за готовый результат.
* Возможность выполнения произвольной shell-команды — назначение relay; её не следует расширять без оценки угроз.

## Проверка

Без SSH-сервера:

```powershell
py -m py_compile .\ssh_relay.py .\ssh_relay_core.py .\ssh_relay_session.py .\ssh_relay_outcomes.py .\ssh_relay_receipts.py .\ssh_relay_p0_contract.py .\ssh_relay_jobs.py .\ssh_relay_transfers.py
py -m unittest discover -s .\tests -v
py .\ssh_relay.py --version
py .\ssh_relay.py --help
py .\ssh_relay.py exec --help
py .\ssh_relay.py sudo-exec --help
py .\ssh_relay.py download --help
py .\ssh_relay.py upload --help
py .\ssh_relay.py job --help
```

Перед ручным тестом изменённого daemon старую сессию обязательно остановите и запустите заново.

Минимальная проверка machine-mode после перезапуска daemon:

```powershell
py .\ssh_relay.py status --name prod
py .\ssh_relay.py exec --name prod --json "hostname && whoami && pwd"
py .\ssh_relay.py exec --name prod --json --risky --transaction-id relay-test-001 --change-description "тестовая безопасная операция" "true"
$LASTEXITCODE
```

Минимальный transfer-тест:

```powershell
Set-Content -NoNewline .\relay-transfer-test.txt "transfer-ok"
py .\ssh_relay.py upload --name prod ".\relay-transfer-test.txt" "/tmp/relay-transfer-test.txt" --overwrite
py .\ssh_relay.py download --name prod "/tmp/relay-transfer-test.txt" ".\relay-transfer-test.downloaded.txt" --overwrite
Get-Content .\relay-transfer-test.downloaded.txt
$LASTEXITCODE
```

PowerShell проверяет код через `$LASTEXITCODE`.

В `cmd.exe` код проверяйте через `%ERRORLEVEL%` в том же `cmd.exe`. В Far Manager не полагайтесь на отдельную следующую команду `echo %ERRORLEVEL%`, потому что она может выполняться в другом экземпляре командного процессора.

## Дальнейшие доработки

* read-only диагностика risky transaction/receipt для последующей проверки `unknown`;
* доказуемое resume с проверкой идентичности источника/partial;
* lifecycle receipts agent-safe для long-job и transfer;
* отдельная безопасная архитектура длительных sudo-job;
* рекурсивная передача каталогов только после отдельной оценки лимитов и угроз;
* дополнительные fault-injection тесты обрывов SFTP.
