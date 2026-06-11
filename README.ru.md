# Job Hunter v0.6.0

English version: [README.md](README.md)

`Job Hunter` — Python-инструмент для автоматизации поиска QA/testing вакансий на нескольких job board-платформах, их оценки через LLM и автоотклика там, где площадка это позволяет.

Поддерживает изолированные профили пользователей, LLM-анализ резюме, воронку откликов с A/B тестированием резюме, интерактивный мастер настройки, авто-ответ на анкеты работодателя (radio/checkbox/select), решение captcha hh.ru (vision-LLM + TG-bridge), авто-диалог с AI-помощниками hh.ru и безопасный Telegram-approval flow для подозрительных HR/AI-скринингов под обычным именем рекрутера.

Текущий публичный статус: `OBT` (open beta testing) → freeware. Ожидай дрейф селекторов, captcha-ограничения и платформенные edge case'ы.

## Что Он Делает

### Поиск и отклик
- Ищет вакансии из нескольких источников за один прогон
- Убирает дубли между площадками
- Применяет быстрый keyword-filter до вызова LLM
- Оценивает каждую вакансию относительно резюме + базы знаний кандидата (knowledge base) + структурированных фактов (facts.json)
- Генерирует короткое сопроводительное письмо для релевантных совпадений
- Отправляет автоотклики там, где это поддерживается

### Анкеты работодателя на hh.ru
- Авто-ответ на формы с вопросами после отклика: text/textarea/number, **radio/checkbox/select** (включая «Свой вариант» с custom-текстом)
- Контекст вакансии и канонический профиль кандидата подкладываются в LLM-промпт
- Retry-без-skip для radio/select (LLM делает best-guess вместо отказа)
- Авто-ответы транслируются в Telegram-уведомление об отклике вместе с цитатами вопросов

### Captcha hh.ru (hybrid solver)
- Этап 0: vision-LLM (`qwen3-vl:235b-instruct`) распознаёт текст с captcha-картинки автоматически
- Этап 1: если vision не справился — скриншот + inline-кнопка «🔁 Перезапустить поиск» уходят в Telegram, ты вводишь буквы текстом → бот вставляет в форму
- Soft-cooldown 15 мин вместо 6-часового бана при таймауте человека

### AI и screening-чаты на hh.ru
- Polling чатов на `chatik.hh.ru` каждые 30 минут (cron), плюс piggyback после поиска
- Детект официальных ботов hh.ru («ИИ-помощник», «Робот-помощник») по аватарке, автору и самопрезентации в тексте
- Детект подозрительных scripted HR-сообщений, которые выглядят как AI-скрининг, но приходят от обычного имени рекрутера
- На официальных AI-ботов можно отвечать автоматически; подозрительные HR-сообщения сначала уходят в Telegram на подтверждение
- Telegram-preview содержит сгенерированный ответ, ссылку на чат и inline-кнопку **Отправить ответ** для ручного подтверждения
- Safety: лимит ответов на чат, защита от дублей по message_id, cooldown между ответами и детерминированные безопасные ответы на чувствительные вопросы вроде справки с места учебы

### База знаний кандидата
- `profiles/<name>/knowledge/*.md` — структурированные документы про опыт, навыки, проекты
- 2-pass LLM-фильтр: для каждой вакансии выбираются 5 самых релевантных секций (например, для Mobile-QA — API/Charles/SQL, без 3D-печати)
- Используется в cover letter, ответах на анкеты, чатах с AI-помощником

### Анти-бот гигиена
- 90 секунд между апплаями, лимит 30 за 24 часа (по умолчанию)
- `playwright-stealth` скрывает headless-маркеры от hh.ru anti-bot detection

### Прочее
- Переводит вакансии в manual review и отправляет Telegram-уведомления, если автоотклик невозможен
- Ведёт воронку откликов: отклик → просмотр → ожидание / отказ / позитив
- Поддерживает A/B тестирование резюме с отдельной статистикой по вариантам
- Анализирует резюме через LLM и отправляет рекомендации в Telegram
- Поддерживает изолированные профили пользователей для многопользовательских сценариев
- Хранит `seen`, cookies, runtime status, knowledge base и debug-артефакты вне репозитория

## Поддерживаемые Источники

| Источник | Поиск | Детали | Автоотклик |
| --- | --- | --- | --- |
| `hh.ru` | Да | Да | Да |
| `Habr Career` | Да | Да | Да |
| `SuperJob` | Да | Да | Да |
| `GeekJob` | Да | Да | Да |

## Как Это Работает

1. Собирает вакансии со всех включённых источников.
2. Убирает дубли между источниками и повторяющимися поисковыми запросами.
3. Применяет быстрый keyword-filter, чтобы не тратить LLM на очевидный мусор.
4. Подтягивает полные детали вакансий.
5. Просит LLM оценить вакансию относительно твоего резюме и кратко объяснить решение.
6. Если вакансия релевантна:
   - делает автоотклик на поддерживаемых площадках;
   - либо создаёт manual-review задачу и отправляет уведомление.

