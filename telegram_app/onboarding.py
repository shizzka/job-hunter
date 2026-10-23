"""First-run checklist «🚀 С чего начать»: where the user is and the next step, as buttons."""
from __future__ import annotations

CALLBACK_PREFIX = "gs:"
# Inline action → existing bot command (no new behaviour behind the buttons).
ACTIONS = {
    "auth": "/hh_auth",
    "resumes": "/hh_resumes",
    "new": "/direction_new",
    "dirs": "/directions",
    "confirm": "/search_application_confirm",
    "auto": "/search_application_auto",
    "search": "/search",
    "check": "/getting_started",
}
MODE_LABELS = {
    "confirm": "спрашивать меня — бот присылает вакансию и откликается только после вашего «да»",
    "preview": "только показ — бот присылает вакансии без кнопки отклика",
    "auto": "автоматически — бот откликается сам по подходящим вакансиям",
}


def build(*, hh_ok: bool, resumes: list[dict], directions: list[dict], active: dict | None,
          mode: str, has_search: bool, hh_message: str = "") -> tuple[str, list[list[tuple[str, str]]], bool]:
    """Return (text, inline rows [(label, action)], complete)."""
    def mark(ok):
        return "✅" if ok else "⬜️"

    resume_titles = [str(r.get("title") or "").strip() or "Резюме без названия" for r in resumes[:3]]
    mode_ok = mode in MODE_LABELS
    steps_ok = [hh_ok, bool(resumes), bool(active), mode_ok, has_search]
    lines = ["🚀 С чего начать", "",
             "Бот ищет вакансии на hh.ru и откликается вашими резюме. Пройдите шаги по порядку:", ""]
    rows: list[list[tuple[str, str]]] = []

    lines.append(f"{mark(hh_ok)} 1. Войти в hh.ru" + ("" if hh_ok else
                 " — " + (hh_message or "бот откроет вход, понадобится код из SMS/почты.")))
    if not hh_ok:
        rows.append([("🔐 Войти в hh.ru", "auth")])

    if resumes:
        shown = ", ".join(f"«{title}»" for title in resume_titles)
        remaining = len(resumes) - len(resume_titles)
        if remaining:
            shown += f" и ещё {remaining}"
        lines.append(f"✅ 2. Резюме на hh.ru: {len(resumes)} — {shown}")
    else:
        lines.append("⬜️ 2. Резюме на hh.ru — создайте их на сайте hh.ru (по одному на каждую профессию, "
                     "которую ищете), затем нажмите «Проверить резюме».")
        if hh_ok:
            rows.append([("🔄 Проверить резюме", "resumes")])

    if active:
        others = len(directions) - 1
        lines.append(f"✅ 3. Направления: {active.get('title', '')}" + (f" (и ещё {others})" if others > 0 else ""))
        rows.append([("🧭 Направления", "dirs")])
    else:
        lines.append("⬜️ 3. Направления — выберите резюме, бот сам предложит запросы и как вас представить; "
                     "вы проверите и сохраните.")
        if resumes:
            rows.append([("➕ Создать направление", "new")])

    scope = f" по направлению «{active.get('title', '')}»" if active else ""
    lines.append(f"{mark(mode_ok)} 4. Отклики{scope}: " + MODE_LABELS.get(mode, "не выбрано"))
    rows.append([(("✓ " if mode == value else "") + label, value)
                 for label, value in [("✋ Спрашивать меня перед откликом", "confirm"), ("🤖 Откликаться автоматически", "auto")]])

    ready = all(steps_ok[:4])
    lines.append(f"{mark(has_search)} 5. Первый поиск" + ("" if has_search else " — когда шаги 1–4 готовы."))
    if ready:
        if mode == "auto":
            lines.append("При поиске бот сразу откликнется на подходящие вакансии.")
        rows.append([("🔎 Найти вакансии сейчас", "search")])

    complete = all(steps_ok)
    lines += ["", "Всё готово! Дальше бот присылает вакансии сам. Этот экран открывается кнопкой «С чего начать»."
              if complete else "Нажмите кнопку под нужным шагом. Вернуться к списку — «🚀 С чего начать»."]
    rows.append([("🔄 Проверить шаги заново", "check")])
    return str("\n".join(lines)), rows, complete
