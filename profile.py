"""
Формализованная модель профиля пользователя (F-001).

Профиль содержит все настройки, специфичные для конкретного пользователя/персоны поиска:
- поисковые запросы и фильтры по каждой площадке
- идентификаторы резюме
- флаги авто-отклика
- пути к файлам состояния (seen, analytics, cookies)
- настройки уведомлений

Глобальные настройки (LLM, Playwright, таймеры, базовые URL) остаются в config.py.
"""
from __future__ import annotations

import atexit
import fcntl
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import config

log = logging.getLogger("profile")

_profiles_root_home = os.path.expanduser(getattr(config, "JOB_HUNTER_HOME", "~/.job-hunter"))


@dataclass
class SourceConfig:
    """Настройки одного источника вакансий."""
    enabled: bool = True
    auto_apply: bool = True
    cookies_file: str = ""


@dataclass
class HHConfig(SourceConfig):
    """Настройки hh.ru."""
    search_queries: list[str] = field(default_factory=list)
    search_profiles: list[dict] = field(default_factory=list)
    search_experience: str = ""
    search_salary: int = 0
    search_only_with_salary: bool = False
    search_pages: int = 3
    # Резюме
    primary_resume_title: str = ""
    primary_resume_id: str = ""
    secondary_resume_title: str = ""
    secondary_resume_id: str = ""
    tertiary_resume_title: str = ""
    tertiary_resume_id: str = ""
    resume_pipeline_enabled: bool = False
    resume_retry_delay_hours: int = 24
    resume_pipeline_file: str = ""


@dataclass
class SuperJobConfig(SourceConfig):
    """Настройки SuperJob."""
    search_queries: list[str] = field(default_factory=list)
    search_profiles: list[dict] = field(default_factory=list)
    search_pages: int = 3
    resume_id: int = 0
    api_key: str = ""
    client_id: int = 0
    auth_file: str = ""


@dataclass
class HabrConfig(SourceConfig):
    """Настройки Хабр Карьеры."""
    search_paths: list[str] = field(default_factory=list)
    search_pages: int = 3
    min_seconds_between_applications: int = 10


@dataclass
class GeekJobConfig(SourceConfig):
    """Настройки GeekJob."""
    search_pages: int = 5
    resume_id: str = ""


@dataclass
class NotifyConfig:
    """Настройки уведомлений."""
    chat_id: int = 0
    bot_token: str = ""
    proxy: str = ""


@dataclass
class Profile:
    """
    Профиль пользователя — все настройки, специфичные для одного поиска.

    Один экземпляр Profile = один «пользователь» или «персона поиска».
    В будущем (multi-tenant) каждый пользователь сервера получит свой Profile.
    """
    name: str = "default"
    home_dir: str = ""  # корневая директория состояния профиля

    # Лимиты
    max_applications_per_run: int = 0
    max_auto_applications_per_source: int = 20
    search_interval_min: int = 30
    invite_check_interval_min: int = 60

    # Резюме
    resume_file: str = ""

    # Источники
    hh: HHConfig = field(default_factory=HHConfig)
    superjob: SuperJobConfig = field(default_factory=SuperJobConfig)
    habr: HabrConfig = field(default_factory=HabrConfig)
    geekjob: GeekJobConfig = field(default_factory=GeekJobConfig)

    # Уведомления
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    # Файлы состояния (вычисляются из home_dir)
    seen_file: str = ""
    analytics_events_file: str = ""
    analytics_state_file: str = ""
    run_history_file: str = ""
    runtime_status_file: str = ""
    daemon_pid_file: str = ""
    telegram_bot_pid_file: str = ""
    telegram_bot_state_file: str = ""
    telegram_bot_runtime_file: str = ""
    log_file: str = ""
    error_log_file: str = ""
    telegram_bot_log_file: str = ""
    telegram_bot_debug_log_file: str = ""
    state_dir: str = ""

    def __post_init__(self):
        if not self.home_dir:
            self.home_dir = config.JOB_HUNTER_HOME
        self._resolve_state_paths()

    def _resolve_state_paths(self):
        """Вычислить пути файлов состояния из home_dir."""
        home = self.home_dir
        if not self.seen_file:
            self.seen_file = os.path.join(home, "seen_vacancies.json")
        if not self.analytics_events_file:
            self.analytics_events_file = os.path.join(home, "analytics_events.jsonl")
        if not self.analytics_state_file:
            self.analytics_state_file = os.path.join(home, "analytics_state.json")
        if not self.run_history_file:
            self.run_history_file = os.path.join(home, "run_history.jsonl")
        if not self.runtime_status_file:
            self.runtime_status_file = os.path.join(home, "runtime_status.json")
        if not self.daemon_pid_file:
            self.daemon_pid_file = os.path.join(home, "job-hunter-daemon.pid")
        if not self.telegram_bot_pid_file:
            self.telegram_bot_pid_file = os.path.join(home, "telegram-bot.pid")
        if not self.telegram_bot_state_file:
            self.telegram_bot_state_file = os.path.join(home, "telegram-bot-state.json")
        if not self.telegram_bot_runtime_file:
            self.telegram_bot_runtime_file = os.path.join(home, "telegram-bot-runtime.json")
        if not self.log_file:
            self.log_file = os.path.join(home, "job-hunter.log")
        if not self.error_log_file:
            self.error_log_file = os.path.join(home, "job-hunter-errors.log")
        if not self.telegram_bot_log_file:
            self.telegram_bot_log_file = os.path.join(home, "telegram-bot.log")
        if not self.telegram_bot_debug_log_file:
            self.telegram_bot_debug_log_file = os.path.join(home, "telegram-bot-debug.jsonl")
        if not self.state_dir:
            self.state_dir = os.path.join(home, "state")
        if not self.resume_file:
            self.resume_file = os.path.join(home, "resume.md")