Подробнее: [Architecture](docs/ARCHITECTURE.md)

## Настройка LLM

`Job Hunter` ходит в матчинг через OpenAI-compatible API. Это значит, что можно использовать:

- OpenAI
- Ollama Cloud / `ollama.com`
- локальный `Ollama`, который отдаёт OpenAI-compatible `/v1` endpoint

Через этого провайдера идут и оценка вакансий, и генерация cover letter.

## Установка с помощью AI-ассистента (самый простой способ)

Если у тебя есть AI-ассистент для кода (Claude Code, Cursor, Windsurf и т.д.), просто дай ему этот промпт:

> Склонируй https://github.com/shizzka/job-hunter и следуй инструкции из файла SETUP_AGENT.md — выполни все шаги по порядку, задавая мне вопросы на каждом этапе.

AI сам всё установит, объяснит как работает Job Hunter, поможет выбрать нейросеть, настроит Telegram-уведомления и проведёт через всю конфигурацию в диалоговом режиме.

Подробная инструкция: [SETUP_AGENT.md](SETUP_AGENT.md)

## Быстрый Старт

### Вариант А: Интерактивная настройка (рекомендуется)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env
# заполни минимум LLM_BASE_URL, JOB_HUNTER_LLM_KEY, LLM_MODEL

./run.sh setup            # интерактивный мастер: профиль, резюме, площадки
./run.sh dry-run
./run.sh search
```

Мастер проведёт через настройку поисковых запросов, загрузку резюме, подключение площадок и опциональный LLM-анализ резюме.

### Вариант Б: Ручная настройка

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env

./run.sh login
./run.sh habr-login
./run.sh superjob-login
./run.sh geekjob-login

./run.sh dry-run
./run.sh search
```

## Конфигурация

Во время запуска проект читает переменные окружения:

- из `JOB_HUNTER_ENV_FILE`
- или по умолчанию из `~/.job-hunter/job-hunter.env`

Ключевые переменные:

- `JOB_HUNTER_LLM_KEY`: API key для OpenAI-compatible LLM-провайдера
- `LLM_BASE_URL`: base URL провайдера
- `LLM_MODEL`: модель по умолчанию (используется как fallback)
- `SUPERJOB_API_KEY`: нужен для поиска на `SuperJob`
- `HUNTER_BOT_TOKEN`: необязательный Telegram bot token для уведомлений
- `NOTIFY_CHAT_ID`: необязательный Telegram chat ID для уведомлений
- `OFFICE_URL`: необязательный base URL AI Office HTTP API
- `OFFICE_DB`: необязательный путь к AI Office SQLite
- `JOB_HUNTER_HOME`: директория для cookies, resume, seen state, runtime status и скриншотов

### Per-task LLM модели (опционально)

Под каждую задачу можно выбирать свою модель — экономия времени и точности. Если переменная пустая, используется `LLM_MODEL`:

- `HH_MATCHER_MODEL`: оценка релевантности вакансии (рекомендуется `cogito-2.1:671b`)
- `HH_COVER_LETTER_MODEL`: генерация сопроводительного письма
- `HH_QUESTION_MODEL`: ответы на свободно-текстовые анкеты hh.ru
- `HH_CHOICE_MODEL`: выбор radio/checkbox/select (рекомендуется `qwen3-coder:480b` — быстрая и точная)
- `HH_FACTS_EXTRACT_MODEL`: извлечение структурированных фактов из резюме (`./run.sh extract-facts`)
- `HH_CHAT_RESPONDER_MODEL`: ответы AI-помощнику в чатах
- `HH_CAPTCHA_VISION_MODEL`: vision-LLM для OCR captcha (по умолчанию `qwen3-vl:235b-instruct`)

См. `scripts/smoke/model_bench.py` для бенчмарка 6 моделей × 4 задач.

### Анти-бот и captcha

- `HH_MIN_SECONDS_BETWEEN_APPLICATIONS=90`: пауза между откликами
- `HH_AUTO_APPLY_MAX_PER_24H=30`: лимит откликов за сутки
- `HH_ANTI_BOT_COOLDOWN_HOURS=6`: пауза после captcha-блока
- `HH_CAPTCHA_VISION_RETRIES=2`: попыток vision-OCR перед эскалацией в TG
- `HH_CAPTCHA_HUMAN_WINDOW_S=300`: окно ожидания ответа человека в TG (потом soft-cooldown 15 мин)

### Авто-ответ на анкеты + чаты

