# Stage 1 — проверяемый цикл кристаллизации

Статус: proposed

Горизонт: 6 недель для одного разработчика или 3–4 недели для небольшой команды

Основная среда: локальная изолированная рабочая папка и терминальные инструменты

## 1. Зачем начинать именно с этого

Главная гипотеза LLMIN состоит не в том, что LLM может управлять компьютером. Это уже технически достижимо. Не доказано другое: может ли система безопасно превращать успешные решения в проверяемые знания, повторно применять их дешевле и автоматически отказываться от них при деградации.

Поэтому Stage 1 реализует один полный вертикальный срез. Поддержка GUI, браузера, Office и CAD откладывается: она добавляет сложность восприятия и управления, но почти не помогает проверить механизм самообучения.

## 2. Цель этапа

Создать локальный прототип, который на ограниченном наборе файловых и терминальных задач умеет:

1. принять формализованную задачу;
2. найти применимый известный навык или обратиться к LLM-планировщику;
3. исполнить план в изолированной рабочей папке;
4. независимо проверить постусловия;
5. сохранить трассу, стоимость, версию среды и доказательства;
6. построить кандидата в эвристику на основании повторяющихся успехов;
7. протестировать кандидата на отложенных вариантах задач;
8. опубликовать подтверждённый детерминированный навык;
9. применить этот навык без LLM;
10. отправить навык в карантин при дрейфе или падении качества и выполнить безопасный fallback.

## 3. Проверяемая гипотеза

Для повторяющихся семейств задач можно сократить использование LLM и среднюю стоимость решения без статистически значимого ухудшения качества, если каждое повторное использование знания ограничено контрактом применимости и независимо проверяется.

Stage 1 считается успешным, если прототип демонстрирует это на воспроизводимом benchmark, а не на одной подготовленной демонстрации.

## 4. Scope

### Входит

- работа только внутри явно заданной временной или тестовой директории;
- чтение, создание и преобразование текстовых файлов;
- безопасные команды из allowlist;
- структурированные задачи с машинно проверяемыми постусловиями;
- LLM как планировщик неизвестных задач через заменяемый provider interface;
- локальная SQLite-память и файловое хранилище артефактов;
- детерминированные исполнители на Python;
- запись трасс, стоимости и доказательств;
- генерация кандидатов в эвристики;
- offline-проверка и публикация навыка;
- деградация, карантин и fallback;
- CLI для запуска задач, просмотра знаний и benchmark.

### Не входит

- произвольное управление всей ОС;
- GUI, браузер, Office, CAD и компьютерное зрение;
- доступ к личным файлам, секретам и учётным записям;
- автономное выполнение необратимых действий;
- распределённое исполнение;
- multi-agent orchestration как самоцель;
- fine-tuning или обучение собственной модели;
- векторная база как обязательная зависимость;
- автоматическое слияние сгенерированного кода в production;
- оптимизация промптов до появления надёжного benchmark.

## 5. Демонстрационные семейства задач

Benchmark должен содержать минимум 30 задач: по 6 вариантов в каждом из 5 семейств. Варианты делятся на train/evidence и holdout; кристаллизатор не видит holdout до проверки кандидата.

1. **Нормализация текстовых файлов** — окончания строк, кодировка, завершающая новая строка.
2. **Структурное преобразование данных** — CSV ↔ JSON с заданной схемой.
3. **Поиск и исправление конфигурации** — точечное изменение TOML/JSON/YAML с проверкой инвариантов.
4. **Организация набора файлов** — переименование и раскладка по детерминированным правилам в sandbox.
5. **Диагностика небольшого проекта** — запуск разрешённой проверки, классификация типовой ошибки, применение известного безопасного исправления.

Каждое семейство обязано включать:

- обычные случаи;
- граничные входы;
- несовместимый вход, на котором навык должен отказаться от исполнения;
- изменение среды или контракта, вызывающее деградацию;
- ожидаемый результат и независимый verifier fixture.

## 6. Архитектура Stage 1

```text
CLI / Benchmark Runner
          │
          ▼
    Task Gateway
          │
          ▼
    Orchestrator ──────────────┐
      │       │                │
      │       ├─→ Economist   │
      │       ├─→ Policy      │
      │       └─→ Context     │
      │            Compiler   │
      ▼                        │
 Knowledge Router             │
   │ known      │ unknown     │
   ▼            ▼             │
Compiled/     LLM Planner     │
Heuristic        │             │
   └──────┬──────┘             │
          ▼                    │
       Executor               │
          ▼                    │
       Verifier               │
          ▼                    │
 Evidence + Trace Store ──────┘
          │
          ▼
      Crystallizer
          │
          ▼
 Candidate → Offline Eval → Registry / Quarantine
```

