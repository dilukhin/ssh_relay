# Дизайн двоичного replay stdout/stderr

Статус: **предлагается к принятию; реализация в этом изменении отсутствует**.

Связано с Issue #41.

Основание дизайна: `main` на коммите `39dea792ee2923a8853ba5fa416fde7be24a7db6`.

## 1. Цель и исходная проблема

Текущий путь короткой команды на `main` получает `stdout` и `stderr` как байты, накапливает их в памяти до общего лимита 4 МиБ, а затем декодирует оба потока как UTF-8 с `errors="replace"`. После такой декодировки исходные байты уже нельзя восстановить. Если пользователь или агент видит повреждённый текст, повторное выполнение удалённой команды ради другой кодировки небезопасно: команда могла уже изменить внешнее состояние.

Минимальный целевой поток данных:

```text
remote raw bytes
    -> bounded local replay
    -> decode
    -> caller
```

Replay означает только повторное локальное чтение уже полученных байтов. **Replay никогда не отправляет удалённую команду повторно и не вызывает `exec`/`sudo-exec` на daemon.**

## 2. Область v1

Replay v1 применяется только к публичным коротким операциям:

- `exec`;
- `sudo-exec`.

Из v1 исключаются:

- `job start/status/tail/wait/stop/list`;
- `upload` и `download`;
- `status`, `list`, `stop`;
- внутренние safe receipt-команды;
- внутренние короткие команды управления job.

Причины:

1. Job уже имеет отдельный удалённый жизненный цикл. Его журнал создаётся как один объединённый `log` с `stdout` и `stderr`, поэтому восстановить раздельные потоки из него невозможно без изменения job-протокола. `job tail` является ограниченным read-only чтением и не повторяет исходную длительную команду.
2. Transfers уже работают с двоичными чанками/base64 и частичными файлами. Текстовая декодировка stdout/stderr не является их каналом данных; дополнительный replay только дублировал бы существующее двоичное состояние.
3. Receipt-вызовы являются внутренней частью risky-протокола и не должны засорять пользовательский replay или давать ложное впечатление, что replay receipt равен replay исходной команды.

Если позже понадобится двоичное восстановление `job tail`, это отдельная задача с отдельным контрактом для объединённого журнала.

## 3. Threat model

### 3.1. Защищаемые свойства

Replay должен защищать одновременно:

- **безопасность удалённого состояния**: никакого автоматического или скрытого повторного remote execution;
- **целостность результата**: исходные сохранённые байты не заменяются результатом lossy-декодирования;
- **конфиденциальность локального вывода**: replay может содержать секреты, даже если команда сама по себе не секретна;
- **ограниченность диска**: удалённый хост не должен иметь возможность заполнить локальный диск неограниченным выводом;
- **совместимость CLI**: обычный stdout/stderr и машинный JSON не должны превращаться в новый смешанный протокол;
- **безопасную очистку**: повреждённые metadata не должны позволять читать или удалять произвольные локальные пути.

### 3.2. Учитываемые противники и сбои

Учитываются:

- удалённая команда, возвращающая произвольные байты и произвольный объём вывода;
- другой непривилегированный локальный пользователь;
- частично записанные или вручную повреждённые replay-файлы;
- параллельные CLI-процессы и несколько daemon разных именованных сессий;
- PID reuse;
- падение CLI или daemon;
- потеря локального ответа;
- разрыв SSH во время команды;
- Windows sharing/locking errors;
- symlink/reparse point и path traversal.

Не защищаемся от владельца той же локальной учётной записи, root/Administrator либо полного компромета локальной ОС. Такой субъект уже может читать процесс и пользовательское состояние relay.

## 4. Главные инварианты

1. Replay не выполняет remote command и не обращается к SSH.
2. Источник replay — только raw bytes, записанные до декодирования.
3. `stdout` и `stderr` хранятся раздельно.
4. Полный текст команды не хранится.
5. Session token, SSH/sudo passwords, key/passphrase и stdin не хранятся.
6. Любой активный request имеет конечный заранее зарезервированный бюджет.
7. TTL не продлевается чтением replay.
8. Metadata никогда не содержит файлового пути, который используется как доверенный путь чтения/удаления.
9. PID без fingerprint времени создания процесса не считается идентичностью процесса.
10. `unknown` и `partial_success` не меняют существующее правило: автоматический retry запрещён.
11. Ошибка replay storage не должна приводить к записи с ослабленными правами.
12. `errors="replace"` или `ignore` не применяются при повторном декодировании replay.

