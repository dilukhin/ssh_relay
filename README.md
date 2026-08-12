# ssh_relay

`ssh_relay.py` — локальный SSH-relay для коротких неинтерактивных команд и управляемых длительных задач через заранее открытые именованные SSH-сессии.

Пользователь вручную запускает `daemon` и проходит SSH-аутентификацию. После этого CLI-агент, в частности OpenCode, использует локальный relay: `exec`/`sudo-exec` для коротких команд и `job` для сборок, CTest, интеграционных тестов и других длительных неинтерактивных процессов. Прямой `ssh` агенту не нужен.

Текущая версия: `0.7.0`.

Внутренняя структура: `ssh_relay.py` остаётся основным CLI и задаёт фактическую версию; существующая реализация daemon/reconnect/SFTP/risky вынесена без функциональных изменений в `ssh_relay_core.py`, а удалённый протокол длительных задач изолирован в `ssh_relay_jobs.py`. `ssh_relay_core.py` не является самостоятельной точкой входа.

## Возможности

* именованные SSH-сессии с парольной или key/certificate-аутентификацией;
* проверка host key только через доверенный `known_hosts`;
* `exec` для коротких неинтерактивных команд с раздельными stdout/stderr и исходным exit code;
* `sudo-exec` при явном `daemon --enable-sudo`;
* `job start/status/tail/wait/stop/list` для управляемых длительных задач;
* `download` и `upload` одного обычного файла через SFTP;
* SSH keepalive и автоматический reconnect с backoff `1, 2, 5, 10, 30` секунд;
* локальный TCP-server только на `127.0.0.1` и токен сессии;
* `status`, `status --all`, `list`, `stop`, `stop --all`;
* detached daemon по SSH-ключу без интерактивных запросов;
* существующий `--risky`/agent-safe receipt для коротких `exec`/`sudo-exec`.

## Ограничения

`exec` и `sudo-exec` предназначены только для коротких команд. Они не поддерживают интерактивный stdin, PTY, редакторы, shell, `top`, `less`, `passwd`, повторные запросы пароля, длительные процессы и потенциально большой вывод. Вывод короткой команды ограничен 4 МиБ, время — `--command-timeout`.

`job` поддерживает длительные **неинтерактивные** процессы на Linux/Ubuntu. Встроенный sudo-пароль daemon для long-job на этапе 1 не используется. `job tail` читает только ограниченный хвост журнала.
Команды, которые сами отделяются в новую session/process group или иным способом daemonize за пределы группы job, не поддерживаются: relay не сможет надёжно отслеживать и останавливать такие потомки.

`download`/`upload` работают только с обычными файлами, доступными текущему SSH-пользователю. Каталоги, рекурсивная передача, специальные файлы, `sudo-download` и `sudo-upload` не поддерживаются.

Ключевое правило для reconnect и long-job:

```text
таймаут или обрыв управляющего транспорта != завершение удалённого процесса
```

Если результат уже начатой операции неизвестен, relay не повторяет её автоматически. Для `job start` после reconnect сначала проверяйте `job status`/`job list`, а не запускайте вторую копию.

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
py .\ssh_relay.py exec [--name NAME] [--risky] [--receipt-path REMOTE_JSONL] "COMMAND"
py .\ssh_relay.py sudo-exec [--name NAME] [--risky] [--receipt-path REMOTE_JSONL] "COMMAND"
py .\ssh_relay.py job start [--name NAME] --job JOB "COMMAND"
py .\ssh_relay.py job status [--name NAME] --job JOB
py .\ssh_relay.py job tail [--name NAME] --job JOB [--lines N] [--bytes SIZE]
py .\ssh_relay.py job wait [--name NAME] --job JOB [--poll-interval SECONDS] [--timeout SECONDS]
py .\ssh_relay.py job stop [--name NAME] --job JOB [--grace SECONDS] [--force]
py .\ssh_relay.py job list [--name NAME]
py .\ssh_relay.py download [--name NAME] [--overwrite] [--create-dirs] REMOTE_PATH LOCAL_PATH
py .\ssh_relay.py upload [--name NAME] [--overwrite] [--create-dirs] LOCAL_PATH REMOTE_PATH
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