### 6.1 Task Gateway

Принимает `TaskSpec`, валидирует схему и запрещает неявное расширение scope.

Минимальные поля:

```yaml
task_id: uuid
family: config_patch
objective: "Изменить таймаут на 30 секунд"
workspace: sandbox/task-123
inputs:
  files: [config.toml]
constraints:
  writable_paths: [config.toml]
  allowed_capabilities: [read_file, patch_toml]
postconditions:
  - type: toml_value_equals
    path: config.toml
    key: service.timeout
    value: 30
risk_class: low
budget:
  max_llm_calls: 2
  max_cost_usd: 0.10
  timeout_seconds: 30
```

### 6.2 Orchestrator

Реализуется как явный конечный автомат, а не свободный бесконечный agent loop.

```text
RECEIVED → ROUTED → PLANNED → AUTHORIZED → EXECUTED
    → VERIFIED → RECORDED → COMPLETED
                          ↘ FAILED / ESCALATED
```

Каждое состояние имеет лимит попыток и допустимые переходы. Повторное планирование ограничено бюджетом.

### 6.3 Economist

На первом этапе это прозрачная функция оценки, а не отдельный LLM-агент:

```text
expected_utility =
    p_success × value
    - execution_cost
    - verification_cost
    - expected_failure_cost
    + information_value
```

Он сравнивает минимум три маршрута: известный compiled skill, crystallized heuristic и LLM fallback. Все коэффициенты конфигурируются и записываются в трассу.

### 6.4 Knowledge Router

Ищет знания по структурированным признакам: family, schema, capabilities, environment fingerprint и preconditions. Семантический поиск можно добавить позже; на Stage 1 он не должен скрывать ошибки модели данных.

Router обязан вернуть:

- выбранный knowledge artifact;
- совпавшие preconditions;
- несовпавшие или неизвестные условия;
- оценку доверия;
- объяснимую причину выбора или отказа.

### 6.5 LLM Planner

LLM получает только минимальный `ContextBundle`: TaskSpec, доступные способности, релевантные ограничения, сокращённое состояние workspace и формат плана.

Выход — структурированный `ExecutionPlan`, который проходит schema validation и policy check. LLM не получает прямой shell и не может изменять budget или список полномочий.

### 6.6 Executor

Исполняет типизированные действия, а не произвольный текст:

- `read_text`;
- `write_text_atomic`;
- `apply_structured_patch`;
- `move_path` внутри sandbox;
- `run_check` из allowlist;
- `emit_artifact`.

Каждое действие имеет JSON-схему, preconditions, timeout, журнал побочных эффектов и по возможности rollback.

### 6.7 Verifier

Verifier проверяет заявленные postconditions независимо от plan/executor. Он не принимает «задача выполнена» от LLM как доказательство.

Виды проверок Stage 1:

- точное значение в структурированном файле;
- соответствие JSON Schema;
- hash или snapshot ожидаемого артефакта;
- отсутствие изменений вне allowlist;
- успешный exit code разрешённой проверки;
- property-based инварианты;
- metamorphic checks для преобразований.

Результат `VerificationReport` содержит verdict, список evidence, coverage postconditions и сведения о самом verifier.

### 6.8 Trace и Evidence Store

SQLite хранит индексируемые метаданные, файловая директория — крупные артефакты. Все сущности связаны через immutable identifiers.

Минимальные таблицы:

- `tasks`;
- `attempts`;
- `plans`;
- `actions`;
- `verification_reports`;
- `evidence`;
- `knowledge_artifacts`;
- `knowledge_evaluations`;
- `environment_snapshots`;
- `cost_events`.

Нельзя сохранять секреты, полный environment или неотфильтрованные промпты. Для чувствительных полей применяется redaction до записи.

### 6.9 Crystallizer

Работает offline после накопления успешных трасс:

1. группирует попытки одного семейства;
2. выделяет общий параметризуемый план;
3. формулирует preconditions и counterexamples;
4. создаёт candidate artifact;
5. прогоняет его на evidence-наборе и holdout;
6. сравнивает с LLM baseline;
7. публикует, оставляет гипотезой или отклоняет.

LLM может предложить обобщение или код кандидата, но решение о публикации принимает детерминированный evaluation pipeline.

### 6.10 Knowledge Registry

Состояния знания:

