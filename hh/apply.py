"""Helpers for the HH vacancy application flow."""

import os

from hh.text import compact_text, normalize_text


CLOSED_OR_ARCHIVED_HH_TEXT_MARKERS = (
    "вакансия в архиве",
    "вакансия находится в архиве",
    "вакансия уже в архиве",
    "вакансия перемещена в архив",
    "вакансия закрыта",
    "вакансия уже закрыта",
    "закрыта и не принимает отклики",
    "не принимает отклики",
    "прием откликов закрыт",
    "приём откликов закрыт",
    "отклики больше не принимаются",
    "вакансия неактивна",
    "страница вакансии удалена",
)
CLOSED_OR_ARCHIVED_HH_COMPACT_MARKERS = (
    '"archived":"true"',
    '"archived":true',
    "'archived':'true'",
    "'archived':true",
    "&quot;archived&quot;:&quot;true&quot;",
    "&quot;archived&quot;:true",
)


def has_archived_hh_state(value: str) -> bool:
    compact = compact_text(value)
    return any(marker in compact for marker in CLOSED_OR_ARCHIVED_HH_COMPACT_MARKERS)


def looks_like_closed_or_archived_hh(value: str) -> bool:
    if has_archived_hh_state(value):
        return True

    compact = compact_text(value)
    if "<html" in compact or "<template" in compact:
        return False

    text = normalize_text(value)
    return any(marker in text for marker in CLOSED_OR_ARCHIVED_HH_TEXT_MARKERS)


def looks_like_existing_hh_response(value: str) -> bool:
    text = normalize_text(value)
    return (
        "вы откликнулись" in text
        or "уже отклик" in text
        or "отклик другим резюме" in text
        or "откликнуться повторно" in text
    )


def looks_like_hh_apply_success(value: str) -> bool:
    text = normalize_text(value)
    return (
        looks_like_existing_hh_response(value)
        or "резюме доставлено" in text
        or "отклик отправлен" in text
        or "связаться с работодателем можно в чате" in text
    )


async def click_with_fallbacks(session, element, label: str, *, logger) -> bool:
    """Надёжный клик по элементу с fallback-стратегиями."""
    if not element:
        return False

    try:
        await element.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center'})"
        )
        await session._page.wait_for_timeout(300)
    except Exception:
        pass

    strategies = (
        ("normal", lambda: element.click(timeout=5000)),
        ("force", lambda: element.click(timeout=5000, force=True)),
        (
            "js",
            lambda: element.evaluate(
                "el => { el.scrollIntoView({block: 'center', inline: 'center'}); el.click(); }"
            ),
        ),
    )

    for strategy_name, action in strategies:
        try:
            logger.info("Clicking %s via %s strategy", label, strategy_name)
            await action()
            await session._page.wait_for_timeout(1000)
            return True
        except Exception as e:
            logger.warning("%s click via %s failed: %s", label, strategy_name, e)

    return False


