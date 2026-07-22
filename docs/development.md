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

Первый инкремент намеренно не выполняет задачи. Он фиксирует строгие контракты и допустимые переходы до появления реальных побочных эффектов.

После E2/E3 минимальный pipeline исполняет только типизированные файловые capabilities и считает задачу завершённой лишь после независимого verifier verdict `PASSED`.

Для воспроизводимого запуска fake-плана сначала скопируйте template workspace в отдельный base root, затем выполните:

```powershell
llmin run-fixture <task.json> <plan.json> <base-root>
```

Команда возвращает ненулевой exit code, если pipeline не достиг состояния `COMPLETED`.