_active_profile: Profile | None = None
_lock_fd: int | None = None


class ProfileLockedError(RuntimeError):
    """Профиль уже используется другим процессом."""


def active() -> Profile:
    """Вернуть текущий активный профиль. Если не активирован — default."""
    global _active_profile
    if _active_profile is None:
        _active_profile = load_default_profile()
    return _active_profile


def _profiles_root() -> str:
    """Корень каталога профилей, не зависящий от активированного именованного профиля."""
    global _profiles_root_home
    env_home = os.getenv("JOB_HUNTER_HOME", "").strip()
    if env_home:
        _profiles_root_home = os.path.expanduser(env_home)
        return _profiles_root_home

    current_home = os.path.expanduser(getattr(config, "JOB_HUNTER_HOME", "") or "~/.job-hunter")
    active_home = os.path.expanduser(getattr(_active_profile, "home_dir", "") or "")
    if not active_home or current_home != active_home:
        _profiles_root_home = current_home
    return _profiles_root_home


def activate(name: str = "default") -> Profile:
    """Активировать профиль с эксклюзивным lock-файлом."""
    return _activate(name, acquire_lock=True)


def _activate(name: str = "default", acquire_lock: bool = True) -> Profile:
    """
    Загрузить профиль и сделать его активным.

    Патчит config.* так, что все существующие модули (которые делают
    import config; config.HH_ENABLED) автоматически видят значения профиля.

    Захватывает lock-файл профиля — если профиль уже используется
    другим процессом, выбрасывает ProfileLockedError.
    """
    global _active_profile
    p = load_profile(name)
    if acquire_lock:
        _acquire_lock(p)
    else:
        _release_lock()
    _active_profile = p
    _patch_config(p)
    return p


def activate_no_lock(name: str = "default") -> Profile:
    """Активировать профиль без lock-файла для read-only/control процессов."""
    return _activate(name, acquire_lock=False)


def _lock_path(p: Profile) -> str:
    """Путь к lock-файлу профиля."""
    return os.path.join(p.home_dir, ".lock")


def _acquire_lock(p: Profile) -> None:
    """Захватить эксклюзивный lock на профиль (fcntl.LOCK_EX | LOCK_NB)."""
    global _lock_fd
    # Освободить предыдущий lock, если был
    _release_lock()

    lock_file = _lock_path(p)
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)

    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        raise ProfileLockedError(f"Не удалось создать lock-файл {lock_file}: {e}") from e

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ProfileLockedError(
            f"Профиль '{p.name}' уже используется другим процессом.\n"
            f"Lock: {lock_file}\n"
            f"Если процесс завис — удали файл вручную: rm {lock_file}"
        )

    # Записываем PID для диагностики
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())

    _lock_fd = fd
    atexit.register(_release_lock)
    log.debug("Acquired lock for profile '%s' (pid=%d)", p.name, os.getpid())


