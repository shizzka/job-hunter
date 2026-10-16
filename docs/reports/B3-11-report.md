# B3-11: доставка отклика отдельным серверным фактом

Работа выполнена в `/mnt/work/jh-b3-11`, ветка `b3-11-delivery`, база `7b1be24`. Обращений к hh.ru нет: браузерные проверки используют перехваченный транспорт и синтетические ответы. Полный сьют не запускался.

Причина живых `uncertain`: прежняя сверка безопасности требует ровно один application POST; второй malformed POST и unknown-действия исключают `_Verified`, хотя сервер уже содержит новый topic. Это требование безопасности сохранено. Доставка теперь устанавливается независимо от него.

Полная карта всех исходных чтений (И)/(Б), составленная до правки потребителей: [B3-11-consumer-map.md](B3-11-consumer-map.md). В ней указаны строки базового коммита; ниже — итоговые точки реализации.

| Место | Класс и изменение |
|---|---|
| `outcome.py:38` | Б: `apply_result_is_uncertain` сохранён. |
| `outcome.py:43`, `outcome.py:70` | И: исход `sent`, сообщение владельцу и статус письма выводятся из типизированного факта. |
| `apply_orchestrator.py:122` | И: после прежней проверки безопасности привязывает факт к клиенту и точной вакансии. Копия публичного `server_delivery` не считается доказательством. |
| `apply_orchestrator.py:151` | Б: прежнее тело нормализатора сохранено, изменено только имя. |
| `apply_orchestrator.py:362`, `apply_orchestrator.py:371`, `apply_orchestrator.py:393`, `apply_orchestrator.py:404` | И: трейс и application-result аналитики получают независимый исход доставки, включая исключения. |
| `analytics.py:384` | И: число отправленных определяется через нормализованный исход. |
| `agent.py:660`, `agent.py:1458` | Б + И: разделены локально. Queue/seen/pipeline остаются `uncertain`/`apply_uncertain`; аналитическое решение и счётчики при доставке — `applied_auto`/applied. |
| `agent.py:916`, `agent.py:1508`, `agent.py:3251` | И: отправка показывается через уведомление об отклике, ручная задача о непроверенной доставке не создаётся; CLI сообщает доставку при прежнем неуспешном exit status. |
| `telegram_bot_ui.py:1907` | И: результат manual AI apply показывает серверную доставку отдельно от остановленного выполнения команды. |
| `notifier.py:417`, `notifier.py:480` | И: без правки; вызывается существующее уведомление об отправке, остальные статусы идут по прежнему manual-уведомлению с диагностикой сервера. |
| `run_summary.py:14`, `analytics.py:351`, `reporting.py:33`, `telegram_bot_ui.py:1649` | И: без правки; получают исправленные решения/счётчики и исход `sent`. Старые события не переписываются. |
| `agent.py:926`, `hh/verification.py:89`, `hh/verification.py:368`, `hh/verification.py:451`, `hh/verification.py:464` | Б: receipt, `_owned_post`, продолжение сессии и прежний `confirmed_result` сохранены. |
| `state_store/native_apply.py:125`, `seen.py:53`, `manual_apply_queue.py:174`, `hh_resume_pipeline.py:460` | Б: без правки, терминальные статусы и право claim не меняются. |
| `analytics.py:1119`, `seen.py:83` | Совмещённая история без типизированного факта: backfill из одного старого seen-state остаётся `apply_uncertain`. Угадывать доставку по историческому dict небезопасно; этот путь не правился. |

`telegram_bot.py` прямых чтений этих полей не содержит; форматирование находится в `telegram_bot_ui.py`.

Выбран отдельный `hh/delivery.py`: его факт не должен давать полномочий `_Verified` или `_UncertainSession`. Замороженные `_Snapshot`/`_Delivery` находятся в `hh/delivery.py:25` и `hh/delivery.py:37`; единственный конструктор итогового факта — `_publish`, `hh/delivery.py:89`. На попытке хранится `_hh_server_delivery`. Единственное место вывода публичного `server_delivery` — `result_with_delivery`, `hh/delivery.py:69`. JSON-совместимый dict несёт непубличную типизированную ссылку; dict-merge/ручная подстановка публичного поля её не создают.

Снимок before снимается в `hh/apply.py:1304`. Синхронная фиксация допускается только при preparing этого owner, до acting/command_started и до любого зарегистрированного POST (`hh/delivery.py:131`). `NativeAttempt.begin()` синхронно переводит preparing в acting; между проверками снимка нет await. Дополнительно проверяется строгий порядок before.monotonic < первый одобренный POST и completed_POST < after.monotonic. Снимок привязан к объекту попытки, owner и vacancy id. Добавлено Unix wall time снимков, окно creationTime — ровно ±10 минут по заданию.

Одобренный request и его proof фиксируются в `hh/recovery.py:136`, начало POST — `hh/recovery.py:440`; завершение POST использует существующий requestfinished. `_post` (`hh/delivery.py:191`) проверяет реальный payload, canonical RESPONSE_PATH, owner, vacancy, hash и письмо без требования единственного элемента posts. Unknown-действия не обесценивают доказанный POST, но сохраняют прежние стоп-флаги.

Сервер читается в `hh/observation.py:289`: копия cookies, новый изолированный APIRequestContext, только GET вакансии на исходном региональном хосте, max_redirects=0/max_retries=0, без JS и браузерных действий. `hh/verification.py:306` запускает проверку в finally при любом выходе, в том числе reentry. Существующие after-снимки используются повторно; вместе с дополнительными чтениями лимит — три. Дополнительные чтения планируются сразу, около +1.5 и +3 секунд, с остановкой при первом confirmed либо unknown. Challenge/login/403/redirect/non-HTML/ошибка разбора прекращают новые чтения.