```text
candidate → hypothesis → heuristic → compiled
                 ↘ rejected     ↘ quarantined → retired
```

Каждый artifact содержит:

- version и content hash;
- происхождение и родительские traces;
- task family;
- input/output contract;
- preconditions и exclusions;
- required capabilities;
- environment compatibility;
- verifier suite;
- evidence summary;
- reliability score с доверительным интервалом;
- стоимость и latency;
- дату последней проверки;
- fallback route;
- статус и причину последнего перехода.

## 7. Правила повышения и понижения знания

Числа ниже являются стартовой политикой для проверки механизма, а не вечной истиной продукта.

### Candidate → Hypothesis

- минимум 3 независимо верифицированных успешных traces;
- не менее 2 различных входов;
- найден общий параметризованный план;
- нет изменения вне разрешённого scope;
- сформулированы preconditions и хотя бы один negative case.

### Hypothesis → Heuristic

- минимум 10 запусков;
- минимум 90% успеха на evidence-наборе;
- 100% корректных отказов на известных несовместимых входах;
- успех на holdout не ниже LLM baseline более чем на 2 процентных пункта;
- verifier покрывает все обязательные postconditions;
- fallback успешно протестирован.

### Heuristic → Compiled

- алгоритм детерминирован и не вызывает LLM;
- минимум 20 успешных запусков на 3 и более вариантах среды/входов;
- нижняя граница 95% доверительного интервала success rate выше заданного порога семейства;
- нет критических или scope-violation ошибок;
- стоимость ниже heuristic-маршрута;
- код проходит unit, property и sandbox integration tests;
- artifact воспроизводимо собирается из зафиксированной версии.

### Понижение или карантин

Немедленный карантин:

- запись вне разрешённого scope;
- нарушение политики полномочий;
- ошибочный положительный verdict для критического postcondition;
- несовместимая версия контракта без корректного отказа.

Понижение по статистике:

- rolling success rate ниже порога на окне последних запусков;
- нижняя доверительная граница стала недостаточной;
- стоимость или latency устойчиво хуже альтернативы;
- environment fingerprint больше не совместим;
- накоплены новые counterexamples, не описанные контрактом.

После понижения новые задачи идут по fallback-маршруту, а artifact остаётся доступен для анализа и переоценки.

## 8. Context Compiler

Stage 1 должен измерять контекст с первого дня. `ContextBundle` строится из типизированных секций:

- objective;
- constraints;
- capabilities;
- relevant workspace facts;
- applicable knowledge summaries;
- output schema;
- token and cost budget.

Компилятор ведёт provenance каждого фрагмента и считает токены. Benchmark сравнивает полный контекст с минимальным по качеству, стоимости и числу лишних фактов.

## 9. Безопасность Stage 1

- Работа только в созданном runner sandbox с проверкой resolved paths.
- Запрет сетевого доступа исполнителям по умолчанию.
- Команды только из декларативного allowlist.
- Раздельные read/write capabilities.
- Atomic write и журнал изменений.
- Snapshot sandbox до исполнения для тестового rollback.
- Жёсткие лимиты времени, действий, LLM-вызовов и стоимости.
- Redaction секретов до trace storage и LLM context.
- Никакой возможности для знания менять свой статус напрямую.
- Candidate code исполняется в отдельном процессе с ограничениями.

## 10. Технологический стек

Начальный выбор оптимизирован под скорость эксперимента и наблюдаемость:

- Python 3.12+;
- `uv` для окружения и lock-файла;
- Pydantic v2 для контрактов;
- Typer для CLI;
- SQLite + SQLModel/SQLAlchemy для метаданных;
- pytest, Hypothesis и golden fixtures для тестов;
- structlog или стандартный JSON logging;
- OpenTelemetry-compatible trace identifiers без обязательного collector;
- Ruff и mypy/pyright для статического контроля;
- provider-neutral LLM interface;
- JSON Schema для переносимых планов и отчетов.

Не вводить Kafka, Kubernetes, отдельную vector DB и микросервисы до появления измеренной необходимости. Компоненты сначала существуют как модули одного процесса с явными интерфейсами.

## 11. Предлагаемая структура кода