- `HH_AUTO_ANSWER_SIMPLE_QUESTIONS=1`: включить авто-ответ
- `HH_AUTO_ANSWER_USE_LLM=1`: использовать LLM для свободного текста
- `HH_AUTO_ANSWER_MAX_QUESTIONS=10`: лимит полей в форме
- `HH_AUTO_ANSWER_SALARY_BASELINE=80000`: базовая планка зарплаты (₽)
- `HH_AUTO_ANSWER_SALARY_RULE`: правило корректировки под условия вакансии
- `HH_AUTO_ANSWER_PROFILE_NOTE`: каноничный профиль кандидата (приоритет в промпте)
- `HH_CHAT_RESPONDER_ENABLED=1`: включить авто-ответ в чатах с AI-помощниками
- `HH_CHAT_AUTOSEND=1`: реальная отправка (0 = dry-run + preview в TG)
- `HH_CHAT_MAX_REPLIES_PER_CHAT=5`: safety-лимит ответов на один чат

Полный шаблон: [job-hunter.env.example](job-hunter.env.example)

## Как Менять Направление Поиска

По умолчанию конфиг ориентирован на `QA`, потому что это исходный use case проекта, но сам проект не ограничен только QA-вакансиями.

Менять цели поиска можно двумя способами:

- править дефолты в [config.py](config.py);
- или переопределять их через env-файл без правки кода.

Примеры env overrides по площадкам:

```env
HH_SEARCH_QUERIES=QA engineer||SDET||automation tester
SUPERJOB_SEARCH_QUERIES=QA||qa engineer||sdet
HABR_SEARCH_PATHS=/vacancies/testirovschik_qa/remote||/vacancies/devops/remote
```

Что важно:

- `HH_SEARCH_QUERIES` и `SUPERJOB_SEARCH_QUERIES` это обычные текстовые запросы.
- `HABR_SEARCH_PATHS` это не текстовый поиск, а список path'ов листинга.
- Для нескольких значений используется разделитель `||`.
- `GeekJob` сейчас обходит общий листинг вакансий и опирается на общий filter/LLM pipeline, а не на отдельный список запросов.
- Для автоотклика в `GeekJob` нужна сохранённая браузерная сессия после `./run.sh geekjob-login`.

### Пример: Ollama Cloud

```env
LLM_BASE_URL=https://ollama.com/v1
JOB_HUNTER_LLM_KEY=your-ollama-cloud-key
LLM_MODEL=deepseek-v3.1:671b
```

### Пример: локальный Ollama

1. Установи Ollama на машину.
2. Подтяни chat-capable модель.
3. Убедись, что локальный сервер запущен.
4. Направь `Job Hunter` на локальный OpenAI-compatible endpoint.

```bash
ollama pull qwen2.5:14b
ollama serve
```

```env
LLM_BASE_URL=http://127.0.0.1:11434/v1
JOB_HUNTER_LLM_KEY=ollama
LLM_MODEL=qwen2.5:14b
```

Для локального `Ollama` API key может быть любым непустым placeholder-значением, потому что локальный сервер обычно не требует hosted-style авторизацию.

## Команды

```bash
# Управление профилями
./run.sh setup                  # интерактивный мастер настройки профиля
./run.sh profiles               # список всех профилей
./run.sh analyze-resume         # LLM-анализ резюме → файл + Telegram
./run.sh extract-facts          # LLM извлекает structured facts.json из resume.md

# Логин (интерактивно, открывает браузер)
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh geekjob-login
./run.sh grab-resume

# Поиск и отклик
./run.sh dry-run
./run.sh search
./run.sh check
./run.sh daemon
./run.sh stats
./run.sh digest
./run.sh analytics-backfill

# AI/screening-чаты hh.ru
./run.sh chat-respond           # проверить чаты, ответить AI-помощникам или уведомить о подозрительном HR-скрининге
./run.sh chat-respond-one <chat_id> [message_id]  # подготовить one-shot preview ответа для конкретного чата

# Поиск по конкретным площадкам
./run.sh superjob-dry-run
./run.sh superjob-search
./run.sh habr-dry-run
./run.sh habr-search
./run.sh geekjob-dry-run
./run.sh geekjob-search

# Telegram-бот
./run.sh bot                    # foreground (для отладки)
./run.sh bot-daemon             # фоном
```

### Cron (рекомендуемое расписание)

```
30 07,14 * * * cd /home/q/job-hunter && /usr/bin/flock -n /tmp/job-hunter-search.lock ./run.sh search >> /tmp/job-hunter.log 2>&1
00 23 * * * cd /home/q/job-hunter && /usr/bin/flock -n /tmp/job-hunter-search.lock ./run.sh search >> /tmp/job-hunter.log 2>&1
*/30 * * * * cd /home/q/job-hunter && /usr/bin/flock -n /tmp/job-hunter-search.lock ./run.sh chat-respond >> /tmp/job-hunter-chat.log 2>&1
```

