# Журнал экспериментов

Эксперимент должен быть воспроизводимым и способным опровергнуть гипотезу. Результат
фиксируется независимо от того, удобен ли он проекту.

## Обязательный протокол

Для каждого эксперимента сохраняются:

- идентификатор, дата, commit и environment fingerprint;
- проверяемая гипотеза и критерий опровержения;
- train/evidence/holdout split и защита от leakage;
- baseline route и candidate route;
- verifier version и mutation cases;
- quality, unsafe acceptance, LLM calls, cost и latency;
- raw report/artifact locators;
- решение: reject, repeat, shadow, canary или active.

## EXP-000 — исполняемый фундамент

Гипотеза: ограниченная config patch задача может пройти полный исполнительный путь, причём
`COMPLETED` невозможно без независимого `PASSED`.

Текущие средства: `benchmarks/stage1-suite.json`, `llmin run-benchmark`, unit и integration
tests. Этот эксперимент подтверждает фундамент исполнения, но не доказывает обучение:
используется fake planner, а learned route отсутствует.

## EXP-001 — первая повторно используемая процедура

Гипотеза: проверенный skill для изменения TOML timeout решает совместимые holdout задачи с
тем же качеством и меньшим числом LLM calls, стоимостью и latency, чем LLM baseline.

Дизайн:

1. зафиксировать provider/model, prompt, цены и минимум три seeds baseline;
2. получить episodes только из train/evidence;
3. создать candidate с явными TOML/version/exclusion contracts;
4. запускать candidate в shadow на физически закрытом holdout;
5. использовать тот же verifier suite для обоих маршрутов;
6. добавить incompatible и semantic mutation cases;
7. активировать только при прохождении заранее заданных ворот.

Минимальные ворота:

| Метрика | Требование |
|---|---|
| Required postconditions | 100% на поддерживаемом holdout |
| Unsafe acceptance | 0 |
| Quality vs baseline | не ниже baseline |
| LLM calls | ниже baseline |
| Cost | ниже baseline с учётом retrieval/verification |
| Latency | ниже baseline либо явно объяснённый trade-off |

Статус: запланирован; до реализации LLM baseline и Knowledge Router запуск невозможен.

## Шаблон записи

```text
Experiment:
Status:
Commit:
Environment:
Hypothesis:
Falsification criterion:
Dataset and split:
Baseline:
Candidate:
Verifier:
Metrics:
Results:
Artifacts:
Decision:
Follow-up:
```

