# Интеграция 1С с HR-платформой

Пример двустороннего кадрового обмена.

## Возможности

- справочники и кадровые события;
- исходящая и входящая очереди;
- идемпотентность документов и вложений;
- миграция внешних ID;
- ограниченные повторы и повторная авторизация;
- журнал без секретов и персональных данных.

## Состав

- `extension/` — BSL-алгоритмы;
- `mock-api/` — локальный API;
- `tests/` — тесты;
- `docs/` — архитектура.

## Проверка

```powershell
python -m unittest discover -s tests -v
python tools/safety_audit.py
cd mock-api
python -m hr_mock.http_server --port 8080
```

API доступен только на `127.0.0.1`.

[Архитектура](docs/architecture.md) · [Состояния](docs/state-machine.md) ·
[Идемпотентность](docs/idempotency.md) · [Безопасность](docs/security.md)
