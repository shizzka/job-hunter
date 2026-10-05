# Browser action boundary: root-cause remediation, 2026-10-05

Рабочая ветка: `audit-remediation-2026-10-04`. Исходный reviewed HEAD: `529d173ba5aef8ce8bea2454de1b1bdff5d1c487`.

Contract: [STABILIZATION_AUDIT_REMEDIATION_2026-10-04.md](STABILIZATION_AUDIT_REMEDIATION_2026-10-04.md).
Исходный independent report: `/home/q/job-hunter-final-rereview-529d173.md`; исполняемые evidence: `/home/q/job-hunter-final-rereview-529d173-evidence/README.md`.

Это один общий P1 package browser boundary и отдельный локальный P2 package notifier. A1/A3/A5/A7 не пересматриваются. Main, production state и версия не менялись. Tailoring не запускался. Live HH/Habr/SuperJob/Form/Telegram действий не было.

## Воспроизведение до изменений

Все независимые cases сначала запущены на исходном `529d173`: **25 failed / 25 passed**, 76.83s. Failures — assertions zero external action/no replay, а не setup failures. Полные исходные reproducer’ы сохранены неизменными вне checkout; рабочие копии и логи — `/home/q/job-hunter-browser-boundary-root-20261005-evidence/README.md` (исходные промежуточные runs также в `/tmp/jh-boundary-root-evidence`).

- `before.log`: исходные input-button, capture-order, concurrent chats, full-document navigation, foreign submitter, listener lifecycle и production notifier timeout failures.
- `regression-before.log`: добавленные repository regressions на исходном коде, также 25 failed / 25 passed.
- `p2-before.txt`: genuine timeout / HTTP failure / API `ok=False`, с proxy и без него — 6 failed. Sink получал HTTP до ошибки; alternative и proxy fallback создавали дополнительные requests.
- `extra-root-before.txt`: 3 failed / 1 passed на промежуточном code export. Readback вызывал `formdata` handlers; file содержимое менялось при идентичных metadata; missing own Forms approval мог брать чужой план.
- `dialog-before.txt`: 2 failed / 2 passed — guard отдельно от HH UI classifier неверно связывал dialog с вложенной native form. Public HH UI guard уже блокировал неизвестный dialog; его ограничения не расширялись.

Habr public ElementHandle при full-document navigation уже давал zero action до исправления. Finding о Locator подтверждён для generic guarded paths обоих adapters и полного SuperJob workflow; безопасный Habr caller не объявляется исходно unsafe. Локальный oversized-preview rejection также уже проходил на исходном HEAD: сохранён как подтверждённый safe path, без нового production fix.

## Общий invariant и реализация

`browser_action_boundary.py` содержит локальный commit helper, используемый существующими workflows. Source-specific resume, chat, Form и destination validators остаются в своих modules. Новый service/framework/database не создавался.

1. Подготовка не вызывает pointer/key/submit commands: только visible/stable/enabled wait, scroll и hit-target readback. Locator заранее закрепляется за исходным ElementHandle. Даже `click(trial=True)` оказался неподходящим: промежуточный Chromium regression доказал достижение earlier window capture handler. Trial click удалён.
2. В одном синхронном browser evaluation проверяются собственный nonce, исходный document/root/control, свежий source identity, exact approved values и successful controls; только затем вызывается сохранённый native `HTMLElement.click` или guarded `requestSubmit`. Между проверкой и вызовом page JS нет await/event-loop handoff.
3. Window trampoline и wrappers для native form submit entrypoints устанавливаются до site scripts через browser lifecycle bootstrap. Actual `event.submitter` проверяется повторно перед submit handlers; approved clicked control не подставляется вместо фактического submitter. Неавторизованный submit, вызванный click handler, блокируется, но сам состоявшийся click не превращается в zero receipt.
4. Каждое approval имеет собственный nonce, immutable payload snapshot и исходные DOM references. Approvals не лежат в mutable last-approval document globals. Forms callers обязаны передавать собственный approval ID; multipage plan принадлежит явному workflow owner. Nonce допускает один commit command. Concurrent actions не используют approval друг друга.
5. Pending attempt владеет removable CDP document-start fence, включённым через `Page.enable` и `Page.addScriptToEvaluateOnNewDocument`. Новый документ не наследует старое approval. Fence не позволяет pending raw Locator вызвать known external control на wrong/foreign document; закреплённый production handle не перенаправляется на новую кнопку. Terminal cleanup удаляет только собственную CDP регистрацию и browser binding, сохраняя другие pending attempts.
6. Successful controls определяются native FormData на **инертной копии полного документа** с live values/checks/selections/files. Это сохраняет `form="id"` association и duplicate keys, включая controls вне DOM root, но не вызывает page `formdata` handlers во время проверки. Actual submit handlers в regressions строят настоящий FormData оригинальной формы; сравниваются их entries и intercepted POST. Reserved resume/letter/vacancy keys и actual submitter проходят source validation. File controls дополнительно привязаны к исходным immutable File objects, поскольку metadata не доказывают идентичность bytes.
7. Только собственный `dispatched:false` receipt до вызова page code или ошибка подготовки без action command подтверждают zero action. Later `blocked && !admitted` больше не очищает durable claim. После начатого dispatch lost/foreign receipt, timeout или cancellation остаются uncertain и запрещают replay.
8. После terminal action binding/fence снимается в finally. Document-start trampoline сохраняет первоначальный порядок регистрации, но без активного scope пропускает unrelated events; новый attempt не получает поздний listener. Preview-only Form scope также освобождается после завершения preview.

