# План улучшений после ревью Memory v0

Дата ревью: 2026-07-27
Проверенный commit: `ab8a85c` (`Persist complete pipeline attempts`)

## Итоговая оценка

Memory v0 задаёт правильные архитектурные границы:

- evidence journal отделён от управляемых объектов памяти;
- завершённая попытка хранит task, plan, execution, verification и environment;
- финализация attempt и запись verifier evidence объединены одной SQLite-транзакцией;
- payload episode удаляется через аудируемый tombstone;
- benchmark разделяет наблюдаемое поведение и оценку относительно ожиданий;
- TOML verifier использует строгое сравнение типа и значения.

Переход к LLM planning и автоматической кристаллизации пока преждевременен. Сначала
необходимо устранить недетерминированную сериализацию и замкнуть идентичность
`task → attempt → trace → report → evidence → episode`.

## Наблюдения ревью

Штатный запуск сначала дал `89 passed, 1 failed, 2 skipped`: повторная запись одного
завершённого attempt иногда нарушает заявленную идемпотентность. Повторный полный запуск
прошёл (`90 passed, 2 skipped`), а серия из 20 отдельных запусков воспроизвела один отказ.
Отдельная проверка round-trip сериализации `TaskSpec` воспроизвела изменение JSON в 2 из
50 процессов. Причина — неканонический порядок set-подобных полей, которые сравниваются как
JSON-массивы.

Ручные негативные проверки также подтвердили:

- episode принимается с несуществующими `verification_report_ids` и
  `parent_artifact_ids`;
- episode принимается без environment fingerprint, хотя документация требует environment
  compatibility;
- повторный `begin_attempt()` с теми же аргументами, но автоматическим `created_at`, не
  идемпотентен;
- результат сбоя planner можно записать под другим `TaskSpec`: task в `AttemptRecord` и task
  в trace journal расходятся;
- текущий benchmark проходит 17 из 17 cases и закрывает шесть mutation cases.

## P0 — восстановить детерминированность persistence gate

### 1. Каноническая сериализация контрактов

Не использовать обычный `model_dump_json()` как представление идентичности для моделей с
`frozenset`. Ввести одну функцию canonical document:

```text
model
  → JSON-compatible structure
  → сортировка всех set-подобных коллекций
  → стабильное представление Decimal, UUID и datetime
  → canonical JSON bytes
```

Эту функцию должны использовать:

- immutable-record comparison;
- content fingerprints;
- SQLite documents, если их побайтовая стабильность является контрактом;
- golden persistence fixtures.

Критерии готовности:

- повторная запись finalized attempt всегда идемпотентна;
- round-trip каждого внешнего контракта стабилен при разных `PYTHONHASHSEED`;
- минимум 100 последовательных запусков persistence-теста без флейков;
- тест намеренно меняет порядок входных set-полей и получает тот же canonical document.

### 2. Разделить identity и presentation serialization

Человекочитаемый JSON может сохранять удобный порядок полей, но проверка неизменности должна
сравнивать canonical content hash, а не случайный порядок массивов. В таблицах стоит хранить:

```text
document
document_sha256
```

При чтении hash пересчитывается. Это обнаруживает повреждение документа и упрощает
идемпотентную вставку.

## P1 — замкнуть доказательную целостность attempt

### 3. Создавать open attempt до запуска pipeline

Текущий recorder вызывается после `Pipeline.run()`, поэтому crash может оставить trace без
attempt. Нужен coordinator:

```text
prepare task + environment
  → begin_attempt
  → run pipeline with fixed trace_id/attempt_id
  → finalize_attempt
```

`OrchestratorRun` должен принимать заранее созданные идентификаторы. `PipelineResult` должен
явно содержать `task_id`, а не полагаться на косвенные plan/report ссылки.

Критерии готовности:

- planner failure нельзя записать под другой task;
- каждый persisted trace с известным attempt связан с существующей open/finalized записью;
- task_id и trace_id всех событий совпадают с AttemptRecord;
- повторный запуск coordinator не создаёт новую попытку;
- аварийное завершение оставляет диагностируемый open attempt.

### 4. Проверять полноту attempt перед финализацией

`finalize_attempt()` должен проверять journal, а не только вложенные отчёты:

- trace принадлежит тому же `task_id`, `trace_id` и `attempt_id`;
- присутствует начальный transition;
- присутствует terminal transition, соответствующий `final_state`;
- plan/report identities согласованы;
- evidence принадлежит VerificationReport этого attempt;
- `COMPLETED` требует успешный execution и независимый `PASSED`;
- отсутствие обязательной части даёт явную ошибку `IncompleteAttempt`.

