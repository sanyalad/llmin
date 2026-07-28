# Roadmap LLMIN

Roadmap управляется доказательными воротами. Номер уровня не означает календарное обещание.

## Уровень 0 — архитектурная база

Цель: единая модель мира и проверяемые границы системы.

Готово, когда handbook описывает компоненты, понятия, решения, эксперименты и roadmap без
смешения реализованного и целевого состояния. Этот набор документов закрывает структуру
уровня; актуальность поддерживается вместе с изменениями контрактов.

## Уровень 1 — MVP замкнутой петли

Цель: на семействе config patch показать `сделал → проверил → запомнил → использовал снова`.

Текущий фундамент: строгие контракты, fake planner, sandbox executor, независимый verifier,
trace/evidence, benchmark и Memory v0.

Следующие ворота:

1. закрыть детерминированность и identity/provenance persistence;
2. добавить exact-match Knowledge Router;
3. получить первый вручную подготовленный skill;
4. сравнить baseline и learned route на закрытом holdout;
5. показать неухудшение качества и снижение LLM calls/cost/latency.

## Уровень 2 — Memory System v0

Цель: надёжный lifecycle `Trace → Episode → Rule → Skill`. Требуются artifact registry,
reconciliation, conservative GC, миграции и автоматический cost ledger.

## Уровень 3 — Knowledge Router

Цель: объяснимый ответ «знаю ли я, как это делать?». Сначала exact match по task family,
environment, verifier и constraints; embeddings рассматриваются только после измерения
ошибок recall/precision.

## Уровень 4 — кристаллизация

Цель: получать кандидаты из набора episodes, проверять их на изолированных данных и сохранять
происхождение. LLM предлагает pattern/rule hypothesis, но не активирует её.

## Уровень 5 — shadow и canary

Цель: безопасно сравнивать новый skill с основным маршрутом и автоматически отправлять
деградировавшее знание в quarantine с fallback.

## Уровень 6 — Memory Economist

Цель: принимать решения retention/routing по будущей полезности, recovery value, полной
стоимости хранения/поиска и retrieval noise. Реализуется только после накопления статистики.

## Уровень 7 — полноценная память

Цель: provenance graph, contradiction resolution, revalidation и строгие applicability
contracts для всех видов artifacts.

## Уровень 8 — компьютерный агент

Цель: подключать browser/files/apps/CAD как новые capabilities за теми же границами
Executor → Verifier → Evidence. Не начинается до доказательства терминального контура.

## Уровень 9 — многоуровневая память

Цель: разделить global, organization, user и session scopes. В более широкий scope попадает
только доказанное, безопасное и действительно переносимое знание.

## Ближайший порядок инкрементов

Подробный технический backlog и критерии готовности находятся в
[плане улучшений Memory v0](improvement-plan.md). При конфликте roadmap определяет направление,
а improvement plan — ближайшую последовательность инженерных работ.

