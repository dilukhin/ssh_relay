# Машинный контракт `exec` / `sudo-exec` — staging 0.9

## Статус

Документ описывает P0.1 из issue #24 на staging-ветке `agent/machine-contract-0.9`.

Это ещё не полный risky-контракт 0.9.0:

- безопасный risky receipt выполняется в #25;
- `partial_success` и объединение command/receipt outcomes выполняются в #26;
- до завершения #25 машинный режим с `--risky` намеренно блокируется до отправки команды.

Исторический `RISKY_OPERATION_CONTRACT.md` из ветки `agent/safe-integration-improvements` использован только как reference. Источник истины — актуальный код staging-ветки.

## CLI

```text
py ssh_relay.py exec --json --name default "hostname"
py ssh_relay.py sudo-exec --json --name default "id"
```

Обычный режим без `--json` сохраняет прежнюю семантику stdout, stderr и remote exit code.

`argparse`-ошибки до запуска обработчика остаются обычными CLI-ошибками с кодом `2`.

## Формат stdout/stderr

При штатном машинном результате:

- stdout CLI содержит ровно один JSON-объект UTF-8 и перевод строки;
- удалённые stdout/stderr находятся в полях `stdout` / `stderr`;
- локальный stderr CLI пуст;
- полный текст пользовательской команды не включается отдельным полем в JSON.

Удалённая команда сама может вывести любые данные в stdout/stderr; вызывающая сторона отвечает за секреты, которые намеренно печатает сама команда.

## Схема P0.1

```json
{
  "schema_version": 1,
  "tool": "ssh_relay",
  "tool_version": "0.8.2",
  "action": "exec",
  "operation_status": "succeeded",
  "session": "default",
  "remote_host": "198.51.100.42",
  "remote_port": 22,
  "remote_user": "donpedro",
  "sudo": false,
  "risky": false,
  "command_status": "succeeded",
  "command_exit_code": 0,
  "receipt_status": "not_requested",
  "partial_success": false,
  "stdout": "host\n",
  "stderr": "",
  "output_encoding": "utf-8-replace",
  "error_code": null,
  "error_stage": null,
  "error_message": null,
  "started_at_utc": "2026-08-15T05:00:00Z",
  "finished_at_utc": "2026-08-15T05:00:01Z"
}
```

`tool_version` на staging остаётся фактической версией исходников. Публичный bump до `0.9.0` выполняется только после завершения #24–#26.

## `operation_status`

P0.1 использует:

- `succeeded` — удалённая команда достоверно завершилась с кодом `0`;
- `command_failed` — получен точный ненулевой remote exit code;
- `not_started` — достоверно известно, что команда не запускалась;
- `unknown` — команда или запрос могли быть запущены/доставлены, но достоверный результат не получен.

`partial_success` зарезервирован для #26.

## `command_status`

- `succeeded`;
- `failed`;
- `not_started`;
- `unknown`.

`command_exit_code`:

- `0` для `succeeded`;
- точный ненулевой remote code для `failed`;
- `null` для `not_started` и `unknown`.

Наличие текста в stderr не определяет успех или ошибку команды.

## Границы `not_started` / `unknown`

### Локальный запрос daemon

Foundation из PR #30 хранит доказуемый признак `request_sent`.

- подтверждённый `ConnectionRefusedError` локального listener → `not_started`;
- timeout/send/read failure после установленного локального соединения или иной недоказанный transport outcome → `unknown`;
- повреждённый ответ после возможной отправки → `unknown`.

Если старый daemon/клиент не предоставляет достаточных structured markers, машинный режим выбирает `unknown`, а не оптимистичный `not_started`.

### Удалённая команда

Foundation хранит `command_started`:

- ошибка до SSH `exec-request` → `not_started`;
- ошибка во время/после возможного `exec-request`, timeout, output limit или потеря transport → `unknown`;
- полученный remote exit status → `succeeded` или `failed`.

Автоматический retry после `unknown` запрещён.

## Process exit codes

- `0` — `succeeded`;
- `10` — `not_started`;
- `11` — `command_failed`;
- `12` — зарезервирован для `partial_success` (#26);
- `13` — `unknown`.

## Risky в P0.1

До #25 команда

```text
exec --json --risky ...
```

не отправляет удалённую команду и возвращает:

```text
operation_status = not_started
command_status = not_started
receipt_status = not_attempted
error_code = risky_machine_contract_not_ready
process exit code = 10
```

Это исключает использование старого receipt, который ещё содержит полный текст команды.

## Безопасность

Машинный результат не должен содержать:

- session token;
- SSH-пароль;
- sudo-пароль;
- приватный ключ;
- полный текст пользовательской команды как отдельное поле.

Для `sudo-exec` structured error не сохраняет secret-bearing исходное исключение в exception chain.

Проверка `known_hosts`/fingerprint, лимиты времени/вывода и запрет интерактивного stdin не меняются.

## Следующие этапы

После merge P0.1 в staging:

1. #25 заменяет старый risky receipt безопасной схемой и добавляет корреляцию транзакций;
2. #26 включает `--json --risky`, формализует `receipt_status` и `partial_success`;
3. полный staging проходит cross-platform CI/review;
4. только затем staging с одним bump до `0.9.0` сливается в `main`.
