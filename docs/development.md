# Локальная разработка

## Требования

- Python 3.12+
- Git

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

## Проверки

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\python -m llmin.cli validate-task benchmarks\tasks\config_patch\001.json
```

Для детерминированного `run-fixture` не нужны Docker, внешний сервис базы данных, `.env` или
API-ключ LLM. SQLite-файл создаётся внутри `<base-root>\.llmin` при первом запуске.

Реальный агентный путь использует OpenRouter Chat Completions со strict structured outputs:

```powershell
$env:OPENROUTER_API_KEY = "<secret>"
$env:OPENROUTER_MODEL = "<provider/model>"
llmin plan-task <task.json>
llmin run-agent <task.json> <base-root>
```

`plan-task` не выполняет действий. `run-agent` делает один разрешённый бюджетом LLM-вызов,
проверяет план локально, пропускает его через capability policy и sandbox, независимо проверяет
postconditions и сохраняет attempt. Секрет читается только из окружения; передавать его аргументом
командной строки или коммитить `.env` запрещено.

Первый инкремент намеренно не выполняет задачи. Он фиксирует строгие контракты и допустимые переходы до появления реальных побочных эффектов.

После E2/E3 минимальный pipeline исполняет только типизированные файловые capabilities и считает задачу завершённой лишь после независимого verifier verdict `PASSED`.

Для воспроизводимого запуска fake-плана сначала скопируйте template workspace в отдельный base root, затем выполните:

```powershell
llmin run-fixture <task.json> <plan.json> <base-root>
```

Полный запуск репозиторного fixture из чистого временного каталога:

```powershell
$runRoot = Join-Path $env:TEMP ("llmin-run-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path (Join-Path $runRoot "sandbox") | Out-Null
Copy-Item -Recurse benchmarks\workspaces\config-patch-001 `
  (Join-Path $runRoot "sandbox\config-patch-001")

$summary = .\.venv\Scripts\llmin.exe run-fixture `
  benchmarks\tasks\config_patch\001.json `
  benchmarks\plans\config_patch\001.json `
  $runRoot | ConvertFrom-Json

.\.venv\Scripts\llmin.exe show-attempt `
  (Join-Path $runRoot ".llmin\memory.sqlite3") `
  $summary.attempt_id
```

`run-fixture` возвращает ненулевой exit code, если pipeline не достиг состояния `COMPLETED`.
`show-attempt` возвращает сохранённые статус, последовательность состояний, диагностику и
evidence по `attempt_id`.
