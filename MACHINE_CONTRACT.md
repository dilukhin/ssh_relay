# Машинный контракт ssh_relay 0.9

## Назначение

Этот документ — каноническая спецификация машинного результата коротких команд `exec` и `sudo-exec` в `ssh_relay` 0.9.

Машинный режим предназначен для внешнего агента, которому нужно различать:

- команда достоверно не запускалась;
- команда завершилась с ненулевым remote exit code;
- команда завершилась успешно;
- risky-команда завершилась успешно и safe receipt подтверждён;
- risky-команда завершилась успешно, но receipt failed/unknown;
- результат команды неизвестен после возможного запуска.

Неоднозначный исход никогда не является основанием для автоматического повтора команды.

## Вызов

```text
py ssh_relay.py exec --name prod --json "hostname"
py ssh_relay.py sudo-exec --name prod --json "whoami"
```

Risky-вариант:

```text
py ssh_relay.py exec --name prod --json --risky \
  --transaction-id deploy-20260815-001 \
  --change-target /etc/app.conf \
  --change-description "обновлена конфигурация" \
  "install -m 0644 /tmp/app.conf /etc/app.conf"
```

`change_target` и `change_description` передаются только явно. Relay не пытается извлекать их из текста команды.

## Process exit code CLI

| Код | `operation_status` | Значение |
|---:|---|---|
| 0 | `succeeded` | Операция полностью подтверждена. |
| 10 | `not_started` | Пользовательская команда достоверно не была отправлена/запущена. |
| 11 | `command_failed` | Команда завершилась с ненулевым remote exit code. |
| 12 | `partial_success` | Команда завершилась успешно, но safe receipt failed/unknown. |
| 13 | `unknown` | Команда могла быть запущена, но достоверный результат не получен. |

Remote exit code хранится отдельно в `command_exit_code` и не подменяется локальным process exit code.

## Базовый JSON

Каждый вызов `--json` печатает в stdout ровно один JSON-объект и завершается одним из кодов выше.

Основные поля:

```json
{
  "schema_version": 1,
  "tool": "ssh_relay",
  "tool_version": "0.9.0",
  "action": "exec",
  "operation_status": "succeeded",
  "session": "prod",
  "remote_host": "198.51.100.42",
  "remote_port": 22,
  "remote_user": "donpedro",
  "sudo": false,
  "risky": false,
  "command_status": "succeeded",
  "command_exit_code": 0,
  "receipt_status": "not_requested",
  "partial_success": false,
  "stdout": "...",
  "stderr": "...",
  "output_encoding": "utf-8-replace",
  "error_code": null,
  "error_stage": null,
  "error_message": null,
  "started_at_utc": "2026-08-15T00:00:00Z",
  "finished_at_utc": "2026-08-15T00:00:01Z"
}
```

Полный текст пользовательской команды в JSON не включается.

## `command_status`

Допустимые значения:

- `not_started` — есть доказательство, что команда не запускалась;
- `succeeded` — получен remote exit code 0;
- `failed` — получен ненулевой remote exit code;
- `unknown` — команда могла стартовать, но достоверный exit status отсутствует.

Потеря локального ответа после возможной доставки запроса не переводится в `not_started`.

## Risky receipt

Для `--risky` машинный объект дополнительно содержит:

```json
{
  "transaction_id": "deploy-20260815-001",
  "receipt_id": "0f0f0f0f-0000-4000-8000-000000000001",
  "receipt_hash": "<sha256-or-null>",
  "receipt_path": "~/.local/state/agent-safe/changes.jsonl",
  "change_target": "/etc/app.conf",
  "change_description": "обновлена конфигурация"
}
```

`transaction_id` задаётся вызывающей стороной либо генерируется relay.

`receipt_id` всегда генерируется клиентом **до отправки risky-команды** и передаётся daemon. Поэтому при `receipt_status=unknown` внешний агент всё равно имеет идентификатор для последующей read-only диагностики.

### `receipt_status`

- `not_requested` — команда не была risky;
- `not_attempted` — risky-команда не достигла подтверждённого успешного завершения, поэтому receipt не должен был создаваться;
- `succeeded` — safe receipt записан и контрольное чтение подтвердило добавленную JSONL-строку;
- `failed` — есть достоверная ошибка writer, например duplicate transaction, symlink/type/permission/append/verify failure;
- `unknown` — writer мог стартовать, но достоверный результат записи потерян; либо потерян ответ на весь risky-запрос после возможной доставки команды.

## Матрица risky outcomes

