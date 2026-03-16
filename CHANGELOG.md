# Changelog

## v0.3.0

### Multi-profile система (F-001, F-002)
- Добавлен модуль `profile.py` — изолированные профили пользователей (state, cookies, настройки)
- Каждый профиль хранит конфиг в `~/.job-hunter/profiles/<name>/profile.env`
- OS-level блокировка (fcntl) — защита от параллельного запуска одного профиля
- `activate(name)` патчит `config.*` — все 14 модулей работают без рефакторинга
- Полная обратная совместимость: профиль `default` = текущие env-переменные

### Интерактивный wizard (`setup_profile.py`)
- `./run.sh setup` — пошаговое создание профиля без ручного редактирования файлов
- Ввод поисковых запросов, загрузка резюме (текст/файл), настройка площадок
- Логика: нет аккаунта → площадка отключена, аккаунт без резюме → manual_review
- Предлагает LLM-анализ резюме и логин на площадках

### Анализ резюме через LLM (`resume_analyzer.py`)
- `./run.sh analyze-resume` — полный анализ резюме с рекомендациями
- Промт загружается из `~/.job-hunter/resume_prompt.md` (не в репозитории)
- Формат: system-промт + `---` + user-промт с плейсхолдерами
- Результат сохраняется в файл и отправляется в Telegram

### Стабилизация и рефакторинг
- Извлечён `search_pipeline.py` — дедупликация и сбор вакансий
- Извлечён `apply_orchestrator.py` — логика автооткликов
- Извлечён `filters.py` — keyword-фильтр до LLM
- Извлечён `reporting.py` — форматирование статистики
- Извлечён `invitation_sync.py` — синхронизация приглашений
- `agent.py` сокращён на 500+ строк

### Аналитика и отчётность
- `analytics.py` — JSONL event logging, воронка откликов, A/B тестирование резюме
- Telegram-дайджест с воронкой и статистикой по вариантам резюме
- Нормализация статусов outcomes (invited, rejected, и т.д.)
- `hh_resume_pipeline.py` — staged A/B тестирование резюме на hh.ru

### Фильтрация и безопасность
- Расширенные keyword-фильтры с настраиваемыми red flag'ами
- Детекция капчи при поиске на hh.ru
- Верификация успешности отклика

### Тесты
- 152 теста: smoke-импорты, фильтры, дедупликация, профили, парсеры
- Fixture-based тесты парсеров из debug-артефактов
- Изолированный smoke runner

## v0.2.0-obt

- added `GeekJob` as a searchable source
- added browser-based `SuperJob` login/apply flow
- added `Habr Career` auto-apply rate limiting
- added runtime status persistence for external status readers
- added Telegram search-start notifications
- sanitized configuration for public use
- removed local secret fallbacks from `run.sh`
- made `AI Office` and Telegram integrations optional via env
- documented setup, architecture, and operations
- added an MIT `LICENSE` for public release
- marked the first public build as `OBT`