`hh/delivery.py:234` проверяет diff множеств, ровно один новый id, vacancy/type/time и hash, когда карта доступна. Пустой diff — только `not_seen`, никогда доказательство недоставки. Исключение проверки или её точки входа публикует unknown; исключение проекции оставляет прежний результат безопасности.

Карта `resumeId → hash`: B3-10 fixture `hh_negotiations_two.html` содержит явные числовые ключи `state.resumes`. У формы структура `applicantVacancyResponseStatuses[vacancyId].resumes` уже используется в `hh/submit_boundary.py:43`; fixture `hh_react_response_form.html` содержит `id` вместе с `hash`. Новый разбор читает только явный numeric id/key плюс hash (`hh/observation.py:74`), включая список объектов с id. Порядок элементов и `usedResumeIds` не устанавливают соответствие и не используются. Существующий preflight GET передаёт исходный HTML до sanitization (`hh/response_navigation.py:128`, `hh/delivery.py:171`). Не добавлены GET формы или чтения React/DOM. Если явной карты нет — resume_verified=null; если доступная карта не подтверждает hash — mismatch; сломанная/противоречивая карта — unknown. Санитизированные HTML-артефакты не использовались как источник соответствия.

`HH_SERVER_DELIVERY` (`hh/delivery.py:97`) содержит статус, причину, topic id, before/after/new ids, resume_verified, has_response_letter, число GET и post_to_confirmation_seconds. Последнее поле — монотонная задержка до первого подтверждающего снимка. Письма, cookies, заголовки, HTML и значения полей в событие не попадают.

Новые ветки не пишут в stop reasons, monitor/watch.unknown, `_hh_reconciliation_blocked`, `_hh_uncertain_session`, `_hh_verified_receipt` и не делают durable transitions. AST-проверка подтвердила неизменность старых функций безопасности, `_HHApplyReceipt` и тела нормализатора; `state_store/native_apply.py`, `seen.py`, `manual_apply_queue.py`, `hh_resume_pipeline.py` не имеют диффа. В обоих живых сценариях и офлайн Chromium-интеграции доставка считается отправленной, сессия останавливается, следующая HH-вакансия не отправляется, durable/queue uncertain и запрет claim сохраняются.

Полный дифф production-кода: [артефакт](../../.b3-11-production.log). Тестовые журналы и окончательные результаты перечислены ниже.

Проверки (stdout/stderr pytest полностью направлены в файлы):

- Расширенный набор: 56 файлов из [манифеста](../../.b3-11-gate-files.log), включая все 10 обязательных гейтов и тесты изменённых потребителей. Команда: `venv/bin/python -m pytest -q $(cat .b3-11-gate-files.log) > .b3-11-gates.log 2>&1`. Итог: `10 failed, 1139 passed in 655.75s (0:10:55)`. Все обязательные файлы гейтов без падений; полный сьют не выбран. [Полный журнал](../../.b3-11-gates.log).
- Доставка, включая сбой записи delivery-трейса: `42 passed in 3.22s`. [Журнал](../../.b3-11-delivery-tests.log).
- Исходы, оба живых id, Chromium до/после GET, manual queue, проекция при исключении, Telegram UI и тексты: `53 passed in 34.52s`. [Журнал](../../.b3-11-ui-delivery.log).
- Дополнительные регрессии uncertainty/manual/late-outcome/outcome: `208 passed in 6.35s`. [Журнал](../../.b3-11-outcome-tests.log).
- Начальная проверка наблюдения и доставки: `73 passed in 93.56s (0:01:33)`. [Журнал](../../.b3-11-stage1-tests.log).

Все 10 падений расширенного набора побитово совпадают по test id с падениями на исходном `7b1be24`. Базу проверяли через временный `git archive` внутри этого worktree; временная копия удалена. Это старые несовпадения fake DOM с действующим apply-путём; они не исправлялись в B3-11. Базовые результаты: `7 failed, 75 passed in 6.93s` ([журнал](../../.b3-11-baseline-tests.log)) и `3 failed, 36 passed in 13.88s` ([журнал](../../.b3-11-baseline-resume-tests.log)).

Точный список падений:

- `tests/test_audit_package1.py::test_hh_possible_click_never_replays_on_this_or_next_run[cancel]`
- `tests/test_audit_package1.py::test_hh_possible_click_never_replays_on_this_or_next_run[lost]`
- `tests/test_audit_package1.py::test_hh_possible_click_never_replays_on_this_or_next_run[wait_timeout]`
- `tests/test_closing_safety.py::test_inconclusive_native_apply_never_repeats_submit[letter]`
- `tests/test_closing_safety.py::test_inconclusive_native_apply_never_repeats_submit[required_question]`
- `tests/test_closing_safety.py::test_inconclusive_native_apply_never_repeats_submit[resume]`
- `tests/test_closing_safety.py::test_inconclusive_native_apply_never_repeats_submit[unchanged]`
- `tests/test_hh_resume_target_safety.py::test_full_apply_exact_target_and_fresh_guards[existing_topic]`
- `tests/test_hh_resume_target_safety.py::test_full_apply_exact_target_and_fresh_guards[failed_click_switch]`
- `tests/test_hh_resume_target_safety.py::test_full_apply_exact_target_and_fresh_guards[switch_to_target]`

Два коммита: `feat(hh): confirm server delivery from attempt-bound topic diffs` и `feat(outcome): report confirmed HH delivery without changing safety receipts`. Push не выполнялся. Итоговые SHA и `git status --short` приведены в финальном ответе.
