# LLMIN

LLMIN — экспериментальная автономная агентная система, которая использует LLM для исследования новых задач, а успешный проверенный опыт постепенно превращает в более дешёвые эвристики и обычный программный код.

> LLM — ресурс системы, а не сама система.

Проект находится на стадии архитектурного прототипа. Первый этап сфокусирован на проверке полного цикла обучения в контролируемой терминальной среде, а не на преждевременной поддержке всех приложений компьютера.

## Основные документы

- [Architect Handbook](docs/architecture.md): [компоненты](docs/components.md),
  [понятия](docs/concepts.md), [глоссарий](docs/glossary.md),
  [решения](docs/decisions.md), [roadmap](docs/roadmap.md),
  [эксперименты](docs/experiments.md)
- [Манифест](docs/manifesto.md)
- [Подробный план первого этапа](docs/stage-1-plan.md)
- [ADR-0001: первый вертикальный срез](docs/adr/0001-terminal-vertical-slice.md)
- [Правила ведения GitHub](docs/github-workflow.md)
- [Локальная разработка](docs/development.md)
- [Sandbox execution](docs/sandbox.md)
- [Независимая верификация](docs/verification.md)
- [Stage 1 benchmark](docs/benchmark.md)
- [Memory v0](docs/memory.md)
- [План улучшений после ревью Memory v0](docs/improvement-plan.md)
- [Как участвовать в разработке](CONTRIBUTING.md)

## Коротко об архитектуре

```text
Task
  ↓
Orchestrator ─── Economist
  ↓                  ↓
Context Compiler   бюджет/риск
  ↓
Known skill? ── yes ─→ Executor ─→ Verifier
  │                              ↓
  no                       Evidence Store
  ↓                              ↓
LLM planner ───────────────→ Crystallizer
                                  ↓
                       heuristic / compiled skill
```

Каждое знание должно иметь область применимости, доказательства, измеренную надёжность, стоимость исполнения, версию среды и путь отката.

## Статус

Текущая цель — Stage 1: доказать, что система способна на ограниченном наборе задач:

1. безопасно выполнить новую задачу с помощью LLM;
2. независимо проверить результат;
3. выделить повторяемую закономерность;
4. повторно решить аналогичную задачу с меньшим участием LLM;
5. обнаружить деградацию знания и откатиться к более гибкому способу решения.

До завершения Stage 1 не входят в scope: управление GUI, браузером, офисными приложениями и CAD; долговременная автономная работа без ограничений; самостоятельное расширение полномочий; обучение моделей.

## Первый исполняемый инкремент

Реализован минимальный проверяемый pipeline: строгие контракты → fake planner → policy → sandbox executor → независимый verifier → терминальное состояние. Валидация не создаёт побочных эффектов:

```powershell
llmin validate-task benchmarks\tasks\config_patch\001.json
```

Воспроизводимый fixture можно прогнать в отдельной копии template workspace:

```powershell
llmin run-fixture <task.json> <plan.json> <base-root>
```

Команда возвращает `attempt_id` и `trace_id`. Сохранённый статус, диагностические события и
evidence можно повторно получить из SQLite, в том числе из нового процесса:

```powershell
llmin show-attempt <base-root>\.llmin\memory.sqlite3 <attempt-id>
```

Состояние `COMPLETED` возможно только после verifier verdict `PASSED`.

## Ограниченный LLM planner

OpenRouter используется только для построения типизированного `ExecutionPlan`. Модель не
получает shell, файловый доступ или сетевые инструменты. План повторно проверяется локальными
контрактами, policy и sandbox до любого изменения.

Сначала можно получить план без исполнения:

```powershell
$env:OPENROUTER_API_KEY = "<secret>"
$env:OPENROUTER_MODEL = "<provider/model>"
llmin plan-task <task.json>
```

Полный путь `plan → authorize → execute → verify → persist` запускается отдельно:

```powershell
llmin run-agent <task.json> <base-root>
```

Ключ нельзя передавать аргументом команды или сохранять в Git. Поддерживающую strict structured
outputs модель следует выбирать явно через `OPENROUTER_MODEL`. Имя провайдера и модели сохраняется
в environment-записи attempt и возвращается командой `show-attempt`.
