# Безопасный risky receipt 0.9

Safe risky receipt v1 опубликован как часть машинного контракта `ssh_relay` 0.9. Каноническая схема, failure matrix, правила `transaction_id`/`receipt_id`, `command_hash`/`receipt_hash`, `partial_success` и `unknown` находятся в `MACHINE_CONTRACT.md`.

Ключевые свойства:

* receipt не содержит полный текст команды, stdout/stderr, session token, SSH/sudo-пароли, passphrase и приватные ключи;
* `transaction_id` связывает вызывающую операцию с receipt; повтор ID не означает идемпотентность и отклоняется writer;
* `receipt_id` создаётся клиентом до отправки risky-команды и сохраняется даже при неизвестном исходе;
* `command_hash` — SHA-256 точных UTF-8 байтов пользовательской команды;
* `receipt_hash` — SHA-256 канонического JSON без самого поля `receipt_hash`;
* `previous_receipt_hash` в 0.9 не используется: без внешнего доверенного anchor цепочка не защищает журнал от полного переписывания;
* writer использует portable POSIX `sh`, `umask 077`, проверяет final symlink и тип файла, устанавливает `0600`, выполняет append и контрольное чтение;
* portable shell не устраняет полностью symlink TOCTOU, поэтому parent directory receipt должен быть доверенным и недоступным для записи посторонним пользователям;
* при `partial_success` или `unknown` автоматический retry risky-команды запрещён.

После обновления daemon перед ручным тестированием старую активную сессию нужно остановить и запустить заново.