## 5. Идентичность request и параллельность

### 5.1. `request_id`

Каждый публичный `exec`/`sudo-exec` получает канонический UUIDv4 `request_id` в нижнем регистре.

`request_id` создаётся **клиентским CLI до отправки локального запроса daemon**. Это важно: если daemon получил запрос, но ответ потерян, вызывающая сторона всё равно может заранее знать идентификатор.

CLI получает новый необязательный параметр:

```text
--request-id UUID
```

Если параметр не задан, CLI создаёт UUID сам. Если параметр задан, он строго валидируется и канонизируется как UUIDv4.

`request_id` не является:

- `transaction_id`;
- `receipt_id`;
- идентификатором job;
- доказательством идемпотентности команды.

Эти идентификаторы решают разные задачи и не должны подменять друг друга.

### 5.2. Как получить `request_id`, не ломая stdout

Для text mode **не добавляется никакая служебная строка в stdout или stderr**. Существующее правило `remote stdout -> stdout`, `remote stderr -> stderr` сохраняется.

Есть три безопасных способа корреляции:

1. вызывающая сторона заранее передаёт собственный UUID через `--request-id` и уже знает его;
2. в `--json` `request_id` возвращается дополнительным полем того же единственного JSON-объекта;
3. интерактивный пользователь может использовать `replay --last` при однозначном выборе.

Таким образом, ради идентификатора не вводится отдельная строка, которая могла бы сломать pipe, парсер или JSON stdout.

### 5.3. Параллельные invocations

Каноническая запись всегда per-request. Один глобальный `last` отсутствует.

Текущий daemon сериализует remote operations одной сессии через `command_lock`, но:

- разные именованные сессии имеют разные daemon-процессы;
- разные CLI-вызовы могут пересекаться по времени;
- будущая архитектура может убрать текущую сериализацию.

Поэтому уникальность и storage isolation строятся только на `request_id`, а не на предположении об однопоточности.

## 6. Data model

### 6.1. Каталог

Локальный корень:

```text
<state_directory>/replay/v1/
```

Записи:

```text
replay/v1/requests/<request-id>/
    stdout.bin
    stderr.bin
    metadata.json
```

Имя сессии намеренно **не используется как компонент файлового пути**. Оно хранится только в metadata и используется как фильтр. Единственный переменный компонент пути — канонический UUID, прошедший строгую проверку.

Имена `stdout.bin`, `stderr.bin`, `metadata.json` фиксированы кодом и никогда не читаются из metadata.

### 6.2. Metadata v1

`metadata.json` содержит только безопасную корреляционную информацию:

```json
{
  "schema_version": 1,
  "request_id": "<uuid>",
  "session": "prod",
  "source_action": "exec",
  "sudo": false,
  "risky": false,
  "command_sha256": "<sha256>",
  "created_at_utc": "...",
  "finished_at_utc": "...",
  "state": "completed",
  "operation_status": "succeeded",
  "command_status": "succeeded",
  "command_exit_code": 0,
  "receipt_status": "not_requested",
  "initial_encoding": "utf-8",
  "initial_decode_status": "clean",
  "retention_class": "clean",
  "stdout": {
    "total_bytes": 12,
    "stored_bytes": 12,
    "dropped_prefix_bytes": 0,
    "sha256_full": "...",
    "sha256_stored": "..."
  },
  "stderr": {
    "total_bytes": 0,
    "stored_bytes": 0,
    "dropped_prefix_bytes": 0,
    "sha256_full": "...",
    "sha256_stored": "..."
  },
  "owner_chain": [],
  "writer_identity": {}
}
```

Допустимые состояния записи:

- `active` — daemon ещё пишет raw bytes;
- `completed` — исход команды известен и запись финализирована;
- `unknown` — команда могла исполняться, но достоверный результат отсутствует;
- `abandoned` — найден незавершённый active record, а writer достоверно умер;
- `corrupt` не записывается как нормальное состояние: повреждённый metadata определяется сканером и обрабатывается консервативно.

