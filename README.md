# ssh_relay

`ssh_relay.py` — локальный SSH-relay для выполнения коротких неинтерактивных команд через заранее открытые именованные SSH-сессии.

Пользователь вручную запускает daemon и проходит SSH-аутентификацию. После этого CLI-агент, в частности OpenCode, использует локальный вызов relay без прямого `ssh` и повторного ввода SSH-пароля.

Текущая версия: `0.5.1`.

## Возможности

- именованные SSH-сессии;
- вход по паролю, приватному ключу или OpenSSH-сертификату;
- `exec` для обычных коротких команд;
- `sudo-exec` через явно включённый режим `daemon --enable-sudo`;
- удалённый JSONL receipt для команд с `--risky`;
- загрузка и скачивание одного обычного файла;
- `status`, `list` и безопасный `stop`;
- фоновый запуск daemon по ключу через `--detach`;
- прослушивание только `127.0.0.1`;
- обязательная проверка host key через `known_hosts`;
- ограничения времени, объёма вывода и размера файлов.

## Ограничения

Relay предназначен только для коротких неинтерактивных операций.

Не поддерживаются:

- интерактивный stdin;
- shell, редакторы, `top`, `less`, `passwd` и другие интерактивные программы;
- команды с запросом пароля;
- длительные процессы и команды с большим выводом;
- параллельное выполнение команд;
- рекурсивная передача каталогов;
- `sudo-download` и `sudo-upload`;
- специальные файлы и SCP-режим.

Команды выполняются последовательно. Псевдотерминал не создаётся. Максимальный вывод команды — 4 МиБ. Значения timeout и лимиты файлов задаются при запуске daemon.

## Требования

Локальная сторона:

- Windows с `cmd.exe` или PowerShell;
- Python 3.12 или новее;
- `paramiko`;
- сетевой доступ к SSH-порту сервера.

Удалённая сторона:

- Linux/Ubuntu;
- SSH-служба;
- POSIX shell;
- для `sudo-exec` — право пользователя выполнять требуемые команды через `sudo`.

Установка зависимости:

```cmd
py -m pip install paramiko
```

## Версия и справка

```cmd
py ssh_relay.py --version
py ssh_relay.py --help
py ssh_relay.py daemon --help
```

Ожидаемый вывод:

```text
ssh_relay 0.5.1
```

## Подготовка known_hosts

Relay не принимает неизвестный host key автоматически.

Пример для `cmd.exe`:

```cmd
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
ssh-keyscan -H 198.51.100.42 >> "%USERPROFILE%\.ssh\known_hosts"
ssh-keygen -lf "%USERPROFILE%\.ssh\known_hosts"
```

Пример для PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keyscan -H 198.51.100.42 | Out-File -Append -Encoding ascii "$env:USERPROFILE\.ssh\known_hosts"
ssh-keygen -lf "$env:USERPROFILE\.ssh\known_hosts"
```

Ключ, полученный через сеть, нельзя считать доверенным автоматически. Сравните fingerprint с данными, полученными по доверенному каналу.

## Команды

```text
py ssh_relay.py daemon [--name NAME] --host HOST --user USER [--port PORT] [-i PATH] [--ask-key-passphrase] [--known-hosts PATH] [--command-timeout SECONDS] [--download-timeout SECONDS] [--download-max-size SIZE] [--upload-timeout SECONDS] [--upload-max-size SIZE] [--enable-sudo] [--detach] [--detach-log PATH]
py ssh_relay.py exec [--name NAME] [--risky] [--receipt-path REMOTE_JSONL] "COMMAND"
py ssh_relay.py sudo-exec [--name NAME] [--risky] [--receipt-path REMOTE_JSONL] "COMMAND"
py ssh_relay.py download [--name NAME] [--overwrite] [--create-dirs] REMOTE_PATH LOCAL_PATH
py ssh_relay.py upload [--name NAME] [--overwrite] [--create-dirs] LOCAL_PATH REMOTE_PATH
py ssh_relay.py status [--name NAME] [--all]
py ssh_relay.py stop [--name NAME] [--all]
py ssh_relay.py list
```

## Запуск daemon

### Парольная SSH-аутентификация

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
```

SSH-пароль вводится пользователем и не сохраняется.

### Вход по ключу