| Команда | Receipt | `operation_status` | Process exit | Retry |
|---|---|---|---:|---|
| не запускалась | `not_attempted` | `not_started` | 10 | допустим только после устранения причины и проверки контекста |
| remote exit != 0 | `not_attempted` | `command_failed` | 11 | не автоматический |
| succeeded | `succeeded` | `succeeded` | 0 | не требуется |
| succeeded | `failed` | `partial_success` | 12 | запрещён автоматически |
| succeeded | `unknown` | `partial_success` | 12 | запрещён автоматически |
| unknown | `unknown` | `unknown` | 13 | запрещён автоматически |

`partial_success=true` означает: удалённое состояние уже изменилось, но audit receipt не подтверждён. Вызывающая сторона не должна продолжать цепочку risky-операций как после полного успеха.

## Capability handshake

Перед risky-командой клиент выполняет read-only `status` и требует:

```json
{"receipt_schema_version":1}
```

Если capability отсутствует или не подтверждён, пользовательская команда не отправляется и machine result возвращает `not_started`.

На wire новый клиент не использует legacy `risky=true`: safe receipt layer переводит запрос во внутреннюю risky-операцию только на совместимом daemon. Это не позволяет старому daemon случайно вызвать старый writer, сохранявший полный текст команды.

После обновления daemon-кода перед ручным тестом старый daemon нужно остановить и запустить заново.

## Safe receipt v1

Receipt содержит:

- `schema_version`;
- UTC timestamp;
- tool/tool_version;
- session и remote host/port/user;
- action/sudo;
- `transaction_id`;
- `receipt_id`;
- optional `change_target`/`change_description`;
- `command_status=succeeded`;
- `command_exit_code=0`;
- `command_hash`;
- `receipt_hash`.

Receipt не содержит:

- полный текст команды;
- stdout/stderr;
- session token;
- SSH/sudo passwords;
- private key/passphrase.

`command_hash` — SHA-256 точных UTF-8 байтов пользовательской команды.

`receipt_hash` — SHA-256 канонического JSON без поля `receipt_hash`: UTF-8, `sort_keys=true`, `ensure_ascii=false`, без пробелов.

`previous_receipt_hash` в 0.9 не используется: без внешнего доверенного anchor цепочка не защищает от полного переписывания журнала владельцем удалённой учётной записи.

## Duplicate transaction

Повторный `transaction_id` отклоняется writer отдельной ошибкой `duplicate_transaction_id`.

Это **не** означает идемпотентность команды. Если команда уже завершилась успешно, а writer обнаружил duplicate transaction, итог — `partial_success`, а автоматический повтор команды запрещён.

## Receipt path

Writer использует portable POSIX `sh`, `umask 077`, проверяет final symlink/тип файла, устанавливает `0600`, добавляет одну строку и проверяет последнюю строку после append.

Portable shell не может полностью устранить symlink TOCTOU между проверкой и append. Поэтому parent directory receipt должен быть доверенным и недоступным для записи посторонним пользователям.

## Session lifecycle и reconnect

Read-only `status` может повторяться безопасно. `exec`, `sudo-exec`, receipt writer и другие изменяющие операции автоматически не повторяются после неоднозначного результата.

Если SSH потерян между запросами, daemon восстанавливает соединение для последующих команд. Если связь потеряна во время уже начатой команды, machine result — `unknown`, а session registration сохраняется.

## Совместимость text-mode

Без `--json` сохраняется прежняя модель:

- remote stdout -> stdout CLI;
- remote stderr -> stderr CLI;
- remote exit code -> process exit code для обычной команды;
- `--risky` использует safe receipt v1, но не меняет команду на интерактивную;
- stdin/PTY/password prompts по-прежнему не поддерживаются.

## Требования к потребителю

Внешний агент должен принимать решение по полям/кодам, а не по разбору русского текста `error_message`.

Особенно:

- не retry `operation_status=unknown`;
- не retry `partial_success`;
- не считать ненулевой `command_exit_code` ошибкой transport;
- не считать `stderr` признаком failure без remote exit code;
- использовать `transaction_id` и `receipt_id` для корреляции и последующей диагностики;
- не передавать секреты в `change_target` и `change_description`.

## Дополнение 0.10.0: локальный replay

Для публичных `exec --json` и `sudo-exec --json` добавлены поля:

- `request_id`: UUIDv4, созданный клиентом до отправки запроса;
- `replay_status`: `available`, `partial`, `disabled` или `unavailable`;
- `replay_truncated`: признак отброшенного префикса raw-вывода.