```text
llmin/
├─ pyproject.toml
├─ src/llmin/
│  ├─ cli.py
│  ├─ domain/          # TaskSpec, plans, evidence, knowledge
│  ├─ orchestrator/    # state machine
│  ├─ economics/       # route scoring and budgets
│  ├─ context/         # Context Compiler
│  ├─ planning/        # provider interface and LLM planner
│  ├─ execution/       # typed capabilities and sandbox
│  ├─ verification/    # independent verifiers
│  ├─ memory/          # trace/evidence repositories
│  ├─ knowledge/       # registry, routing, lifecycle
│  └─ crystallization/ # mining, candidate generation, eval
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ property/
│  └─ adversarial/
├─ benchmarks/
│  ├─ tasks/
│  ├─ holdout/
│  └─ reports/
└─ docs/
```

Зависимости направляются внутрь к domain contracts. Исполнитель, LLM provider и storage подключаются через интерфейсы, чтобы benchmark мог заменять их fake-реализациями.

## 12. План работ

### Неделя 1 — контракты, benchmark и наблюдаемость

Результат: задачи можно описать, прогнать через пустой pipeline и получить воспроизводимый отчёт.

- Зафиксировать TaskSpec, ExecutionPlan, Action, VerificationReport, Evidence и KnowledgeArtifact.
- Создать state machine оркестратора с запрещёнными переходами.
- Реализовать trace IDs, structured events и cost ledger.
- Подготовить первые 10 benchmark fixtures и генераторы вариантов.
- Сделать fake planner и fake executor для тестирования pipeline.
- Зафиксировать baseline-метрики до оптимизации.

Критерий готовности: одинаковый seed создаёт одинаковый benchmark; каждая задача заканчивается терминальным состоянием; все переходы наблюдаемы.

### Неделя 2 — sandbox, capabilities и независимая проверка

Результат: система безопасно выполняет типизированные файловые действия и доказывает постусловия.

- Реализовать sandbox path resolver.
- Добавить atomic file operations и change manifest.
- Реализовать 5–6 typed capabilities.
- Создать verifier registry и первые независимые проверки.
- Добавить property-based тесты против path traversal и scope escape.
- Реализовать snapshot/rollback для тестовой среды.

Критерий готовности: adversarial tests не позволяют выйти из sandbox; изменение вне allowlist приводит к fail и rollback.

### Неделя 3 — LLM fallback, Context Compiler и Economist v0

Результат: неизвестная задача планируется через заменяемый LLM provider в пределах бюджета.

- Определить provider-neutral интерфейс LLM.
- Создать структурированный planner output и repair-once для невалидной схемы.
- Реализовать Context Compiler с provenance и token accounting.
- Реализовать простую функцию выбора маршрута.
- Ввести hard budgets и отказ при их превышении.
- Записать baseline по LLM calls, tokens, cost, latency и success.

Критерий готовности: ни один LLM-ответ не исполняется до schema/policy validation; budget нельзя изменить через prompt.

### Неделя 4 — память, mining и первая кристаллизация

Результат: повторяющиеся успешные traces создают проверяемого кандидата.

- Реализовать SQLite repositories и artifact store.
- Группировать traces по task family и сигнатуре действий.
- Выделять параметры и общую последовательность.
- Формировать candidate с preconditions и exclusions.
- Создать offline evaluation pipeline.
- Запретить доступ к holdout во время генерации кандидата.

Критерий готовности: кандидат имеет provenance до исходных traces и не может повысить собственный статус.

### Неделя 5 — compiled skill, routing и обратная эволюция

Результат: известная задача решается без LLM, а деградировавшее знание автоматически отключается.

- Создать формат и loader compiled skills.
- Реализовать Knowledge Router с объяснением выбора.
- Добавить lifecycle transitions и policy thresholds.
- Реализовать shadow evaluation перед включением навыка.
- Смоделировать drift и incompatible input.
- Реализовать quarantine, fallback и re-evaluation queue.

Критерий готовности: после инъекции дрейфа compiled skill перестаёт применяться, событие объяснимо, задача безопасно возвращается к fallback.

### Неделя 6 — benchmark, отчёт и решение о Stage 2

Результат: воспроизводимый отчёт подтверждает или опровергает основную гипотезу.

- Довести benchmark до 30+ задач.
- Запустить baseline и learned routing минимум с 3 seeds.
- Сравнить качество, LLM calls, cost, latency и отказоустойчивость.
- Провести adversarial suite.
- Задокументировать ложные кристаллизации и причины.
- Пересмотреть пороги promotion/demotion по данным.
- Выпустить Stage 1 report и ADR о следующем интерфейсе среды.

Критерий готовности: любой разработчик может повторить эксперимент одной командой и получить машинно читаемый и человеческий отчёт.

## 13. Метрики и критерии выхода

### Качество

