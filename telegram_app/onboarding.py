"""First-run checklist «🚀 С чего начать»: where the user is and the next step, as buttons."""
from __future__ import annotations

import html

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
    "confirm": "на подтверждение — бот присылает вакансию, отклик после вашей кнопки",
    "preview": "только показ — бот присылает вакансии без кнопки отклика",
    "auto": "автоматически — бот откликается сам по подходящим вакансиям",
}


def build(*, hh_ok: bool, resumes: list[dict], directions: list[dict], active: dict | None,
          mode: str, has_search: bool, hh_message: str = "") -> tuple[str, list[list[tuple[str, str]]], bool]:
    """Return (text, inline rows [(label, action)], complete)."""
    def mark(ok):
        return "✅" if ok else "⬜️"

    resume_titles = [str(r.get("title") or "").strip() for r in resumes if str(r.get("title") or "").strip()]
    steps_ok = [hh_ok, bool(resumes), bool(active), bool(mode), has_search]
    lines = ["🚀 С чего начать", "",
             "Бот ищет вакансии на hh.ru и откликается вашими резюме. Пройдите шаги по порядку:", ""]
    rows: list[list[tuple[str, str]]] = []

    lines.append(f"{mark(hh_ok)} 1. Войти в hh.ru" + ("" if hh_ok else
                 " — " + (hh_message or "бот откроет вход, понадобится код из SMS/почты.")))
    if not hh_ok:
        rows.append([("🔐 Войти в hh.ru", "auth")])

    if resumes:
        shown = ", ".join(resume_titles[:4]) + ("…" if len(resume_titles) > 4 else "")
        lines.append(f"✅ 2. Резюме на hh.ru: {len(resumes)} ({shown})")
    else:
        lines.append("⬜️ 2. Резюме на hh.ru — создайте их на сайте hh.ru (по одному на каждую профессию, "
                     "которую ищете), затем нажмите «Проверить резюме».")
        if hh_ok:
            rows.append([("🔄 Проверить резюме", "resumes")])

    if active:
        others = len(directions) - 1
        lines.append(f"✅ 3. Что ищу: {active.get('title', '')}" + (f" (и ещё {others})" if others > 0 else ""))
        rows.append([("🧭 Мои направления", "dirs")])
    else:
        lines.append("⬜️ 3. Что ищу — выберите резюме, бот сам предложит запросы и как вас представить; "
                     "вы проверите и сохраните.")
        if resumes:
            rows.append([("➕ Создать направление", "new")])

    lines.append(f"{mark(bool(mode))} 4. Как откликаться: " + MODE_LABELS.get(mode, "не выбрано"))
    rows.append([("✋ На подтверждение", "confirm"), ("🤖 Автоматически", "auto")])

    ready = all(steps_ok[:4])
    lines.append(f"{mark(has_search)} 5. Первый поиск" + ("" if has_search else " — когда шаги 1–4 готовы."))
    if ready:
        rows.append([("🔎 Найти вакансии", "search")])

    complete = all(steps_ok)
    lines += ["", "Всё готово! Дальше бот присылает вакансии сам; эта кнопка — всегда в главном меню."
              if complete else "Нажмите кнопку под нужным шагом. Вернуться к списку — «🚀 С чего начать»."]
    rows.append([("🔄 Обновить", "check")])
    return str("\n".join(lines)), rows, complete