Search 3 раза в день + chat-respond каждые 30 минут. Один flock на оба — `search` имеет приоритет, `chat-respond` пропускается если search идёт (и сам же дёрнется в конце search-цикла как piggyback).

Для работы с конкретным профилем используй `--profile <name>`:

```bash
./run.sh --profile john search
./run.sh --profile john stats
```

## Профили

`Job Hunter` поддерживает изолированные профили пользователей. У каждого профиля своя директория состояния, cookies, seen-вакансии, аналитика и конфигурация.

```
~/.job-hunter/                  # состояние профиля по умолчанию
~/.job-hunter/profiles/john/    # именованный профиль: конфиг + состояние
~/.job-hunter/profiles/anna/    # другой именованный профиль
```

Профили защищены OS-level file lock — два демона не могут работать с одним профилем одновременно.

## Состояние И Приватность

Runtime state специально хранится вне репозитория, по умолчанию в `~/.job-hunter/`:

- cookies для browser sessions
- скачанное резюме
- `seen_vacancies.json`
- `run_history.jsonl`
- `analytics_events.jsonl` / `analytics_state.json`
- `hh_resume_pipeline.json` — состояние A/B тестирования резюме
- `facts.json` — структурированные факты кандидата (из `./run.sh extract-facts`)
- `knowledge/*.md` — пользовательская база знаний (about_me, qa_kb, и т.п.)
- `chat_responder_state.json` — last_replied_msg_id, состояние уведомлений о подозрительных сообщениях и replies_count per чат
- `hh_guard_state.json` — счётчик откликов + anti-bot блокировки
- runtime status
- Playwright debug screenshots и HTML-dumps (включая `captcha_*.png` и `chat_preview_*.png`)

Это позволяет безопасно публиковать репозиторий, не таща в него персональные данные и рабочее состояние.

## База Знаний Кандидата

В `~/.job-hunter/profiles/<name>/knowledge/` можно класть `.md`/`.txt` файлы со структурированными фактами о кандидате: «о себе», «база технических знаний», «опыт по конкретным инструментам», и т.п.

При генерации cover letter / ответа на анкету / реплики в AI-чат — модуль делает **2-pass LLM-фильтрацию**: первый малый запрос выбирает 5 самых релевантных секций (по заголовкам `## NN. Title` внутри файлов), второй запрос уже использует только эти секции в контексте. Это:

- экономит токены (12 KB полного KB → ~8 KB релевантных);
- повышает точность (для Mobile-QA вакансии не подкладываем секции про электрику/3D-печать);
- даёт детальные, фактические ответы вместо общих формулировок.

Файлы можно обновлять в любой момент — следующий run подхватит автоматически.

## Необязательные Интеграции

Telegram-уведомления и интеграция с AI Office необязательны. Если оставить их env-переменные пустыми, основной pipeline поиска всё равно будет работать.

## Встроенная Статистика

`./run.sh stats` показывает:

- накопленные счётчики обработанных / откликнутых / ручных / пропущенных вакансий из `seen_vacancies.json`;
- разбивку по площадкам (`hh.ru`, `Хабр Карьера`, `GeekJob`, `SuperJob`);
- самые частые действия вроде `applied`, `skipped_low_score`, `manual_*`;
- несколько последних прогонов поиска из `run_history.jsonl`.
- скользящую аналитику из `analytics_events.jsonl`: запросы, варианты резюме и исходы переговоров `hh`.
- воронку откликов: отклик → просмотрено → ожидание / отказ / позитив, с процентами отклика и конверсии.
- A/B сравнение резюме: по каждому варианту — откликов, просмотрено, позитив, отказ, response rate, conversion rate.

## Известные Ограничения

- DOM у `hh.ru` и `Habr Career` может меняться и ломать селекторы.
- `hh.ru` может включать captcha после большого числа подряд идущих автооткликов. Hybrid solver (vision-LLM + TG-bridge) обычно справляется, но не гарантия.
- Автоотклик `GeekJob` зависит от сохранённой specialist-сессии и может ломаться, если сайт меняет JSON/API flow.
- Дефолтные поисковые наборы ориентированы на `QA`, пока ты не переопределишь их через env или `config.py`.
- Качество LLM-оценки полностью зависит от выбранного провайдера, модели и качества резюме/базы знаний.
- Детект подозрительного HR-скрининга эвристический. Такие сообщения специально не автоотправляются от имени обычного рекрутера: перед отправкой нужен Telegram-approval.
- Ollama Cloud имеет недельные лимиты — если упёрся, временно переключайся на другой ключ (см. `~/.job-hunter/llm-providers.env`) или другую модель.

## Документация

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Publication Notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## Лицензия

MIT. См. [LICENSE](LICENSE).