Для зашифрованного ключа используйте `--ask-key-passphrase`. Пароль, passphrase и sudo-пароль не записываются в session-файл. При reconnect необходимые секреты остаются только в памяти daemon до его остановки.

`--detach` доступен только с `--identity-file` без интерактивного passphrase и без `--enable-sudo`:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519" --detach
```

Пример рабочего daemon:

```text
SSH-соединение установлено: donpedro@198.51.100.42:22
Имя сессии: prod
Relay слушает локальный адрес 127.0.0.1:54321
Файл сессии: C:\Users\User\AppData\Local\ssh_relay\sessions\prod.json
Режим sudo: выключен
Автовосстановление SSH: включено, ожидание запроса до 30 с, keepalive 30 с
Для завершения нажмите Ctrl+C или выполните команду: stop --name prod
```

Имя сессии: 1–64 символа `[A-Za-z0-9_.-]`. `default` используется, если `--name` не задан.

## Автоматический reconnect

Daemon сохраняет локальный TCP-server и session-файл при разрыве SSH. Повторные попытки выполняются с backoff `1, 2, 5, 10, 30` секунд, затем каждые 30 секунд. Host key проверяется заново тем же `known_hosts`.

Если SSH недоступен **до начала** новой операции, relay может ждать восстановления до 30 секунд. Если разрыв произошёл **после начала**, операция не повторяется: сервер мог успеть выполнить её полностью или частично.

`status` различает живой daemon и состояние SSH. Разрыв только удалённого SSH не удаляет session-файл.

## exec и sudo-exec

Короткие команды:

```powershell
py .\ssh_relay.py exec --name prod "hostname && whoami && pwd"
```

Sudo-режим включается только вручную при старте daemon:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
py .\ssh_relay.py sudo-exec --name prod "whoami"
```

Sudo-пароль передаётся только из памяти daemon во внутренний stdin `sudo -S`; `sudo-exec` остаётся неинтерактивным и предназначен для коротких команд.

Для короткой изменяющей команды сохраняется `--risky`:

```powershell
py .\ssh_relay.py exec --name prod --risky "mkdir -p ~/work/app"
```

При exit code `0` receipt пишется в `~/.local/state/agent-safe/changes.jsonl` либо путь из `--receipt-path`. Ненулевой exit code финальный receipt не создаёт.

## job — длительные задачи

### Жизненный цикл

```powershell
py .\ssh_relay.py job start --name prod --job build-app "cd ~/src/app && cmake --build build -j2"
py .\ssh_relay.py job status --name prod --job build-app
py .\ssh_relay.py job tail --name prod --job build-app
py .\ssh_relay.py job wait --name prod --job build-app --poll-interval 5 --timeout 7200
py .\ssh_relay.py job stop --name prod --job build-app
py .\ssh_relay.py job list --name prod
```

CTest:

```powershell
py .\ssh_relay.py job start --name prod --job ctest-app "cd ~/src/app/build && ctest --output-on-failure"
py .\ssh_relay.py job wait --name prod --job ctest-app --timeout 3600
```

### start

`--job` принимает 1–64 символа `[A-Za-z0-9_.-]`. Активный job с тем же именем повторно не запускается. Если от старой задачи остался state без exit code, а сохранённый процесс нельзя подтвердить, состояние становится `unknown`; автоматическая перезапись такого state запрещена.

Runner запускается через `setsid` в отдельной session/process group. Команда передаётся detached-runner через pipe и не сохраняется в job-state. stdout и stderr пишутся в один журнал, exit code — атомарно в отдельный файл.

Успешный `job start` означает только, что launcher подтверждён. Это **не** успех длительной команды.

Если подтверждение `start` потеряно, не повторяйте запуск:

```powershell
py .\ssh_relay.py job status --name prod --job build-app
py .\ssh_relay.py job list --name prod
```

### status

Состояния:

* `running` — exit code ещё нет, PID и process start time подтверждены;
* `succeeded` — `exit_code=0`;
* `failed` — ненулевой exit code;
* `unknown` — exit code отсутствует, а исходный процесс нельзя надёжно подтвердить.

Поля: `job`, `state`, `pid`, `elapsed`, `exit_code`, `log_size`, `log_age`. Для защиты от PID reuse проверяется start time из `/proc/<pid>/stat`. Исчезновение PID не считается успехом.

Remote state:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/ssh_relay/jobs/<job>/
```

С `umask 077` сохраняются только `pid`, `start_ticks`, `started_epoch`, `exit_code` и `log`. Полная команда отдельно на диск не пишется.

### tail

По умолчанию возвращаются максимум 80 строк и 64 КиБ; верхние пределы — 1000 строк и 256 КиБ:

```powershell
py .\ssh_relay.py job tail --name prod --job build-app --lines 120 --bytes 128K
```

Строки прогресса вроде `[ 71%] Building CXX object ...` передаются без преобразований.

### wait

`job wait` **локально** опрашивает `job status` короткими запросами, а не удерживает SSH-channel на всё время. По умолчанию poll interval 5 секунд, локальный timeout 3600 секунд.

Истечение локального timeout возвращает `124` и не останавливает удалённую задачу. `succeeded` возвращает `0`; `failed` возвращает сохранённый exit code, если он подходит для exit code локального процесса.

### stop

`job stop` не ищет процессы по тексту команды. Используются сохранённые PID, process start time и process group. Сначала отправляется SIGTERM и выполняется ожидание, по умолчанию 5 секунд:

```powershell
py .\ssh_relay.py job stop --name prod --job build-app
```

Если мягкая остановка не сработала, `--force` — отдельная явная ступень SIGKILL:

```powershell
py .\ssh_relay.py job stop --name prod --job build-app --grace 5 --force
```

### sudo и agent-safe

На этапе 1 long-job через встроенный sudo-пароль daemon не реализован: безопасный вариант требует единого privilege context для запуска, state/status/tail/stop. Ограниченный `sudo -n`/`NOPASSWD` внутри конкретной команды допустим только как отдельная политика сервера.

`job start` не поддерживает `--risky`, потому что существующий agent-safe receipt `status=done` после успешного launcher был бы ложным финальным успехом. Для отдельной доработки agent-safe нужен lifecycle-контракт:

* события `started`, `completed`, `failed` и желательно `stopped`;
* стабильный `job`/correlation ID;
* terminal `exit_code`;
* идемпотентный dedup-ключ;
* возможность не сохранять исходную команду либо хранить только безопасный hash/redacted summary.

До появления такого контракта `ssh_relay` не создаёт несовместимый формат receipts для job.

## download и upload

Скачать один файл:

```powershell
py .\ssh_relay.py download --name prod "/var/log/app.log" ".\downloads\app.log" --create-dirs
```

Загрузить один файл:

```powershell
py .\ssh_relay.py upload --name prod ".\config.json" "/tmp/config.json" --overwrite
```

Размер и время ограничиваются параметрами daemon `--download-*`/`--upload-*`. `upload` начиная с `0.5.1` читает локальный файл в CLI-процессе и поэтому корректно работает с detached daemon; Windows-style удалённые пути нормализуются для SFTP.

## status, list, stop

```powershell
py .\ssh_relay.py status --name prod
py .\ssh_relay.py status --all
py .\ssh_relay.py list
py .\ssh_relay.py stop --name prod
py .\ssh_relay.py stop --all
```

`stop` завершает daemon только через аутентифицированный локальный запрос и токен, а не по PID из session-файла.

Session-файлы:

* Windows: `%LOCALAPPDATA%\ssh_relay\sessions\<name>.json`;
* Linux: `${XDG_STATE_HOME:-~/.local/state}/ssh_relay/sessions/<name>.json`.

На Linux каталоги состояния создаются с `0700`, session-файлы — `0600`. Токен session-файла чувствителен и не должен попадать в Git, логи или недоверенные процессы.

## Использование с OpenCode

Авторитетная инструкция находится в `opencode/skills/ssh-relay/SKILL.md`. Основной порядок:

```text
1. Не используй прямой ssh.
2. status --name <session>.
3. Короткая диагностика через exec: hostname && whoami && pwd.
4. Короткие команды — exec/sudo-exec.
5. Длительные процессы — job.
6. Таймаут транспорта != ошибка процесса.
7. При неизвестном результате job start не повторяй запуск; сначала job status/job list.
8. Не раскрывай пароли, ключи, токены, секреты из команд и логов.
```

Skill не содержит IP, имён конкретных серверов или специфики BS Downloader и предназначен как источник истины для последующей установки через `opencode_setup`.

## Безопасность

* SSH-пароль, passphrase и sudo-пароль не сохраняются на диск и не выводятся в логи.
* Приватный ключ не копируется в session-файл.
* Session token не выводится в CLI и даёт доступ к активному локальному daemon; защищайте session-файл.
* `known_hosts` обязателен; автоматический accept неизвестного host key не используется.
* Все локальные relay-запросы идут только на `127.0.0.1` и проверяются по токену.
* Job-state создаётся с `umask 077`; полная команда отдельно в state не сохраняется.
* `job stop` проверяет PID вместе с process start time и не выполняет поиск по тексту команды.
* Возможность выполнения произвольной shell-команды — назначение relay; не расширяйте её без оценки угроз.
* Для постоянной эксплуатации root-команд предпочтительнее ограниченный `NOPASSWD`, а не широкий sudo-пароль в памяти relay.

## Проверка

Без SSH-сервера:

```powershell
py -m py_compile .\ssh_relay.py .\ssh_relay_jobs.py .\tests\test_jobs.py
py -m unittest discover -s .\tests -v
py .\ssh_relay.py --version
py .\ssh_relay.py --help
py .\ssh_relay.py daemon --help
py .\ssh_relay.py exec --help
py .\ssh_relay.py sudo-exec --help
py .\ssh_relay.py job --help
py .\ssh_relay.py job start --help
py .\ssh_relay.py job status --help
py .\ssh_relay.py job tail --help
py .\ssh_relay.py job wait --help
py .\ssh_relay.py job stop --help
py .\ssh_relay.py job list --help
py .\ssh_relay.py download --help
py .\ssh_relay.py upload --help
py .\ssh_relay.py status --help
py .\ssh_relay.py stop --help
py .\ssh_relay.py list --help
```

Перед ручным тестом изменённого daemon старую сессию нужно остановить и запустить заново.

PowerShell проверяет код через `$LASTEXITCODE`. В `cmd.exe`/Far Manager код нужно читать в той же командной строке; отдельный следующий `echo %ERRORLEVEL%` в Far Manager ненадёжен:

```cmd
cmd /V:ON /C "py .\ssh_relay.py sudo-exec --name prod ^"sh -c 'exit 7'^" & echo Exit code: !ERRORLEVEL!"
```

Минимальный remote job-тест:

```powershell
py .\ssh_relay.py job start --name prod --job relay-long-test "sh -c 'echo start; sleep 3; echo done; exit 0'"
py .\ssh_relay.py job status --name prod --job relay-long-test
py .\ssh_relay.py job tail --name prod --job relay-long-test
py .\ssh_relay.py job wait --name prod --job relay-long-test --timeout 30
```

## Дальнейшие доработки

* lifecycle receipts agent-safe для long-job;
* отдельная безопасная архитектура длительных sudo-job;
* этап 2 — крупные/рекурсивные SFTP-передачи;
* дополнительные интеграционные тесты reconnect/job на тестовом SSH-сервере.
