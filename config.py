"""Конфигурация Job Hunter агента."""
import os
import re


def _env_flag(name: str, default: str = "0") -> bool:
    value = os.getenv(name, default).strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default).strip())
    except (TypeError, ValueError):
        return int(default)


def _infer_superjob_client_id(secret_key: str) -> int:
    match = re.match(r"^v\d+\.[^.]+\.(\d+)\.", (secret_key or "").strip())
    return int(match.group(1)) if match else 0


JOB_HUNTER_HOME = os.path.expanduser(os.getenv("JOB_HUNTER_HOME", "~/.job-hunter"))


# ── hh.ru ────────────────────────────────────────────────────────────────
HH_ENABLED = _env_flag("HH_ENABLED", "1")
HH_BASE_URL = "https://hh.ru"
HH_COOKIES_FILE = os.path.join(JOB_HUNTER_HOME, "hh_cookies.json")
HH_STATE_DIR = os.path.join(JOB_HUNTER_HOME, "state")

# Поисковые запросы (каждый будет искаться отдельно)
SEARCH_QUERIES = [
    "тестировщик",
    "QA engineer",
    "QA тестировщик",
    "manual QA",
    "инженер по тестированию",
    "функциональное тестирование",
    "тестировщик веб приложений",
]

# Фильтры поиска
# Несколько наборов: (area, schedule)
# 2 = Санкт-Петербург (гибрид), 113 = вся Россия (удалёнка)
SEARCH_PROFILES = [
    {"area": 2,   "schedule": ""},         # СПб — любой формат (гибрид/офис/удалёнка)
    {"area": 113, "schedule": "remote"},   # Вся Россия — только удалёнка
    {"area": 9,   "schedule": "remote"},   # Беларусь — удалёнка
    {"area": 40,  "schedule": "remote"},   # Казахстан — удалёнка
    {"area": 97,  "schedule": "remote"},   # Узбекистан — удалёнка
]
SEARCH_EXPERIENCE = ""   # "" = любой, "noExperience", "between1And3", "between3And6", "moreThan6"
SEARCH_SALARY = 0        # минимальная зарплата (0 = не фильтровать)
SEARCH_ONLY_WITH_SALARY = False  # только с указанной зарплатой
SEARCH_PAGES = 3                 # сколько страниц листать (hh.ru даёт ~50 на страницу)

# Максимум автооткликов за один прогон по всем площадкам.
# 0 = без общего лимита.
MAX_APPLICATIONS_PER_RUN = _env_int("MAX_APPLICATIONS_PER_RUN", "0")

# Обычный лимит успешных автооткликов на одну площадку за прогон.
# Manual-разбор поверх этого лимита не режется.
# 0 = без лимита.
MAX_AUTO_APPLICATIONS_PER_SOURCE = _env_int(
    "MAX_AUTO_APPLICATIONS_PER_SOURCE",
    os.getenv("MAX_MATCHES_PER_SOURCE", "20"),
)

# Уже обработанные вакансии (хранятся в файле)
SEEN_VACANCIES_FILE = os.path.join(JOB_HUNTER_HOME, "seen_vacancies.json")
RUNTIME_STATUS_FILE = os.path.join(JOB_HUNTER_HOME, "runtime_status.json")

# ── LLM (для оценки релевантности и cover letter) ────────────────────────
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("JOB_HUNTER_LLM_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")

