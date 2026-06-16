# ssh_relay

`ssh_relay.py` — локальный SSH-relay для выполнения коротких неинтерактивных команд через одну или несколько заранее открытых именованных SSH-сессий.

Основной сценарий: пользователь вручную запускает один или несколько `daemon`, проходя SSH-аутентификацию паролем либо заданным ключом/сертификатом, после чего CLI-агент, в частности OpenCode, выполняет удалённые команды через локальный вызов `exec` или явно включённый `sudo-exec`, не используя прямой SSH и не запрашивая SSH-учётные данные повторно.

Текущая версия: `0.5.0`.

## Возможности

* открытие одной или нескольких именованных SSH-сессий по паролю, приватному SSH-ключу либо OpenSSH-сертификату и их использование до ручной остановки;
* выполнение одной удалённой команды за вызов `exec`;
* явный режим `sudo` через `daemon --enable-sudo` и отдельную команду `sudo-exec`;
* скачивание одного обычного удалённого файла через активную SSH-сессию командой `download`;
* загрузка одного обычного локального файла на удалённый сервер через активную SSH-сессию командой `upload`;
* проверка активной сессии через аутентифицированный запрос `status`;
* проверка всех известных сессий через `status --all` и просмотр списка через `list`;
* корректная остановка daemon через аутентифицированный запрос `stop`;
* прослушивание только локального адреса `127.0.0.1`;
* обязательная проверка SSH host key через `known_hosts`;
* ограничение вывода команды размером 4 МиБ, ограничение времени выполнения команды, а также лимиты размера и времени для скачивания и загрузки файлов.

## Ограничения

Relay предназначен только для коротких неинтерактивных команд.

Не поддерживаются:

* интерактивный ввод в удалённую команду;
* обычный пропуск `sudo` через `exec` с запросом пароля;
* редакторы, интерактивные shell, `top`, `less`, `passwd`, команды с повторными запросами ввода;
* скачивание и загрузка каталогов, рекурсивное копирование, SCP-режим и специальные файлы;
* `sudo-download` и `sudo-upload`: команды `download` и `upload` работают только с файлами, доступными текущему SSH-пользователю через SFTP;
* параллельное выполнение удалённых команд и скачиваний;
* длительные задачи и команды с большим выводом.

Команды, скачивания и загрузки выполняются последовательно. Псевдотерминал для удалённых команд не создаётся. Если команда выдаёт более 4 МиБ данных либо работает дольше установленного лимита, relay завершает её с диагностическим сообщением. Если файл превышает лимит передачи либо передаётся дольше разрешённого времени, relay останавливает операцию и удаляет временный файл.

`sudo-exec` не делает relay интерактивным. Sudo-пароль передаётся только внутренне из памяти daemon в stdin удалённой команды `sudo -S`; обычный `exec` stdin от пользователя не принимает.

## Требования

Локальная сторона:

* Windows с PowerShell или `cmd.exe`;
* Python 3.12 или новее;
* библиотека `paramiko`;
* сетевой доступ к SSH-порту удалённого сервера.

Удалённая сторона:

* Linux/Ubuntu-сервер;
* работающая служба SSH;
* учётная запись с разрешённой парольной SSH-аутентификацией либо аутентификацией по ключу/сертификату;
* shell для выполнения переданных команд;
* для `sudo-exec` — право пользователя выполнять нужные команды через `sudo`.

Сам relay работает локально. Установка Python или `paramiko` на удалённый сервер для работы `ssh_relay.py` не требуется.

## Установка зависимости

```powershell
py -m pip install paramiko
```

## Проверка версии и справка

```powershell
py .\ssh_relay.py --version
py .\ssh_relay.py -v
py .\ssh_relay.py --help
```

Ожидаемый вывод версии:

```text
ssh_relay 0.5.0
```

## Подготовка `known_hosts`

Relay не принимает неизвестный ключ SSH-сервера автоматически. До первого подключения добавьте ключ сервера в `%USERPROFILE%\.ssh\known_hosts` либо передайте отдельный проверенный файл параметром `--known-hosts`.