## Finding → reproduction → fix → regression → commit

Файлы regressions: [independent](../tests/test_browser_boundary_independent.py), [commit boundary](../tests/test_browser_boundary_commit.py), [full workflows](../tests/test_browser_boundary_workflows.py), [notifier transport](../tests/test_browser_notifier_boundary.py).

| Finding | Reproducer / исходный failure | Fix | Behavioral regression | Commit |
| --- | --- | --- | --- | --- |
| P1 supported controls / R1 / A2 | `test_r1_native_input_button_late_mutation`: supported input-button отправлял late B; Locator replacement обходил binding | Общий owned commit применяется ко всем production clickable shapes; pinned control, complete associated successful controls | `test_every_hh_clickable_shape_checks_payload_before_handlers` (6 shapes), `test_r1_locator_replacement_custom_dispatch`, association/fallback cases, nested native-form cases | `6f05005` |
| P1 ordering / R2 / A4 | Earlier capture отправлял B раньше guard; stored approved workflow записывал retryable failure после POST | No-action preparation; synchronous pre-dispatch validation; document-start trampoline | `test_earlier_window_capture_cannot_dispatch_late_payload`, `test_stored_approved_chat_real_send` | `6f05005` |
| P1 ordering / R3 / A6 | Earlier capture отправлял late Form values после successful readback | Тот же commit invariant, immutable field/checkbox/root/FormData readback непосредственно перед dispatch | `test_saved_forms_full_native_workflow`, `test_document_start_scope_checks_implicit_actual_submit_before_page_handler`, existing R3 tests | `6f05005` |
| P1 isolation / R2 / A4 | A/B overlap на одном page давал два sends B; missing own Forms ID мог брать чужой plan | Per-attempt immutable nonce binding, single-use command, explicit own Forms ID/owner | `test_r2_parallel_native_sends_one_page`, `test_two_stored_chat_workflows_same_browser`, `test_forms_attempt_cannot_borrow_other_approval`, missing-ID и overlapping-command tests | `6f05005` |
| P1 navigation / R4 / A8 | Pending Locator отправлял на wrong-ID/foreign новом document, включая apply/final submit полного SuperJob | Pin original handle + owned document-start CDP fence; fresh current-document identity при commit | `test_r4_pending_click_document_navigation`, `test_r4_superjob_full_method_navigation` (оба stages/destinations), replacement-control matrix | `6f05005` |
| P1 actual submitter / R1,R4 / A2,A8 | Habr/SJ отправляли `vacancy_id=2` вместе с foreign submitter `vacancy_id=1`; HH foreign resume/letter также должны быть zero | Проверка actual submitter и complete successful payload до submit handlers, отказ при duplicate/reserved conflicting values | `test_r4_native_actual_submitter_identity`, `test_hh_foreign_actual_submitter_precedes_earlier_submit_handler`, inert-formdata/file-identity tests | `6f05005` |
| N1 false zero proof | Earlier handler уже выполнил POST, late listener выдавал zero, claim становился retryable | Zero только до page-code invocation; admitted/unknown result остаётся uncertain; provable no-command failure retryable | `test_n1_prior_capture_dispatch_is_not_zero`, `test_no_pointer_preparation_failure_is_proven_zero`, `tests/test_rereview_n1.py` | `6f05005` |
| P2 listener disarm/scoping | После terminal action unrelated submit блокировался всеми пятью workflows | Release only own nonce/fence в finally; trampoline вне scope пропускает events; убрать future-document fence | `test_completed_guard_disarms_unrelated_submit`, cancellation/error cleanup, `test_terminal_fence_is_removed_from_future_documents`, concurrent-fence cleanup | `6f05005` |
| P2 notifier timeout/False / N2 | Production sink получил POST до timeout/False; proxy fallback и alternative повторяли delivery | Один transport route, ack `ok=True` для каждого recipient; False/timeout propagates в notification uncertainty, без replay | `test_n2_native_notifier_actual_transport_timeout` (6 cases), partial photo/text False tests | `b82417c` |
| N2 local oversized preview | Исходные raw/escaped HTML × screenshot cases уже давали zero transport и позволяли short alternative | Existing local rejection сохранён; нового исправления этой части не требовалось | `test_n2_independent_local_reject_then_alternative` (4 cases), `tests/test_rereview_n2.py` | Existing baseline + P2 regression `b82417c` |