`command_sha256` — SHA-256 точных UTF-8 байтов пользовательской команды. Полный текст команды не сохраняется.

Не сохраняются:

- command text;
- session `auth_token`;
- SSH/sudo password;
- private key/passphrase;
- stdin;
- полный receipt writer command;
- произвольные локальные или удалённые пути из command line.

`change_target`/`change_description` не нужны replay и также не копируются в replay metadata.

### 6.3. Поток записи

Для каждого полученного SSH chunk:

```text
recv chunk
  -> обновить incremental hashes/counters
  -> записать raw chunk в bounded stdout.bin/stderr.bin
  -> выполнить текущее presentation/decode
  -> передать дальше по существующему пути
```

Критично: запись raw bytes логически происходит до их декодирования.

Хеш полного потока считается инкрементально и не требует хранения полного потока в RAM. Хеш retained-буфера считается/пересчитывается при финализации.

## 7. Bounded rolling storage

### 7.1. Жёсткие лимиты v1

Предлагаются следующие самостоятельные значения, не наследуемые из старых экспериментов:

| Уровень | Лимит |
|---|---:|
| один stdout | 4 МиБ retained bytes |
| один stderr | 4 МиБ retained bytes |
| один request | 8 МиБ reserved bytes |
| одна session | 16 МиБ replay bytes/reservations и максимум 8 terminal records |
| global | 64 МиБ replay bytes/reservations и максимум 32 terminal records |

Обоснование:

- текущий короткий `exec` уже имеет общий предел 4 МиБ, поэтому такой per-stream tail полностью покрывает любой сегодняшний успешный короткий вывод и одновременно задаёт верхнюю границу для будущего streaming;
- 8 МиБ reservation делает верхнюю границу request независимой от распределения между stdout/stderr;
- 16 МиБ на session допускает один активный request и несколько краткоживущих terminal records, но не превращает replay в архив;
- 64 МиБ global ограничивает несколько параллельных daemon/сессий и остаётся на порядки меньше неограниченного накопления.

Лимиты являются частью design v1 и должны быть оформлены как именованные константы, чтобы их можно было менять осознанным последующим изменением контракта.

### 7.2. Reservation

До remote execution daemon под межпроцессным GC/storage lock:

1. запускает bounded GC;
2. считает фактический размер terminal records и reservations активных records;
3. резервирует до 8 МиБ для нового `request_id`;
4. только после успешного создания приватной active-записи начинает захват.

Reservation нужна, чтобы несколько daemon разных сессий не превысили global limit одновременно.

Если безопасную reservation создать нельзя, replay для этого request имеет состояние `unavailable`; нельзя обходить проблему записью вне replay root или ослаблением permissions. Обычная команда сохраняет существующую совместимость и может выполняться без replay; будущий отдельный `--require-replay` возможен, но не входит в v1.

### 7.3. Rolling policy

Каждый поток хранит **последние** 4 МиБ в исходном порядке байтов. При переполнении удаляется самый старый prefix, а metadata увеличивает `dropped_prefix_bytes`.

Replay не пытается восстановить interleaving stdout/stderr. Межпоточный порядок исходных событий не гарантируется и не моделируется.

Активный request никогда не превышает reservation. Не допускается схема «сначала накопить всё, потом обрезать».

## 8. Clean и suspect retention

### 8.1. Классификация

Record считается `clean`, только если одновременно выполнено всё:

- исход команды терминальный и не `unknown`;
- оба retained-потока строго декодируются как исходная `utf-8` без `replace`/`ignore`;
- нет truncation (`dropped_prefix_bytes == 0`);
- storage завершён без ошибки/несогласованности.

Record считается `suspect`, если выполняется хотя бы одно:

- строгая UTF-8-проверка любого потока не прошла;
- raw buffer был усечён rolling policy;
- исход команды `unknown`;
- active record после crash переклассифицирован как `abandoned`;
- обнаружена частичная storage inconsistency, при которой fixed raw files ещё безопасно читать;
- пользователь явно делает replay в кодировке, отличной от исходной, пока record жив.

Ненулевой remote exit code сам по себе не делает output suspect: это нормальный подтверждённый исход команды.

Важно: некоторые байтовые последовательности могут быть валидным UTF-8, но семантически представлять другой charset. Это автоматически доказать нельзя. Поэтому `clean` означает только «не найден признак повреждения», а не «кодировка гарантированно угадана».