def _release_lock() -> None:
    """Освободить lock профиля."""
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


def _patch_config(p: Profile):
    """Записать значения профиля обратно в config.*, чтобы все модули их видели."""
    # State paths
    config.JOB_HUNTER_HOME = p.home_dir
    config.SEEN_VACANCIES_FILE = p.seen_file
    config.ANALYTICS_EVENTS_FILE = p.analytics_events_file
    config.ANALYTICS_STATE_FILE = p.analytics_state_file
    config.RUN_HISTORY_FILE = p.run_history_file
    config.RUNTIME_STATUS_FILE = p.runtime_status_file
    config.DAEMON_PID_FILE = p.daemon_pid_file
    config.TELEGRAM_BOT_PID_FILE = p.telegram_bot_pid_file
    config.TELEGRAM_BOT_STATE_FILE = p.telegram_bot_state_file
    config.TELEGRAM_BOT_RUNTIME_FILE = p.telegram_bot_runtime_file
    config.LOG_FILE = p.log_file
    config.ERROR_LOG_FILE = p.error_log_file
    config.TELEGRAM_BOT_LOG_FILE = p.telegram_bot_log_file
    config.TELEGRAM_BOT_DEBUG_LOG_FILE = p.telegram_bot_debug_log_file
    config.HH_STATE_DIR = p.state_dir
    config.RESUME_FILE = p.resume_file

    # Limits
    config.MAX_APPLICATIONS_PER_RUN = p.max_applications_per_run
    config.MAX_AUTO_APPLICATIONS_PER_SOURCE = p.max_auto_applications_per_source
    config.SEARCH_INTERVAL_MIN = p.search_interval_min
    config.INVITE_CHECK_INTERVAL_MIN = p.invite_check_interval_min

    # HH
    config.HH_ENABLED = p.hh.enabled
    config.HH_COOKIES_FILE = p.hh.cookies_file
    config.SEARCH_QUERIES = p.hh.search_queries
    config.SEARCH_PROFILES = p.hh.search_profiles
    config.SEARCH_EXPERIENCE = p.hh.search_experience
    config.SEARCH_SALARY = p.hh.search_salary
    config.SEARCH_ONLY_WITH_SALARY = p.hh.search_only_with_salary
    config.SEARCH_PAGES = p.hh.search_pages
    config.HH_PRIMARY_RESUME_TITLE = p.hh.primary_resume_title
    config.HH_PRIMARY_RESUME_ID = p.hh.primary_resume_id
    config.HH_SECONDARY_RESUME_TITLE = p.hh.secondary_resume_title
    config.HH_SECONDARY_RESUME_ID = p.hh.secondary_resume_id
    config.HH_TERTIARY_RESUME_TITLE = p.hh.tertiary_resume_title
    config.HH_TERTIARY_RESUME_ID = p.hh.tertiary_resume_id
    config.HH_RESUME_PIPELINE_ENABLED = p.hh.resume_pipeline_enabled
    config.HH_RESUME_RETRY_DELAY_HOURS = p.hh.resume_retry_delay_hours
    config.HH_RESUME_PIPELINE_FILE = p.hh.resume_pipeline_file

    # SuperJob
    config.SUPERJOB_ENABLED = p.superjob.enabled
    config.SUPERJOB_AUTO_APPLY = p.superjob.auto_apply
    config.SUPERJOB_COOKIES_FILE = p.superjob.cookies_file
    config.SUPERJOB_SEARCH_QUERIES = p.superjob.search_queries
    config.SUPERJOB_SEARCH_PROFILES = p.superjob.search_profiles
    config.SUPERJOB_SEARCH_PAGES = p.superjob.search_pages
    config.SUPERJOB_RESUME_ID = p.superjob.resume_id
    config.SUPERJOB_API_KEY = p.superjob.api_key
    config.SUPERJOB_CLIENT_ID = p.superjob.client_id
    config.SUPERJOB_AUTH_FILE = p.superjob.auth_file

    # Habr
    config.HABR_ENABLED = p.habr.enabled
    config.HABR_AUTO_APPLY = p.habr.auto_apply
    config.HABR_COOKIES_FILE = p.habr.cookies_file
    config.HABR_SEARCH_PATHS = p.habr.search_paths
    config.HABR_SEARCH_PAGES = p.habr.search_pages
    config.HABR_MIN_SECONDS_BETWEEN_APPLICATIONS = p.habr.min_seconds_between_applications

    # GeekJob
    config.GEEKJOB_ENABLED = p.geekjob.enabled
    config.GEEKJOB_AUTO_APPLY = p.geekjob.auto_apply
    config.GEEKJOB_COOKIES_FILE = p.geekjob.cookies_file
    config.GEEKJOB_SEARCH_PAGES = p.geekjob.search_pages
    config.GEEKJOB_RESUME_ID = p.geekjob.resume_id

    # Notifications
    config.NOTIFY_CHAT_ID = p.notify.chat_id
    config.TELEGRAM_BOT_TOKEN = p.notify.bot_token
    config.TELEGRAM_PROXY = p.notify.proxy


