# Состояния

~~~mermaid
stateDiagram-v2
    [*] --> New
    New --> Processing: claim
    RetryScheduled --> Processing: retry time
    Processing --> Delivered: 2xx or duplicate
    Processing --> RetryScheduled: timeout, 429, 5xx
    Processing --> Failed: permanent error
    Processing --> Processing: one re-auth on 401
    RetryScheduled --> Failed: attempts exhausted
    Delivered --> [*]
    Failed --> [*]
~~~

## Правила

- Один элемент обрабатывает только один worker.
- **Delivered** требует внешнего ID.
- 401 допускает одну повторную авторизацию.
- 408, 429 и 5xx планируют повтор с ограниченным backoff.
- Остальные 4xx завершаются постоянной ошибкой.
- Ручной повтор сохраняет ключ идемпотентности.

Текст ответа не управляет состоянием. Маршрутизация использует HTTP-код и машинный код ошибки.

Для входящей очереди успех означает, что локальный документ записан, а внешнее задание завершено. Повтор выполняет
только отсутствующий шаг.