### 8.2. TTL

TTL считается от terminal timestamp и **не продлевается чтением**:

- `clean`: 5 минут;
- `suspect`, `unknown`, `abandoned`: 30 минут.

Это краткоживущий recovery window, а не архив. Выбор значений мотивирован тем, что clean replay нужен главным образом для немедленного исправления отображения, а suspect/unknown требует больше времени для безопасной диагностики после сбоя. Независимые byte/count limits остаются обязательными даже внутри TTL.

## 9. Owner process, ancestry и PID reuse

### 9.1. Не один PID, а bounded owner chain

Нельзя считать непосредственный `ppid` единственным владельцем: агент может запускать relay через shell, launcher и другие промежуточные процессы.

При запуске CLI записывается bounded ancestry snapshot до 16 процессов, начиная с самого CLI и двигаясь к родителям. Системный корень (`PID 1` на POSIX, системные псевдопроцессы Windows) не считается owner anchor.

Для каждого элемента сохраняется только process identity:

- PID;
- parent PID, если доступен;
- start fingerprint;
- platform marker, необходимый для проверки fingerprint.

Не сохраняются argv, command line, environment, cwd или другие потенциально секретные сведения.

### 9.2. Strong и weak identity

PID сам по себе никогда не является strong identity.

Предпочтительный fingerprint:

- Linux/Android/Termux: PID + `/proc/<pid>/stat` start time + boot id;
- Windows: PID + process creation time, полученная через Win32 API посредством stdlib `ctypes`;
- на платформе, где надёжный start fingerprint получить нельзя: identity помечается `weak`.

Weak identity нельзя использовать для:

- удаления stale lock;
- доказательства смерти owner;
- заключения, что PID reuse не произошёл.

В этом случае действуют только TTL/count/byte limits и явный `request_id`.

### 9.3. Owner death

Record считается связанным с живым caller-контекстом, если хотя бы один не-системный strong ancestor из snapshot всё ещё совпадает по PID **и** start fingerprint.

Если все strong owner candidates достоверно завершились, record становится кандидатом раннего GC, но это лишь приоритет eviction:

- dead-owner `clean` можно удалять раньше при pressure;
- dead-owner `suspect` сохраняется предпочтительно до своего TTL, пока это позволяет global budget.

Так caller crash не уничтожает единственное доказательство partial/unknown output немедленно.

### 9.4. `--last`

`--last` никогда не означает глобальный последний request.

Выбор:

1. фильтр по `--name` session;
2. terminal records внутри TTL;
3. предпочитаются записи с сильным пересечением текущего ancestry snapshot и сохранённого owner chain;
4. если после фильтра остаётся ровно одна запись — она выбирается;
5. если несколько записей нельзя однозначно различить — команда завершается ошибкой `ambiguous_last` и требует `--request-id`.

Нельзя выбирать запись только по PID.

## 10. CLI contract replay

### 10.1. Вызов

```text
ssh_relay replay --name prod --request-id 3d33f5d8-... --encoding cp866
ssh_relay replay --name prod --last --encoding cp1251
ssh_relay replay --name prod --request-id 3d33f5d8-... --encoding utf-8 --json
```

Правила:

- `--request-id` и `--last` взаимоисключающие;
- один из них обязателен;
- `--encoding` обязателен и проверяется через стандартный codec registry Python;
- replay использует strict decode; `replace`/`ignore` не предлагаются как CLI options;
- replay не требует активного daemon и не вызывает `request_daemon`.

### 10.2. Text mode replay

После успешного strict decode:

- сохранённый `stdout.bin` печатается в stdout;
- сохранённый `stderr.bin` печатается в stderr;
- исходный remote exit code **не становится process exit code replay-команды**.

Process exit codes replay:

- `0` — локальный replay найден, полностью сохранён и декодирован;
- `2` — retained bytes успешно декодированы, но запись partial/truncated/unknown/abandoned; это локально неполный replay, а не новый результат remote command;
- `1` — lookup/security/metadata/decode error, полезный replay не сформирован.

Это предотвращает ошибочную интерпретацию replay как повторного выполнения исходной команды.

### 10.3. JSON replay