Следует отдельно определить допустимые неполные записи для planner, authorization, executor и
verifier failures.

### 5. Сделать `begin_attempt()` действительно идемпотентным

Автоматический `created_at` нельзя включать в повторно вычисляемый candidate до проверки
существующей записи. Алгоритм:

1. прочитать attempt по ID;
2. если он существует — сравнить только входной identity envelope и вернуть его;
3. если отсутствует — один раз назначить timestamp и вставить.

Добавить тест повторного прямого вызова без явного `created_at`.

## P1 — усилить provenance MemoryArtifact

### 6. Валидировать все четыре типа provenance

При создании episode сейчас проверяются только trace и evidence. Необходимо проверять:

- `source_event_ids` существуют и относятся к task/attempt;
- `evidence_ids` существуют, относятся к attempt и входят в его VerificationReport;
- `verification_report_ids` существуют и относятся к тому же attempt;
- `parent_artifact_ids` существуют в artifact registry;
- parent не является tombstoned без сохранённого достаточного evidence envelope;
- provenance не образует запрещённый цикл.

Если failed attempt разрешено превращать в episode, это должно быть явным типом outcome, а не
побочным эффектом отсутствующей проверки.

### 7. Связать episode с EnvironmentRecord

Для episode следует требовать хотя бы один environment fingerprint и проверять, что fingerprint
исходного attempt включён в applicability. Пустой набор можно оставить только для
неактивированных rule/experiment candidates со смыслом `environment_unknown`.

Не следует использовать пустой набор одновременно как «не исследовано» и «подходит везде».

### 8. Ввести единый artifact registry

Сейчас relations знают только таблицу episodes, хотя контракты уже описывают rule, experiment и
compiled skill. Полезна базовая таблица:

```text
memory_artifacts(
    artifact_id,
    kind,
    state,
    content_hash,
    document,
    document_sha256
)
```

Специализированные payload-таблицы могут ссылаться на неё. Тогда provenance, relations,
contradictions и verifier results проверяются одинаково для всех видов артефактов.

## P1 — укрепить Content Addressed Artifact Store

### 9. Закрыть path/reparse boundary

Перед каждой операцией CAS необходимо:

- запретить symlink/junction/reparse-компоненты внутри shard path;
- после resolve проверять, что путь остаётся внутри trusted root;
- не доверять существующему shard-каталогу;
- использовать безопасное создание без следования по ссылкам там, где платформа это позволяет.

Добавить adversarial-тест с заранее созданным symlink/junction для `digest[:2]`.

### 10. Добавить эксплуатационные ограничения

- максимальный размер одного blob и общий quota;
- проверка, что `application/json` действительно JSON, а `application/toml` — TOML;
- `fsync` shard-директории после atomic rename на POSIX;
- атомарная обработка конкурентной записи одного digest;
- безопасная валидация `logical_name`;
- отдельная политика для payload, который невозможно надёжно очистить от секретов.

Regex-redaction является защитным слоем, но не доказательством отсутствия секретов. Для
чувствительных источников лучше хранить secret reference или keyed HMAC, а не исходное значение.

## P2 — recovery и эксплуатация SQLite

### 11. Startup reconciliation

При открытии store формировать отчёт, не меняя данные автоматически:

- trace-only attempts;
- open attempts без активности дольше порога;
- evidence без валидного verification report;
- ArtifactBlob references без файла;
- CAS blobs без ссылок;
- terminal traces при open attempt;
- document hash mismatch.

После появления отчёта добавить отдельную явную repair-команду. Автоматически удалять
сомнительные данные при старте нельзя.

### 12. Консервативный garbage collector

Первый GC должен работать только в `--dry-run` и учитывать:

- AttemptRecord references;
- episode/rule/experiment provenance;
- retention minimum;
- открытые contradictions;
- quarantine;
- legal/audit hold;
- grace period после последнего наблюдения.

Удаление blob и tombstoning memory artifact — разные операции и должны иметь разные audit events.

### 13. Настоящие пошаговые миграции

Заменить общий `CREATE TABLE IF NOT EXISTS` на последовательные миграции `vN → vN+1`.
Неподдерживаемая версия не должна успевать изменить базу до отказа.

Дополнительно:

