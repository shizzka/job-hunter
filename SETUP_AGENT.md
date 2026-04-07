# AI Agent Setup Guide for Job Hunter

You are an AI assistant helping a user install and configure **Job Hunter**. Follow the steps below **in order**. At each step, **ask the user** for the required information before proceeding. Do not skip steps or assume values. Explain what each option means in simple, non-technical language.

**Important:** Many users are not developers. They may not know what "venv", "API key" or "terminal" means. Adapt your language to the user's level. If they seem confused — slow down, explain terms, offer to do things for them where possible.

---

## Before you start — explain what Job Hunter is

**Tell the user this at the very beginning, before any installation:**

> **Job Hunter** — это программа-робот, которая ищет вакансии за тебя. Вот как она работает:
>
> 1. **Робот заходит на сайты с вакансиями** (hh.ru, SuperJob, Habr Career, GeekJob) — точно так же, как ты заходишь через браузер, только автоматически.
>
> 2. **Читает каждую вакансию и сравнивает с твоим резюме** — для этого используется нейросеть (LLM). Она оценивает, насколько вакансия тебе подходит, по шкале от 0 до 100.
>
> 3. **Если вакансия подходит — робот сам откликается** и пишет сопроводительное письмо. Если не может откликнуться автоматически — отправляет тебе ссылку в Telegram, чтобы ты откликнулся вручную.
>
> 4. **Отслеживает результаты:** кто просмотрел, кто ответил, кто отказал. Статистику можно посмотреть командой или получить в Telegram.

### Что нужно понимать

> **Это не "нажал кнопку и забыл".** Job Hunter — это инструмент, который нужно:
>
> - **Настроить один раз** — это мы сейчас сделаем вместе (займёт 20-40 минут).
> - **Держать запущенным постоянно** — робот работает в фоновом режиме на твоём компьютере или сервере. Пока он запущен, он каждые ~30 минут проверяет новые вакансии. **Выключил компьютер — робот остановился.**
> - **Иногда обслуживать** — примерно раз в 1-2 недели нужно:
>   - перелогиниться на площадках (cookies протухают);
>   - проверить, что робот ещё работает;
>   - посмотреть статистику и, возможно, скорректировать поисковые запросы.
>
> **Telegram обязательно нужен** (ну, сильно рекомендован). Без него ты не будешь знать, что робот нашёл, кто тебе ответил, и есть ли вообще отклики. Telegram — это твоя "панель управления".

### Где должен работать робот

> Job Hunter должен работать **постоянно** — как фоновый процесс. Варианты:
>
> - **Домашний компьютер / ноутбук** — работает, пока комп включён и не в спящем режиме. Ноутбук закрыл — робот остановился. Подходит для теста, но не для постоянной работы.
> - **VPS / облачный сервер** (рекомендуется) — дешёвый сервер за 300-500 руб/мес, работает 24/7. Идеальный вариант. Нужен Linux (Ubuntu).
> - **Старый компьютер / Raspberry Pi** — тоже подходит, если он включён постоянно.

Ask the user: **"Ты понимаешь, что робот должен работать постоянно на включённом компьютере или сервере? На чём планируешь запускать?"**

If the user says "on my laptop" — warn them that the robot stops when the laptop sleeps or is closed. Suggest a VPS if they want 24/7 operation.

---

## Step 0 — Prerequisites

Check that the machine has:
- **Python 3.10+** (`python3 --version`)
- **pip** (`pip3 --version`)
- **git** (`git --version`)

If anything is missing, install it:
```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# macOS (Homebrew)
brew install python3 git

# Windows — install Python from https://python.org (check "Add to PATH"), git from https://git-scm.com
```

---

## Step 1 — Clone the repository

```bash
git clone https://github.com/shizzka/job-hunter.git
cd job-hunter
```

> Ask the user: "Where do you want to clone the project? Default is the current directory."

---

## Step 2 — Create virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate    # Linux/macOS
# Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

If `playwright install` fails behind a firewall/proxy, suggest:
```bash
HTTPS_PROXY=http://proxy:port playwright install chromium
```

