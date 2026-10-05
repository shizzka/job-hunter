# Job Hunter

**Текущая версия:** `v0.8.0`
**Статус:** Стабилизация v0.8.0 закрыта и заморожена; реальное soak/E2E наблюдение.

English version: [README.md](README.md)

Job Hunter — система автоматизации поиска работы, которая не пытается выиграть количеством откликов. Она ищет вакансии, отсекает очевидный шум, оценивает соответствие реальному опыту кандидата, проверяет факты и только затем решает, можно ли безопасно откликаться автоматически или нужен человек.

Основной сценарий проекта сегодня — QA / testing-вакансии, но search policy можно переключить на другие направления.

## Зачем он нужен

Обычный auto-apply выглядит примерно так:

```text
vacancy -> rewrite resume -> apply -> apply -> apply
```

Job Hunter устроен иначе:

```text
job boards
    ↓
search + dedupe
    ↓
deterministic filters
    ↓
LLM matcher
    ↓
candidate facts / resume / knowledge base
    ↓
decision
   ↙      ↘
skip    manual review / apply
               ↓
        safety guards
               ↓
        platform adapter
               ↓
            analytics
```

Цель — не максимальное число откликов, а максимальное качество решения об отклике.

## Что умеет

| Возможность | Статус |
| --- | --- |
| Поиск и дедуп вакансий из нескольких источников | ✅ |
| LLM-оценка релевантности вакансии | ✅ |
| Фильтры до LLM, чтобы не жечь токены на очевидный мусор | ✅ |
| Генерация сопроводительного по подтверждённым данным кандидата | ✅ |
| Точный выбор HH-резюме перед отправкой | ✅ |
| Анкеты работодателя на hh.ru | ✅ |
| Google Forms из рекрутерских чатов с preview перед submit | ✅ |
| Telegram control plane и ручное подтверждение рискованных действий | ✅ |
| HH chat drafts / screening replies | ✅ |
| Изолированные профили пользователей | ✅ |
| Application funnel, A/B резюме и аналитика | ✅ |
| Structured traces для HH-откликов | ✅ |
| Multi-provider LLM fallback | ✅ |
| Controlled Resume Tailoring | 🚧 следующий спринт |

## Поддерживаемые площадки

| Источник | Поиск | Детали | Auto-apply | Зрелость |
| --- | --- | --- | --- | --- |
| hh.ru | ✅ | ✅ | ✅ | основной, live-tested |
| GeekJob | ✅ | ✅ | ✅ | beta, guarded |
| SuperJob | ✅ | ✅ | ✅ | beta |
| Habr Career | ✅ | ✅ | ✅ | beta, зависит от DOM |

Внешние сайты меняют DOM, API и антибот-механику без предупреждения. Поэтому наличие адаптера не означает одинаковую зрелость всех площадок.

## Safety-first поведение

Job Hunter старается **не делать действие, если не может доказать, что оно безопасно**.

Ключевые правила:

- неизвестный факт о кандидате остаётся неизвестным;
- требования вакансии не считаются доказательством опыта кандидата;
- inferred / weak facts не используются как подтверждённый опыт;
- неподтверждённое или неоднозначное HH-резюме блокирует submit;
- ошибка или исчерпание LLM-провайдеров даёт `deferred_unscored`, а не fake score=0;
- изменившийся draft / approval / form revision блокирует отправку;
- profile state, cookies, resume, facts и analytics изолированы;
- внешние действия стараются использовать fail-closed guards и manual review;
- приватные runtime-артефакты хранятся вне репозитория.

Подробности: [Architecture](docs/ARCHITECTURE.md), [Account & submission safety](docs/ACCOUNT_SUBMISSION_SAFETY.md), [Answer grounding](docs/ANSWER_GROUNDING_AND_ANALYSIS.md).

## Быстрый старт

### 1. Установка

```bash
git clone https://github.com/shizzka/job-hunter.git
cd job-hunter

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Конфигурация

```bash
mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env
```

Минимально нужен OpenAI-compatible LLM provider:

```env
LLM_BASE_URL=https://your-provider.example/v1
JOB_HUNTER_LLM_KEY=your-key
LLM_MODEL=your-model
```

Telegram и дополнительные площадки опциональны.

### 3. Мастер настройки

```bash
./run.sh setup
```

Он создаёт профиль, помогает подключить резюме, площадки и основные параметры.

### 4. Сначала dry-run

```bash
./run.sh dry-run
```

### 5. Реальный поиск

```bash
./run.sh search
```

## AI-assisted setup

Если используешь coding agent, можно дать ему:

> Clone https://github.com/shizzka/job-hunter and follow SETUP_AGENT.md step by step. Ask before any operation that logs in, submits an application, sends Telegram messages, or changes production configuration.

Полный сценарий: [SETUP_AGENT.md](SETUP_AGENT.md).

## Основные команды

```bash
# Профили
./run.sh setup
./run.sh profiles
./run.sh --profile qa dry-run
./run.sh --profile qa search