def load_default_profile() -> Profile:
    """
    Загрузить профиль из текущих переменных окружения (через config.py).

    Полная обратная совместимость: если нет многопрофильности,
    всё работает как раньше.
    """
    return Profile(
        name="default",
        home_dir=config.JOB_HUNTER_HOME,
        max_applications_per_run=config.MAX_APPLICATIONS_PER_RUN,
        max_auto_applications_per_source=config.MAX_AUTO_APPLICATIONS_PER_SOURCE,
        search_interval_min=config.SEARCH_INTERVAL_MIN,
        invite_check_interval_min=config.INVITE_CHECK_INTERVAL_MIN,
        resume_file=config.RESUME_FILE,
        log_file=config.LOG_FILE,
        error_log_file=config.ERROR_LOG_FILE,
        telegram_bot_log_file=config.TELEGRAM_BOT_LOG_FILE,
        telegram_bot_debug_log_file=config.TELEGRAM_BOT_DEBUG_LOG_FILE,
        hh=HHConfig(
            enabled=config.HH_ENABLED,
            auto_apply=True,  # hh всегда auto
            cookies_file=config.HH_COOKIES_FILE,
            search_queries=list(config.SEARCH_QUERIES),
            search_profiles=list(config.SEARCH_PROFILES),
            search_experience=config.SEARCH_EXPERIENCE,
            search_salary=config.SEARCH_SALARY,
            search_only_with_salary=config.SEARCH_ONLY_WITH_SALARY,
            search_pages=config.SEARCH_PAGES,
            primary_resume_title=config.HH_PRIMARY_RESUME_TITLE,
            primary_resume_id=config.HH_PRIMARY_RESUME_ID,
            secondary_resume_title=config.HH_SECONDARY_RESUME_TITLE,
            secondary_resume_id=config.HH_SECONDARY_RESUME_ID,
            tertiary_resume_title=config.HH_TERTIARY_RESUME_TITLE,
            tertiary_resume_id=config.HH_TERTIARY_RESUME_ID,
            resume_pipeline_enabled=config.HH_RESUME_PIPELINE_ENABLED,
            resume_retry_delay_hours=config.HH_RESUME_RETRY_DELAY_HOURS,
            resume_pipeline_file=config.HH_RESUME_PIPELINE_FILE,
        ),
        superjob=SuperJobConfig(
            enabled=config.SUPERJOB_ENABLED,
            auto_apply=config.SUPERJOB_AUTO_APPLY,
            cookies_file=config.SUPERJOB_COOKIES_FILE,
            search_queries=list(config.SUPERJOB_SEARCH_QUERIES),
            search_profiles=list(config.SUPERJOB_SEARCH_PROFILES),
            search_pages=config.SUPERJOB_SEARCH_PAGES,
            resume_id=config.SUPERJOB_RESUME_ID,
            api_key=config.SUPERJOB_API_KEY,
            client_id=config.SUPERJOB_CLIENT_ID,
            auth_file=config.SUPERJOB_AUTH_FILE,
        ),
        habr=HabrConfig(
            enabled=config.HABR_ENABLED,
            auto_apply=config.HABR_AUTO_APPLY,
            cookies_file=config.HABR_COOKIES_FILE,
            search_paths=list(config.HABR_SEARCH_PATHS),
            search_pages=config.HABR_SEARCH_PAGES,
            min_seconds_between_applications=config.HABR_MIN_SECONDS_BETWEEN_APPLICATIONS,
        ),
        geekjob=GeekJobConfig(
            enabled=config.GEEKJOB_ENABLED,
            auto_apply=config.GEEKJOB_AUTO_APPLY,
            cookies_file=config.GEEKJOB_COOKIES_FILE,
            search_pages=config.GEEKJOB_SEARCH_PAGES,
            resume_id=config.GEEKJOB_RESUME_ID,
        ),
        notify=NotifyConfig(
            chat_id=config.NOTIFY_CHAT_ID,
            bot_token=config.TELEGRAM_BOT_TOKEN,
            proxy=config.TELEGRAM_PROXY,
        ),
    )