# ── SuperJob ───────────────────────────────────────────────────────────────
SUPERJOB_ENABLED = _env_flag("SUPERJOB_ENABLED", "1")
SUPERJOB_API_BASE_URL = "https://api.superjob.ru/2.0"
SUPERJOB_API_KEY = os.getenv("SUPERJOB_API_KEY", "")
SUPERJOB_CLIENT_ID = _env_int(
    "SUPERJOB_CLIENT_ID",
    str(_infer_superjob_client_id(SUPERJOB_API_KEY) or 0),
)
SUPERJOB_AUTH_FILE = os.path.join(JOB_HUNTER_HOME, "superjob_auth.json")
SUPERJOB_LOGIN_URL = "https://www.superjob.ru/auth/login/"
SUPERJOB_COOKIES_FILE = os.path.join(JOB_HUNTER_HOME, "superjob_cookies.json")
SUPERJOB_AUTO_APPLY = _env_flag("SUPERJOB_AUTO_APPLY", "1")
SUPERJOB_RESUME_ID = _env_int("SUPERJOB_RESUME_ID", "0")
SUPERJOB_COUNT_PER_PAGE = 100
SUPERJOB_SEARCH_PAGES = 3
# Для SuperJob держим более узкий набор запросов: широкие фразы вроде
# "тестировщик" дают слишком много нерелевантных админских вакансий.
SUPERJOB_SEARCH_QUERIES = [
    "QA",
    "qa engineer",
    "qa specialist",
    "quality engineer",
    "quality assurance engineer",
    "инженер по тестированию",
    "инженер по качеству",
    "тестировщик ПО",
]
SUPERJOB_REMOTE_COUNTRIES = [
    (1, "Россия"),
    (10, "Беларусь"),
    (11, "Молдова"),
    (12, "Грузия"),
    (13, "Армения"),
    (14, "Азербайджан"),
    (15, "Казахстан"),
    (16, "Узбекистан"),
    (17, "Таджикистан"),
    (18, "Кыргызстан"),
    (19, "Туркменистан"),
]
SUPERJOB_SEARCH_PROFILES = []
for country_id, country_label in SUPERJOB_REMOTE_COUNTRIES:
    SUPERJOB_SEARCH_PROFILES.append(
        {
            "countries": [country_id],
            "place_of_work": 2,  # 2 = на дому / удалённо
            "label": f"{country_label} удалёнка",
        }
    )
    SUPERJOB_SEARCH_PROFILES.append(
        {
            "countries": [country_id],
            "label": f"{country_label} любой формат",
        }
    )

# ── Хабр Карьера ───────────────────────────────────────────────────────────
HABR_ENABLED = _env_flag("HABR_ENABLED", "1")
HABR_CAREER_BASE_URL = "https://career.habr.com"
HABR_LOGIN_URL = f"{HABR_CAREER_BASE_URL}/users/auth/tmid"
HABR_COOKIES_FILE = os.path.join(JOB_HUNTER_HOME, "habr_cookies.json")
HABR_AUTO_APPLY = _env_flag("HABR_AUTO_APPLY", "1")
HABR_MIN_SECONDS_BETWEEN_APPLICATIONS = _env_int(
    "HABR_MIN_SECONDS_BETWEEN_APPLICATIONS",
    "10",
)
HABR_SEARCH_PAGES = 3
HABR_SEARCH_PATHS = [
    "/vacancies/testirovschik_qa/remote",
]

# ── GeekJob ────────────────────────────────────────────────────────────────
GEEKJOB_ENABLED = _env_flag("GEEKJOB_ENABLED", "1")
GEEKJOB_BASE_URL = "https://geekjob.ru"
GEEKJOB_SEARCH_PAGES = 5

# ── Резюме ────────────────────────────────────────────────────────────────
RESUME_FILE = os.path.join(JOB_HUNTER_HOME, "resume.md")

# ── AI Office ─────────────────────────────────────────────────────────────
OFFICE_URL = os.getenv("OFFICE_URL", "").rstrip("/")
OFFICE_DB = os.getenv("OFFICE_DB", "").strip()
AGENT_ID = os.getenv("AGENT_ID", "hunter").strip() or "hunter"

# ── Telegram ──────────────────────────────────────────────────────────────
# Уведомления полностью опциональны.
NOTIFY_CHAT_ID = _env_int("NOTIFY_CHAT_ID", os.getenv("OWNER_CHAT_ID", "0"))
TELEGRAM_BOT_TOKEN = os.getenv("HUNTER_BOT_TOKEN", "")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")

# ── Таймеры ───────────────────────────────────────────────────────────────
SEARCH_INTERVAL_MIN = 30      # поиск каждые N минут
INVITE_CHECK_INTERVAL_MIN = 60  # проверка приглашений каждые N минут

# ── Playwright ────────────────────────────────────────────────────────────
HEADLESS = _env_flag("HEADLESS", "1")
SLOW_MO = _env_int("SLOW_MO", "500")
BROWSER_PROXY = os.getenv("BROWSER_PROXY", os.getenv("HH_PROXY", "")).strip()