---

## Step 3 — Set up Telegram (strongly recommended)

**Tell the user:** "Telegram — это основной способ получать уведомления от робота. Без Telegram ты не узнаешь о найденных вакансиях, пока не зайдёшь в терминал. Настоятельно рекомендую настроить."

### What the Telegram bot does

- Присылает **каждую подходящую вакансию** с оценкой, зарплатой и ссылкой
- Сообщает, когда робот **автоматически откликнулся**
- Присылает вакансии для **ручного отклика** (когда автоматически не получилось)
- Отправляет **дайджест** и **статистику** по запросу
- Уведомляет об **ответах работодателей** (просмотры, приглашения, отказы)
- Присылает **анализ резюме** от нейросети с рекомендациями по улучшению

### How to set up

Walk the user through this step by step:

1. **Создать бота:**
   - Открой Telegram, найди @BotFather
   - Отправь `/newbot`
   - Придумай имя (например, "My Job Hunter") и username (например, `my_jobhunter_bot`)
   - BotFather пришлёт **токен** — длинная строка вида `123456:ABC-DEF...`. Скопируй его.

2. **Узнать свой Chat ID:**
   - Найди в Telegram бота @userinfobot (или @getmyid_bot)
   - Нажми Start — он пришлёт твой числовой ID (например, `987654321`). Скопируй его.

3. **Активировать своего бота:**
   - Найди своего нового бота в Telegram по username, который выбрал
   - Нажми Start — это обязательно, иначе бот не сможет тебе писать

The values to save:
```env
HUNTER_BOT_TOKEN=123456:ABC-DEF...        # токен от BotFather
NOTIFY_CHAT_ID=987654321                   # твой числовой ID
```

If the user is in Russia and Telegram API is blocked, they may need a proxy:
```env
TELEGRAM_PROXY=socks5://127.0.0.1:7897
```

---

## Step 4 — Choose and configure LLM provider

**Tell the user:** "Робот использует нейросеть, чтобы читать вакансии и оценивать, подходят ли они тебе. Нужно выбрать, какую нейросеть использовать. Это как 'мозг' робота."

Ask the user: **"Какой вариант тебе ближе?"**

### Option A: Ollama Cloud (recommended for most users)

**Что это:** Облачные нейросети на сайте ollama.com. Ничего устанавливать не надо, работает через интернет.
**Стоимость:** Есть бесплатный лимит, потом очень дёшево.
**Рекомендуемая модель:** `deepseek-v3.1:671b` — лучшее качество оценки вакансий.

**Как подключить:**
1. Зайди на https://ollama.com и создай аккаунт
2. Перейди в Settings → API Keys → создай новый ключ
3. Скопируй ключ

```env
LLM_BASE_URL=https://ollama.com/v1
JOB_HUNTER_LLM_KEY=<вставь ключ>
LLM_MODEL=deepseek-v3.1:671b
```

### Option B: Local Ollama (for users with a GPU)

**Что это:** Нейросеть работает прямо на твоём компьютере. Бесплатно, полностью приватно.
**Нужно:** Видеокарта с 8+ ГБ видеопамяти (NVIDIA RTX 3060 и выше, или аналог).
**Нет мощной видеокарты? → Выбери Option A.**

**Рекомендуемые модели:**
- `qwen2.5:7b` — лёгкая, работает на 8 ГБ видеопамяти
- `qwen2.5:14b` — оптимальный баланс (нужно ~10 ГБ)
- `deepseek-v3:32b` — лучшее качество локально (нужно ~20 ГБ)

**Как установить Ollama:**
```bash
# Linux (одна команда):
curl -fsSL https://ollama.com/install.sh | sh

# macOS:
brew install ollama
# или скачать с https://ollama.com/download

# Windows:
# Скачать установщик с https://ollama.com/download
```

**После установки:**
```bash
ollama serve &              # запустить сервер (работает в фоне)
ollama pull qwen2.5:14b     # скачать модель (один раз, ~8 ГБ)
```

