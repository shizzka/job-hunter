# Changelog

## v0.6.0 — 2026-06-29

Итерация вокруг HH-диалогов, ручного контроля через Telegram и повторных откликов разными резюме.

### Telegram control plane
- Добавлены кнопки главного меню для ручного AI-ответа в HH-чат, входа HH и просмотра HH-резюме.
- Yellow-zone вакансии теперь можно отправлять через Telegram-кнопку ручного AI-отклика; рядом сохраняется feedback `норм` / `мимо`.
- Runtime-статусы и результаты команд стали подробнее: бот показывает прогресс, последний прогон и ошибки вместо молчаливого падения.

### HH chats and Google Forms
- Расширен детект AI/screening сообщений: официальные HH-боты и подозрительные HR-сообщения под обычным именем рекрутера.
- Для ручного ответа бот генерирует preview, даёт ссылку на чат и отправляет ответ только после подтверждения.
- Добавлено извлечение Google Forms из HH-чатов, preview заполнения и submit по Telegram-подтверждению.

### Resume retry pipeline
- Повторные HH-отклики теперь работают не только после отказа, но и после долгого молчания / просмотра без ответа.
- Для retry используется staged resume pipeline с вариантами резюме и fallback cover letter, если LLM не вернул текст.
- Добавлен QA-only guard для retry-кандидатов, чтобы не уходить в сервисных инженеров, техподдержку, Java/dev и техник/РЭА роли.

### LLM and matching
- Cover letters стали разнообразнее и получают больше фактического контекста из резюме, facts и knowledge base.
- Улучшены provider failover/лимиты и тесты для прокси/LLM-клиента.
- Fresh-search отделён от полного поиска для лёгкой проверки новых HH-вакансий.

## v0.5.0 — 2026-05-23

Большая итерация по hh.ru auto-apply: автоответ на сложные анкеты,
интерактивный captcha-solver через vision-LLM + Telegram-bridge,
кастомизация ответов под конкретного кандидата, рефакторинг кодовой базы.

### Авто-ответ на анкеты работодателя (`hh_client.py`)
- **Поддержка radio / checkbox / select** — раньше формы с этими полями целиком отвергались («автоответ пропущен: есть неподдерживаемые поля»), теперь LLM выбирает один или несколько вариантов из списка
  - JS-инспектор `_inspect_employer_questions` достаёт все варианты (label, value, index, признак «Свой вариант») и кладёт в `fields[]`, а не в `unsupported_items`
  - Новый `_answer_choice_with_llm`: LLM получает вопрос + перечень опций, возвращает индексы выбора + опциональный `custom_text` для «Свой вариант»
  - JS-заполнитель `_fill_employer_question_answers` умеет кликать radio-member по индексу, тогглить checkbox, выставлять `<select>`-value, а также заполнять связанный текстовый input при выборе «Свой вариант» (эвристика `findCustomTextNear`)
- **Retry без SKIP** для radio/select — если первая попытка вернула `status=skip`, делается второй заход с директивой «лучше угадай чем пропустить отклик» (отмечается `[best-guess]` в notes)
- **Контекст вакансии в промпте** — `apply_to_vacancy(... vacancy_context: str = "")` пробрасывается из `apply_orchestrator.dispatch_apply` (склейка `title + company + details[:1800]`); закрывает кейсы где placeholder вроде «Писать тут» не давал LLM понять что отвечать
- **Каноничный профиль кандидата** — новый env `HH_AUTO_ANSWER_PROFILE_NOTE` подкладывается в промпт первым, приоритетным блоком; решает проблему когда LLM путает общий стаж (13 лет инженер) с QA-стажем (<1 года) и пишет ложь в auto-cover
- **Зарплатная подсказка** — `HH_AUTO_ANSWER_SALARY_BASELINE` + `HH_AUTO_ANSWER_SALARY_RULE` в env даёт LLM базовую планку + правила корректировки (вахта/командировки/ВПК/удалёнка) — без жёсткого значения, чтобы число подстраивалось под конкретную вакансию
- **Лимит полей** `HH_AUTO_ANSWER_MAX_QUESTIONS` 3 → 10
- **Robust JSON-парсинг ответов LLM** — `_parse_llm_json` чинит markdown-fence и thinking-токены вокруг JSON-объекта (через сбалансированный поиск `{…}` с учётом строк/эскейпов)