`schema_version=1`, прежние поля, статусы и коды завершения не меняются.
`request_id` не заменяет `transaction_id`/`receipt_id` и не обеспечивает идемпотентность.
При потере ответа клиент сохраняет свой `request_id`, но не утверждает доступность
replay до локального чтения. Старый daemon означает `unavailable`.
Read-only `status` нового daemon возвращает `replay_schema_version=1`.

`replay --json` возвращает один JSON-объект с `action=replay`, `schema_version=1`,
`request_id`, `session`, `source_action`, `source_operation_status`,
`source_command_status`, `source_command_exit_code`, `encoding`, `stdout`, `stderr`,
`stdout_truncated`, `stderr_truncated`, `replay_complete`, `error_code`, `error_message`.
Коды локальной операции: `0` — полный результат, `2` — неполный, `1` — ошибка.
Поля `source_*` относятся к прежнему выполнению; нового удалённого выполнения нет.

## Дополнение 0.11.0: подтверждение цели рискованной команды

До `exec --json --risky` и `sudo-exec --json --risky` клиент требует в ответе `status` точную версию клиента, `receipt_schema_version=1` и `risky_identity_schema_version=1`. Если проверка не прошла, команда не отправляется (`not_started`, код 10). Текстовый risky-вызов также требует совпадения версии и safe receipt v1.

Daemon извлекает `remote_host_key_sha256` из активного аутентифицированного SSH-транспорта, открытого со строгой проверкой `known_hosts`, перед выполнением команды. Формат — `SHA256:` и 43 символа Base64 без заполнения. Ответ связывает этот отпечаток с `remote_host`, `remote_port`, `remote_user`, `transaction_id` и `receipt_id` квитанции. Квитанция хранит тот же отпечаток. Значения из локального файла сессии сами по себе не подтверждают ключ сервера.

При отсутствующем ключе команда не запускается. Если ответ о завершённой команде не подтверждает цель или связь с квитанцией, клиент возвращает `unknown` (код 13); автоматический повтор запрещён. После обновления клиента daemon следует остановить и запустить заново из той же установки.

## Дополнение: явно проверяемая identity для agent-safe (кандидат на review)

Обычный `--json --risky` версии 0.11.0 сохраняет прежнюю совместимость и показывает
наблюдаемый отпечаток, но **не требует ожидаемого pin**. Такой вызов не даёт
`agent-safe` права считать цель заранее разрешённой. Новое соглашение opt-in:

1. Read-only `status --name NAME --json` возвращает один объект
   `action=preflight`, `operation_status=succeeded|not_started`,
   `verified_identity_schema_version=1` и `verified_identity` только при активном
   аутентифицированном SSH-транспорте. В identity входят daemon-side
   `remote_host/port/user`, алгоритм ключа, fingerprint `SHA256:`,
   `trusted_known_hosts=true`, `daemon_instance_id` (UUID),
   `connection_generation` (целое от 1), `daemon_source_sha` (полный SHA,
   внедрённый в установленную сборку). При reconnect generation увеличивается;
   при disconnected identity не выдаётся. Локальный session-файл и его имя —
   не свидетельство ключа.
2. Отдельный `exec|sudo-exec --json --risky --require-verified-identity`
   требует все поля `--expected-remote-host`, `--expected-remote-port`,
   `--expected-remote-user`, `--expected-host-key-algorithm`,
   `--expected-host-key-sha256`, `--expected-daemon-instance-id`,
   `--expected-connection-generation`, `--expected-daemon-source-sha`.
   CLI проверяет identity/capability в read-only status; **daemon ещё раз**
   сравнивает переданную ожидаемую identity с текущим verified transport
   внутри того же exec/sudo request **до** отправки команды на SSH.
   Если соединение изменилось после preflight, отказ `not_started`/exit10.
   `--verified-command-timeout` допускает только целые 1–3600 секунд и
   передаётся в тот же запрос; истечение после доставки — `unknown`.
3. Main result и подтверждённый receipt включают наблюдаемую identity,
   fingerprint, endpoint и существующие `transaction_id`/`receipt_id`/
   `command_hash`. `identity_observed_before_command` не доказывает, что
   удалённая команда завершилась после разрыва; при потере ответа доступна
   лишь `preflight_verified_identity` (прошлое наблюдение), итог — `unknown`.
   Полученный неэквивалентный ответ никогда не принимается как успех.

