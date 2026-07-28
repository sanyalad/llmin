# Понятия и жизненный цикл

## От выполнения к навыку

```text
Trace
  → Episode
  → Rule candidate
  → verified Skill candidate
  → shadow
  → canary
  → active Skill
```

Переходы между уровнями означают рост доказательной силы, а не просто новый формат записи.

## Trace и Attempt

Trace — упорядоченный журнал событий одного исполнения. Attempt — durable envelope,
объединяющий исходный task, environment, plan, execution, verification и terminal state.
Trace объясняет ход работы; Attempt замыкает идентичность и итог.

## Evidence

Evidence — наблюдаемый факт с locator, типом и при возможности SHA-256. Evidence не равно
интерпретации. Verdict строится на evidence, но хранится отдельно, чтобы позднее можно было
проверить verifier или применить новую методику оценки.

## Episode

Episode — один сохранённый случай выполнения. Он содержит краткий полезный payload,
applicability, provenance, retention policy и счётчики использования. Episode может описывать
как успех, так и диагностически полезный неуспех.

## Rule

Rule — обобщённое утверждение, полученное из нескольких episodes. Пока правило не прошло
отдельную проверку, оно остаётся гипотезой и не управляет production route.

## Skill

Skill — исполняемый, версионированный и проверенный способ решения семейства задач. Его
обязательные части:

- входной контракт;
- executable procedure;
- applicability и exclusions;
- verifier suite;
- provenance и reliability;
- cost profile;
- fallback и rollback.

## Applicability

Applicability отвечает на два симметричных вопроса: где знание работает и где оно запрещено.
Пустое значение означает «не исследовано», а не «универсально». Environment fingerprint
исходной попытки должен входить в область применимости episode.

## Provenance

Provenance связывает artifact с исходными events, evidence, verification reports и parent
artifacts. Новая версия не стирает происхождение старой. Противоречие создаёт отдельную
запись и объяснимое решение.

## Состояния памяти

`active` используется маршрутизатором; `cold` сохраняется с пониженным приоритетом;
`quarantined` исключено из применения до revalidation; `tombstoned` лишено payload, но
оставляет аудируемую оболочку.

## Доказательство обучения

Обучение считается показанным только на сопоставимых задачах:

```text
quality_learned ≥ quality_baseline
unsafe_acceptance_learned ≤ unsafe_acceptance_baseline
LLM_calls_learned < LLM_calls_baseline
cost_learned < cost_baseline
latency_learned < latency_baseline
```

Хотя бы одно ресурсное улучшение должно быть статистически различимо, а качество и
безопасность — не деградировать. Одиночная красивая демонстрация не является доказательством.