```env
LLM_BASE_URL=http://127.0.0.1:11434/v1
JOB_HUNTER_LLM_KEY=ollama
LLM_MODEL=qwen2.5:14b
```

> API-ключ для локального Ollama может быть любой строкой — локальный сервер его не проверяет.

### Option C: OpenAI API

**Что это:** Модели от OpenAI (ChatGPT). Платно, но качество гарантированное.
**Стоимость:** ~$0.02-0.05 за один прогон (50 вакансий). Минимальное пополнение $5.
**Рекомендуемая модель:** `gpt-4.1-mini` — быстрая, дешёвая, достаточно умная для оценки вакансий.

**Как подключить:**
1. Зайди на https://platform.openai.com → регистрация
2. API Keys → создай ключ
3. Пополни баланс (минимум $5)

```env
LLM_BASE_URL=https://api.openai.com/v1
JOB_HUNTER_LLM_KEY=<вставь ключ>
LLM_MODEL=gpt-4.1-mini
```

### Option D: Any OpenAI-compatible provider

Job Hunter works with any provider exposing `/v1/chat/completions`: OpenRouter, Together AI, Groq, local vLLM, LM Studio, etc.

Ask the user for: Base URL, API key, Model name.

```env
LLM_BASE_URL=<URL провайдера>/v1
JOB_HUNTER_LLM_KEY=<ключ>
LLM_MODEL=<название модели>
```

---

## Step 5 — Create the configuration file

```bash
mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env
```

Edit `~/.job-hunter/job-hunter.env` — fill in:
1. LLM values from Step 4
2. Telegram values from Step 3

Then ask the user about additional settings:

1. **Search queries** — "Какие вакансии ищем? По умолчанию настроено на QA/тестирование."
   - If not QA: customize `HH_SEARCH_QUERIES`, `SUPERJOB_SEARCH_QUERIES`, `HABR_SEARCH_PATHS`
   - Use `||` as separator for multiple queries
   - Example for developers: `HH_SEARCH_QUERIES=Python developer||backend engineer||Django`

2. **SuperJob API key** — "Хочешь искать на SuperJob? Нужен бесплатный API-ключ."
   - If yes: register an app at https://api.superjob.ru → get `SUPERJOB_API_KEY`

3. **Proxy** — "Есть проблемы с доступом к Telegram API или сайтам вакансий?"
   - If yes: set `TELEGRAM_PROXY` and/or `BROWSER_PROXY`

---

## Step 6 — Prepare the resume

**Tell the user:** "Резюме — это самое важное. Нейросеть сравнивает каждую вакансию с твоим резюме. Чем подробнее резюме — тем точнее оценка и лучше сопроводительные письма."

Ask the user: **"У тебя есть резюме в текстовом виде? Можешь скинуть сюда, я сохраню."**

- If yes: save it as `~/.job-hunter/resume.md`
- If no: copy the template and help them fill it in:

```bash
cp resume.md.example ~/.job-hunter/resume.md
```

Key sections to fill:
- **ФИО и целевая должность** — кем хочешь работать
- **Опыт работы** — где работал, сколько лет, что делал
- **Ключевые навыки** — технологии, инструменты, языки
- **Образование** (опционально)
- **Языки** (опционально)

> Tip: If the user has a resume on hh.ru, they can download it later with `./run.sh grab-resume` after logging in.

---

## Step 7 — Log in to job platforms (interactive)

**Tell the user:** "Теперь нужно один раз залогиниться на сайтах с вакансиями. Робот откроет браузер — тебе нужно ввести логин/пароль как обычно. После этого робот запомнит сессию и будет заходить сам."

Ask the user: **"На каких площадках у тебя есть аккаунт?"**

For each platform:

```bash
./run.sh login              # hh.ru — основная площадка, рекомендуется всем
./run.sh habr-login         # Habr Career — для IT-специалистов
./run.sh superjob-login     # SuperJob — много вакансий, нужен API-ключ
./run.sh geekjob-login      # GeekJob — для IT, много удалёнки
```