`cmd.exe`:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519"
```

PowerShell:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519"
```

Для зашифрованного ключа:

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519" --ask-key-passphrase
```

Passphrase не передаётся в командной строке и не записывается в session-файл.

### Режим sudo

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
```

Daemon локально запрашивает sudo-пароль, проверяет его и хранит только в памяти процесса. Полное гарантированное обнуление строки в памяти Python невозможно.

`sudo-exec` доступен только для daemon, запущенного с `--enable-sudo`.

### Фоновый запуск

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519" --detach
```

`--detach`:

- требует `--identity-file`;
- несовместим с `--ask-key-passphrase`;
- несовместим с `--enable-sudo`;
- пишет лог в каталог состояния либо в путь из `--detach-log`.

После изменения daemon старый процесс необходимо остановить и запустить заново перед проверкой.

## Именованные сессии

```cmd
py ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
py ssh_relay.py daemon --name test --host 198.51.100.43 --user donpedro
```

Имя сессии содержит от 1 до 64 символов: латинские буквы, цифры, точку, дефис и подчёркивание. Пробелы, `/`, `\`, `:` и `..` запрещены.

Перед работой:

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "hostname && whoami && pwd"
```

## exec

```cmd
py ssh_relay.py exec --name prod "hostname"
py ssh_relay.py exec --name prod "cd /opt/project && git status --short"
```

Код завершения удалённой команды возвращается вызывающему процессу. Удалённый stdout выводится в stdout, stderr — в stderr. Наличие текста в stderr само по себе не является ошибкой.

## risky receipt

Для команды, меняющей состояние хоста:

```cmd
py ssh_relay.py exec --name prod --risky "mkdir -p /tmp/example"
```

Путь по умолчанию:

```text
~/.local/state/agent-safe/changes.jsonl
```

Другой путь:

```cmd
py ssh_relay.py exec --name prod --risky --receipt-path "/tmp/agent-safe-changes.jsonl" "touch /tmp/example"
```

Текущая реализация `0.5.1` сохраняет полную команду в receipt. Не передавайте через `--risky` команды с токенами, паролями, приватными URL и другими секретами.

Если команда завершилась с ненулевым кодом, receipt не записывается. Если команда выполнена, но receipt записать не удалось, relay возвращает ошибку. Удалённое состояние при этом уже могло измениться. Исправление этого протокола запланировано отдельно.

## sudo-exec

```cmd
py ssh_relay.py sudo-exec --name prod "whoami"
py ssh_relay.py sudo-exec --name prod "systemctl restart nginx"
py ssh_relay.py sudo-exec --name prod --risky --receipt-path "/var/lib/agent-safe/changes.jsonl" "systemctl restart nginx"
```

Команду передавайте без внешнего `sudo`. Relay сам формирует запуск через `sudo -S`.

Для постоянной эксплуатации безопаснее ограниченный `NOPASSWD` только для заранее разрешённых команд. `sudo-exec` — ручной режим для доверенного пользователя и сервера.

## download

```cmd
py ssh_relay.py download --name prod "/var/log/app.log" ".\downloads\app.log" --create-dirs
py ssh_relay.py download --name prod "/tmp/result.json" ".\result.json" --overwrite
```

Поддерживаются только обычные файлы, доступные SSH-пользователю через SFTP. Существующий локальный файл не перезаписывается без `--overwrite`.

## upload

```cmd
py ssh_relay.py upload --name prod ".\config.json" "/tmp/config.json" --overwrite
py ssh_relay.py upload --name win ".\tool.ps1" "C:\Windows\Temp\tool.ps1" --overwrite
```

Начиная с `0.5.1`, локальный файл читает CLI-процесс и передаёт содержимое daemon. Поэтому upload работает при другом рабочем каталоге daemon и в режиме `--detach`.

Windows-style удалённый путь нормализуется для SFTP:

```text
C:\Windows\Temp\tool.ps1 -> C:/Windows/Temp/tool.ps1
```

Поддерживаются только обычные файлы. Существующий удалённый файл не перезаписывается без `--overwrite`.

