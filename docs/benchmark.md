# Stage 1 benchmark

Benchmark является исполняемым контрактом качества, а не демонстрационным сценарием. Suite [stage1-suite.json](../benchmarks/stage1-suite.json) содержит 17 детерминированных cases:

- 4 `train`;
- 3 `evidence`;
- 10 `holdout`;
- 6 намеренно неправильных mutation cases внутри holdout, включая четыре TOML
  cross-type случая.

Каждый case задаёт initial workspace, fake plan и ожидаемые execution, verification и
terminal outcomes. UUID задач, планов и actions детерминированно выводятся из schema,
имени suite, `case_id` и hash полного содержимого case. Один UUID поэтому не может
обозначать две разные версии задачи.

## Запуск

```powershell
llmin benchmark benchmarks\stage1-suite.json --seed 0 --output benchmark-report.json
```

Отдельный split:

```powershell
llmin benchmark benchmarks\stage1-suite.json --split holdout
```

Команда возвращает ненулевой exit code, если хотя бы один case не совпал с ожидаемым результатом или mutation case был ошибочно принят.

Проверенный результат хранится в
[`benchmarks/baselines/stage1-foundation.json`](../benchmarks/baselines/stage1-foundation.json).
Интеграционный тест сравнивает с ним manifest, функциональный fingerprint и ключевые
метрики. Намеренное изменение поведения требует явного обновления baseline вместе с
объяснением причины в review.

## Fingerprints

Отчёт содержит:

- `suite_fingerprint` — hash канонического manifest;
- `environment_fingerprint` — Python, ОС, архитектура и версия LLMIN;
- `observed_outcome_fingerprint` — только наблюдаемое поведение: terminal state,
  execution/verifier outcomes, error types и terminal reason, evidence hashes, change manifest и
  каноническая последовательность типов trace events;
- `evaluation_fingerprint` — наблюдаемое поведение вместе с ожиданиями manifest и
  результатом оценки;
- latency и подробности каждого case;
- число LLM calls и переменную стоимость baseline.

Wall-clock latency, timestamp и случайные evidence/event IDs не входят в observed
fingerprint. Поэтому повторный запуск
с тем же suite и реализацией сравнивается по функциональным результатам, не требуя
одинакового порядка исполнения или побитового совпадения времени.
Initial workspace материализуется с каноническими LF-переносами, поэтому файловые hashes
совпадают на Windows и Linux.

## Mutation gate

Шесть holdout-case содержат неправильный action result относительно postcondition.
Четыре из них проверяют строгую TOML-семантику: `true`/`1`, `false`/`0`, `1`/`1.0`
и `"1"`/`1` не считаются равными. Mutation-контракт требует успешного execution,
`FAILED` от verifier и строго различающиеся action/expected значения. Тест подменяет
verifier реализацией «always pass» и доказывает, что benchmark фиксирует шесть
`unsafe_acceptances`, ноль `safe_rejections` и закрывает quality gate.

Этот suite проверяет только фундамент `config_patch`. Он ещё не является финальным Stage 1
benchmark на пяти семействах и не используется для promotion знаний. Split сейчас
изолирован логически, но все cases находятся в одном открытом manifest. До реализации
crystallization holdout должен быть вынесен в API или хранилище, недоступное генератору
кандидатов.