`replay --json` печатает в stdout ровно один JSON-объект. Он содержит как минимум:

```json
{
  "schema_version": 1,
  "tool": "ssh_relay",
  "action": "replay",
  "request_id": "...",
  "session": "prod",
  "source_action": "exec",
  "source_operation_status": "unknown",
  "source_command_status": "unknown",
  "source_command_exit_code": null,
  "encoding": "cp866",
  "stdout": "...",
  "stderr": "...",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "replay_complete": false,
  "error_code": null,
  "error_message": null
}
```

Исход remote outcome всегда обозначен `source_*` и не смешивается с локальным исходом replay.

### 10.4. Изменение существующего `exec --json`

Существующий машинный контракт остаётся одним JSON-объектом и сохраняет текущие поля/exit semantics. Добавляются только additive fields:

- `request_id`;
- `replay_status`: `available | partial | disabled | unavailable`;
- `replay_truncated`: boolean.

`schema_version` существующего machine result остаётся `1`: расширение additive, существующие поля не меняют смысл. Реализация обязана тестом подтвердить, что stdout по-прежнему содержит ровно один JSON-объект.

Поле `output_encoding` в исходном machine result остаётся совместимым с текущим `utf-8-replace`. Но replay source-of-truth — raw bytes, а классификация `clean/suspect` выполняется отдельным strict probe; результат lossy presentation нельзя использовать как доказательство clean output.

## 11. Text mode исходной команды

Для обычного `exec`/`sudo-exec` без `--json` v1 не добавляет служебных строк ни в stdout, ни в stderr.

Текущая presentation-модель остаётся совместимой:

- remote stdout -> stdout CLI;
- remote stderr -> stderr CLI;
- remote exit code -> process exit code для обычной команды.

Текущий `utf-8-replace` presentation может временно остаться ради обратной совместимости, но:

- raw bytes записываются до него;
- строгая UTF-8-проверка определяет retention class;
- replay никогда не использует lossy decode.

Автоматическая смена кодировки исходного text mode и автоматическое определение charset не входят в v1.

Для чувствительного вызова предусматривается `--no-replay`: daemon не должен создавать raw record. Это opt-out от дополнительного локального хранения, а не изменение remote execution semantics.

## 12. Lifecycle state machine

Логический lifecycle:

```text
          storage unavailable
                 |
                 v
            [DISABLED]

[ALLOCATING] -> [ACTIVE] -> [COMPLETED]
                    |             |
                    |             +-> clean TTL/GC
                    |             +-> suspect TTL/GC
                    |
                    +-> [UNKNOWN] ----> suspect TTL/GC
                    |
                    +-- daemon crash --> [ABANDONED] -> suspect TTL/GC
```

### 12.1. До remote execution

1. CLI создаёт/валидирует `request_id`.
2. Daemon под storage lock выполняет bounded GC и reservation.
3. Создаются приватный UUID-каталог, `stdout.bin`, `stderr.bin` и initial `metadata.json` со state `active`.
4. Только после этого начинается capture remote output.

Если безопасная storage allocation не получилась, record не создаётся частично в альтернативном месте.

### 12.2. Во время команды

- каждый raw chunk записывается bounded способом;
- counters/hashes обновляются инкрементально;
- active metadata не переписывается на каждый chunk;
- memory usage не зависит от полного размера replay.

### 12.3. Финализация

После известного исхода daemon:

1. flush/fsync raw files в разумной платформенной форме;
2. закрывает writer handles;
3. формирует terminal metadata;
4. публикует metadata атомарной заменой временного файла;
5. освобождает reservation;
6. выполняет opportunistic GC.

## 13. Crash, disconnect и `unknown`

### 13.1. SSH disconnect во время команды

Существующее правило сохраняется: remote operation автоматически не повторяется, outcome `unknown`.

Доступные к моменту разрыва raw bytes финализируются как `unknown/suspect` в пределах текущего budget. Replay этих байтов не доказывает, что удалённая команда завершилась или не завершилась.

### 13.2. Потеря локального ответа после возможной доставки

Если daemon успел финализировать record, запись остаётся доступной по заранее созданному `request_id`, даже если клиент получил machine `unknown` или вообще не получил ответ.

Replay может показать raw output, но **не отменяет** консервативную семантику `unknown`, если terminal outcome не был достоверно зафиксирован daemon.

