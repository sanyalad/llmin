# Компоненты LLMIN

Для каждого компонента фиксируются назначение, вход, выход, граница доверия и текущий статус.

| Компонент | Назначение | Вход | Выход | Статус |
|---|---|---|---|---|
| Task Gateway | Валидировать задачу и ограничения | внешний запрос | `TaskSpec` | контракт реализован |
| Orchestrator | Управлять допустимыми состояниями попытки | `TaskSpec`, события | terminal state, trace identity | реализован |
| Knowledge Router | Найти совместимый проверенный опыт | task family, environment, verifier, constraints | route decision | запланирован |
| Planner | Построить структурированный план | `TaskSpec`, context | `ExecutionPlan` | интерфейс + fake |
| Executor | Авторизовать и выполнить действия | task, plan | `ExecutionReport` | реализован |
| Verifier | Независимо проверить postconditions | task, after state | `VerificationReport` | реализован |
| Evidence Store | Сохранить неизменяемые доказательства | trace, reports, evidence | audit journal | SQLite v0 |
| Memory | Управлять опытом и его жизненным циклом | verified attempt | artifacts, relations | episode v0 |
| Crystallizer | Обобщить эпизоды в проверяемый кандидат | episodes | rule/skill candidate | только контракты |
| Economist | Сравнить полезность и полную стоимость | cost ledger, outcomes | retain/route decision | запланирован |

## Task Gateway

Принимает только версионированный `TaskSpec`. Задача обязана иметь хотя бы одну обязательную
postcondition, нормализованный workspace, бюджет, risk class и явные capabilities. Gateway
не должен угадывать полномочия из текста objective.

## Orchestrator

Владеет состоянием попытки и идентичностями `task_id`, `trace_id`, `attempt_id`. Он не
реализует бизнес-действия. Недопустимый переход является ошибкой системы. Авария должна
оставлять диагностируемый open attempt, а не бесследный trace.

## Planner

Преобразует задачу в `ExecutionPlan`. План содержит только структурированные actions.
Planner не получает права непосредственно обращаться к файловой системе. Для Stage 1
`FakePlanner` обеспечивает воспроизводимые fixtures; будущий LLM adapter обязан сохранять
тот же контракт и учитывать бюджет.

## Executor

Сначала проверяет plan against task constraints, затем выполняет actions через registry
capabilities. Sandbox ограничивает пути и фиксирует изменения. Ошибка действия не может
быть преобразована в успешный результат.

## Verifier

Назначение: независимо доказать, что after state соответствует обязательным postconditions.

Вход:

- task и ожидаемый outcome;
- фактическое состояние workspace;
- идентичности trace и attempt.

Выход:

- `PASSED`, `FAILED` или `INCONCLUSIVE`;
- покрытые postconditions;
- reason/errors;
- content-addressed evidence.

Verifier не доверяет заявлению Executor и не переиспользует его success flag как доказательство.

## Evidence Store и Memory

Evidence journal отвечает на вопрос «что наблюдалось?». MemoryArtifact отвечает на вопрос
«что система считает пригодным для будущего использования?». Эти хранилища логически
разделены: удаление payload эпизода не должно уничтожать доказательную цепочку.

## Knowledge Router

До embeddings использует точное совместное совпадение:

```text
task family
+ environment compatibility
+ verifier compatibility
+ constraints compatibility
```

Ответ должен объяснять решение: artifact, confidence и причины match/reject. Неопределённость
маршрутизируется в planner, а не маскируется слабым совпадением.

## Crystallizer и Skill runtime

Crystallizer ищет повторяемый паттерн, формирует гипотезу и отправляет её в изолированную
оценку. Skill получает executable payload, applicability contract, provenance, verifier suite
и fallback route. Путь активации: `candidate → shadow → canary → active`; деградация ведёт
в `quarantined`, а не к тихому продолжению использования.