def load_profile(name: str = "default") -> Profile:
    """
    Загрузить профиль по имени.

    - "default" → текущие env vars (обратная совместимость)
    - "<name>" → из ~/.job-hunter/profiles/<name>/profile.env

    В будущем: поддержка YAML, загрузка из базы данных (multi-tenant).
    """
    if name == "default":
        return load_default_profile()

    profiles_dir = os.path.join(_profiles_root(), "profiles", name)
    env_file = os.path.join(profiles_dir, "profile.env")

    if not os.path.isfile(env_file):
        raise FileNotFoundError(
            f"Профиль '{name}' не найден: {env_file}"
        )

    # Загружаем env-файл профиля поверх текущего окружения
    profile_env = _parse_env_file(env_file)

    # Строим профиль с home_dir = profiles/<name>/
    profile = load_default_profile()
    profile.name = name
    profile.home_dir = profiles_dir
    # Сбрасываем state-пути, чтобы _resolve_state_paths пересчитал из нового home_dir
    profile.seen_file = ""
    profile.analytics_events_file = ""
    profile.analytics_state_file = ""
    profile.run_history_file = ""
    profile.runtime_status_file = ""
    profile.daemon_pid_file = ""
    profile.telegram_bot_pid_file = ""
    profile.telegram_bot_state_file = ""
    profile.telegram_bot_runtime_file = ""
    profile.log_file = ""
    profile.error_log_file = ""
    profile.telegram_bot_log_file = ""
    profile.telegram_bot_debug_log_file = ""
    profile.state_dir = ""
    profile.resume_file = ""
    profile._resolve_state_paths()

    # Переопределяем cookies-файлы на профильную директорию
    profile.hh.cookies_file = os.path.join(profiles_dir, "hh_cookies.json")
    profile.superjob.cookies_file = os.path.join(profiles_dir, "superjob_cookies.json")
    profile.superjob.auth_file = os.path.join(profiles_dir, "superjob_auth.json")
    profile.habr.cookies_file = os.path.join(profiles_dir, "habr_cookies.json")
    profile.geekjob.cookies_file = os.path.join(profiles_dir, "geekjob_cookies.json")
    profile.hh.resume_pipeline_file = os.path.join(profiles_dir, "hh_resume_pipeline.json")

    # Применяем переопределения из env-файла
    _apply_env_overrides(profile, profile_env)

    return profile


def profile_env_path(name: str) -> str:
    """Абсолютный путь к env-файлу именованного профиля."""
    if not name or name == "default":
        raise ValueError("Для default нет отдельного profile.env")
    return os.path.join(_profiles_root(), "profiles", name, "profile.env")