### Captcha-solver (`captcha_solver.py`, `captcha_bridge.py`)

Текстовая captcha hh.ru («Подтвердите, что вы не робот: введите буквы с картинки») теперь решается автоматически или с минимальным участием.

- **Stage 0 — vision-LLM OCR**: `qwen3-vl:235b-instruct` через тот же Ollama-ключ. Бесплатная модель, на реальной captcha hh.ru показала точность близкую к 100%. Конфигурируется через `HH_CAPTCHA_VISION_MODEL` + `HH_CAPTCHA_VISION_RETRIES` (default 2). До эскалации делается до N попыток (на каждую — свежий скрин, hh.ru сам обновляет картинку)
- **Stage 1 — TG-bridge**: если vision не справился — скрин уходит в админ-чат бота через новый `notifier.send_photo`, бот ждёт текстовый ответ человека до `HH_CAPTCHA_HUMAN_WINDOW_S` секунд (default 300 = 5 мин, в пределах ожидаемого TTL hh.ru-токена)
  - `captcha_bridge.py` — file-based IPC между search-процессом и telegram_bot-процессом (атомарные write через tmp+rename, JSON-файлы `captcha_pending.json`/`captcha_response.json` в profile state-dir)
  - В `telegram_bot._handle_update` — перехват входящего текста от админа: если есть pending captcha и сообщение не похоже на menu-кнопку, ответ пишется в `captcha_response.json`, search его подхватывает
- **Two-stage timeout flow** (предложено Eugene): по таймауту 5 мин — `hh_guard.record_soft_cooldown(15 min)` (вместо жёстких 6 ч) + follow-up сообщение «токен истёк, нажми кнопку» с inline-`callback_data="captcha_retry:<request_id>"`. При нажатии — handler в `_handle_callback_query` вызывает `hh_guard.clear_cooldown()` + `subprocess.Popen(["./run.sh", "search"])` под flock — новый search генерирует свежий токен, человек уже видит уведомление и быстро отвечает
- 3 попытки на одну captcha-сессию суммарно (между vision и TG)

### Анти-бот гигиена
- `HH_MIN_SECONDS_BETWEEN_APPLICATIONS`: 12 → **90 сек**
- `HH_AUTO_APPLY_MAX_PER_24H`: 45 → **15**
- `playwright-stealth` 2.x применён к `BrowserContext` после старта — скрывает `navigator.webdriver` и прочие headless-маркеры. Добавлен в `requirements.txt`

### Structured facts о кандидате (`facts.py`)
- Новый модуль с `extract_facts_from_resume()` (LLM-extraction, system-prompt «strict JSON»), `load_facts()`, `format_facts_for_prompt()` для подкладывания структурированных фактов в LLM-промпт
- CLI: `./run.sh extract-facts` (`agent.py --extract-facts`) — один раз сгенерировать `~/.job-hunter/profiles/<name>/facts.json` из текущего `resume.md`. Файл бэкапится при перезаписи
- Поля рекомендованы (location, willing_remote, english_level, tools_used/not_used, experience_years, current_position и т.п.), но LLM волен добавить свои; всё пустое игнорируется в формате промпта
- `prompt_blocks.build_facts_block()` подкладывает блок в `_answer_question_with_llm` и `_answer_choice_with_llm` перед резюме

### `hh_guard` расширен
- `record_soft_cooldown(minutes, reason)` — короткий cooldown (минуты вместо часов) для случая «captcha ждёт человека, но окно вышло»; не использует `HH_ANTI_BOT_COOLDOWN_HOURS=6`
- `clear_cooldown()` — снять `blocked_until` (для ручного перезапуска из TG-кнопки)