### 13.3. Daemon crash

При следующем безопасном сканировании запись `active` проверяется по `writer_identity`.

Если смерть writer доказана strong fingerprint, запись переклассифицируется в `abandoned/suspect`. Если доказать смерть нельзя, запись остаётся active/stale и не удаляется по одному PID; byte reservation всё равно учитывается до TTL/консервативной recovery policy.

### 13.4. Corrupt metadata

Повреждённый metadata:

- не участвует в `--last`;
- не даёт путь для чтения/удаления;
- фиксированные `stdout.bin`/`stderr.bin` могут быть доступны только при явном валидном `request_id` и после проверки, что UUID-dir и файлы являются обычными объектами без symlink/reparse;
- source outcome в таком случае считается неизвестным;
- запись учитывается в global disk budget по фактическому размеру фиксированных файлов.

## 14. GC и retention model

### 14.1. Триггеры GC

GC выполняется bounded и opportunistic:

- при старте daemon;
- перед reservation нового replay request;
- после финализации request;
- периодически работающим daemon не чаще одного раза в минуту;
- при запуске `replay` допускается только безопасный bounded GC terminal records.

GC никогда не является причиной remote retry.

### 14.2. Межпроцессный lock

Несколько daemon используют один replay root, поэтому глобальный budget требует межпроцессной синхронизации.

Используется фиксированный `replay/v1/gc.lock`, создаваемый атомарно (`O_CREAT|O_EXCL` или эквивалент). Lock содержит только PID/start fingerprint владельца и timestamp.

Stale lock разрешено удалить только если смерть владельца доказана strong identity. Если доказательства нет, GC/reservation пропускается/отклоняется; нельзя «лечить» lock удалением по возрасту или PID без fingerprint.

### 14.3. Порядок eviction

При превышении count/byte budgets:

1. expired clean;
2. clean с доказанно мёртвым owner chain;
3. oldest clean;
4. expired suspect;
5. suspect с доказанно мёртвым owner chain;
6. oldest suspect.

Активный record не удаляется GC. Если после очистки новую reservation нельзя разместить без превышения session/global limit, replay нового request становится `unavailable`.

Suspect сохраняется предпочтительно clean, но не имеет иммунитета от hard global limit.

### 14.4. Без рекурсивного удаления произвольного дерева

Cleanup разрешено удалять только:

- валидный UUID request directory под фиксированным replay root;
- известные fixed files внутри него;
- пустой request directory после проверки.

Если внутри UUID-dir обнаружен неизвестный entry, symlink/reparse point или неожиданный тип, обычный GC **не выполняет рекурсивный `rmtree`** и помечает запись проблемной для консервативного последующего разбора.

## 15. Filesystem security

### 15.1. POSIX

Обязательные свойства:

- replay root и request directories: `0700`;
- raw/metadata/lock files: `0600`;
- `umask 077`;
- создание новых файлов с `O_EXCL`;
- проверка `lstat/fstat` на regular file;
- отказ при symlink в любом управляемом компоненте;
- atomic replace metadata только внутри уже проверенного UUID-dir.

GC не следует symlink и не удаляет путь, полученный из metadata.

### 15.2. Windows

Replay размещается только под пользовательским `%LOCALAPPDATA%/.../ssh_relay/replay`.

Обязательные свойства:

- reject symlink/junction/любой `FILE_ATTRIBUTE_REPARSE_POINT` в replay path;
- request dir и файлы создаются без обхода проверенного root;
- ACL пользовательского replay root должна быть проверена как не допускающая чтение другими обычными локальными пользователями; проверка/настройка может использовать Win32 API через stdlib `ctypes`;
- если приватную ACL нельзя установить или подтвердить, replay отключается для данного request, а не создаётся в менее защищённом месте.

### 15.3. Windows locking/cleanup

Windows может отказать в replace/unlink, пока другой процесс держит handle.

Политика:

- writer закрывает raw handles до terminal metadata replace;
- `replay` по умолчанию не читает `active` record;
- GC работает только с terminal/dead-writer records;
- sharing violation/permission error означает «оставить record и повторить GC позже»;
- никакого force-delete, переименования наружу replay root или рекурсивного обхода вокруг lock.