# Логин
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh geekjob-login

# Резюме и факты
./run.sh grab-resume
./run.sh analyze-resume
./run.sh extract-facts

# Аналитика
./run.sh stats
./run.sh analytics
./run.sh digest

# HH chats
./run.sh chat-respond

# Сервисы
./run.sh daemon
./run.sh bot
./run.sh status
```

Полный список команд и переменных находится в [Operations](docs/OPERATIONS.md) и [job-hunter.env.example](job-hunter.env.example).

## LLM

Job Hunter использует OpenAI-compatible API и умеет работать с несколькими провайдерами через fallback chain.

Основные task-specific модели:

```env
HH_MATCHER_MODEL=
HH_COVER_LETTER_MODEL=
HH_QUESTION_MODEL=
HH_CHOICE_MODEL=
HH_FACTS_EXTRACT_MODEL=
HH_CHAT_RESPONDER_MODEL=
HH_CAPTCHA_VISION_MODEL=
```

Пустое значение использует `LLM_MODEL`.

Временный LAN Ollama fallback существует как аварийный compatibility path и не является целевой архитектурой проекта. После миграции на общий AI Gateway он должен быть удалён из Job Hunter.

## Профили кандидатов

Каждый профиль получает собственные:

```text
resume
facts
knowledge base
cookies
seen history
manual queues
analytics
runtime state
debug traces
```

По умолчанию состояние находится в:

```text
~/.job-hunter/
~/.job-hunter/profiles/<name>/
```

Профили не должны наследовать биографию, salary expectations, contacts или resume IDs другого кандидата.

## Candidate knowledge base

В `knowledge/*.md` можно хранить подтверждённые сведения о навыках, проектах и опыте.

Перед генерацией текста Job Hunter выбирает релевантные секции и использует их вместе с резюме и structured facts. Vacancy text используется как контекст требования, но не как источник фактов о кандидате.

## Telegram

Telegram используется как control plane для действий, где полезен человек:

- manual-review вакансии;
- yellow-zone apply;
- подтверждение chat replies;
- редактирование Google Forms;
- captcha bridge;
- управление поиском и профилем;
- мониторинг и аналитика.

Рискованные сценарии по возможности требуют явного подтверждения пользователя.

## Аналитика

Job Hunter пишет локальные события по этапам:

```text
found
→ filtered
→ matched
→ applied/manual/deferred
→ viewed
→ rejected/positive
→ interview/test task/offer
```

Также сохраняются requested/selected resume metadata, apply mode, match score, provider/model metadata и локальные resume hashes там, где это возможно.

Это позволяет оценивать не только число откликов, но и качество фильтрации, резюме и стратегии.

## Тесты и CI

Обычный test suite не должен выполнять реальные отклики, Telegram sends или внешние submit.

```bash
python -m pytest -q
```

GitHub Actions запускает изолированный offline regression suite. Browser regressions используют синтетические страницы и установленный Chromium.

Live acceptance checks выполняются отдельно и только явно разрешёнными сценариями.

## Состояние и приватность

Runtime state хранится вне Git:

- cookies и auth state;
- resume / facts / knowledge;
- `seen_vacancies.json`;
- analytics journals;
- queues и approval state;
- screenshots / HTML traces;
- Google Form previews;
- chat state.

Файлы состояния и диагностические артефакты могут содержать персональные данные. Не прикладывай целые trace-каталоги в публичные issues без ручной проверки и редактирования.

## Ограничения

- DOM hh.ru, Habr Career и других площадок может измениться.
- CAPTCHA и антибот не гарантируют автоматическое прохождение.
- LLM grounding снижает риск выдуманных утверждений, но не является математическим доказательством истины.
- Существующий HH-отклик не всегда позволяет восстановить, каким резюме он был отправлен.
- Доставка внешнего действия не всегда может быть доказана exactly-once.
- Другие job boards имеют меньшую live-coverage, чем основной HH workflow.
- Offline-аудит не сертифицировал совместимость с live сторонними площадками.

## Разработка

Цель текущей ветки продукта:

```text
v0.8.0 stabilization frozen
→ real soak / E2E observation
```

Не каждое потенциальное улучшение должно становиться новой подсистемой. Новые abstractions оправданы только тогда, когда закрывают реальный повторяющийся failure mode.

## Документация

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Account & submission safety](docs/ACCOUNT_SUBMISSION_SAFETY.md)
- [Answer grounding](docs/ANSWER_GROUNDING_AND_ANALYSIS.md)
- [Google Forms editing](docs/GOOGLE_FORM_EDITING.md)
- [Publication notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## Лицензия

MIT. См. [LICENSE](LICENSE).
