# Job Hunter v0.3.0

English version: [README.md](README.md)

`Job Hunter` — Python-инструмент для автоматизации поиска QA/testing вакансий на нескольких job board-платформах, их оценки через LLM и автоотклика там, где площадка это позволяет.

Поддерживает изолированные профили пользователей, LLM-анализ резюме, воронку откликов с A/B тестированием резюме и интерактивный мастер настройки — проект подходит и для личного использования, и как основа для многопользовательского сервиса.

Текущий публичный статус: `OBT` (open beta testing). Ожидай дрейф селекторов, captcha-ограничения и платформенные edge case'ы.

## Что Он Делает

- Ищет вакансии из нескольких источников за один прогон
- Убирает дубли между площадками
- Применяет быстрый keyword-filter до вызова LLM
- Оценивает каждую вакансию относительно твоего резюме
- Генерирует короткое сопроводительное письмо для релевантных совпадений
- Отправляет автоотклики там, где это поддерживается
- Переводит вакансии в manual review и отправляет Telegram-уведомления, если автоотклик невозможен
- Ведёт воронку откликов: отклик → просмотр → ожидание / отказ / позитив
- Поддерживает A/B тестирование резюме с отдельной статистикой по вариантам
- Анализирует резюме через LLM и отправляет рекомендации в Telegram
- Поддерживает изолированные профили пользователей для многопользовательских сценариев
- Хранит `seen`, cookies, runtime status и debug-артефакты вне репозитория

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
- `LLM_MODEL`: модель для оценки вакансий и cover letter
- `SUPERJOB_API_KEY`: нужен для поиска на `SuperJob`
- `HUNTER_BOT_TOKEN`: необязательный Telegram bot token для уведомлений
- `NOTIFY_CHAT_ID`: необязательный Telegram chat ID для уведомлений
- `OFFICE_URL`: необязательный base URL AI Office HTTP API
- `OFFICE_DB`: необязательный путь к AI Office SQLite
- `JOB_HUNTER_HOME`: директория для cookies, resume, seen state, runtime status и скриншотов

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

# Поиск по конкретным площадкам
./run.sh superjob-dry-run
./run.sh superjob-search
./run.sh habr-dry-run
./run.sh habr-search
./run.sh geekjob-dry-run
./run.sh geekjob-search
```

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
- runtime status
- Playwright debug screenshots и HTML-dumps

Это позволяет безопасно публиковать репозиторий, не таща в него персональные данные и рабочее состояние.

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
- `hh.ru` может включать captcha после большого числа подряд идущих автооткликов.
- Автоотклик `GeekJob` зависит от сохранённой specialist-сессии и может ломаться, если сайт меняет JSON/API flow.
- дефолтные поисковые наборы ориентированы на `QA`, пока ты не переопределишь их через env или `config.py`.
- Качество LLM-оценки полностью зависит от выбранного провайдера, модели и качества резюме.

## Документация

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Publication Notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## Лицензия

MIT. См. [LICENSE](LICENSE).