- `PRAGMA integrity_check`;
- явный `busy_timeout`;
- тест двух конкурентных writers;
- решение по WAL с учётом durability;
- индексы по attempt, task, state, kind и created_at;
- SQL CHECK для дублируемых state/status колонок либо отказ от их дублирования.

## P2 — уточнить lifecycle и contradiction semantics

### 14. Монотонное время переходов

`occurred_at` перехода не может быть раньше `created_at` артефакта или предыдущего transition.
Будущее время за допустимым clock-skew должно отклоняться либо явно маркироваться.

### 15. Выход из quarantine только через revalidation

Прямой переход `quarantined → active/cold` должен требовать:

- нового ArtifactVerifierResult;
- совместимого environment;
- причины;
- policy decision ID.

Документация и `_ALLOWED_TRANSITIONS` должны показывать одинаковый граф.

### 16. Однозначное разрешение contradictions

Сейчас один open contradiction теоретически может получить несколько независимых resolution.
Нужно либо:

- запретить более одного superseding resolution уникальным ограничением;
- либо явно поддержать ветвящиеся competing resolutions и отдельный arbitration state.

Для Stage 1 проще первый вариант.

## P2 — сделать cost ledger автоматическим

Ручной `append_cost()` недостаточен для экономических выводов. Instrumentation должна создавать
CostEntry для:

- execution latency и resource units;
- verification;
- artifact bytes × retention duration;
- retrieval;
- revalidation;
- будущих LLM calls/tokens;
- cleanup и recovery.

Метрика с нулём должна означать измеренный ноль, а не отсутствие instrumentation. Для этого в
отчёте нужен признак coverage источников стоимости.

## P3 — следующий функциональный вертикальный срез

После закрытия P0–P2:

1. Реализовать provider-neutral LLM interface и строгий structured planner output.
2. Ввести ContextBundle с provenance, token accounting и причинами включения/исключения памяти.
3. Реализовать exact-match Knowledge Router без embeddings:
   `matched`, `unknown`, `rejected`, `no reliable memory found`.
4. Сохранять RuleArtifact и ExperimentArtifact через общий registry.
5. Добавить candidate mining только из train/evidence.
6. Физически изолировать holdout за evaluator API.
7. Провести offline evaluation и mutation testing кандидатов.
8. Ввести `candidate → shadow → canary → active → quarantined`.
9. Добавить fallback и re-evaluation queue.
10. Только после накопления данных вводить Memory Economist и автоматическое старение.

## Расширение benchmark до выхода Stage 1

Текущие 17 cases хорошо проверяют фундамент `config_patch`, но не основную гипотезу целиком.
Нужно довести suite минимум до 30 задач и пяти семейств:

1. нормализация текста;
2. CSV ↔ JSON;
3. TOML/JSON/YAML config patch;
4. организация и переименование файлов;
5. диагностика небольшого проекта.

Для каждого семейства:

- train/evidence/физически закрытый holdout;
- обычные, граничные и несовместимые входы;
- type-confusion и semantic mutation cases;
- environment drift;
- baseline route и learned route;
- не менее трёх seeds;
- quality, unsafe acceptance, LLM calls, cost, latency и confidence intervals.

## Рекомендуемый порядок инкрементов

```text
I1  Canonical persistence и устранение флейка
 ↓
I2  Pre-run attempt coordinator и identity closure
 ↓
I3  Полная provenance/environment validation
 ↓
I4  CAS hardening, reconciliation и dry-run GC
 ↓
I5  Automatic cost ledger и exact-match router
 ↓
I6  LLM planner + Context Compiler
 ↓
I7  Candidate mining + isolated holdout evaluation
 ↓
I8  Shadow/canary compiled skill + quarantine/fallback
 ↓
Stage 1 report
```

## Definition of Done ближайшего этапа

Memory v0 можно считать закрытой, когда:

- весь test suite стабильно зелёный на Windows/Linux и Python 3.12/3.13;
- persistence-тесты не зависят от hash seed и порядка set-полей;
- attempt создаётся до первого pipeline event;
- task, trace, plan, reports и evidence образуют проверенную цепочку идентичности;
- невозможно сослаться на несуществующий report или parent artifact;
- episode связан с реально сохранённым environment;
- crash/restart создаёт диагностируемое состояние и reconciliation report;
- CAS не выходит за trusted root и соблюдает quota;
- forgetting и recovery остаются аудируемыми;
- документация описывает фактические, а не предполагаемые гарантии.
