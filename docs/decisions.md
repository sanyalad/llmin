# Реестр архитектурных решений

Этот файл является индексом решений, а не заменой ADR. Новое необратимое или дорогое для
пересмотра решение оформляется отдельным `docs/adr/NNNN-title.md`.

| Решение | Статус | Причина | Запись |
|---|---|---|---|
| Начать с терминального вертикального среза | принято | воспроизводимость и строгая граница полномочий | [ADR-0001](adr/0001-terminal-vertical-slice.md) |
| Считать память управляемым жизненным циклом | принято | знание стареет, конфликтует и требует provenance | [ADR-0002](adr/0002-memory-is-a-lifecycle.md) |
| Использовать SQLite для Memory v0 | принято для Stage 1 | транзакции, один переносимый файл, простое тестирование | [Memory v0](memory.md) |
| Начать routing без embeddings | принято для первого router | объяснимость и проверяемая совместимость важнее recall | [Stage 1 plan](stage-1-plan.md) |
| Отделить verifier от executor | обязательный инвариант | success исполнителя не доказывает postcondition | [Verification](verification.md) |
| Не активировать знание прямо из LLM | обязательный инвариант | LLM предлагает гипотезу, система решает по evidence | [Manifesto](manifesto.md) |

## Решения, требующие ADR

- единый artifact registry для episode, rule, experiment и skill;
- формат provider-neutral LLM adapter и structured output recovery;
- физическая изоляция holdout;
- политика canary, quarantine и revalidation;
- критерии статистической значимости Stage 1;
- переход с SQLite на другой backend.

## Шаблон решения

```text
Context:
Decision:
Alternatives:
Consequences:
Safety and rollback:
Evidence required to revisit:
```

