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