## status, list и stop

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py status --all
py ssh_relay.py list
py ssh_relay.py stop --name prod
py ssh_relay.py stop --all
```

`status` проверяет daemon аутентифицированным запросом, а не только наличие session-файла.

`list` показывает известные session-файлы и состояние соответствующих daemon, но ничего не удаляет.

`stop` завершает daemon через токен. PID из session-файла не используется для принудительного завершения процесса, поэтому устаревший PID не может завершить посторонний процесс.

## Session-файлы

Session-файл содержит локальный токен доступа. Он не содержит SSH-пароль, sudo-пароль, passphrase или приватный ключ.

Расположение:

```text
Windows: %LOCALAPPDATA%\ssh_relay\sessions\<name>.json
Linux:   ${XDG_STATE_HOME:-~/.local/state}/ssh_relay/sessions/<name>.json
```

На Linux каталоги создаются с правами `0700`, файлы — `0600`.

Session-файлы и токены нельзя помещать в Git, передавать недоверенным процессам или выводить в логах. В режиме sudo токен фактически даёт доступ к открытому root-каналу через daemon.

## Использование с OpenCode

Пример инструкции агенту:

```text
Удалённый сервер prod доступен через уже запущенный локальный SSH relay.

Не используй прямой ssh и не запрашивай пароль.

Перед работой выполни:
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "hostname && whoami && pwd"

Обычная команда:
py ssh_relay.py exec --name prod "<remote-command>"

Изменяющая команда:
py ssh_relay.py exec --name prod --risky "<remote-command>"

Команда с правами root:
py ssh_relay.py sudo-exec --name prod "<remote-command>"

Изменяющая root-команда:
py ssh_relay.py sudo-exec --name prod --risky --receipt-path "/var/lib/agent-safe/changes.jsonl" "<remote-command>"

Не запускай интерактивные команды, запросы пароля, длительные процессы и команды с большим выводом.
Не передавай секреты в команду с --risky.
```

## Безопасность

- SSH-пароль, passphrase и sudo-пароль не сохраняются на диск;
- токен не выводится в штатной диагностике;
- daemon слушает только `127.0.0.1`;
- каждый запрос проверяет токен;
- неизвестный host key не принимается автоматически;
- session-файл в sudo-режиме следует защищать как средство root-доступа;
- upload позволяет CLI-процессу прочитать локальный файл и передать его daemon;
- download позволяет daemon записать локальный файл;
- возможности произвольного выполнения команд нельзя расширять без отдельной оценки угроз.

## Минимальная ручная проверка

Без сервера:

```cmd
py -m py_compile ssh_relay.py
py ssh_relay.py --version
py ssh_relay.py --help
py ssh_relay.py daemon --help
py ssh_relay.py exec --help
py ssh_relay.py sudo-exec --help
py ssh_relay.py download --help
py ssh_relay.py upload --help
py ssh_relay.py status --help
py ssh_relay.py stop --help
py ssh_relay.py list --help
```

С тестовым сервером:

```cmd
py ssh_relay.py status --name prod
py ssh_relay.py exec --name prod "whoami"
py ssh_relay.py sudo-exec --name prod "whoami"
py ssh_relay.py exec --name prod --risky "touch /tmp/ssh-relay-risky-test.txt"
py ssh_relay.py exec --name prod "tail -n 1 ~/.local/state/agent-safe/changes.jsonl"
py ssh_relay.py stop --name prod
```

PowerShell проверяет код через `$LASTEXITCODE`.

Для `cmd.exe` и Far Manager проверяйте код в той же строке:

```cmd
cmd /V:ON /C "py ssh_relay.py sudo-exec --name prod ^"sh -c 'exit 7'^" & echo Exit code: !ERRORLEVEL!"
```

В Far Manager отдельная следующая команда `echo %ERRORLEVEL%` может показать код нового процесса, а не предыдущей команды.

## Материалы по интеграции с agent-safe

- [Практические findings](AGENT_SAFE_INTEGRATION_FINDINGS.md);
- [план изменений ssh_relay](SSH_RELAY_CHANGE_PLAN.md);
- задача `ssh_relay`: [#2](https://github.com/dilukhin/ssh_relay/issues/2);
- задача `agent-safe`: [dilukhin/agent-safe#4](https://github.com/dilukhin/agent-safe/issues/4).

Планируемые изменения ещё не реализованы в коде. Текущая рабочая версия остаётся `0.5.1`.