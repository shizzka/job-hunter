# Changelog

## v0.4.0

### Telegram-бот управления (`telegram_bot.py`, `job_hunter_ctl.py`)
- Полноценный Telegram-бот для управления Job Hunter: запуск/остановка поиска, статистика, дайджест — всё из чата
- `job_hunter_ctl.py` — контроллер процессов: `daemon-start/stop`, `bot-start/stop`, `status`
- `runtime_control.py` — PID-файлы и состояние демонов
- `run.sh` — новые команды: `bot`, `bot-daemon`, `status`, `bot-status`, `bot-stop`
- Поддержка дефолтного профиля через `JOB_HUNTER_DEFAULT_PROFILE`
- Systemd unit для автозапуска бота (`deploy/systemd/`)
- Скрипт установки user-service (`scripts/install_job_hunter_bot_user_service.sh`)

### Защита от бана hh.ru (`hh_guard.py`)
- Rolling лимит автооткликов за 24 часа (`HH_AUTO_APPLY_MAX_PER_24H`, по умолчанию 45)
- Минимальная пауза между откликами (`HH_MIN_SECONDS_BETWEEN_APPLICATIONS`, 12 сек)
- Детекция anti-bot сигналов (captcha, блокировки) с автоматическим cooldown
- Пропуск поиска на hh.ru при активном cooldown (`HH_SKIP_SEARCH_ON_ANTI_BOT`)
- Персистентное состояние guard в `hh_guard_state.json`

### Автоответы на вопросы работодателя при отклике (`hh_client.py`)
- Автоматическое заполнение вопросов работодателя при отклике на hh.ru
- LLM-генерация ответов на открытые вопросы (на основе резюме и вакансии)
- Авто-определение вопросов о зарплате (`HH_AUTO_ANSWER_SALARY_TEXT/NUMBER`)
- Настройки: `HH_AUTO_ANSWER_SIMPLE_QUESTIONS`, `HH_AUTO_ANSWER_USE_LLM`, `HH_AUTO_ANSWER_MAX_QUESTIONS`

### Мульти-клиентская система (`telegram_clients.py`, `telegram_access.py`)
- Реестр Telegram-клиентов с онбордингом и статусами
- Контроль доступа: привязка клиентов к профилям
- `client_hh_auth.py` — авторизация hh.ru и импорт резюме для клиентских профилей
- Лимиты AI-анализов резюме (`telegram_resume_limits.py`)

### Уведомления
- Мульти-адресат: уведомления уходят привязанным к профилю Telegram-пользователям
- Раздельные токены: `HUNTER_CONTROL_BOT_TOKEN` (управление) и `HUNTER_NOTIFY_BOT_TOKEN` (уведомления)
- Proxy per-profile в notifier

### Улучшения пайплайна
- `search_pipeline.py` — детальная статистика по источникам (fetched/seen/new/applied)
- `agent.py` — информативные заметки при отсутствии новых вакансий, `DECISION_ALREADY_APPLIED`
- `profile.py` — PID-файлы, лог-файлы, интервалы поиска per-profile
- `seen.py`, `analytics.py`, `outcome.py` — расширения для новых сценариев
- `proxy_utils.py` — утилиты для работы с прокси
- `resume_analyzer.py` — улучшения анализа резюме

### Тесты
- 14 новых тест-файлов: hh_guard, hh_client, search_pipeline, proxy_utils, telegram_bot, telegram_clients, telegram_access, seen, и др.

### Документация
- `docs/BOT_SERVICE.md` — документация по Telegram-боту как сервису
- `SETUP_AGENT.md` — интерактивная инструкция для AI-агентов по установке и настройке
- Ссылки на AI-assisted setup в README.md и README.ru.md

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
