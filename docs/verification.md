# Независимая верификация

Успешный `ExecutionReport` означает только то, что разрешённые actions завершились без ошибки. Он не доказывает достижение цели задачи.

После исполнения отдельный `VerificationService` повторно открывает только пути, указанные в postconditions, через новый read-only sandbox. Он не использует output capability как доказательство и не доверяет self-report планировщика.

## Встроенные verifiers

- `text_equals` — точное сравнение UTF-8 содержимого;
- `toml_value_equals` — независимый разбор TOML и проверка dotted key.

Каждый обработанный postcondition создаёт evidence с SHA-256 и размером файла. Verdict `PASSED` требует:

- покрытия всех обязательных postconditions;
- отсутствия ошибок;
- хотя бы одного evidence artifact.

Mismatch создаёт `FAILED`, отсутствие verifier или внутренняя ошибка проверки — `INCONCLUSIVE`. Оба результата переводят pipeline в терминальное состояние `FAILED`. Переход в `VERIFIED` разрешён только после `PASSED`.

Verifier пока работает в том же процессе. Изоляция verifier worker и mutation-testing нескольких реализаций остаются следующими усилениями.
