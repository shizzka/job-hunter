# Job Hunter

**Telegram-бот, который ищет вакансии на hh.ru, SuperJob, Habr Career и GeekJob, объясняет, чем каждая подходит, и может откликаться за вас — по умолчанию только с вашего согласия.**

[![Offline regression tests](https://github.com/shizzka/job-hunter/actions/workflows/offline-tests.yml/badge.svg)](https://github.com/shizzka/job-hunter/actions/workflows/offline-tests.yml) ![version](https://img.shields.io/badge/version-0.11.0-blue) ![python](https://img.shields.io/badge/python-3.12-blue) [![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

English version: [README.md](README.md)

<p align="center">
  <img src="docs/assets/demo-card.gif" width="420" alt="Карточка вакансии в Telegram: оценка, удалёнка, чем подходит; пользователь жмёт «Почему», потом «Откликнуться с ИИ», бот сообщает об отклике и показывает письмо">
  <br><sub>Демо, данные вымышлены. Тексты и кнопки — настоящие из бота.</sub>
</p>

- **Решаете вы, рутину делает бот.** По умолчанию каждая вакансия приходит карточкой: формат работы, оценка и почему подходит. Одно нажатие — и бот откликается с сопроводительным письмом. Полностью автоматические отклики включаются в настройках, если вы сами этого хотите.
- **Шум отсекается до ИИ.** Офис и гибрид при поиске удалёнки, чужая профессия, компании из чёрного списка — всё это убирают простые фильтры, и ИИ оценивает только то, что стоит читать.
- **Не выдумывает.** Письма, ответы на анкеты и в чатах с HR — только из фактов вашего резюме и подтверждённых ответов. Чего бот не знает, того не пишет.
- **Несколько профессий в одном профиле.** У каждого направления поиска (например, QA и электрик) свои резюме, запросы, фильтры и режим откликов.

> «Бот ищет вакансии лучше, чем я сама» — первая пользовательница, ищет только удалёнку.

**Кто делает.** Автор — электрик, не программист. Код пишут ИИ-агенты (Claude Code и Codex), а автор ставит задачи, тестирует бота как пользователь и решает, что выпускать. ИИ пишет, человек проектирует и проверяет: около 7000 офлайн-тестов в CI на каждый пуш, каждую ночь dev-бота проходит ИИ-тестировщик как живой человек, а сам бот каждый день работает на двух настоящих поисках работы. Если бот не может доказать, что действие безопасно, он останавливается и спрашивает.

**Текущая версия:** `v0.11.0` (25.10.2026): отклики не теряются и не уходят дважды после сбоя страницы hh, «только удалёнка» в одно нажатие, статистика сходится на всех экранах, бесплатные модели Ollama Cloud в запасе. Подробно — в [CHANGELOG](CHANGELOG.md).

## Как он решает

Обычный auto-apply гонится за количеством:

```text
vacancy -> rewrite resume -> apply -> apply -> apply
```

Job Hunter оптимизирует решение, а не число откликов:

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

## Что умеет

| Возможность | Статус |
| --- | --- |
| Поиск и дедуп вакансий из нескольких источников | ✅ |
| Несколько направлений поиска в одном профиле (резюме, запросы, фильтры, регионы) | ✅ |
| LLM-оценка релевантности вакансии | ✅ |
| Фильтры до LLM, чтобы не жечь токены на очевидный мусор | ✅ |
| Сопроводительное по подтверждённым данным кандидата (шаблон — только с решения владельца) | ✅ |
| Точный выбор HH-резюме перед отправкой | ✅ |
| Анкеты работодателя на hh.ru; вопрос без уверенного ответа задаётся один раз и запоминается | ✅ |
| Google Forms из рекрутерских чатов с предпросмотром перед отправкой | ✅ |
| Управление через Telegram, очередь команд и ручное подтверждение рискованных действий | ✅ |
| «На разбор»: страницы, сортировка по баллу, массовая уборка | ✅ |
| Черновики ответов в чатах hh фактами того резюме, которым был отклик | ✅ |
| Изолированные профили пользователей | ✅ |
| Воронка откликов, A/B резюме, снимки рынка вакансий и аналитика | ✅ |
| Подробные трассы HH-откликов | ✅ |
| Запасные LLM-провайдеры | ✅ |
| Обучение поисковых запросов по оценкам 👍/👎 | 🚧 копим данные |
| Подгонка резюме под вакансию (Controlled Resume Tailoring) | ⏸ отложено |

## Поддерживаемые площадки

| Источник | Поиск | Детали | Auto-apply | Зрелость |
| --- | --- | --- | --- | --- |
| hh.ru | ✅ | ✅ | ✅ | основной, live-tested |
| GeekJob | ✅ | ✅ | ✅ | beta, guarded |
| SuperJob | ✅ | ✅ | ✅ | beta |
| Habr Career | ✅ | ✅ | ✅ | beta, зависит от DOM |

Внешние сайты меняют DOM, API и антибот-механику без предупреждения. Поэтому наличие адаптера не означает одинаковую зрелость всех площадок.

## Безопасность прежде всего

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

# Чаты hh
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

Временный LAN Ollama fallback существует как аварийный compatibility path и не является целевой архитектурой проекта. Требования и план миграции вынесены в отдельный [AI Gateway](https://github.com/shizzka/ai-gateway); текущий fallback Job Hunter остаётся рабочим до проверенной миграции.

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

## База знаний кандидата

В `knowledge/*.md` можно хранить подтверждённые сведения о навыках, проектах и опыте.

Перед генерацией текста Job Hunter выбирает релевантные секции и использует их вместе с резюме и structured facts. Vacancy text используется как контекст требования, но не как источник фактов о кандидате.

## Настройка в Telegram

<p align="center">
  <img src="docs/assets/demo-setup.gif" width="420" alt="Экран «С чего начать»: шаги отмечены, пользователь выбирает «Спрашивать меня перед откликом», список обновляется">
  <br><sub>Демо, данные вымышлены.</sub>
</p>

`/getting_started` показывает, где вы сейчас: вход в hh.ru, резюме, направления, режим откликов, первый поиск. У каждого шага своя кнопка.

## Telegram

Telegram — пульт управления для действий, где полезен человек:

- направления поиска (что искать, какими резюме, в каких регионах, режим откликов);
- «На разбор»: сверху лучшие, страницы, массовая уборка, оценки 👍/👎;
- отклики из жёлтой зоны и очередь команд вместо «занят»;
- вопросы работодателя, на которые бот не знает ответа, — задаются один раз, ответ запоминается;
- черновики ответов в чатах с подтверждением; отказы приходят одной сводкой;
- редактирование Google Forms;
- помощь с капчей;
- управление профилем, снимки рынка вакансий, мониторинг и аналитика.

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

Выпуски и что в каждом изменилось — в [CHANGELOG.md](CHANGELOG.md). Разработка идёт в ветке `dev`;
каждое изменение проходит полный офлайн-сьют, прежде чем попасть в работающего бота.

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