- не менее 95% корректно решённых поддерживаемых benchmark-задач;
- 100% корректных отказов на явно несовместимых входах;
- 0 изменений вне разрешённого scope;
- learned route не хуже LLM baseline более чем на 2 процентных пункта.

### Экономика

- минимум на 40% меньше LLM-вызовов на повторяющихся holdout-вариантах;
- минимум на 30% ниже средняя переменная стоимость повторных задач;
- измеренная стоимость верификации включена в сравнение;
- отдельно показана amortized стоимость кристаллизации.

### Обучение

- минимум 2 task families доходят до compiled status;
- каждый compiled artifact имеет provenance, verifier suite и fallback;
- минимум один искусственно деградировавший artifact корректно понижен;
- ни один artifact не повышен только на основании self-report LLM.

### Воспроизводимость

- benchmark запускается одной командой;
- версии модели, prompt, кода, fixtures и среды фиксируются;
- отчёт содержит raw aggregates и доверительные интервалы;
- повторный запуск с тем же seed объяснимо воспроизводим.

## 14. Тестовая стратегия

- **Unit:** контракты, state transitions, scoring, routing.
- **Property:** path safety, сериализация, идемпотентность преобразований.
- **Integration:** task → execution → verification → trace.
- **Golden:** стабильные планы, отчёты и compiled artifacts.
- **Adversarial:** prompt injection в файлах, path traversal, malformed schemas, budget escape.
- **Mutation:** убедиться, что verifier действительно ловит ошибочные результаты.
- **Drift:** версии форматов и capabilities, намеренно ломающие старое знание.
- **Benchmark:** сравнение baseline, heuristic и compiled маршрутов.

Критически важно тестировать не только исполнителя, но и verifier. Плохой verifier способен превратить ошибку в «подтверждённое знание».

## 15. Основные риски и меры

### Ложная кристаллизация

Мера: holdout, negative cases, confidence intervals, shadow mode и независимый verifier.

### Переобучение на формулировку задачи

Мера: разделять семантическое семейство и текст objective; генерировать перефразированные и структурно отличающиеся варианты.

### Ошибочный verifier

Мера: mutation tests, несколько ортогональных проверок для важных postconditions, версия verifier в evidence.

### Скрытая стоимость поддержки знаний

Мера: учитывать generation, evaluation, storage, revalidation и rollback в cost ledger.

### Неконтролируемое самоизменение

Мера: кандидаты не исполняются как доверенный код; публикация проходит отдельный policy/eval pipeline.

### Архитектура раньше данных

Мера: модульный монолит, минимум инфраструктуры и обязательное подтверждение каждого усложнения benchmark-данными.

## 16. Эпики для GitHub

### E1 — Domain contracts and state machine

TaskSpec, ExecutionPlan, Evidence, state transitions, serialization.

### E2 — Sandbox execution

Capabilities, path policy, atomic changes, rollback, adversarial tests.

### E3 — Verification

Verifier registry, postcondition DSL, mutation tests, evidence reports.

### E4 — LLM planning and context

Provider interface, Context Compiler, structured planning, budgets.

### E5 — Memory and observability

Trace schema, SQLite repositories, artifact store, cost ledger.

### E6 — Knowledge lifecycle

Candidate mining, evaluation, registry, routing, promotion/demotion.

### E7 — Benchmark and reporting

Task families, holdout, statistical report, reproducibility command.

Порядок зависимостей: E1 → E2/E3 → E4/E5 → E6 → E7. E7 начинается в первую неделю как инфраструктура и завершается последней как отчёт.

## 17. Definition of Done для любой функции

- определён входной и выходной контракт;
- прописаны ошибки и безопасный fallback;
- есть structured trace без секретов;
- стоимость и latency измеримы;
- написаны unit/integration тесты пропорционально риску;
- если функция влияет на знание — есть provenance;
- если функция исполняет действие — policy проверена до действия;
- документация и ADR обновлены при изменении архитектурного решения.

## 18. Решение после Stage 1

Stage 2 выбирается по данным:

- если кристаллизация даёт экономический выигрыш и сохраняет качество — добавить одну новую среду, вероятнее всего браузер с DOM-интерфейсом;
- если кристаллизация работает, но verification слишком дорог — инвестировать в verifier synthesis и metamorphic testing;
- если ложные повышения часты — усилить модель областей применимости и offline evaluation;
- если выигрыш минимален — пересмотреть единицу знания и критерии повторяемости до расширения среды.

Расширять систему на GUI только после доказательства, что её контур знаний действительно работает.