def create_profile(name: str, search_queries: list[str] | None = None) -> Profile:
    """
    Создать новый именованный профиль.

    Создаёт директорию ~/.job-hunter/profiles/<name>/ с шаблонным profile.env.
    Возвращает загруженный Profile.
    """
    if not name or name == "default":
        raise ValueError("Имя профиля не может быть пустым или 'default'")

    # Валидация имени: только буквы, цифры, дефис, подчёркивание
    import re
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise ValueError(
            f"Недопустимое имя профиля '{name}'. "
            "Допустимы: латинские буквы, цифры, дефис, подчёркивание."
        )

    profiles_dir = os.path.join(_profiles_root(), "profiles", name)
    env_file = os.path.join(profiles_dir, "profile.env")

    if os.path.isfile(env_file):
        raise FileExistsError(f"Профиль '{name}' уже существует: {profiles_dir}")

    os.makedirs(profiles_dir, exist_ok=True)

    queries = search_queries or ["QA engineer", "тестировщик"]
    queries_str = "||".join(queries)

    template = (
        f"# Профиль: {name}\n"
        f"# Создан автоматически. Отредактируй под себя.\n"
        f"\n"
        f"# Поисковые запросы hh.ru (разделитель ||)\n"
        f"HH_SEARCH_QUERIES={queries_str}\n"
        f"\n"
        f"# Источники (1=вкл, 0=выкл)\n"
        f"HH_ENABLED=1\n"
        f"SUPERJOB_ENABLED=1\n"
        f"HABR_ENABLED=1\n"
        f"GEEKJOB_ENABLED=1\n"
        f"\n"
        f"# Telegram уведомления (заполни для получения уведомлений)\n"
        f"# NOTIFY_CHAT_ID=\n"
        f"# HUNTER_BOT_TOKEN=\n"
        f"\n"
        f"# Лимиты\n"
        f"MAX_AUTO_APPLICATIONS_PER_SOURCE=20\n"
        f"# MAX_APPLICATIONS_PER_RUN=0\n"
        f"\n"
        f"# Расписание демона\n"
        f"SEARCH_INTERVAL_MIN=480\n"
        f"INVITE_CHECK_INTERVAL_MIN=480\n"
    )

    with open(env_file, "w") as f:
        f.write(template)

    log.info("Created profile '%s' at %s", name, profiles_dir)
    return load_profile(name)


def list_profiles() -> list[str]:
    """Список доступных профилей."""
    profiles = []
    profiles_dir = os.path.join(_profiles_root(), "profiles")
    if os.path.isdir(profiles_dir):
        for entry in sorted(os.listdir(profiles_dir)):
            env_file = os.path.join(profiles_dir, entry, "profile.env")
            if os.path.isfile(env_file):
                profiles.append(entry)
    if not profiles:
        return ["default"]
    ordered = sorted(profiles, key=lambda item: (item != "qa", item))
    ordered.append("default")
    return ordered


def update_profile_env(name: str, updates: dict[str, str | int]) -> str:
    """Обновить profile.env именованного профиля, сохраняя остальное содержимое."""
    env_file = profile_env_path(name)
    if not os.path.isfile(env_file):
        raise FileNotFoundError(f"Профиль '{name}' не найден: {env_file}")

    with open(env_file, encoding="utf-8") as f:
        lines = f.read().splitlines()

    key_to_index: dict[str, int] = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        key_to_index[key.strip()] = idx

    for key, value in updates.items():
        rendered = f"{key}={_normalize_env_value(value)}"
        if key in key_to_index:
            lines[key_to_index[key]] = rendered
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)

    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    return env_file


def _normalize_env_value(value: str | int | None) -> str:
    raw = "" if value is None else str(value)
    return re.sub(r"\s+", " ", raw).strip()


def _parse_env_file(path: str) -> dict[str, str]:
    """Парсить .env файл в словарь."""
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            env[key] = value
    return env


