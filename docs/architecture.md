# Архитектура LLMIN

Этот документ задаёт карту системы. Он отделяет уже работающий вертикальный срез от
целевой архитектуры, чтобы проект не принимал намерение за реализованную возможность.

## Архитектурный тезис

LLM — заменяемый планирующий ресурс. Доверие создаёт не ответ модели, а замкнутый контур:

```text
Task → Orchestrator → Planner → Executor → Verifier → Evidence → Memory → Skill
          │                         │           │          │
          └── policy / budget ─────┴───────────┴──────────┘
```

Успешное выполнение ещё не является знанием. Опыт становится повторно используемым только
после независимой проверки, сохранения происхождения и проверки области применимости.

## Инварианты

1. `COMPLETED` достижим только после независимого verdict `PASSED`.
2. Planner не исполняет действия и не определяет истинность результата.
3. Executor действует только через явно разрешённые capabilities внутри sandbox.
4. Evidence неизменяемо и связано с task, attempt, trace и verification report.
5. MemoryArtifact — версия проверяемого утверждения, а не вечная истина.
6. Skill применяется только при совместимом applicability contract и наличии fallback.
7. Неуспех, противоречие и откат сохраняются как данные, а не скрываются.
8. Повторное использование считается обучением только при сохранении качества и снижении
   измеренной стоимости.

## Текущий вертикальный срез

На текущем этапе реализован детерминированный путь:

```text
TaskSpec
  → OrchestratorRun
  → FakePlanner
  → capability authorization
  → sandbox Executor
  → independent VerificationService
  → PipelineResult
  → AttemptRecord / TraceEvent / Evidence
  → explicit Episode creation (not automatic yet)
  → SQLite + content-addressed blob store
```

Поддерживаются ограниченные файловые действия и проверяемые postconditions. Реальный
LLM planner, Knowledge Router, кристаллизация и автоматическое повышение skill пока не
являются рабочими возможностями.

## Целевая петля обучения

```text
                    ┌──────────── fallback ────────────┐
Task → Router ──────┼→ known Skill → Executor          │
                    └→ LLM Planner → Executor          │
                                      ↓                │
                                   Verifier ───────────┘
                                      ↓
                              Evidence + Episode
                                      ↓
                               Pattern detection
                                      ↓
                         Rule / Skill candidate
                                      ↓
                         shadow → canary → active
```

Активация не следует из уверенности LLM. LLM может предложить гипотезу; система принимает
решение по результатам независимой оценки.

## Контуры доверия

- Контур полномочий: Task constraints → authorization → sandbox.
- Контур истинности: expected outcome → verifier → evidence.
- Контур памяти: trace → episode → provenance → retention.
- Контур обучения: episodes → candidate → shadow evaluation → activation.
- Контур экономики: baseline → learned route → качество, стоимость, время, LLM calls.
- Контур восстановления: contradiction → quarantine → fallback → revalidation.

## Границы Stage 1

В scope: контролируемая терминальная среда, небольшие воспроизводимые семейства задач,
SQLite, exact-match routing, provider-neutral planner boundary и offline evaluation.

Вне scope: собственная LLM, GPU-инфраструктура, универсальный GUI-оператор, сложный RAG,
графовая БД, автономное расширение полномочий и самопереписывание кода.

## Связанные документы

- [Компоненты](components.md)
- [Понятия и жизненный цикл](concepts.md)
- [Глоссарий](glossary.md)
- [Архитектурные решения](decisions.md)
- [Roadmap](roadmap.md)
- [Журнал экспериментов](experiments.md)