> **Important:** These commands open a real browser window. The user must log in manually. After successful login, they should **close the browser** — cookies are saved automatically to `~/.job-hunter/`.
>
> **Cookies expire.** Tell the user: "Примерно раз в 1-2 недели нужно будет перелогиниться — площадки сбрасывают сессии. Если робот перестал находить вакансии или откликаться — первым делом перелогинься."

---

## Step 8 — Test run

```bash
./run.sh dry-run
```

**Tell the user:** "Это пробный запуск. Робот найдёт вакансии и оценит их, но НЕ будет откликаться. Просто проверяем, что всё работает."

Check the output:
- Are vacancies found? (If 0 — check search queries or cookies)
- Are LLM scores reasonable (0-100)?
- Any errors?

If Telegram is configured, the user should receive test notifications.

---

## Step 9 — Launch for real

Once the test run looks good:

### One-time search with auto-apply:
```bash
./run.sh search
```

### Background mode (recommended for daily use):
```bash
./run.sh daemon
```

**Tell the user:**
> "В режиме `daemon` робот работает в фоне:
> - Ищет новые вакансии каждые ~30 минут
> - Проверяет ответы работодателей каждые ~60 минут
> - Отправляет всё в Telegram
>
> **Робот работает, пока работает компьютер.** Если перезагрузишь — нужно запустить снова.
> Остановить: `./run.sh stop`
> Статистика: `./run.sh stats`
> Дайджест в Telegram: `./run.sh digest`"

### Auto-start after reboot (Linux/VPS):

If the user is on a VPS and wants Job Hunter to survive reboots:

```bash
# Add to crontab:
crontab -e
# Add this line:
@reboot cd /path/to/job-hunter && source venv/bin/activate && ./run.sh daemon >> ~/.job-hunter/daemon.log 2>&1
```

---

## Step 10 — Optional extras

1. **LLM resume analysis:** `./run.sh analyze-resume` — нейросеть проанализирует резюме и пришлёт рекомендации в Telegram
2. **Interactive profile wizard:** `./run.sh setup` — продвинутая настройка (A/B тестирование резюме, несколько профилей)
3. **Check invitations:** `./run.sh check` — проверить приглашения на hh.ru

---

## Ongoing maintenance — tell the user!

**This is critical. The user must understand that Job Hunter requires periodic attention.**

> **Что нужно делать регулярно:**
>
> | Как часто | Что делать | Команда |
> |-----------|-----------|---------|
> | Каждый день | Проверять Telegram — смотреть, что робот нашёл | — |
> | Раз в неделю | Смотреть статистику, корректировать запросы | `./run.sh stats` |
> | Раз в 1-2 недели | Перелогиниться на площадках (cookies протухают) | `./run.sh login` и т.д. |
> | При проблемах | Проверить, что робот запущен | `./run.sh stats` или `ps aux \| grep agent` |
> | При необходимости | Обновить резюме | Отредактировать `~/.job-hunter/resume.md` |

---

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| `playwright install` падает | `pip install playwright && playwright install --with-deps chromium` |
| Браузер не открывается при логине | Поставь `HEADLESS=0` в env-файле |
| Нейросеть не отвечает | Проверь доступность: `curl <LLM_BASE_URL>/models` |
| 0 вакансий | Проверь поисковые запросы в env; расширь фильтры |
| Робот не откликается | Cookies протухли — перелогинься на площадке |
| Telegram не присылает уведомления | Проверь `HUNTER_BOT_TOKEN` и `NOTIFY_CHAT_ID`; убедись, что нажал Start у бота |
| `./run.sh daemon` не работает после перезагрузки | Робот не запускается сам — добавь в crontab (см. Step 9) |

---

## Quick Reference

```bash
./run.sh dry-run          # поиск без откликов (тест)
./run.sh search           # поиск + авто-отклик
./run.sh daemon           # фоновый режим (24/7)
./run.sh stop             # остановить фоновый режим
./run.sh stats            # статистика
./run.sh digest           # дайджест в Telegram
./run.sh check            # проверить приглашения на hh.ru
./run.sh login            # перелогиниться на hh.ru
./run.sh analyze-resume   # анализ резюме нейросетью
```
