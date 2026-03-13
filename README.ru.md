# Job Hunter v0.2.0 OBT

English version: [README.md](README.md)

`Job Hunter` это Python-инструмент для автоматизации поиска QA/testing вакансий на нескольких job board-платформах, их оценки через LLM и автоотклика там, где площадка это позволяет.

Проект изначально делался для личного использования, но репозиторий уже приведён в состояние, в котором его можно клонировать, настроить под свои аккаунты и запустить на своей машине.

Текущий публичный статус: `OBT` (open beta testing). Ожидай дрейф селекторов, captcha-ограничения и платформенные edge case'ы.

## Что Он Делает

- Ищет вакансии из нескольких источников за один прогон
- Убирает дубли между площадками
- Применяет быстрый keyword-filter до вызова LLM
- Оценивает каждую вакансию относительно твоего резюме
- Генерирует короткое сопроводительное письмо для релевантных совпадений
- Отправляет автоотклики там, где это поддерживается
- Переводит вакансии в manual review и отправляет Telegram-уведомления, если автоотклик невозможен
- Хранит `seen`, cookies, runtime status и debug-артефакты вне репозитория

## Поддерживаемые Источники

| Источник | Поиск | Детали | Автоотклик |
| --- | --- | --- | --- |
| `hh.ru` | Да | Да | Да |
| `Habr Career` | Да | Да | Да |
| `SuperJob` | Да | Да | Да |
| `GeekJob` | Да | Да | Пока нет |

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

1. Создай и активируй виртуальное окружение.
2. Установи зависимости.
3. Установи браузер для Playwright.
4. Создай локальный env-файл на основе примера.
5. Заполни свои учётные данные и необязательные интеграции.
6. Авторизуйся на площадках, где нужны browser sessions.
7. Сначала сделай dry-run, потом реальный search.

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
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh grab-resume

./run.sh dry-run
./run.sh search
./run.sh check
./run.sh daemon
./run.sh stats

./run.sh superjob-dry-run
./run.sh superjob-search
./run.sh habr-dry-run
./run.sh habr-search
./run.sh geekjob-dry-run
./run.sh geekjob-search
```

## Состояние И Приватность

Runtime state специально хранится вне репозитория, по умолчанию в `~/.job-hunter/`:

- cookies для browser sessions
- скачанное резюме
- `seen_vacancies.json`
- runtime status
- Playwright debug screenshots и HTML-dumps

Это позволяет безопасно публиковать репозиторий, не таща в него персональные данные и рабочее состояние.

## Необязательные Интеграции

Telegram-уведомления и интеграция с AI Office необязательны. Если оставить их env-переменные пустыми, основной pipeline поиска всё равно будет работать.

## Известные Ограничения

- DOM у `hh.ru` и `Habr Career` может меняться и ломать селекторы.
- `hh.ru` может включать captcha после большого числа подряд идущих автооткликов.
- `GeekJob` сейчас работает только как manual-review источник.
- Качество LLM-оценки полностью зависит от выбранного провайдера, модели и качества резюме.

## Документация

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Publication Notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## Лицензия

MIT. См. [LICENSE](LICENSE).
