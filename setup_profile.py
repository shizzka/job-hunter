#!/usr/bin/env python3
"""
Интерактивный визард создания профиля Job Hunter.

Запуск:
    python setup_profile.py
    ./run.sh setup
"""
import os
import re
import sys

import config
import profile as profile_mod


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    answer = input(f"{prompt}{hint}: ").strip()
    return answer or default


def _ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "Д/н" if default else "д/Н"
    answer = input(f"{prompt} ({hint}): ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да", "1")


def _ask_list(prompt: str, default: list[str] | None = None) -> list[str]:
    print(f"{prompt}")
    if default:
        print(f"  По умолчанию: {', '.join(default)}")
    print("  Вводи по одному, пустая строка — конец:")
    items = []
    while True:
        item = input("  > ").strip()
        if not item:
            break
        items.append(item)
    return items or (default or [])


def _ask_resume() -> str:
    """Спросить резюме: вставить текст или указать путь к файлу."""
    print("Резюме (используется для оценки вакансий и генерации сопроводительных).")
    print("Вставь текст резюме или укажи путь к файлу (.txt, .md).")
    print("Можно пропустить (Enter) и добавить позже.")
    print()

    first_line = input("  Резюме (текст / путь к файлу / Enter — пропустить): ").strip()
    if not first_line:
        return ""

    # Если указан путь к файлу
    expanded = os.path.expanduser(first_line)
    if os.path.isfile(expanded):
        try:
            with open(expanded, encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                print(f"  Загружено из {expanded} ({len(content)} символов)")
                return content
        except Exception as e:
            print(f"  Не удалось прочитать файл: {e}")

    # Иначе — многострочный ввод
    print("  (Продолжай вводить текст, две пустые строки подряд — конец)")
    lines = [first_line]
    empty_count = 0
    while True:
        line = input("  ")
        if not line.strip():
            empty_count += 1
            if empty_count >= 2:
                break
            lines.append("")
        else:
            empty_count = 0
            lines.append(line)

    return "\n".join(lines).strip()


# Описания площадок для пользователя
_SOURCE_INFO = {
    "hh": {
        "name": "hh.ru",
        "has_resume": "Резюме заполняется на сайте вручную (форма hh.ru).",
        "no_resume": "Без резюме — вакансии будут приходить на ручной разбор.",
    },
    "superjob": {
        "name": "SuperJob",
        "has_resume": "Резюме заполняется на сайте SuperJob.",
        "no_resume": "Без резюме — вакансии будут приходить на ручной разбор.",
    },
    "habr": {
        "name": "Хабр Карьера",
        "has_resume": "Профиль заполняется на career.habr.com.",
        "no_resume": "Без профиля — вакансии будут приходить на ручной разбор.",
    },
    "geekjob": {
        "name": "GeekJob",
        "has_resume": "Резюме заполняется на geekjob.ru.",
        "no_resume": "Без резюме — вакансии будут приходить на ручной разбор.",
    },
}


def _ask_source(key: str) -> dict:
    """Спросить про одну площадку. Возвращает {enabled, has_account, has_resume}."""
    info = _SOURCE_INFO[key]
    name = info["name"]

    has_account = _ask_yn(f"  Есть аккаунт на {name}?", default=True)
    if not has_account:
        print(f"    → {name} отключён. Создай аккаунт на сайте и перезапусти setup.")
        return {"enabled": False, "has_account": False, "has_resume": False}

    has_resume = _ask_yn(f"  Есть заполненное резюме на {name}?", default=True)
    if has_resume:
        print(f"    → {name}: автопоиск + автоотклик")
    else:
        print(f"    → {name}: автопоиск, отклики — на ручной разбор")
        print(f"      {info['no_resume']}")

    return {"enabled": True, "has_account": has_account, "has_resume": has_resume}


def run_wizard():
    print()
    print("=" * 50)
    print("  Job Hunter — Настройка нового профиля")
    print("=" * 50)
    print()

    # 1. Имя
    while True:
        name = _ask("Имя профиля (латиница, без пробелов)")
        if not name:
            print("  Имя не может быть пустым.")
            continue
        if not re.match(r"^[a-zA-Z0-9_-]+$", name):
            print("  Допустимы: буквы, цифры, дефис, подчёркивание.")
            continue
        if name == "default":
            print("  'default' зарезервировано.")
            continue
        profiles_dir = os.path.join(config.JOB_HUNTER_HOME, "profiles", name)
        if os.path.exists(os.path.join(profiles_dir, "profile.env")):
            print(f"  Профиль '{name}' уже существует.")
            continue
        break

    print()

    # 2. Поисковые запросы
    print("Какую работу ищем?")
    queries = _ask_list(
        "Поисковые запросы (что вбивать в поиск на площадках):",
        default=["QA engineer", "тестировщик", "инженер по тестированию"],
    )
    print(f"  Запросы: {', '.join(queries)}")
    print()

    # 3. Резюме
    resume_text = _ask_resume()
    print()

    # 4. Площадки
    print("Площадки для поиска:")
    print("(Для автоотклика нужен аккаунт с заполненным резюме на площадке.)")
    print("(Без резюме — вакансии будут приходить на ручной разбор в Telegram.)")
    print()
    sources = {}
    for key in ("hh", "superjob", "habr", "geekjob"):
        sources[key] = _ask_source(key)
        print()

    enabled_any = any(s["enabled"] for s in sources.values())
    if not enabled_any:
        print("Ни одна площадка не включена. Нужен аккаунт хотя бы на одной.")
        print("Создай аккаунт и запусти ./run.sh setup заново.")
        return

    # 5. Telegram
    print("Telegram-уведомления (результаты поиска, приглашения, ручные отклики).")
    notify_chat = _ask("Chat ID (Enter — пропустить, настроить позже)")
    notify_token = ""
    if notify_chat:
        notify_token = _ask("Bot token")
    print()

    # 6. Лимиты
    max_per_source = _ask("Макс. автооткликов на площадку за прогон", "20")
    print()

    # 7. Создаём профиль
    print("Создаю профиль...")
    profile_dir = os.path.join(config.JOB_HUNTER_HOME, "profiles", name)
    os.makedirs(profile_dir, exist_ok=True)

    queries_str = "||".join(queries)
    if notify_chat:
        notify_lines = (
            f"NOTIFY_CHAT_ID={notify_chat}\n"
            f"HUNTER_BOT_TOKEN={notify_token}\n"
        )
    else:
        notify_lines = (
            "# NOTIFY_CHAT_ID=\n"
            "# HUNTER_BOT_TOKEN=\n"
        )

    env_content = (
        f"# Профиль: {name}\n"
        f"\n"
        f"HH_SEARCH_QUERIES={queries_str}\n"
        f"\n"
        f"HH_ENABLED={'1' if sources['hh']['enabled'] else '0'}\n"
        f"SUPERJOB_ENABLED={'1' if sources['superjob']['enabled'] else '0'}\n"
        f"HABR_ENABLED={'1' if sources['habr']['enabled'] else '0'}\n"
        f"GEEKJOB_ENABLED={'1' if sources['geekjob']['enabled'] else '0'}\n"
        f"\n"
        f"# Автоотклик (0 = только поиск, отклики вручную)\n"
        f"# HH всегда auto при наличии резюме\n"
        f"SUPERJOB_AUTO_APPLY={'1' if sources['superjob'].get('has_resume') else '0'}\n"
        f"HABR_AUTO_APPLY={'1' if sources['habr'].get('has_resume') else '0'}\n"
        f"GEEKJOB_AUTO_APPLY={'1' if sources['geekjob'].get('has_resume') else '0'}\n"
        f"\n"
        f"{notify_lines}"
        f"\n"
        f"MAX_AUTO_APPLICATIONS_PER_SOURCE={max_per_source}\n"
    )

    env_file = os.path.join(profile_dir, "profile.env")
    with open(env_file, "w") as f:
        f.write(env_content)

    # Сохраняем резюме
    resume_file = os.path.join(profile_dir, "resume.md")
    if resume_text:
        with open(resume_file, "w", encoding="utf-8") as f:
            f.write(resume_text)
        print(f"  Резюме сохранено ({len(resume_text)} символов)")

    p = profile_mod.load_profile(name)
    print(f"✅ Профиль '{name}' создан: {profile_dir}")
    print()

    # Анализ резюме через LLM
    if resume_text and config.LLM_API_KEY:
        if _ask_yn("Проанализировать резюме? (LLM даст рекомендации по улучшению)", default=True):
            print("Анализирую резюме (30-60 секунд)...")
            print()
            import asyncio
            import resume_analyzer
            analysis = asyncio.run(resume_analyzer.analyze_resume(resume_text))
            print(analysis)
            # Сохраняем анализ
            analysis_path = os.path.join(profile_dir, "resume_analysis.md")
            with open(analysis_path, "w", encoding="utf-8") as f:
                f.write(analysis)
            print(f"\n📄 Анализ сохранён: {analysis_path}")
            print()
    elif resume_text and not config.LLM_API_KEY:
        print("  LLM не настроен — анализ резюме пропущен.")
        print(f"  Позже: ./run.sh --profile {name} analyze-resume")
        print()

    # 8. Логин на площадках
    sources_to_login = []
    if sources["hh"]["enabled"]:
        sources_to_login.append(("hh.ru", "login"))
    if sources["habr"]["enabled"]:
        sources_to_login.append(("Хабр Карьера", "habr-login"))
    if sources["superjob"]["enabled"]:
        sources_to_login.append(("SuperJob", "superjob-login"))
    if sources["geekjob"]["enabled"]:
        sources_to_login.append(("GeekJob", "geekjob-login"))

    if sources_to_login:
        print("Логин на площадках.")
        print("Откроется браузер — войди в аккаунт, cookies сохранятся.")
        print()

        for label, cmd in sources_to_login:
            if _ask_yn(f"  Залогиниться на {label} сейчас?", default=True):
                print(f"  Открываю {label}...")
                ret = os.system(f'./run.sh --profile {name} {cmd}')
                if ret == 0:
                    print(f"  ✅ {label} — готово!")
                else:
                    print(f"  ⚠️  Можно повторить позже: ./run.sh --profile {name} {cmd}")
                print()
            else:
                print(f"  Позже: ./run.sh --profile {name} {cmd}")
                print()

    # 9. Сводка
    print()
    print("=" * 50)
    print(f"  Профиль '{name}' готов!")
    print("=" * 50)
    print()

    auto_sources = [
        _SOURCE_INFO[k]["name"]
        for k in ("hh", "superjob", "habr", "geekjob")
        if sources[k]["enabled"] and sources[k].get("has_resume")
    ]
    manual_sources = [
        _SOURCE_INFO[k]["name"]
        for k in ("hh", "superjob", "habr", "geekjob")
        if sources[k]["enabled"] and not sources[k].get("has_resume")
    ]

    if auto_sources:
        print(f"  Автоотклик: {', '.join(auto_sources)}")
    if manual_sources:
        print(f"  Ручной разбор: {', '.join(manual_sources)}")
    if not resume_text:
        print(f"  Резюме: не загружено (добавь позже в {profile_dir}/resume.md)")
    print()
    print("Запуск:")
    print(f"  ./run.sh --profile {name} dry-run   — пробный поиск")
    print(f"  ./run.sh --profile {name} search    — поиск + отклики")
    print(f"  ./run.sh --profile {name} daemon    — фоновый режим")
    print(f"  ./run.sh --profile {name} stats     — статистика")
    print()


if __name__ == "__main__":
    run_wizard()