### 15.4. POSIX cleanup

На POSIX unlink открытого файла возможен, но v1 всё равно применяет ту же консервативную модель: active record не удаляется. Это сохраняет одинаковые lifecycle-инварианты на всех ОС.

## 16. Взаимодействие с machine JSON

Существующие operation statuses и process exit codes `exec --json`/`sudo-exec --json` не меняются.

Replay особенно полезен при:

- `operation_status=unknown`;
- `partial_success` risky-команды;
- успешном remote exit при ошибочном отображении текста.

Но наличие replay **никогда** не преобразует `unknown` в `succeeded` само по себе. Для смены outcome нужны существующие независимые доказательства протокола, а не факт наличия байтов.

`request_id` не должен попадать в safe receipt как замена `transaction_id`/`receipt_id`. При необходимости будущей корреляции receipt может получить отдельное additive поле только отдельным изменением контракта; Issue #41 этого не делает.

## 17. Jobs

Replay v1 не создаётся для внутренних job-control `exec` requests.

Причины:

- они read-only либо являются коротким управляющим протоколом над отдельным job lifecycle;
- `job start` уже имеет собственные unknown/identity safeguards;
- долгий пользовательский output находится в удалённом объединённом `log`, а не в stdout/stderr короткого launcher;
- `job tail` уже bounded и может безопасно повторять read-only чтение журнала без повторного запуска job.

Отдельный binary job-tail design потребуется только если реальная проблема кодировок job-журнала будет подтверждена.

## 18. Transfers

`upload`/`download` не участвуют в replay v1.

Их данные уже являются байтами и передаются bounded chunks, а recovery строится вокруг partial files, offsets и повторных probe. Создание `stdout.bin`/`stderr.bin` для transfer не добавляет возможности восстановления кодировки и увеличивает риск лишнего хранения чувствительных данных.

Прогресс/диагностический текст transfer также не является replay source v1.

## 19. Compatibility constraints

1. Обычный text `exec`/`sudo-exec` сохраняет текущие stdout/stderr и remote exit semantics.
2. `--json` сохраняет ровно один JSON object; новые поля additive.
3. Старый клиент может работать с новым daemon без знания replay.
4. Новый клиент может отправить `request_id` старому daemon: старый daemon его игнорирует; выполнение команды остаётся прежним, а `replay_status` считается `unavailable`.
5. Новый daemon должен рекламировать `replay_schema_version=1` в read-only `status`, чтобы будущий `--require-replay` или диагностика могли подтвердить capability без risky action.
6. Replay CLI читает локальное состояние напрямую и не зависит от живой session registration.
7. `request_id` не влияет на идемпотентность remote command.
8. `unknown`/`partial_success` не разрешают автоматический retry.
9. Реализация не меняет build identity/version contract из Issue #40; #40 и #41 остаются раздельными.
10. Новых runtime dependencies не требуется: filesystem, hashing, codec lookup, UUID, process inspection/Win32 `ctypes` реализуемы Python stdlib.

## 20. Test plan

### 20.1. Raw capture и decode

- stdout с невалидным UTF-8 сохраняется byte-for-byte и replay корректно декодирует его выбранным codec;
- stderr проверяется независимо;
- NUL и произвольные bytes не повреждают storage;
- replay strict UTF-8 падает, если bytes невалидны;
- `replace`/`ignore` отсутствуют в replay CLI;
- хеш полного потока совпадает с incremental hash без полного накопления в RAM.

### 20.2. Раздельные потоки

- `stdout.bin` и `stderr.bin` не смешиваются;
- отсутствие interleaving log явно проверяется;
- replay пишет каждый decoded stream в соответствующий локальный stream.

### 20.3. Limits/rolling

- >4 МиБ stdout сохраняет только tail и корректный `dropped_prefix_bytes`;
- то же для stderr;
- один request никогда не превышает 8 МиБ reservation;
- session limit 16 МиБ;
- global limit 64 МиБ при нескольких daemon/processes;
- count limits 8/session и 32/global;
- active request при pressure не растёт сверх reservation;
- если reservation невозможна, команда не создаёт безлимитный fallback storage.

### 20.4. Clean/suspect/TTL