Пример получения публичного ключа сервера в PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keyscan -H 198.51.100.42 | Out-File -Append -Encoding ascii "$env:USERPROFILE\.ssh\known_hosts"
ssh-keygen -lf "$env:USERPROFILE\.ssh\known_hosts"
```

Ключ, полученный через сеть, нельзя считать доверенным автоматически. До запуска relay сравните показанный fingerprint с fingerprint сервера, полученным по доверенному каналу, например от администратора сервера или из его консоли.

## Команды

```text
py .\ssh_relay.py daemon [--name NAME] --host HOST --user USER [--port PORT] [-i PATH] [--ask-key-passphrase] [--known-hosts PATH] [--command-timeout SECONDS] [--download-timeout SECONDS] [--download-max-size SIZE] [--upload-timeout SECONDS] [--upload-max-size SIZE] [--enable-sudo]
py .\ssh_relay.py exec [--name NAME] "COMMAND"
py .\ssh_relay.py sudo-exec [--name NAME] "COMMAND"
py .\ssh_relay.py download [--name NAME] [--overwrite] [--create-dirs] REMOTE_PATH LOCAL_PATH
py .\ssh_relay.py upload [--name NAME] [--overwrite] [--create-dirs] LOCAL_PATH REMOTE_PATH
py .\ssh_relay.py status [--name NAME] [--all]
py .\ssh_relay.py stop [--name NAME] [--all]
py .\ssh_relay.py list
```

### `daemon`

Устанавливает SSH-соединение и запускает локальный relay. Без параметра `--identity-file` используется режим SSH-пароля: пароль вводится вручную и не сохраняется.

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro
```

Для входа по приватному ключу используйте `--identity-file` либо сокращённый вариант `-i`:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519"
```

Эквивалентный запуск в `cmd.exe`:

```cmd
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "%USERPROFILE%\.ssh\id_ed25519"
```

Для зашифрованного приватного ключа укажите безопасный интерактивный запрос passphrase. Passphrase не передаётся в командной строке и не записывается в session-файл:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519" --ask-key-passphrase
```

Paramiko принимает через `--identity-file` также публичный OpenSSH-сертификат с окончанием `-cert.pub`. Соответствующий приватный ключ должен находиться рядом с сертификатом и иметь имя без суффикса `-cert.pub`:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519-cert.pub" --ask-key-passphrase
```

Приватный ключ и сертификат для входа не заменяют `known_hosts`: файл `known_hosts` проверяет ключ самого SSH-сервера и защищает от подключения к подменённому узлу.

Для нестандартного SSH-порта и отдельного файла `known_hosts`:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --port 2222 --user donpedro --known-hosts .\trusted_known_hosts
```

По умолчанию каждая команда может выполняться не более 120 секунд. Для короткой диагностики лимит можно уменьшить:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro --command-timeout 30
```

Для скачивания и загрузки файлов по умолчанию действует лимит 300 секунд и 64 МиБ на один файл. Лимиты задаются при запуске daemon:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro --download-timeout 120 --download-max-size 16M --upload-timeout 120 --upload-max-size 16M
```

Размер можно задавать числом байт либо с суффиксом `K`, `M` или `G`.

### `daemon --enable-sudo`

Режим sudo включается только явно при запуске daemon:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
```

То же самое при входе по ключу:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519" --enable-sudo
```

При запуске с `--enable-sudo` relay сначала устанавливает обычное SSH-соединение, затем локально запрашивает sudo-пароль через `getpass`, проверяет его удалённой командой `sudo -k && sudo -S -p '' -v` и только после успешной проверки запускает локальный TCP-server relay.

Sudo-пароль не принимается из аргументов командной строки, не записывается в session-файл и не выводится в диагностике. Он хранится только в памяти процесса daemon. При остановке daemon ссылка на строку пароля очищается, но полный контроль над обнулением памяти Python не гарантируется.

Пример вывода после подключения:

```text
SSH-соединение установлено: donpedro@198.51.100.42:22
Relay слушает локальный адрес 127.0.0.1:54321
Файл сессии: C:\Users\User\AppData\Local\ssh_relay\.ssh_relay_session.json
Режим sudo: включён
Для завершения нажмите Ctrl+C или выполните команду stop.
```

Окно терминала с daemon должно оставаться открытым до конца работы.

### Именованные сессии

По умолчанию используется сессия `default`, поэтому команды без `--name` тоже работают:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro
py .\ssh_relay.py exec "hostname"
py .\ssh_relay.py stop
```

Для нескольких одновременных daemon задавайте имя явно. Имя используется только для выбора локального session-файла; каждый daemon по-прежнему слушает свой локальный порт на `127.0.0.1`:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro
py .\ssh_relay.py daemon --name test --host 198.51.100.43 --user donpedro
py .\ssh_relay.py daemon --name rootbox --host 198.51.100.44 --user donpedro --enable-sudo
```

Команды к конкретной сессии:

```powershell
py .\ssh_relay.py exec --name prod "hostname"
py .\ssh_relay.py exec -n test "hostname"
py .\ssh_relay.py sudo-exec --name rootbox "whoami"
py .\ssh_relay.py download --name prod "/var/log/app.log" ".\downloads\app.log" --create-dirs
py .\ssh_relay.py upload --name prod ".\config.json" "/tmp/config.json" --overwrite
py .\ssh_relay.py status --name prod
py .\ssh_relay.py status --all
py .\ssh_relay.py list
py .\ssh_relay.py stop --name prod
py .\ssh_relay.py stop --all
```