## Проверки

- Final HH/common Chromium targeted run: **85 passed**, 149.22s (`p1-actual-form-final.txt`).
- Owned browser/Form/native workflows и integration checks: **147 passed**, 134.22s (`p1-final-owned.txt`); дополнительный preview integration run — **82 passed**, 2.15s (`preview-integration-final.txt`). Два устаревших test doubles обновлены только для нового `approval_owner`; их assertions contacts/cached experience сохранены. Первый полный export дал 2411 passed / 2 failed (только эти сигнатуры; `full-before-test-double-adaptation.txt`); после адаптации повторный полный прогон завершился зелёным.
- P2 targeted: **116 passed**, 48.95s (`p2-targeted.txt`); partial-delivery checks: **13 passed** (`p2-partial-targeted.txt`).
- Final full offline suite / exact clean export: **2413 passed**, 496.21s, code HEAD `b82417ce3080f923a1735818c3b74125dcd329c6` (`final-clean-export-green.txt`). После этого меняется только этот отчёт; CI проверяет итоговый report HEAD..
- CI запускается для HEAD итогового отчёта через existing `offline-tests.yml` workflow_dispatch на audit branch. [Workflow runs этой ветки](https://github.com/shizzka/job-hunter/actions/workflows/offline-tests.yml?query=branch%3Aaudit-remediation-2026-10-04). Exact run, SHA и итог приведены в финальном сообщении после завершения CI.

Full suite command, в cwd чистого git export:

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 \
  HOME="$test_home" PYTHONDONTWRITEBYTECODE=1 \
  PLAYWRIGHT_BROWSERS_PATH=/home/q/.cache/ms-playwright \
  /home/q/job-hunter/venv/bin/python -B -m pytest -q -p no:cacheprovider \
  --basetemp "$test_temp"
```

Browser requests в Chromium перехватываются/fulfill/abort. Настоящие notifier HTTP regressions используют только один synthetic `127.0.0.1` sink, принудительно перенаправляя туда все requests; запускаются в отдельном isolated child. Existing `tests/conftest.py` запрет IP connections и runtime isolation не ослаблялись. Existing тесты, моделировавшие auto-wait через wrapper `click`, перенесены на реальный actionability wait; assertions handler/POST/uncertainty сохранены.

## Оставшиеся ограничения и STOP

- Проверен Linux / real offline Chromium. Live platform DOM/backend compatibility, другие OS/engines не сертифицировались.
- Commit использует native DOM click (`isTrusted=False`). Platform, требующая trusted pointer event, остаётся manual/fail-closed; unsafe pointer fallback не добавлен.
- Unsupported form-associated custom elements и неоднозначные roots/controls fail closed. Это не универсальная изоляция произвольного site JS/network: guard связывает действия существующих workflows с approved state.
- Lost receipt/timeout/cancellation после начатого command остаются uncertain, manual review, без automatic replay/reconcile. После прерванного guard setup/cleanup может потребоваться reset browser context.
- Habr ambiguous initial apply сохраняет existing conservative stop до второго external submit.

Рекомендация merge/freeze как v0.8.0 не выдаётся. Phase 3 не объявляется закрытой. Следующая фаза не начинается. После final offline suite, push и CI — STOP.