async def has_existing_response_ui(session, *, looks_like_existing_response, logger) -> bool:
    """Проверить UI hh.ru на признак уже отправленного отклика."""
    selectors = (
        "[data-qa*='responded']",
        "[data-qa='already-responded-text']",
        "button:has-text('Вы откликнулись')",
        "a:has-text('Вы откликнулись')",
        "text='Вы откликнулись'",
        "button:has-text('Отклик другим резюме')",
        "a:has-text('Отклик другим резюме')",
        "button:has-text('Откликнуться повторно')",
        "a:has-text('Откликнуться повторно')",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if marker:
                return True
        body_text = await session._page_text(limit=8000)
        return looks_like_existing_response(body_text)
    except Exception as e:
        logger.debug("Existing response UI check failed: %s", e)
        return False


async def page_text(session, limit: int = 12000) -> str:
    try:
        return await session._page.evaluate(
            f"() => document.body.innerText.slice(0, {int(limit)})"
        )
    except Exception:
        return ""


async def page_closed_or_archived(session, *, looks_like_closed_or_archived, has_archived_state) -> bool:
    """Detect closed/archived hh vacancy pages before trying to respond."""
    body_text = await session._page_text(limit=20000)
    if looks_like_closed_or_archived(body_text):
        return True

    try:
        page_html = await session._page.content()
    except Exception:
        page_html = ""
    return has_archived_state(page_html)


async def apply_success_detected(session, *, looks_like_apply_success, logger) -> bool:
    selectors = (
        "[data-qa*='responded']",
        "[data-qa='already-responded-text']",
        "[data-qa='vacancy-response-success-standard-notification']",
        "[data-qa*='success-standard-notification']",
        "button:has-text('Вы откликнулись')",
        "a:has-text('Вы откликнулись')",
        "text='Вы откликнулись'",
        "text='Резюме доставлено'",
        "text='Отклик отправлен'",
        "text='Связаться с работодателем можно в чате'",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if marker:
                return True
    except Exception as e:
        logger.debug("Apply success selector check failed: %s", e)

    current_url = session._page.url or ""
    if "/negotiations" in current_url:
        return True

    page_text = await session._page_text(limit=12000)
    return looks_like_apply_success(page_text)


async def response_requires_questions(session, current_url: str = "", *, logger) -> bool:
    page_url = (current_url or session._page.url or "").lower()
    if "vacancy_response_question" in page_url:
        return True

    selectors = (
        "h1:has-text('Ответьте на вопросы')",
        "h2:has-text('Ответьте на вопросы')",
        "text='Ответьте на вопросы'",
        "text='Для отклика необходимо ответить на несколько вопросов работодателя'",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if marker:
                return True
    except Exception as exc:
        logger.debug("Question flow selector check failed: %s", exc)

    page_text = normalize_text(await session._page_text(limit=12000))
    return (
        "ответьте на вопросы" in page_text
        or "для отклика необходимо ответить на несколько вопросов работодателя" in page_text
    )


async def dismiss_magritte_dropdowns(session) -> None:
    popup_selectors = (
        "[data-magritte-drop-base-direction]",
        "[data-qa='drop-base']",
    )
    for _ in range(3):
        popup = None
        for selector in popup_selectors:
            popup = await session._page.query_selector(selector)
            if popup:
                break
        if popup is None:
            return

        try:
            await session._page.keyboard.press("Escape")
        except Exception:
            pass
        await session._page.wait_for_timeout(200)

        popup = None
        for selector in popup_selectors:
            popup = await session._page.query_selector(selector)
            if popup:
                break
        if popup is None:
            return

        try:
            await session._page.evaluate(
                "() => document.activeElement && typeof document.activeElement.blur === 'function' && document.activeElement.blur()"
            )
        except Exception:
            pass
        await session._page.wait_for_timeout(100)


async def expand_cover_letter_input(session) -> bool:
    selectors = (
        "[data-qa='add-cover-letter']",
        "button[data-qa='add-cover-letter']",
        "button:has-text('Добавить сопроводительное')",
        "button:has-text('Приложить письмо')",
        "button:has-text('Добавить письмо')",
    )
    for selector in selectors:
        try:
            button = await session._page.query_selector(selector)
        except Exception:
            continue
        if not button:
            continue
        if await session._click_with_fallbacks(button, f"cover_letter_toggle:{selector}"):
            await session._page.wait_for_timeout(500)
            return True
    return False


async def submit_response_form_via_dom(session, *, logger) -> bool:
    try:
        result = await session._page.evaluate(
            """() => {
                    const form = document.querySelector("form[name='vacancy_response']");
                    if (form && typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return true;
                    }
                    const button = document.querySelector("[data-qa='vacancy-response-submit-popup']");
                    if (button) {
                        button.click();
                        return true;
                    }
                    return false;
                }"""
        )
    except Exception as exc:
        logger.debug("DOM submit fallback failed: %s", exc)
        return False
    return bool(result)


async def save_debug_snapshot(session, prefix: str, *, state_dir: str) -> None:
    """Сохранить скриншот + HTML текущей страницы в state-dir (для отладки)."""
    try:
        debug_path = os.path.join(state_dir, f"{prefix}.png")
        debug_html = os.path.join(state_dir, f"{prefix}.html")
        await session._page.screenshot(path=debug_path)
        with open(debug_html, "w") as f:
            f.write(await session._page.content())
    except Exception:
        pass


async def detect_response_controls(session):
    """Обнаружить элементы формы отклика на текущей странице.

        Возвращает кортеж (current_url, response_header, questions_required,
        resume_select, letter_field, submit_btn). Используется в apply_to_vacancy
        чтобы определить состояние страницы после очередного шага.
        """
    current_url = session._page.url
    response_header = await session._page.query_selector(
        "h1:has-text('Отклик на вакансию'), "
        "h2:has-text('Отклик на вакансию')"
    )
    questions_required = await session._response_requires_questions(current_url)
    resume_select = await session._page.query_selector(
        "[data-qa='resume-select'], "
        "[data-qa*='resume-item'], "
        "[data-qa='vacancy-response-popup-form-resume']"
    )
    letter_field = await session._page.query_selector(
        "[data-qa='vacancy-response-popup-form-letter-input'], "
        "textarea[name='letter'], "
        "textarea[data-qa*='letter'], "
        ".vacancy-response-popup textarea, "
        "textarea"
    )
    submit_btn = await session._page.query_selector(
        "[data-qa='vacancy-response-submit-popup'], "
        "[data-qa='vacancy-response-letter-submit'], "
        "button[data-qa*='submit'], "
        "[data-qa='vacancy-response-link-top-again'], "
        "[data-qa='vacancy-response-link-bottom-again'], "
        "[data-qa='vacancy-response-link-top'], "
        "[data-qa='vacancy-response-link-bottom'], "
        "a[data-qa*='response-link']"
    )
    if not submit_btn:
        # Some hh flows collapse back to the vacancy page after resume selection
        # and expose only a link-style "Откликнуться" control.
        submit_btn = await session._page.query_selector(
            "button:has-text('Откликнуться'), "
            "button:has-text('Отправить'), "
            "a:has-text('Откликнуться'), "
            "a:has-text('Отправить')"
        )
    return (
        current_url,
        response_header,
        questions_required,
        resume_select,
        letter_field,
        submit_btn,
    )


async def fill_cover_letter_post_apply(session, cover_letter: str, *, logger):
    """Заполнить сопроводительное письмо на странице после успешного отклика."""
    try:
        letter_selectors = (
            "textarea[placeholder*='Сопроводительное']",
            "textarea[placeholder*='сопроводительное']",
            "textarea[placeholder*='Сообщение']",
            "textarea[placeholder*='сообщение']",
            "textarea[name='letter']",
            "textarea",
            "input[placeholder*='Сообщение']",
            "[contenteditable='true'][role='textbox']",
            "[contenteditable='true']",
        )
        send_selectors = (
            "button:has-text('Отправить')",
            "[data-qa*='send']",
            "[type='submit']",
        )

        surfaces = [session._page]
        page_frames = getattr(session._page, "frames", None)
        if page_frames:
            surfaces.extend(frame for frame in page_frames if frame is not session._page.main_frame)

        snippet = normalize_text(cover_letter[:120])
        await session._expand_cover_letter_input()
        for surface in surfaces:
            try:
                surface_text = await surface.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 12000) : ''"
                )
            except Exception:
                surface_text = ""
            if snippet and snippet in normalize_text(surface_text):
                logger.info("Cover letter already visible after apply; skipping duplicate send")
                return

            for selector in letter_selectors:
                try:
                    letter_field = await surface.query_selector(selector)
                except Exception:
                    continue
                if not letter_field:
                    continue

                try:
                    await letter_field.scroll_into_view_if_needed()
                except Exception:
                    pass

                try:
                    await letter_field.click()
                except Exception:
                    pass

                await session._page.wait_for_timeout(300)

                filled = False
                try:
                    await letter_field.fill("")
                    await letter_field.type(cover_letter, delay=20)
                    filled = True
                except Exception:
                    try:
                        await letter_field.evaluate(
                            """(el, value) => {
                                    el.focus();
                                    if ('value' in el) {
                                        el.value = '';
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.value = value;
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        return;
                                    }
                                    if (el.isContentEditable) {
                                        el.textContent = value;
                                        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
                                    }
                                }""",
                            cover_letter,
                        )
                        filled = True
                    except Exception:
                        filled = False

                if not filled:
                    continue

                await session._page.wait_for_timeout(500)

                sent = False
                for send_selector in send_selectors:
                    try:
                        send_btn = await surface.query_selector(send_selector)
                    except Exception:
                        continue
                    if not send_btn:
                        continue
                    try:
                        await send_btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    try:
                        await send_btn.click()
                        await session._page.wait_for_timeout(2000)
                        sent = True
                        break
                    except Exception:
                        continue

                if not sent:
                    try:
                        await letter_field.press("Enter")
                        await session._page.wait_for_timeout(2000)
                        sent = True
                    except Exception:
                        pass

                if sent:
                    logger.info("Cover letter sent after apply")
                    return

        logger.debug("No cover letter field found after apply")
    except Exception as e:
        logger.warning("Failed to fill cover letter post-apply: %s", e)


async def apply_to_vacancy(
    session,
    vacancy_url: str,
    cover_letter: str = "",
    response_url: str = "",
    preferred_resume_title: str = "",
    preferred_resume_id: str = "",
    vacancy_context: str = "",
    *,
    absolute_hh_url,
    anti_bot_message,
    logger,
) -> dict:
    """
        Откликнуться на вакансию.
        Возвращает {"ok": bool, "message": str}
        """
    vacancy_url = absolute_hh_url(vacancy_url)
    response_url = absolute_hh_url(response_url)

    save_debug_snapshot = session._save_debug_snapshot

    cover_letter_filled = False
    auto_answer_notes: list[str] = []
    auto_answer_question_answers: list[dict] = []

    async def finalize_success(
        message: str,
        *,
        already_applied: bool = False,
        notes: list[str] | None = None,
    ) -> dict:
        if cover_letter and not cover_letter_filled and not already_applied:
            await session._fill_cover_letter_post_apply(cover_letter)
        result = {"ok": True, "message": message}
        if already_applied:
            result["already_applied"] = True
        if notes:
            result["notes"] = notes
        if auto_answer_question_answers:
            result["question_answers"] = list(auto_answer_question_answers)
        return result

    detect_response_controls = session._detect_response_controls

    async def refetch_response_controls():
        return await session._detect_response_controls()

    async def refetch_letter_field():
        for _ in range(3):
            (
                _current_url,
                _response_header,
                _questions_required,
                _resume_select,
                fresh_letter_field,
                fresh_submit_btn,
            ) = await refetch_response_controls()
            if fresh_letter_field is not None:
                return fresh_letter_field, fresh_submit_btn
            await session._page.wait_for_timeout(400)
        return None, None

    async def select_preferred_resume() -> bool:
        title_norm = normalize_text(preferred_resume_title)
        id_norm = (preferred_resume_id or "").strip()

        if not resume_select and not title_norm and not id_norm:
            return True

        async def collect_resume_items():
            return await session._page.query_selector_all(
                "[data-magritte-select-option], "
                "[data-qa^='magritte-select-option-'], "
                "[data-qa*='resume-item'], "
                "[data-qa='vacancy-response-popup-form-resume'], "
                "[data-qa='resume-select'] [role='button'][tabindex='0'], "
                "[data-qa='resume-select'] [data-qa='cell'], "
                "[data-qa='resume-select'] label, "
                "label[data-qa='cell'], "
                "[data-qa='resume-title'], "
                "[data-qa='resume-detail'], "
                "[data-qa='cell-text-content']"
            )

        async def has_resume_choices() -> bool:
            resume_items = await collect_resume_items()
            seen_texts = set()
            meaningful = 0
            for item in resume_items:
                try:
                    text = normalize_text(await item.inner_text())
                except Exception:
                    continue
                if not text or text in seen_texts:
                    continue
                seen_texts.add(text)
                if len(text) >= 6:
                    meaningful += 1
                if meaningful >= 2:
                    return True
            return False

        async def expand_resume_picker():
            toggles = [
                "[data-qa='resume-select'] [role='button'][tabindex='0']",
                "[role='dialog'] [role='button'][tabindex='0']",
                "form[name='vacancy_response'] [role='button'][tabindex='0']",
                "[data-qa='vacancy-response-popup-form-resume']",
                "[data-qa='resume-select'] [data-qa='cell']",
                "[data-qa='resume-title']",
                "[data-qa='resume-detail']",
                "[data-qa='cell']",
            ]
            for selector in toggles:
                handle = await session._page.query_selector(selector)
                if not handle:
                    continue
                if await session._click_with_fallbacks(handle, f"resume_toggle:{selector}"):
                    await session._page.wait_for_timeout(1000)
                    if await has_resume_choices():
                        return True
            return False

        if resume_select:
            await session._click_with_fallbacks(resume_select, "resume_select")
            await session._page.wait_for_timeout(1000)

        resume_items = await collect_resume_items()

        if not resume_items and (title_norm or id_norm):
            page_text = normalize_text(
                await session._page.evaluate("() => document.body.innerText.slice(0, 4000)")
            )
            if (title_norm and title_norm in page_text) or (id_norm and id_norm in page_text):
                return True
            if not resume_select and (
                letter_field is not None
                or submit_btn is not None
                or "/applicant/vacancy_response" in current_url
            ):
                logger.info(
                    "Resume picker is absent in hh apply flow — assuming current resume is already selected"
                )
                return True
            return False

        if not title_norm and not id_norm:
            if resume_items:
                return await session._click_with_fallbacks(resume_items[0], "resume_item_default")
            return True

        async def find_matching_item(items):
            for item in items:
                try:
                    text = normalize_text(await item.inner_text())
                except Exception:
                    continue
                if not text:
                    continue
                if id_norm and id_norm in text:
                    return item
                if title_norm and (title_norm in text or text in title_norm):
                    return item
            return None

        best_item = await find_matching_item(resume_items)

        # Single-resume shortcut: если на странице ровно одно резюме
        # и пикер не раскрывается — считаем его выбранным
        if best_item is None and resume_items:
            unique_texts = set()
            for item in resume_items:
                try:
                    text = normalize_text(await item.inner_text())
                    if text and len(text) >= 6:
                        unique_texts.add(text)
                except Exception:
                    continue
            if len(unique_texts) <= 1:
                logger.info("Single resume on page — treating as selected")
                return True

        if best_item is None:
            expanded = await expand_resume_picker()
            if expanded:
                resume_items = await collect_resume_items()
                best_item = await find_matching_item(resume_items)

        if best_item is None:
            # Последняя попытка: проверить текст страницы
            page_text = normalize_text(
                await session._page.evaluate("() => document.body.innerText.slice(0, 6000)")
            )
            if title_norm and title_norm in page_text:
                logger.info("Resume title found in page text — treating as selected")
                return True
            return False

        return await session._click_with_fallbacks(best_item, "resume_item_preferred")

    try:
        await session._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.warning("Vacancy page nav issue: %s", e)

    await session._page.wait_for_timeout(3000)
    await save_debug_snapshot("debug_apply_page")

    anti_bot_kind = await session._detect_anti_bot_kind()
    if anti_bot_kind:
        logger.warning("hh.ru anti-bot (%s) encountered on vacancy page: %s", anti_bot_kind, session._page.url)
        if response_url and response_url != vacancy_url:
            logger.info("Retrying apply flow via direct response URL: %s", response_url)
            try:
                await session._page.goto(
                    response_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                logger.warning("Direct response page nav issue: %s", e)
            await session._page.wait_for_timeout(3000)
            await save_debug_snapshot("debug_apply_response_page")
    anti_bot_kind = await session._detect_anti_bot_kind()
    anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="vacancy_page")
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "на странице вакансии")
        session._remember_antibot_signal(anti_bot_kind, "vacancy_page", message)
        return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

    if await session._page_closed_or_archived():
        return {
            "ok": False,
            "message": "Вакансия закрыта или находится в архиве",
            "closed_or_archived": True,
        }

    wants_specific_resume = bool(preferred_resume_title or preferred_resume_id)
    if await session._has_existing_response_ui() and not wants_specific_resume:
        return await finalize_success("Уже откликались ранее", already_applied=True)

    # Ищем кнопку "Откликнуться" — собираем все data-qa для дебага
    apply_btn = await session._page.query_selector(
        "[data-qa='vacancy-response-link-top-again'], "
        "[data-qa='vacancy-response-link-bottom-again'], "
        "[data-qa='vacancy-response-link-top'], "
        "[data-qa='vacancy-response-link-bottom'], "
        "a[data-qa*='response-link'], "
        "button[data-qa*='vacancy-response']"
    )

    if not apply_btn:
        reapply_btn = await session._page.query_selector(
            "button:has-text('Отклик другим резюме'), "
            "a:has-text('Отклик другим резюме'), "
            "button:has-text('Откликнуться повторно'), "
            "a:has-text('Откликнуться повторно')"
        )
        if reapply_btn:
            if wants_specific_resume:
                logger.info("Found reapply button for preferred resume flow")
                apply_btn = reapply_btn
            else:
                return {"ok": True, "message": "Уже откликались ранее", "already_applied": True}

    if not apply_btn:
        # Попробуем найти по тексту
        apply_btn = await session._page.query_selector(
            "button:has-text('Откликнуться'), "
            "a:has-text('Откликнуться')"
        )

    if not apply_btn:
        # Возможно уже откликались. Но если задан preferred resume, продолжаем:
        # на hh повторный отклик может быть доступен отдельной кнопкой/формой.
        if await session._has_existing_response_ui() and not wants_specific_resume:
            return {"ok": True, "message": "Уже откликались ранее", "already_applied": True}

        (
            current_url,
            response_header,
            questions_required,
            resume_select,
            letter_field,
            submit_btn,
        ) = await detect_response_controls()
        direct_response_flow = (
            "/applicant/vacancy_response" in current_url
            or response_header is not None
            or resume_select is not None
            or letter_field is not None
            or submit_btn is not None
        )

        if questions_required:
            logger.info("Vacancy requires employer questions — trying auto-answer")
            auto_question_result = await session._try_auto_answer_questions(vacancy_context=vacancy_context)
            auto_answer_notes.extend(auto_question_result.get("notes") or [])
            auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
            if auto_question_result.get("ok"):
                return await finalize_success(
                    auto_question_result.get("message", "Отклик отправлен"),
                    notes=auto_answer_notes,
                )
            return {
                "ok": False,
                "message": auto_question_result.get(
                    "message",
                    "Требуются доп. вопросы работодателя — пропускаем",
                ),
                "notes": auto_answer_notes,
                "question_answers": list(auto_answer_question_answers),
                "risky_question": auto_question_result.get("risky_question", ""),
            }

        if direct_response_flow:
            logger.info("Direct response flow detected without initial vacancy button")
        else:
            # Дебаг: какие data-qa есть на странице
            qa_attrs = await session._page.evaluate(
                "() => [...document.querySelectorAll('[data-qa]')].map(el => el.getAttribute('data-qa')).filter(a => a.includes('response') || a.includes('vacanc')).slice(0, 20)"
            )
            logger.warning("Apply button not found. Relevant data-qa: %s", qa_attrs)
            return {"ok": False, "message": f"Кнопка не найдена. qa={qa_attrs[:5]}"}
    else:
        direct_response_flow = False

    if not direct_response_flow:
        logger.info("Found apply button, clicking...")
        await apply_btn.scroll_into_view_if_needed()
        await session._page.wait_for_timeout(300)
        await apply_btn.click()
        await session._page.wait_for_timeout(3000)
        await save_debug_snapshot("debug_apply_after_click")

    if await session._apply_success_detected():
        return await finalize_success("Отклик отправлен")

    (
        current_url,
        response_header,
        questions_required,
        resume_select,
        letter_field,
        submit_btn,
    ) = await detect_response_controls()

    if questions_required:
        logger.info("Vacancy requires employer questions — trying auto-answer")
        auto_question_result = await session._try_auto_answer_questions(vacancy_context=vacancy_context)
        auto_answer_notes.extend(auto_question_result.get("notes") or [])
        auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
        if auto_question_result.get("ok"):
            return await finalize_success(
                auto_question_result.get("message", "Отклик отправлен"),
                notes=auto_answer_notes,
            )
        return {
            "ok": False,
            "message": auto_question_result.get(
                "message",
                "Требуются доп. вопросы работодателя — пропускаем",
            ),
            "notes": auto_answer_notes,
            "question_answers": list(auto_answer_question_answers),
                "risky_question": auto_question_result.get("risky_question", ""),
        }

    if resume_select or preferred_resume_title or preferred_resume_id:
        logger.info(
            "Selecting resume in hh apply flow (title=%r, id=%r)",
            preferred_resume_title,
            preferred_resume_id,
        )
        selected = await select_preferred_resume()
        if not selected:
            return {"ok": False, "message": "Не удалось выбрать нужное резюме"}
        await session._page.wait_for_timeout(1000)
        await session._dismiss_magritte_dropdowns()
        (
            current_url,
            response_header,
            questions_required,
            resume_select,
            letter_field,
            submit_btn,
        ) = await refetch_response_controls()

    if not letter_field and cover_letter:
        await session._expand_cover_letter_input()
        letter_field, refreshed_submit_btn = await refetch_letter_field()
        if refreshed_submit_btn is not None:
            submit_btn = refreshed_submit_btn

    if not letter_field:
        (
            _,
            _,
            _,
            _,
            letter_field,
            submit_btn,
        ) = await detect_response_controls()

    if letter_field and cover_letter:
        logger.info("Filling cover letter...")
        for attempt in range(2):
            try:
                await letter_field.scroll_into_view_if_needed()
                await session._page.wait_for_timeout(300)
                await letter_field.fill("")
                await letter_field.type(cover_letter, delay=20)
                break
            except Exception as e:
                if "not attached to the DOM" not in str(e):
                    raise
                logger.warning("Cover letter field detached from DOM, refetching controls (attempt %d)", attempt + 1)
                letter_field, refreshed_submit_btn = await refetch_letter_field()
                if refreshed_submit_btn is not None:
                    submit_btn = refreshed_submit_btn
                if not letter_field:
                    raise
        await session._page.wait_for_timeout(500)
        cover_letter_filled = True
        await session._dismiss_magritte_dropdowns()

    if submit_btn:
        (
            _,
            _,
            _,
            _,
            _,
            refreshed_submit_btn,
        ) = await refetch_response_controls()
        if refreshed_submit_btn is not None:
            submit_btn = refreshed_submit_btn
        await session._dismiss_magritte_dropdowns()
        clicked = await session._click_with_fallbacks(submit_btn, "submit_button")
        if not clicked:
            clicked = await session._submit_response_form_via_dom()
        if not clicked:
            return {"ok": False, "message": "Не удалось нажать кнопку подтверждения"}
        await session._page.wait_for_timeout(4000)

    await save_debug_snapshot("debug_apply_after_submit")

    anti_bot_kind = await session._detect_anti_bot_kind()
    anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="apply_submit")
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "после отклика")
        logger.warning("HH anti-bot (%s) appeared after apply submit", anti_bot_kind)
        session._remember_antibot_signal(anti_bot_kind, "apply_submit", message)
        return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

    if await session._response_requires_questions():
        logger.info("Vacancy requires employer questions after submit — trying auto-answer")
        auto_question_result = await session._try_auto_answer_questions(vacancy_context=vacancy_context)
        auto_answer_notes.extend(auto_question_result.get("notes") or [])
        auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
        if auto_question_result.get("ok"):
            return await finalize_success(
                auto_question_result.get("message", "Отклик отправлен"),
                notes=auto_answer_notes,
            )
        return {
            "ok": False,
            "message": auto_question_result.get(
                "message",
                "Требуются доп. вопросы работодателя — пропускаем",
            ),
            "notes": auto_answer_notes,
            "question_answers": list(auto_answer_question_answers),
                "risky_question": auto_question_result.get("risky_question", ""),
        }

    if await session._apply_success_detected():
        return await finalize_success("Отклик отправлен", notes=auto_answer_notes)

    (
        current_url,
        response_header,
        questions_required,
        _,
        _,
        submit_btn_retry,
    ) = await detect_response_controls()
    if response_header is not None and not questions_required and submit_btn_retry is not None:
        logger.info("Retrying hh submit after inconclusive response state")
        await session._dismiss_magritte_dropdowns()
        retried = await session._submit_response_form_via_dom()
        if retried:
            await session._page.wait_for_timeout(4000)
            anti_bot_kind = await session._detect_anti_bot_kind()
            anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="apply_submit_retry")
            if anti_bot_kind:
                message = anti_bot_message(anti_bot_kind, "после отклика")
                logger.warning("HH anti-bot (%s) appeared after DOM submit fallback", anti_bot_kind)
                session._remember_antibot_signal(anti_bot_kind, "apply_submit_retry", message)
                return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}
            if await session._response_requires_questions():
                logger.info("Vacancy requires employer questions after retry — trying auto-answer")
                auto_question_result = await session._try_auto_answer_questions(vacancy_context=vacancy_context)
                auto_answer_notes.extend(auto_question_result.get("notes") or [])
                auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
                if auto_question_result.get("ok"):
                    return await finalize_success(
                        auto_question_result.get("message", "Отклик отправлен"),
                        notes=auto_answer_notes,
                    )
                return {
                    "ok": False,
                    "message": auto_question_result.get(
                        "message",
                        "Требуются доп. вопросы работодателя — пропускаем",
                    ),
                    "notes": auto_answer_notes,
                    "question_answers": list(auto_answer_question_answers),
                    "risky_question": auto_question_result.get("risky_question", ""),
                }
            if await session._apply_success_detected():
                return await finalize_success("Отклик отправлен", notes=auto_answer_notes)

    page_text = await session._page_text(limit=20000)
    page_lower = page_text.lower()

    # Ошибка rate limit / блокировки
    if "слишком много" in page_lower or "too many" in page_lower:
        logger.warning("Rate limit detected after apply")
        message = anti_bot_message("rate_limit")
        session._remember_antibot_signal("rate_limit", "apply_verify", message)
        return {"ok": False, "message": message, "anti_bot_kind": "rate_limit"}

    # Ошибка на стороне hh
    if "что-то пошло не так" in page_lower or "произошла ошибка" in page_lower or "ошибка" in page_lower:
        logger.warning("hh.ru error page after apply. URL: %s", session._page.url)
        return {"ok": False, "message": "hh.ru показал ошибку после отклика"}

    logger.warning("Apply verification failed. URL: %s", session._page.url)
    await save_debug_snapshot("debug_apply_verification_failed")
    return {"ok": False, "message": "Не удалось подтвердить отклик"}