Допустимое имя сессии: от 1 до 64 символов, только латинские буквы, цифры, точка, дефис и подчёркивание. Символы `/`, `\`, `:`, пробелы и `..` запрещены, чтобы имя нельзя было использовать для выхода за пределы каталога session-файлов.

Если session-файл с таким именем уже существует, `daemon --name NAME` сначала проверяет daemon через токен. Если daemon активен, запуск отклоняется. Если daemon недоступен, устаревший session-файл удаляется и запуск продолжается.

### `exec`

Выполняет одну команду через активную SSH-сессию:

```powershell
py .\ssh_relay.py exec "hostname"
py .\ssh_relay.py exec --name prod "whoami"
py .\ssh_relay.py exec -n prod "cd /opt/project && git status --short"
```

Код завершения удалённой команды возвращается вызывающему процессу. `stdout` удалённой команды выводится в `stdout`, `stderr` — в `stderr`; наличие текста в `stderr` само по себе не является ошибкой relay.

Не запускайте через `exec` редакторы, оболочки, `top`, запросы пароля, длительные процессы либо чтение больших логов целиком.

### `sudo-exec`

Выполняет одну команду через `sudo` в активном relay:

```powershell
py .\ssh_relay.py sudo-exec "whoami"
py .\ssh_relay.py sudo-exec --name prod "systemctl restart nginx"
```

`sudo-exec` доступен только если текущий daemon был запущен с параметром `--enable-sudo`. Если режим sudo не включён, команда вернёт ошибку:

```text
Режим sudo не включён. Перезапустите daemon с параметром --enable-sudo.
```

Команду передавайте без внешнего префикса `sudo`: relay сам формирует удалённый запуск вида `sudo -S -p '' -- sh -c ...` и экранирует пользовательскую shell-строку через `shlex.quote()`.

`sudo-exec` предназначен только для коротких неинтерактивных команд. Не используйте его для редакторов, интерактивных shell, `top`, команд с повторными запросами ввода, длительных процессов и команд с большим выводом.

Для регулярной эксплуатации безопаснее настроить ограниченный `NOPASSWD` в `sudoers` только для заранее разрешённых команд и использовать `sudo -n` в обычном `exec`. `sudo-exec` — временный ручной режим для доверенного локального пользователя и доверенного сервера.

### `download`

Скачивает один обычный файл с удалённого сервера в локальный файл через SFTP внутри уже открытой SSH-сессии:

```powershell
py .\ssh_relay.py download --name prod "/var/log/app.log" ".\downloads\app.log" --create-dirs
py .\ssh_relay.py download --name prod "/tmp/result.json" ".\result.json" --overwrite
```

В `cmd.exe` пример выглядит так:

```cmd
py .\ssh_relay.py download --name prod "/var/log/app.log" ".\downloads\app.log" --create-dirs
```

Команда `download` не использует прямой `ssh`, `scp` или `sftp` из командной строки. Запрос отправляется локальному daemon по `127.0.0.1` с токеном сессии, а daemon скачивает файл через SFTP по уже открытому SSH-соединению. Содержимое файла не кодируется в JSON и не проходит через stdout команды `exec`: daemon пишет локальный файл напрямую во временный файл рядом с целевым путём, затем атомарно переименовывает его в целевой файл.

По умолчанию существующий локальный файл не перезаписывается. Для перезаписи нужен явный параметр `--overwrite`. Если локальный каталог назначения ещё не существует, используйте `--create-dirs` либо создайте каталог вручную.

Поддерживается только скачивание обычных файлов. Каталоги, рекурсивное копирование, специальные файлы и загрузка локальных файлов на сервер не поддерживаются. Файл должен быть доступен текущему SSH-пользователю через SFTP. Для скачивания файла, доступного только root, сначала подготовьте читаемую временную копию на сервере отдельной контролируемой командой, например через `sudo-exec`, затем скачайте эту копию и удалите её.

### `status`

Проверяет не только наличие файла сессии, но и ответ работающего daemon с корректным токеном доступа:

```powershell
py .\ssh_relay.py status
py .\ssh_relay.py status --name prod
py .\ssh_relay.py status --all
```

Пример вывода одной сессии:

```text
Сессия: default
Активна: donpedro@198.51.100.42:22
Локальный порт: 54321
Версия relay: 0.5.0
Режим sudo: включён
```

Если session-файл устарел и daemon недоступен, файл удаляется, а команда завершается с ошибкой.

### `list`

Показывает все известные session-файлы и проверяет доступность соответствующих daemon:

```powershell
py .\ssh_relay.py list
```

Пример вывода:

```text
Имя      Состояние   SSH                         Sudo   Порт relay   Версия
default  активна     donpedro@198.51.100.42:22   выкл.  54321        0.5.0
prod     активна     donpedro@198.51.100.43:22   вкл.   54322        0.5.0
old      недоступна  donpedro@198.51.100.44:22   ?      54323        0.2.0
```

`list` ничего не удаляет. Устаревшие session-файлы удаляются при обращении к конкретной сессии через `status --name`, `exec`, `sudo-exec` или `stop`.

### `stop`

Передаёт действующему daemon команду корректного завершения:

```powershell
py .\ssh_relay.py stop
py .\ssh_relay.py stop --name prod
py .\ssh_relay.py stop --all
```

`stop` не завершает процесс по PID из файла сессии, поэтому устаревший файл не может привести к принудительному завершению постороннего процесса. Daemon также можно остановить клавишами `Ctrl+C` в его терминале.

## Файлы сессий

Файл сессии содержит локальный токен доступа к открытой SSH-сессии. SSH-пароль, passphrase ключа, приватный ключ и sudo-пароль в него не записываются.

В режиме sudo файл сессии становится особенно чувствительным: токен даёт доступ к открытому локальному daemon, который способен выполнять root-команды на удалённом сервере. При множественных сессиях чувствительным считается весь каталог `sessions`. Такой файл нельзя копировать, публиковать, передавать агентам без необходимости или помещать в Git.

Расположение файла больше не зависит от рабочего каталога:

* Windows: `%LOCALAPPDATA%\ssh_relay\sessions\<name>.json`;
* Linux: `${XDG_STATE_HOME:-~/.local/state}/ssh_relay/sessions/<name>.json`.

Старый одиночный файл `%LOCALAPPDATA%\ssh_relay\.ssh_relay_session.json` или `${XDG_STATE_HOME:-~/.local/state}/ssh_relay/.ssh_relay_session.json` читается только как legacy-сессия `default`, если нового `sessions/default.json` ещё нет. Новые daemon всегда записывают session-файлы в каталог `sessions`.

На Linux каталог состояния и каталог `sessions` создаются с правами `0700`, файлы сессий — с правами `0600`. На Windows файл размещается в пользовательском каталоге `%LOCALAPPDATA%`, доступ к которому должен контролироваться правами текущей учётной записи.

Файл `.ssh_relay_session.json` от ранней реализации в каталоге проекта больше не используется и должен быть удалён.

## Использование с OpenCode

Пользователь вручную запускает daemon, например по ключу:

```powershell
cd C:\Tools\ssh-relay
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519"
```

Для задач, где иногда нужны root-права, daemon запускается явно с sudo-режимом:

```powershell
cd C:\Tools\ssh-relay
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519" --enable-sudo
```

После запуска для OpenCode можно использовать следующую инструкцию:

```text
Удалённый сервер prod доступен через уже запущенный локальный SSH relay.

