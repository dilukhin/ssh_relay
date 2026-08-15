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
