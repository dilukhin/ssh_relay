# План изменений ssh_relay для интеграции с agent-safe

Дата актуализации: 2026-07-31.

## Назначение

План определяет состояние работ в проекте `ssh_relay` после реализации версии `0.6.0` и порядок завершения Issue #2.

Доработки адаптера, классификации риска, expected state, rollback и verify выполняются отдельно в `agent-safe`.

Связанные материалы:

- [`RISKY_OPERATION_CONTRACT.md`](RISKY_OPERATION_CONTRACT.md);
- [`AGENT_SAFE_INTEGRATION_FINDINGS.md`](AGENT_SAFE_INTEGRATION_FINDINGS.md);
- [`UX_FINDINGS.md`](UX_FINDINGS.md);
- задача [`ssh_relay#2`](https://github.com/dilukhin/ssh_relay/issues/2);
- зависимая задача [`agent-safe#4`](https://github.com/dilukhin/agent-safe/issues/4).

## Текущее состояние

Целевая версия P0 реализована:

```text
ssh_relay 0.6.0
```

Рабочая релизная ветка:

```text
agent/safe-integration-improvements
```

Ветка `docs/quoting-and-ux-findings` была использована как ветка реализации и слита в основную рабочую ветку через PR #3.

## Сводка этапов

| Этап | Приоритет | Состояние | Результат |
|---|---:|---|---|
| Машинный контракт `exec`/`sudo-exec` | P0 | **Реализован** | JSON-объект, статусы, точный exit code, машинные коды `0/10/11/12/13`. |
| Безопасный risky receipt | P0 | **Реализован** | Нет полной команды; есть IDs, безопасные метаданные, command hash и hash-цепочка. |
| Partial success и unknown | P0 | **Реализован** | Ошибка receipt после успешной команды и неопределённый результат команды различаются. |
| Защита `transaction_id` | P0 | **Реализована** | Повтор уже записанного ID блокируется до основной команды. |
| Совместимость 0.5.x | P0 | **Реализована** | Новый daemon принимает обычные старые запросы; новая risky-семантика требует 0.6.0. |
| Локальные автоматические проверки | P0 | **Выполнены** | Машинная матрица и локальный TCP-протокол. |
| Краткий реальный SSH smoke-тест | P0 | **Выполнен** | `exec`, `sudo-exec`, exit code 7 и успешный нерискованный JSON. |
| Расширенная реальная приёмка Issue #2 | P0 | **Не завершена** | Нужны реальные risky, receipt failure/unknown, сетевой разрыв и две сессии. |
| Интеграция с `agent-safe` | P0/P1 | **Не выполнена** | Требуется адаптер из `agent-safe#4`. |
| `inspect --json` | P1 | **Не реализован** | Остаётся отдельной возможностью. |
| `exec-script` / `sudo-exec-script` | P1 | **Не реализованы** | Нужен отдельный threat review и контракт. |
| `--command-file` / `--command-stdin` | P1 | **Не реализованы** | Главная незакрытая quoting-задача. |
| JSON для `status` и `list` | P1 | **Не реализован** | Рассмотреть после стабилизации P0. |
| Регрессия upload | P1 | **Не выполнена** | Нужны автоматические интеграционные тесты. |
| UTF-8 PowerShell 5.1 | P2 | **Не выполнено** | Требуется безопасный `reconfigure` и ручная проверка. |

## Выполненный P0

### 1. Машинный результат

Для `exec` и `sudo-exec` добавлены:

```text
--json
--transaction-id ID
--change-target TARGET
--change-description TEXT
```

Результат содержит:

- идентичность relay и сессии;
- статус всей операции;
- отдельный статус команды;
- точный удалённый exit code;
- отдельный статус receipt;
- `partial_success`;
- stdout и stderr;
- машинный код и стадию ошибки;
- UTC timestamps.

### 2. Безопасный receipt

Новый receipt содержит:

- `transaction_id`;
- `receipt_id`;
- `change_target`;
- `change_description`;
- `command_hash`;
- `command_exit_code`;
- `previous_receipt_hash`;
- `receipt_hash`;
- идентичность сессии и удалённого узла.

Он не содержит полный текст команды, stdout, stderr, пароли, токены и приватные ключи.

### 3. Preflight и запись

До основной risky-команды relay:

- проверяет последнюю запись журнала;
- проверяет self-hash либо legacy anchor 0.5.x;
- проверяет отсутствие уже записанного `transaction_id`;
- блокирует запуск при повреждённом журнале.

При записи:

- используется `umask 077`;
- файл переводится в `0600`;
- конечная символическая ссылка отклоняется;
- добавленная строка проверяется контрольным чтением.

### 4. Неопределённые результаты

Реализовано разделение:

1. `not_started`;
2. `command_failed`;
3. `succeeded`;
4. `partial_success`;
5. `unknown`.

Session-файл не удаляется автоматически, если запрос daemon мог быть отправлен и результат операции неизвестен.

## Этап A. Расширенная приёмка Issue #2

Приоритет: P0.

Перед тестом изменённый daemon обязательно остановить и запустить заново.

### A1. Реальный risky `exec`

Проверить:

- успешную команду;
- запись JSONL;
- режим файла `0600`;
- отсутствие полного текста команды и тестового секрета;
- корректные `command_hash`, `receipt_hash`, `previous_receipt_hash`;
- соответствие `transaction_id` и `receipt_id` JSON-результату.

### A2. Реальный risky `sudo-exec`

Проверить отдельно:

- системный receipt в доверенном каталоге;
- owner и mode журнала;
- отсутствие sudo-пароля в выводе, протоколе и receipt;
- корректную идентичность target и сессии.

### A3. Command failure

Удалённая команда должна завершиться, например, кодом `7`.

Ожидание:

```text
operation_status = command_failed
command_status = failed
command_exit_code = 7
receipt_status = not_attempted
process exit code = 11
```

### A4. Receipt failure

Смоделировать достоверную ошибку записи после успешной команды.

Ожидание:

```text
operation_status = partial_success
command_status = succeeded
receipt_status = failed
partial_success = true
process exit code = 12
```

### A5. Неизвестный статус receipt

Смоделировать потерю подтверждения после возможного append.

Ожидание:

```text
operation_status = partial_success
command_status = succeeded
receipt_status = unknown
partial_success = true
process exit code = 12
```

### A6. Unknown основной команды

Смоделировать timeout либо потерю соединения после запуска.

Ожидание:

```text
operation_status = unknown
command_status = unknown
command_exit_code = null
process exit code = 13
```

Автоматический повтор запрещён до read-only verify.

### A7. Повторный transaction ID

Повторить уже подтверждённый ID.

Ожидание:

- основная команда не запускается;
- `operation_status=not_started`;
- `error_code=transaction_id_exists`;
- process exit code `10`.

### A8. Повреждённая последняя запись

Изменить self-hash последней строки тестового журнала.

Ожидание:

- основная команда не запускается;
- ошибка указывает стадию `receipt`;
- журнал требует ручного исправления или отдельного нового пути.

### A9. Две именованные сессии

Запустить две сессии на разных узлах либо разных тестовых targets.

Проверить отсутствие смешения:

- session name;
- host и port;
- SSH user;
- `change_target`;
- receipt path;
- `transaction_id`;
- stdout/stderr.

### A10. Потеря ответа локального daemon

После возможной отправки запроса session-файл должен сохраниться. Следующая проверка начинается с `status`, а не с удаления состояния или повторной risky-команды.

## Этап B. Адаптер `agent-safe`

Выполняется в `dilukhin/agent-safe#4` после стабилизации P0.

Адаптер должен:

- поддерживать явные режимы `exec` и `sudo-exec`;
- создавать transaction ID до запуска relay;
- передавать target и безопасное описание;
- разбирать единственный JSON-объект;
- сохранять remote receipt ID и hash в локальном receipt;
- различать command failure, partial success и unknown;
- блокировать последующие risky-операции после кодов `12` и `13`;
- выполнять read-only verify и rollback по собственной политике;
- не использовать прямой SSH при активном relay.

Совместная приёмка должна проверить общий transaction ID, локальный receipt, удалённый receipt, verify и rollback.

## Этап C. Непрозрачная передача команды

Приоритет: P1.

Добавить для `exec` и `sudo-exec`:

```text
--command-file PATH
--command-stdin
```

Требования:

- режимы взаимоисключающие с позиционным `remote_command`;
- файл читается CLI как UTF-8;
- stdin читается до EOF;
- действует небольшой отдельный лимит;
- пустая команда отклоняется;
- интерактивный stdin не появляется;
- содержимое не печатается в диагностике;
- `command_hash` соответствует переданным байтам;
- fixtures проверяются в PowerShell 5.1, PowerShell 7, `cmd.exe` и POSIX shell.

Это основной следующий шаг по `UX_FINDINGS.md`.

## Этап D. Диагностика и read-only JSON

Приоритет: P1.

Рассмотреть:

- безопасную диагностику распавшегося argv;
- `status --json`;
- `list --json`;
- `inspect --json`.

`inspect` может включать:

- session name;
- host, port и SSH user;
- hostname;
- remote cwd;
- режим sudo;
- версию relay;
- безопасный идентификатор host key.

Он не должен включать токены, пароли, приватный ключ и содержимое session-файла.

## Этап E. Ограниченное выполнение скрипта

Приоритет: P1, требует отдельной оценки угроз.

Спроектировать:

```text
exec-script
sudo-exec-script
```

Ограничения:

- только обычный локальный файл;
- небольшой лимит размера;
- явный shell из ограниченного списка;
- hash точных переданных байтов;
- закрытый интерактивный stdin;
- существующие timeout и output limit;
- отсутствие рекурсивной передачи;
- гарантированный cleanup временного файла, если он создаётся.

Эта возможность не должна незаметно превращать relay в общий механизм произвольной передачи и выполнения больших файлов.

## Этап F. Регрессия upload

Приоритет: P1.

Добавить проверки:

- CLI и daemon из разных cwd;
- detached daemon;
- overwrite тем же размером, но другим содержимым;
- SHA-256 локального и удалённого файла;
- Windows-style и POSIX path;
- отказ без `--overwrite`;
- лимит размера;
- timeout;
- cleanup после ошибки.

## Этап G. UTF-8 и версия

Приоритет: P2.

- безопасно настроить локальный UTF-8 через `reconfigure` с fallback;
- проверить PowerShell 5.1;
- добавить тест согласованности `__version__`, `--version` и README.

## Минимальные проверки перед публикацией

```text
python -m py_compile ssh_relay.py
python tests/test_machine_protocol.py
python tests/test_local_tcp_protocol.py
python ssh_relay.py --version
```

Дополнительно проверить `--help` всех подкоманд.

Если код daemon изменился, перед ручным тестом его необходимо перезапустить.

## Критерий завершения Issue #2

Issue #2 можно закрыть после подтверждения всех P0-критериев:

- JSON стабильно разбирается внешним процессом;
- текстовая семантика не нарушена;
- command failure сохраняет точный удалённый код;
- receipt failure и unknown дают `partial_success=true`;
- timeout и потеря ответа дают машинно различимый unknown;
- реальный receipt не содержит команды и тестового секрета;
- `exec` и `sudo-exec` проверены раздельно;
- две именованные сессии не смешиваются;
- README и `__version__` согласованы;
- адаптер `agent-safe` подтвердил совместимость контракта либо отдельно зафиксировал оставшуюся зависимость.