Не используй прямые вызовы ssh и не запрашивай пароль.

Для обычных команд используй:
cd C:\Tools\ssh-relay
py .\ssh_relay.py exec --name prod "<remote-command>"

Для команд, которым нужны права root, используй:
cd C:\Tools\ssh-relay
py .\ssh_relay.py sudo-exec --name prod "<remote-command>"

Для скачивания одного файла с сервера используй:
cd C:\Tools\ssh-relay
py .\ssh_relay.py download --name prod "<remote-path>" "<local-path>"

До выполнения рабочей задачи проверь relay:
py .\ssh_relay.py status --name prod
py .\ssh_relay.py exec --name prod "hostname && whoami && pwd"

Не запускай интерактивные команды.
Не запускай команды, которые ожидают ввод пароля.
Не запускай длительные команды и команды с большим выводом.
Не скачивай и не загружай большие файлы, каталоги и специальные файлы.
Если нужна рекурсивная передача каталога, сначала сообщи, что текущий relay этого не поддерживает.
```

## Пример рабочей сессии

Первый терминал:

```powershell
cd C:\Tools\ssh-relay
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
```

Второй терминал:

```powershell
cd C:\Tools\ssh-relay
py .\ssh_relay.py status --name prod
py .\ssh_relay.py exec --name prod "whoami"
py .\ssh_relay.py sudo-exec --name prod "whoami"
py .\ssh_relay.py exec --name prod "cd /opt/project && git status --short"
py .\ssh_relay.py download --name prod "/tmp/report.txt" ".\downloads\report.txt" --create-dirs
py .\ssh_relay.py stop --name prod
```

## Замечания по безопасности

* SSH-пароль, passphrase ключа и sudo-пароль вводятся только пользователем в терминале daemon и не сохраняются на диск.
* Sudo-пароль хранится только в памяти процесса daemon; полное обнуление памяти Python после очистки ссылки не гарантируется.
* Приватный SSH-ключ не копируется в session-файл; его защита и права доступа остаются ответственностью пользователя.
* Токен сессии не выводится в сообщения и хранится в пользовательском файле сессии.
* В режиме sudo доступ к session-файлу следует рассматривать как доступ к открытому root-каналу через доверенный daemon.
* Relay принимает локальные запросы только на `127.0.0.1` и проверяет токен для `exec`, `sudo-exec`, `download`, `upload`, `status` и `stop`.
* SSH-сервер должен быть заранее доверен через проверенный `known_hosts`; автоматическое принятие неизвестного host key не используется.
* Возможность выполнения произвольной удалённой shell-команды является назначением relay; расширять её без оценки угроз не следует.
* `download` даёт владельцу токена возможность записать локальный файл с правами процесса daemon. `upload` даёт владельцу токена возможность прочитать локальный файл с правами процесса daemon и записать его на сервер через SFTP. Не передавайте session-файл и токен недоверенным процессам.
* Для постоянной эксплуатации предпочтительнее ограниченный `NOPASSWD` в `sudoers` под конкретные команды, а не хранение sudo-пароля в памяти relay.

## Минимальная ручная проверка

Без подключения к серверу:

```powershell
py -m py_compile .\ssh_relay.py
py .\ssh_relay.py --version
py .\ssh_relay.py --help
py .\ssh_relay.py daemon --help
py .\ssh_relay.py sudo-exec --help
py .\ssh_relay.py download --help
py .\ssh_relay.py upload --help
py .\ssh_relay.py status --help
py .\ssh_relay.py stop --help
py .\ssh_relay.py list --help
```

С тестовым сервером после проверки host key запустите новый daemon в выбранном режиме аутентификации. Перед проверкой изменённого `daemon` обязательно остановите старую сессию, если она работала.

Вход по паролю с sudo-режимом:

```powershell
py .\ssh_relay.py daemon --name prod --host 198.51.100.42 --user donpedro --enable-sudo
```

Вход по ключу с sudo-режимом в другом запуске daemon:

```powershell
py .\ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i "$env:USERPROFILE\.ssh\id_ed25519" --enable-sudo
```

Проверка в PowerShell после запуска daemon:

```powershell
py .\ssh_relay.py status --name prod
py .\ssh_relay.py list
py .\ssh_relay.py exec --name prod "whoami"
py .\ssh_relay.py sudo-exec --name prod "whoami"
py .\ssh_relay.py sudo-exec --name prod "sh -c 'printf stdout-ok; printf stderr-ok >&2; exit 7'"
$LASTEXITCODE
py .\ssh_relay.py exec --name prod "printf download-ok > /tmp/ssh-relay-download-test.txt"
py .\ssh_relay.py download --name prod "/tmp/ssh-relay-download-test.txt" ".\downloads\ssh-relay-download-test.txt" --create-dirs --overwrite
Get-Content .\downloads\ssh-relay-download-test.txt
py .\ssh_relay.py stop --name prod
```

Ожидаемо:

* обычный `exec "whoami"` показывает пользователя `donpedro`;
* `sudo-exec "whoami"` показывает `root`;
* stdout и stderr удалённой команды проходят раздельно;
* код завершения удалённой команды возвращается вызывающему процессу;
* `status` показывает включённый режим sudo;
* `download` скачивает тестовый файл в локальный каталог `downloads`;
* `upload` загружает тестовый файл на сервер и сохраняет его содержимое;
* `stop` завершает только активный daemon через токен, а не по PID из файла.

Проверка возврата кода в `cmd.exe` или командной строке Far Manager выполняется одной строкой, чтобы `%ERRORLEVEL%` не потерялся при запуске нового экземпляра командного процессора:

```cmd
cmd /V:ON /C "py .\ssh_relay.py sudo-exec --name prod ^"sh -c 'exit 7'^" & echo Exit code: !ERRORLEVEL!"
```

В Far Manager нельзя полагаться на отдельную следующую команду `echo %ERRORLEVEL%`: она может выполняться в новом экземпляре командного процессора и показывать `0`.

Сложные команды с перенаправлениями и вложенным экранированием в `cmd.exe` не следует использовать как базовую диагностику; для проверки `stderr` надёжнее применять PowerShell либо отдельный `.cmd`-файл.

Проверка должна подтвердить: подключение в каждом нужном режиме аутентификации, успешную команду со `stderr`, возврат кода `7`, корректный `status` и завершение только активного daemon через `stop`.

## Возможные дальнейшие доработки

* отдельная команда регистрации и показа fingerprint SSH-сервера с явным подтверждением пользователя;
* рекурсивное скачивание и загрузка каталогов с явными лимитами и фильтрами;
* настройка ограниченного sudo-профиля с `sudo -n` для заранее разрешённых команд;
* дополнительные автоматизированные тесты протокола daemon/exec/sudo-exec/download/upload/status/stop.