### Унифицированный notifier
- `send_message`, `send_message_with_markup`, `send_photo` — все три метода теперь делегируют в общий `_send_to_chats(method, build_request, multipart=…)` с резолвом профиля/токена/чатов и автоматическим `proxy → direct` fallback
- Builders `_build_text_payload` / `_build_photo_form` создают payload/FormData на каждый chat и каждый retry (file-handle одноразовый в aiohttp)
- ~80 строк дубликатов убрано

### Рефакторинг (без поведенческих изменений)
- **`llm_utils.py`** — единый `parse_llm_json` + `extract_first_json_object` (раньше дублировались в `matcher.py`, `hh_client.py`, `facts.py`)
- **`prompt_blocks.py`** — `build_salary_rule_block`, `build_facts_block`, `build_profile_note_block`, `build_vacancy_context_block` (раньше inline в `hh_client.py`)
- **`captcha_solver.py`** — `solve_captcha_with_vision_llm`, `try_solve_captcha_interactively`, `handle_anti_bot_with_solver` (раньше методы `HHClient`, ~250 строк)
- **`captcha_bridge.py`** — отдельный IPC модуль (CB2)
- `hh_client.apply_to_vacancy` — closures `save_debug_snapshot` и `detect_response_controls` подняты в методы класса `_save_debug_snapshot` и `_detect_response_controls` (closures с мутируемым outer-scope `cover_letter_filled`/`auto_answer_notes` оставлены внутри — их вынос требует переоформления через dataclass-state, риск регрессии)

### Apply-orchestrator
- `dispatch_apply` собирает `vacancy_context` из `vacancy["title"/"company"/"details"]` и пробрасывает в `hh_client.apply_to_vacancy(... vacancy_context=...)` (был только URL)

### matcher
- `evaluate_vacancy` использует robust `_parse_llm_json` (закрыл 6 ошибок `Expecting value: line 2 column 15` за один прогон 23 мая)
- В промпт добавлено явное «Первым символом ответа должен быть `{`, последним `}`» — снижает thinking-токены от gpt-oss

### Tests
- `tests/test_hh_client.py:test_apply_to_vacancy_autoanswers_resume_question_with_llm` — мок `fake_llm_answer` обновлён под новую сигнатуру `(field, resume_text, page_text, vacancy_context)`
- 3 новых smoke-скрипта (без браузера/с LLM) перенесены в `scripts/smoke/`:
  - `choice_prompt_offline.py` — 6 синтетических hh-вопросов, проверка `_answer_choice_with_llm`. На 2026-05-23 после фиксов: **picked=6/6, skipped=0, failed=0** (с 33% на стартовой итерации)
  - `choice_model_compare.py` — `gpt-oss:120b` vs `qwen3-coder:480b` на тех же 6 кейсах
  - `question_inspector_smoke.py` — JS-инспектор на сохранённой captcha-HTML
- Pytest: **289 passed** (4 pre-existing analytics-фейла не из этой итерации)

### Новые env-параметры (`config.py`)
- `HH_AUTO_ANSWER_SALARY_BASELINE` — базовая планка зарплаты (руб)
- `HH_AUTO_ANSWER_SALARY_RULE` — текстовое правило корректировки под условия вакансии
- `HH_AUTO_ANSWER_PROFILE_NOTE` — каноничный профиль (приоритет в промпте)
- `HH_CAPTCHA_VISION_MODEL` — vision-LLM для OCR (default `qwen3-vl:235b-instruct`)
- `HH_CAPTCHA_VISION_RETRIES` — попыток vision перед эскалацией (default 2)
- `HH_CAPTCHA_HUMAN_WINDOW_S` — окно ожидания ответа от человека в TG (default 300)
- `HH_AUTO_ANSWER_MAX_QUESTIONS` дефолт 3 → 6 (env override: 10)

### Зависимости
- `playwright-stealth>=2.0,<3` добавлен в `requirements.txt`

### Стратегия проекта
- jobhunter переходит в режим **freeware**. Коммерциализация невозможна из-за hh.ru captcha-mitigation: продукт для платных пользователей небезопасен, hh.ru может закрутить гайки в любой момент. Защита `.git/info/commercial-paths` сохранена, но фактически не используется (push через `ALLOW_COMMERCIAL_PUSH=1`).

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
