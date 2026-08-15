# Безопасный risky receipt — staging 0.9

## Статус

Документ описывает P0.2 из issue #25 на staging-ветке `agent/machine-contract-0.9`.

Это ещё не финальный публичный risky-контракт 0.9.0:

- P0.1 (`exec --json` / `sudo-exec --json`) подготовлен в #24;
- этот этап заменяет legacy receipt безопасной записью и вводит корреляцию транзакций;
- `--json --risky`, `partial_success` и единая failure matrix включаются в #26;
- публичный version bump выполняется один раз после общей P0-приёмки.

## Цель

Risky receipt подтверждает успешную команду отдельной JSONL-записью на удалённой стороне, но не хранит полный текст команды, stdout/stderr и секреты.

Новый receipt не является разрешением на автоматический retry. Если команда выполнилась, а результат записи receipt неизвестен или запись завершилась ошибкой, повтор команды запрещён до проверки состояния.

## Схема receipt v1

Пример записи:

```json
{
  "action": "exec",
  "change_description": "обновлена конфигурация",
  "change_target": "/etc/app.conf",
  "command_exit_code": 0,
  "command_hash": "<sha256>",
  "command_status": "succeeded",
  "receipt_hash": "<sha256>",
  "receipt_id": "<uuid>",
  "remote_host": "198.51.100.42",
  "remote_port": 22,
  "remote_user": "donpedro",
  "schema_version": 1,
  "session": "prod",
  "sudo": false,
  "timestamp_utc": "2026-08-15T00:00:00Z",
  "tool": "ssh_relay",
  "tool_version": "0.8.2",
  "transaction_id": "deploy-20260815-001"
}
```

Поля `change_target` и `change_description` могут быть `null`. Они передаются вызывающей стороной явно и не извлекаются эвристически из команды.

Receipt не содержит:

- полного текста команды;
- stdout/stderr;
- session token;
- SSH-пароля;
- sudo-пароля;
- passphrase или приватного SSH-ключа.

## Идентификаторы

`transaction_id` связывает локальную операцию с удалённой записью. Допускаются 1–128 ASCII-символов: буквы, цифры, точка, дефис, подчёркивание и двоеточие. Если ID не передан, relay генерирует UUID до отправки risky-команды.

`receipt_id` всегда генерируется daemon до попытки записи receipt.

Повторный `transaction_id` не означает идемпотентность команды. Writer отказывается добавлять вторую запись с тем же ID и возвращает отдельную ошибку `duplicate_transaction_id`. Если пользовательская команда к этому моменту уже успешно выполнилась, автоматический повтор запрещён; финальная классификация этого случая как `partial_success` выполняется в #26.

## Hash

`command_hash`:

```text
SHA-256(точные UTF-8 байты пользовательской команды)
```

Команда не нормализуется и не сохраняется рядом с hash.

`receipt_hash`:

1. из объекта временно исключается поле `receipt_hash`;
2. JSON сериализуется с `sort_keys=true`, UTF-8, без ASCII escaping и без пробелов (`separators=(",", ":")`);
3. вычисляется SHA-256 точных UTF-8 байтов этой строки.

`previous_receipt_hash` в 0.9 не используется. Цепочка без внешнего доверенного anchor не мешает владельцу удалённой учётной записи переписать журнал и пересчитать hashes, но делает повреждение одной строки причиной хрупкой зависимости всех последующих записей. Tamper-evident chain следует проектировать отдельно вместе с внешним anchor/signature.

## Receipt path и права

Writer выполняется переносимым POSIX `sh` без нового helper и без новой зависимости.

Перед append он:

- отклоняет пустой путь, путь с управляющими символами, слишком длинный путь и путь, заканчивающийся `/`;
- создаёт parent directory с `umask 077`;
- непосредственно перед append отклоняет final symlink;
- требует, чтобы существующий target был обычным файлом;
- создаёт новый файл при необходимости и устанавливает `0600`;
- проверяет отсутствие существующего `transaction_id`;
- добавляет одну каноническую JSONL-строку;
- читает последнюю строку и сравнивает её с записанным JSON.

Полностью устранить POSIX symlink TOCTOU переносимым shell-кодом нельзя. Поэтому parent directory должен быть доверенным и недоступным для записи посторонним пользователям. Для стандартного пути `~/.local/state/agent-safe/changes.jsonl` это означает корректные права домашнего каталога и каталога состояния. Отдельный privileged helper в P0.2 не вводится.

## Исход записи receipt

Внутренний writer различает:

- `succeeded` — строка добавлена и контрольное чтение совпало;
- `failed` — достоверная локальная/удалённая ошибка, включая symlink, неправильный тип файла, права, duplicate transaction, append/verify failure;
- `unknown` — SSH-команда writer могла стартовать, но достоверный результат потерян.

`failed` и `unknown` предназначены для интеграции в #26. Они не являются основанием повторять пользовательскую risky-команду.

## Защита от старого daemon

Новый клиент сначала проверяет capability `receipt_schema_version=1` через read-only `status`. Если capability нет, risky-команда не отправляется.

После успешного preflight новый клиент всё равно не посылает legacy `risky=true`. На wire передаются `risky=false` и `receipt_schema_version=1`. Только новый daemon преобразует такой запрос во внутреннюю risky-операцию и вызывает safe writer.

Поэтому старый daemon, даже если он неожиданно окажется получателем запроса, не должен вызвать legacy writer с полным текстом команды. Если пользовательская команда всё же успела выполниться, но safe receipt не подтверждён, клиент возвращает ошибку с неизвестным receipt outcome и запрещает автоматический повтор.

После обновления production-кода перед ручным тестированием daemon нужно остановить и запустить заново.

## CLI text-mode

Обычный risky вызов сохраняет существующую команду и добавляет безопасные optional metadata:

```text
py ssh_relay.py exec --name prod --risky \
  --transaction-id deploy-20260815-001 \
  --change-target /etc/app.conf \
  --change-description "обновлена конфигурация" \
  "install -m 0644 /tmp/app.conf /etc/app.conf"
```

Для sudo:

```text
py ssh_relay.py sudo-exec --name prod --risky \
  --transaction-id restart-nginx-001 \
  --change-target nginx \
  --change-description "перезапущен сервис" \
  "systemctl restart nginx"
```

`transaction_id`, `change_target` и `change_description` не должны содержать секреты. Эти параметры допустимы только вместе с `--risky`.

## Machine-mode

На этом этапе `exec --json --risky` и `sudo-exec --json --risky` остаются заблокированными до отправки пользовательской команды. Это намеренно: #26 должен единообразно вернуть command result, receipt result и `partial_success`.

## Следующий этап

#26 должен:

1. включить `--json --risky`;
2. перенести `transaction_id`, `receipt_id`, `receipt_hash` и receipt outcome в machine result;
3. формализовать `partial_success` для «команда succeeded, receipt failed/unknown»;
4. проверить disconnect/reconnect, duplicate transaction и две именованные сессии;
5. выполнить общую P0-приёмку, обновить README и один раз поднять версию до `0.9.0` перед merge staging в `main`.
