# Stage 1 benchmark

Benchmark является исполняемым контрактом качества, а не демонстрационным сценарием. Suite [stage1-suite.json](../benchmarks/stage1-suite.json) содержит 13 детерминированных cases:

- 4 `train`;
- 3 `evidence`;
- 6 `holdout`;
- 2 намеренно неправильных mutation cases внутри holdout.

Каждый case задаёт initial workspace, fake plan и ожидаемые execution, verification и terminal outcomes. UUID задач, планов и actions детерминированно выводятся из имени suite и `case_id`.

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
- `outcome_fingerprint` — функциональные результаты в каноническом порядке `case_id`,
  независимо от seed-порядка исполнения;
- latency, terminal state, verdict и число trace events для каждого case;
- число LLM calls и переменную стоимость baseline.

Wall-clock latency и timestamp не входят в outcome fingerprint. Поэтому повторный запуск
с тем же suite и реализацией сравнивается по функциональным результатам, не требуя
одинакового порядка исполнения или побитового совпадения времени.

## Mutation gate

Два holdout-case содержат неправильный action result относительно postcondition. Настоящий verifier переводит их в `FAILED`. Тест подменяет verifier реализацией «always pass» и доказывает, что benchmark фиксирует две `unsafe_acceptances` и закрывает quality gate.

Этот suite проверяет только фундамент `config_patch`. Он ещё не является финальным Stage 1 benchmark на пяти семействах и не используется для promotion знаний.