def _apply_env_overrides(profile: Profile, env: dict[str, str]):
    """Применить переопределения из env-словаря к профилю."""
    def _flag(key: str, default: bool = False) -> bool:
        val = env.get(key, "").strip().lower()
        if not val:
            return default
        return val not in {"", "0", "false", "no", "off"}

    def _int(key: str, default: int = 0) -> int:
        val = env.get(key, "").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    def _list(key: str, default: list[str] | None = None) -> list[str] | None:
        raw = env.get(key, "").strip()
        if not raw:
            return default
        values = []
        for line in raw.replace("\r", "\n").split("\n"):
            for item in line.split("||"):
                item = item.strip()
                if item:
                    values.append(item)
        return values or default

    # HH
    if "HH_ENABLED" in env:
        profile.hh.enabled = _flag("HH_ENABLED", profile.hh.enabled)
    queries = _list("HH_SEARCH_QUERIES")
    if queries is not None:
        profile.hh.search_queries = queries
    if "HH_SEARCH_PAGES" in env:
        profile.hh.search_pages = _int("HH_SEARCH_PAGES", profile.hh.search_pages)
    if "HH_PRIMARY_RESUME_ID" in env:
        profile.hh.primary_resume_id = env["HH_PRIMARY_RESUME_ID"].strip()
    if "HH_PRIMARY_RESUME_TITLE" in env:
        profile.hh.primary_resume_title = env["HH_PRIMARY_RESUME_TITLE"].strip()
    if "HH_SECONDARY_RESUME_ID" in env:
        profile.hh.secondary_resume_id = env["HH_SECONDARY_RESUME_ID"].strip()
    if "HH_SECONDARY_RESUME_TITLE" in env:
        profile.hh.secondary_resume_title = env["HH_SECONDARY_RESUME_TITLE"].strip()
    if "HH_TERTIARY_RESUME_ID" in env:
        profile.hh.tertiary_resume_id = env["HH_TERTIARY_RESUME_ID"].strip()
    if "HH_TERTIARY_RESUME_TITLE" in env:
        profile.hh.tertiary_resume_title = env["HH_TERTIARY_RESUME_TITLE"].strip()
    if "HH_RESUME_PIPELINE_ENABLED" in env:
        profile.hh.resume_pipeline_enabled = _flag("HH_RESUME_PIPELINE_ENABLED")

    # SuperJob
    if "SUPERJOB_ENABLED" in env:
        profile.superjob.enabled = _flag("SUPERJOB_ENABLED", profile.superjob.enabled)
    if "SUPERJOB_AUTO_APPLY" in env:
        profile.superjob.auto_apply = _flag("SUPERJOB_AUTO_APPLY", profile.superjob.auto_apply)
    sj_queries = _list("SUPERJOB_SEARCH_QUERIES")
    if sj_queries is not None:
        profile.superjob.search_queries = sj_queries
    if "SUPERJOB_SEARCH_PAGES" in env:
        profile.superjob.search_pages = _int("SUPERJOB_SEARCH_PAGES", profile.superjob.search_pages)
    if "SUPERJOB_RESUME_ID" in env:
        profile.superjob.resume_id = _int("SUPERJOB_RESUME_ID")
    if "SUPERJOB_API_KEY" in env:
        profile.superjob.api_key = env["SUPERJOB_API_KEY"].strip()

    # Habr
    if "HABR_ENABLED" in env:
        profile.habr.enabled = _flag("HABR_ENABLED", profile.habr.enabled)
    if "HABR_AUTO_APPLY" in env:
        profile.habr.auto_apply = _flag("HABR_AUTO_APPLY", profile.habr.auto_apply)
    habr_paths = _list("HABR_SEARCH_PATHS")
    if habr_paths is not None:
        profile.habr.search_paths = habr_paths
    if "HABR_SEARCH_PAGES" in env:
        profile.habr.search_pages = _int("HABR_SEARCH_PAGES", profile.habr.search_pages)

    # GeekJob
    if "GEEKJOB_ENABLED" in env:
        profile.geekjob.enabled = _flag("GEEKJOB_ENABLED", profile.geekjob.enabled)
    if "GEEKJOB_AUTO_APPLY" in env:
        profile.geekjob.auto_apply = _flag("GEEKJOB_AUTO_APPLY", profile.geekjob.auto_apply)
    if "GEEKJOB_SEARCH_PAGES" in env:
        profile.geekjob.search_pages = _int("GEEKJOB_SEARCH_PAGES", profile.geekjob.search_pages)
    if "GEEKJOB_RESUME_ID" in env:
        profile.geekjob.resume_id = env["GEEKJOB_RESUME_ID"].strip()

    # Уведомления
    if "NOTIFY_CHAT_ID" in env:
        profile.notify.chat_id = _int("NOTIFY_CHAT_ID")
    if "HUNTER_BOT_TOKEN" in env:
        profile.notify.bot_token = env["HUNTER_BOT_TOKEN"].strip()

    # Лимиты
    if "MAX_APPLICATIONS_PER_RUN" in env:
        profile.max_applications_per_run = _int("MAX_APPLICATIONS_PER_RUN")
    if "MAX_AUTO_APPLICATIONS_PER_SOURCE" in env:
        profile.max_auto_applications_per_source = _int("MAX_AUTO_APPLICATIONS_PER_SOURCE")
    if "SEARCH_INTERVAL_MIN" in env:
        profile.search_interval_min = _int("SEARCH_INTERVAL_MIN", profile.search_interval_min)
    if "INVITE_CHECK_INTERVAL_MIN" in env:
        profile.invite_check_interval_min = _int("INVITE_CHECK_INTERVAL_MIN", profile.invite_check_interval_min)