- clean strict UTF-8 получает короткий TTL;
- invalid UTF-8 -> suspect;
- truncation -> suspect;
- unknown/abandoned -> suspect;
- nonzero remote exit без decode/storage проблемы остаётся clean;
- replay access не продлевает TTL;
- suspect вытесняется позже clean, но global hard limit соблюдается.

### 20.5. Request identity и parallelism

- два параллельных CLI с одной/разными сессиями получают разные request_id;
- caller-provided UUID проходит round-trip;
- invalid/non-v4 UUID отклоняется до remote request;
- `--last` выбирает запись только при однозначности;
- ambiguous last завершается ошибкой;
- PID reuse с другим start fingerprint не считается тем же owner;
- weak PID identity не используется для unsafe GC/lock recovery.

### 20.6. Crash/unknown

- SSH disconnect после нескольких chunks оставляет partial raw bytes и state unknown;
- потеря клиентского ответа не вызывает повтор command;
- daemon crash оставляет active record, который после доказанной смерти writer становится abandoned;
- corrupt/partial metadata не вызывает traversal;
- replay explicit request_id после daemon shutdown работает локально.

### 20.7. Security

- session name с traversal не влияет на path;
- request_id traversal/не-UUID отклоняется;
- symlink stdout/stderr/metadata/request-dir отклоняются;
- Windows reparse point отклоняется;
- cleanup не удаляет файл вне replay root;
- неизвестный extra entry запрещает recursive cleanup;
- metadata не содержит command text, token, password, key, passphrase, stdin;
- sudo stdin/password не появляется ни в raw metadata, ни в ошибках;
- POSIX modes 0700/0600;
- Windows private ACL проверяется;
- stale lock не удаляется по одному PID.

### 20.8. Existing contracts

- все существующие tests проходят без изменения ожидаемого text stdout/stderr;
- `exec --json` остаётся одним JSON object;
- существующие machine operation_status/process exit semantics сохраняются;
- risky receipt semantics и `transaction_id`/`receipt_id` не меняются;
- job commands не создают replay records;
- transfer commands не создают replay records;
- reconnect не повторяет `exec`/`sudo-exec`.

### 20.9. Cross-platform

Обязательные CI/ручные проверки:

- Windows control-plane: ACL, reparse point, sharing violation, atomic metadata replace, PID creation fingerprint;
- Ubuntu: mode bits, symlink protection, `/proc` start fingerprint, lock/GC;
- macOS/другая POSIX без надёжного `/proc`: weak-identity fallback не должен приводить к unsafe owner deletion;
- Android/Termux: Linux `/proc` path и XDG/home state semantics.

## 21. Explicit non-goals

В Issue #41 не входят:

- постоянный архив вывода;
- `replay save`;
- автоматический retry remote command;
- повтор `exec`/`sudo-exec` из команды replay;
- хранение полного command text;
- хранение stdin/password/token/key/passphrase;
- unlimited output accumulation;
- обязательное накопление полного stdout/stderr в RAM;
- восстановление межпоточного порядка stdout/stderr;
- автоматическое определение charset;
- изменение job storage/protocol;
- binary replay transfers;
- изменение receipt semantics;
- изменение build identity/version contract из Issue #40;
- защита от root/Administrator или владельца той же локальной учётной записи.

## 22. Решение: implement или не implement

**Решение дизайна: implement после принятия этого design.**

Причины:

1. На текущем `main` исходные bytes необратимо теряются при `utf-8` + `errors="replace"`.
2. Повтор удалённой команды ради восстановления текста противоречит существующей модели `unknown`/`partial_success` и может повторить внешнее изменение.
3. Raw capture можно встроить на границе `recv()` без расширения возможности remote execution.
4. Жёсткие per-request/per-session/global reservations делают локальный storage bounded.
5. Replay можно сделать локальной read-only операцией, независимой от живого SSH/daemon.
6. Отдельный `request_id` решает корреляцию параллельных invocations и потерянного ответа без изменения text stdout contract.

Код реализации **не должен** входить в design PR.

После принятия этого документа нужно создать отдельный implementation Issue со scope:

- новый bounded replay storage/helper;
- raw capture для public `exec`/`sudo-exec`;
- request_id transport/capability;
- `replay` CLI;
- GC/process identity/platform security;
- тесты из раздела 20;
- без изменений #40, jobs protocol и transfers protocol.