Ожидаемое значение fingerprint для опасного действия должно прийти из
**независимого доверенного источника и отдельного разрешения**, а не из самого
ответа `ssh_relay` или из session metadata. `status --json` не заменяет
разрешение человека и не делает локальный token/аргументы CLI защищёнными от
модельной подстановки. Вызовы без `--require-verified-identity` **не
соответствуют** этому строгому контракту. Новая реализация — source candidate;
старый установленный runtime и daemon не обновляются публикацией ветки.

Коррекция диагностики 0.10.1: нижняя строка
`risky_machine_contract_not_ready` в `ssh_relay_outcomes.py` перехватывается
более поздней P0-обёрткой. Установленный локальный безопасный probe с
несуществующей сессией и receipt path `/` вернул `not_started` и
`invalid_risky_metadata`, а **не** `not_ready`. Это подтверждает только parser
dispatch, не реальный remote risky E2E.


## Отдельный контракт `sudo-job`, версия 1 (кандидат 0.12.0)

Этот JSON не является результатом `exec --json --risky`. Старый адаптер
agent-safe не должен принимать его без отдельной реализации. Спецификация
жизненного цикла, доверия и восстановления: [решение по #52](docs/sudo_jobs_design_ru.md).

Daemon status объявляет `sudo_job_schema_version=1`, `sudo_jobs_enabled`.
Для каждого обращения требуется точное `expected_verified_identity` из PR #51,
повторно проверяемое в том же запросе. Клиент/daemon имеют одинаковый полный
SHA. Команда `sudo-job` всегда возвращает один JSON без текстового пролога.

| Поле | Значение |
| --- | --- |
| `schema_version` | 1 в отдельном пространстве команд `sudo-job` |
| `operation` | `start`, `status`, `tail`, `wait`, `stop` |
| `job_id`, `transaction_id` | Канонические UUID, обязательно сохранённые до мутации |
| `command_sha256` | SHA-256 точных UTF-8 байт строки команды; не shell argv |
| `state` | `not_started`, `running`, `succeeded`, `failed`, `unknown` |
| `request_not_started` | Подтверждает отказ текущего обращения до отправки полезного запроса; не описывает прежнее задание |
| `exit_code` | Точный удалённый код только при установленном завершении |
| `process_exit_code` | Код клиента; отличается от удалённого при отказе учёта/ожидания |
| `verified_identity` | Проверенный транспорт текущего чтения/запуска |
| `start_witness` | Root-свидетельство запуска наблюдателя; не завершение установки |
| `completion_witness` | Root-свидетельство завершения с точным кодом |
| `accounting_status` | `pending`, `recorded`, `failed` при определённом состоянии |
| `wait_timed_out` | Локальное ожидание закончилось; удалённому процессу сигнал не отправлялся |
| `stop_requested` | SIGTERM отправлен проверенной группе; это не подтверждение завершения |
| `log` | Только для tail; до 64 КиБ одного потока, содержимое может быть чувствительным |

Свидетельство содержит `schema_version`, UUID задания/транзакции,
`command_sha256`, `target` (адрес, порт, пользователь, алгоритм и отпечаток SSH),
`boot_id`, `unit`, `phase`, `invocation_id`, `witness_sha256`.
Completion дополнительно содержит `exit_code`, `completed_at`, `output_bytes`,
`logs_truncated`. Hash считается над UTF-8 JSON с `sort_keys=True`,
`ensure_ascii=False`, `separators=(",", ":")`, без `witness_sha256`.
Hash — контроль целостности, не подпись; подлинность обеспечивается проверенным
SSH и защищёнными файлами root. В свидетельствах нет исходной команды, stdin,
stdout/stderr, паролей, ключей и токена relay.

Коды клиента: 0 — подтверждённое наблюдение/запуск либо успех; удалённый ненулевой
код — для `failed`; 2 — отказ/ошибка учёта; 3 — `unknown`; 124 — локальный
таймаут wait. Числа могут совпадать с удалёнными кодами, поэтому состояние
определять по JSON, а не только по коду процесса. Любой malformed response или
потеря ответа после возможной доставки означает `unknown`, без повторного запуска.
Успешная команда с отказом completion имеет `state=succeeded`, `exit_code=0`,
`accounting_status=failed`; она не становится `not_started`.

Повторный UUID запрещён даже после завершения. Исчезновение записи не доказывает,
что ранее команда не выполнялась. После смены daemon/поколения соединения
обновляется только точное ожидание транспорта; старые UUID и hash сохраняются.
Agent-safe завершает транзакцию лишь после completion и своей read-only проверки
ожидаемого состояния. Согласование запуска/остановки/восстановления остаётся
в agent-safe/opencode_permissions, формального `--approved` нет.
