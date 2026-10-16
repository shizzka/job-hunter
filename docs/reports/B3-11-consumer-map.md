# B3-11: карта до правок потребителей

База: `7b1be24`, ветка `b3-11-delivery`. Поиск production Python по `uncertain`, `apply_result_is_uncertain`, `application_outcome`, `apply_uncertain`, `submission_status`. Это исходные строки; итоговые точки изменений перечисляются в отчёте.

И — сообщение владельцу, аналитика, письмо. Б — остановки, продолжение сессии, claim и durable/очередь/seen. Совмещённые участки разделяются локально: прежнее действие Б сохраняется, проекция И получает типизированный факт. Исторический `seen` (sticky `apply_uncertain`), manual queue и HH pipeline остаются Б; статусы не переписываются. Прочие адаптеры, чаты и неоднозначность ответов анкеты вне задачи. `telegram_bot.py` прямых совпадений не содержит; отображение находится в `telegram_bot_ui.py`.

| Исходная строка | Класс | Чтение / использование |
|---|---|---|
| `seen.py:53` | Б | `if (prior == "apply_uncertain" and action != prior) or (` |
| `seen.py:54` | Б | `prior == "manual_hh_guard_stop" and action not in {prior, "apply_uncertain"}` |
| `seen.py:119` | Б | `elif action.startswith("manual_") or action == "apply_uncertain":` |
| `superjob_client.py:198` | Б | `def _mark_auth_uncertain(self, owner):` |
| `superjob_client.py:200` | Б | `self._auth_session.uncertain(owner)` |
| `superjob_client.py:364` | Б | `self._mark_auth_uncertain(owner)` |
| `superjob_client.py:388` | Б | `self._mark_auth_uncertain(owner)` |
| `google_form_filler.py:535` | вне HH delivery | `# Before click => safe failure; from durable click boundary => uncertain.` |
| `google_form_filler.py:633` | вне HH delivery | `"message": "submitted" if ok else "submit clicked, verification uncertain" if submitted else (navigation_error or validation_message),` |
| `google_form_filler.py:645` | вне HH delivery | `uncertain = []` |
| `google_form_filler.py:653` | вне HH delivery | `uncertain.append(str(question.get("question") or f"поле {idx + 1}"))` |
| `google_form_filler.py:654` | вне HH delivery | `uncertain.extend(str(item.get("question") or f"поле {item.get('index', '')}") for item in skipped)` |
| `google_form_filler.py:655` | вне HH delivery | `uncertain = list(dict.fromkeys(item for item in uncertain if item))` |
| `google_form_filler.py:662` | вне HH delivery | `if uncertain:` |
| `google_form_filler.py:663` | вне HH delivery | `caption += "\n\n<b>Нужно проверить/уточнить:</b>\n" + "\n".join(f"• {html.escape(item[:180])}" for item in uncertain[:12])` |
| `google_form_filler.py:675` | вне HH delivery | `if uncertain:` |
| `google_form_filler.py:676` | вне HH delivery | `caption += "\n\n<b>Поля для ручной проверки:</b>\n" + "\n".join(f"• {html.escape(item[:180])}" for item in uncertain[:12])` |
| `google_form_filler.py:687` | вне HH delivery | `f"\nПолей: {len(questions)}; заполнено: {len(filled)}; уточнить: {len(uncertain)}.\n"` |
| `hh_client.py:177` | Б | `exc.hh_uncertain = bool(getattr(self, '_hh_recovery_uncertain', False)` |
| `hh_client.py:179` | Б | `or getattr(live, 'uncertain', False) or getattr(previous, 'uncertain', False))` |
| `hh_client.py:196` | Б | `self._hh_recovery_uncertain = True` |
| `hh_client.py:544` | Б | `self._hh_recovery_uncertain = True` |
| `hh_client.py:617` | Б | `self._hh_recovery_uncertain = True` |
| `telegram_bot_ui.py:1649` | И | `if fields["application_outcome"] == "uncertain" and run.get("ok") and not has_losses:` |
| `telegram_bot_ui.py:1651` | И | `icon = "⚠" if has_losses or fields["application_outcome"] == "uncertain" else (` |
| `telegram_bot_ui.py:1652` | И | `"✅" if fields["execution_status"] == "completed" and fields["application_outcome"] == "sent"` |
| `telegram_bot_ui.py:1761` | И | `if summary.get("application_outcome") in {"no_applications", "dry_run"}:` |
| `telegram_bot_ui.py:1762` | И | `states["success"] = "штатно; откликов нет" if summary["application_outcome"] == "no_applications" else "штатно; dry-run"` |
| `telegram_bot_ui.py:1763` | И | `if summary.get("application_outcome") == "uncertain" and summary.get("execution_status") == "completed":` |
| `telegram_bot_ui.py:1783` | И | `("failed", "❌ неудачных операций"), ("uncertain", "❔ требуют сверки")):` |
| `telegram_bot_ui.py:1791` | И | `problem = "❔ Требуется ручная сверка" if failure.get("reason_code") == "apply_uncertain" else "❌ Основная проблема"` |
| `telegram_bot_ui.py:1924` | И | `uncertain = label == "manual AI apply" and "Исход отклика не подтверждён" in stdout` |
| `telegram_bot_ui.py:1925` | И | `if uncertain:` |
| `geekjob_client.py:663` | Б | `return {'ok': False, 'submission_status': self._apply_repository.get(vacancy_url).get('status'),` |
| `geekjob_client.py:708` | Б | `'submission_status': 'failed' if response['error'] else 'completed'}` |
| `geekjob_client.py:710` | Б | `status = 'uncertain' if acting else 'failed'` |
| `geekjob_client.py:718` | Б | `return {'ok': False, 'submission_status': status, 'error_kind': type(exc).__name__,` |
| `state_store/superjob_auth.py:34` | Б | `or not attempt["id"] or attempt.get("status") not in ("active", "uncertain")` |
| `state_store/superjob_auth.py:113` | Б | `def uncertain(self, owner):` |
| `state_store/superjob_auth.py:119` | Б | `value["_token_attempt"]["status"] = "uncertain"` |
| `notifier.py:480` | И | `*, uncertain: bool = False,` |
| `notifier.py:489` | И | `heading = "❔ Исход отклика не подтверждён — нужна ручная сверка" if uncertain else "📝 Подходящая вакансия (нужен ручной отклик)"` |
| `notifier.py:490` | И | `link_label = "Открыть и проверить" if uncertain else "Открыть и откликнуться"` |
| `notifier.py:535` | И | `if bucket.get("uncertain"):` |
| `notifier.py:536` | И | `parts.append(f"исход не подтверждён {bucket['uncertain']}")` |
| `notifier.py:561` | И | `if bucket.get("uncertain"):` |
| `notifier.py:562` | И | `parts.append(f"исход не подтверждён {bucket['uncertain']}")` |
| `notifier.py:583` | И | `uncertain_total = sum((bucket or {}).get("uncertain", 0) for bucket in (source_stats or {}).values())` |
| `notifier.py:584` | И | `apply_label = "🧪 Dry-run matches" if dry_run else ("📨 Подтверждённых откликов" if uncertain_total else "📨 Откликов")` |
| `notifier.py:591` | И | `f"⏭ Не отправлено автоматически: {max(0, skipped - uncertain_total)}"` |
| `notifier.py:593` | И | `if uncertain_total:` |
| `notifier.py:594` | И | `text += f"\n⚠️ Исход отклика не подтверждён: {uncertain_total}; нужна ручная сверка, автоматического повтора нет."` |
| `notifier.py:680` | И | `if analytics_summary.get("uncertain"):` |
| `notifier.py:681` | И | `text += f"\nИсход отклика не подтверждён: {analytics_summary['uncertain']}; нужна ручная сверка."` |
| `notifier.py:729` | И | `# A failed/cancelled/uncertain send retains a short retry cooldown.` |
| `apply_orchestrator.py:21` | Б | `from outcome import application_outcome, apply_result_is_uncertain` |
| `apply_orchestrator.py:122` | Б | `def apply_result_with_current_uncertainty(vacancy: dict, hh_client, result: dict, *, owned_attempt=None) -> dict:` |
| `apply_orchestrator.py:126` | Б | `def uncertain():` |
| `apply_orchestrator.py:127` | Б | `stale_confirmation = ({'submission_status': 'uncertain'}` |
| `apply_orchestrator.py:128` | Б | `if result.get('submission_status') == 'success_confirmed' else {})` |
| `apply_orchestrator.py:129` | Б | `return {**result, **stale_confirmation, "ok": False, "uncertain": True,` |
| `apply_orchestrator.py:132` | Б | `if result.get('submission_status') == 'success_confirmed':` |
| `apply_orchestrator.py:133` | Б | `if hh_client is not None and result.get('ok') is True and not apply_result_is_uncertain(result):` |
| `apply_orchestrator.py:139` | Б | `return uncertain()` |
| `apply_orchestrator.py:140` | Б | `if apply_result_is_uncertain(result):` |
| `apply_orchestrator.py:141` | Б | `return uncertain()` |
| `apply_orchestrator.py:145` | Б | `return uncertain()` |
| `apply_orchestrator.py:149` | Б | `if getattr(hh_client, "_hh_recovery_uncertain", False):` |
| `apply_orchestrator.py:150` | Б | `return uncertain()` |
| `apply_orchestrator.py:156` | Б | `return uncertain()` |
| `apply_orchestrator.py:158` | Б | `return uncertain()` |
| `apply_orchestrator.py:169` | Б | `return uncertain()` |
| `apply_orchestrator.py:178` | Б | `return uncertain()` |
| `apply_orchestrator.py:184` | Б | `or active.repository.source != "hh" or not active.owner or active.uncertain` |
| `apply_orchestrator.py:196` | Б | `return uncertain()` |
| `apply_orchestrator.py:201` | Б | `return uncertain()` |
| `apply_orchestrator.py:204` | Б | `return uncertain()` |
| `apply_orchestrator.py:212` | Б | `return uncertain()` |
| `apply_orchestrator.py:218` | Б | `return uncertain()` |
| `apply_orchestrator.py:229` | Б | `return uncertain()` |
| `apply_orchestrator.py:234` | Б | `return uncertain()` |
| `apply_orchestrator.py:236` | Б | `return uncertain()` |
| `apply_orchestrator.py:240` | Б | `if attempt.uncertain:` |
| `apply_orchestrator.py:241` | Б | `return uncertain()` |
| `apply_orchestrator.py:244` | Б | `return uncertain()` |
| `apply_orchestrator.py:246` | Б | `return uncertain()` |
| `apply_orchestrator.py:249` | Б | `return uncertain()` |
| `apply_orchestrator.py:330` | Б | `result = apply_result_with_current_uncertainty(vacancy, hh_client, result, owned_attempt=owned_attempt)` |
| `apply_orchestrator.py:333` | И | `application_outcome=application_outcome(result),` |
| `apply_orchestrator.py:340` | Б | `result = apply_result_with_current_uncertainty(vacancy, hh_client, result, owned_attempt=owned_attempt)` |
| `apply_orchestrator.py:342` | И | `"outcome": "uncertain" if apply_result_is_uncertain(result) else "error",` |
| `apply_orchestrator.py:345` | И | `trace.update_application_outcome(application_outcome(result))` |
| `apply_orchestrator.py:346` | Б | `if apply_result_is_uncertain(result):` |
| `apply_orchestrator.py:351` | Б | `if apply_result_is_uncertain(result):` |
| `apply_orchestrator.py:352` | Б | `result = {**result, "ok": False, "uncertain": True}` |
| `apply_orchestrator.py:353` | Б | `result = apply_result_with_current_uncertainty(vacancy, hh_client, result, owned_attempt=owned_attempt)` |
| `apply_orchestrator.py:361` | Б | `result = apply_result_with_current_uncertainty(vacancy, hh_client, result, owned_attempt=owned_attempt)` |
| `apply_orchestrator.py:364` | И | `application_outcome=application_outcome(result),` |
| `apply_orchestrator.py:374` | Б | `result = apply_result_with_current_uncertainty(vacancy, hh_client, result, owned_attempt=owned_attempt)` |
| `apply_orchestrator.py:375` | И | `outcome = application_outcome(result)` |
| `apply_orchestrator.py:381` | И | `trace.update_application_outcome(outcome)` |
| `run_summary.py:28` | И | `"applied", "skipped", "deferred_unscored", "not_processed", "failed", "uncertain")}` |
| `run_summary.py:36` | И | `if decisions.get(key, {}).get("decision") != "apply_uncertain":` |
| `run_summary.py:39` | И | `uncertain_keys = {key for key, event in decisions.items() if event.get("decision") == "apply_uncertain"}` |
| `run_summary.py:40` | И | `recorded_uncertain = counters.get("uncertain") or 0` |
| `run_summary.py:41` | И | `uncertain = max(recorded_uncertain, data.get("uncertain") or 0,` |
| `run_summary.py:42` | И | `(data.get("reason_breakdown") or {}).get("apply_uncertain", 0), len(uncertain_keys))` |
| `run_summary.py:43` | И | `if uncertain:` |
| `run_summary.py:44` | И | `counters["uncertain"] = uncertain` |
| `run_summary.py:48` | И | `superseded &= uncertain_keys` |
| `run_summary.py:49` | И | `if superseded and (incomplete or recorded_uncertain < len(uncertain_keys)):` |
| `run_summary.py:65` | И | `# Older journals recorded the uncertainty decision but omitted its failure` |
| `run_summary.py:71` | И | `if key[0] != "hh" or event.get("decision") != "apply_uncertain":` |
| `run_summary.py:74` | И | `uuid.NAMESPACE_URL, f"job-hunter:{run_id}:{key[0]}:{key[1]}:apply_uncertain").hex` |
| `run_summary.py:77` | И | `failures.append({"failure_id": fid, "reason_code": "apply_uncertain",` |
| `run_summary.py:155` | И | `problems = sum(counters.get(key) or 0 for key in ("deferred_unscored", "failed", "uncertain"))` |
| `state_store/native_apply.py:41` | Б | `if status not in {'acting', 'completed', 'failed', 'uncertain'}:` |
| `state_store/native_apply.py:48` | Б | `if current == 'uncertain' and status == 'uncertain':` |
| `state_store/native_apply.py:50` | Б | `# Independent uncertainty can upgrade an owned terminal receipt;` |
| `state_store/native_apply.py:52` | Б | `if current not in {'preparing', 'acting'} and not (status == 'uncertain' and current in {'failed', 'completed'}):` |
| `state_store/native_apply.py:69` | Б | `self.uncertain = False` |
| `state_store/native_apply.py:81` | Б | `self.uncertain = True` |
| `state_store/native_apply.py:84` | Б | `self.uncertain = True` |
| `state_store/native_apply.py:90` | Б | `self.uncertain = True` |
| `state_store/native_apply.py:95` | Б | `self.uncertain = True` |
| `state_store/native_apply.py:96` | Б | `raise RuntimeError('Native browser action evidence is uncertain')` |
| `state_store/native_apply.py:105` | Б | `if self.acting or self.command_started or self.uncertain:` |
| `state_store/native_apply.py:106` | Б | `self.uncertain = True` |
| `state_store/native_apply.py:112` | Б | `self.uncertain = True` |
| `state_store/native_apply.py:125` | Б | `def _explicit_uncertain(result):` |
| `state_store/native_apply.py:126` | Б | `return bool(result.get('uncertain')) or result.get('submission_status') in {'uncertain', 'preparing', 'acting'}` |
| `state_store/native_apply.py:130` | Б | `"""Ownership/read/write failures preserve uncertainty and never escape as failure."""` |
| `state_store/native_apply.py:134` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:135` | Б | `return 'uncertain'` |
| `state_store/native_apply.py:136` | Б | `if state.get('status') == 'uncertain':` |
| `state_store/native_apply.py:137` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:138` | Б | `return 'uncertain'` |
| `state_store/native_apply.py:140` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:141` | Б | `status = 'uncertain'` |
| `state_store/native_apply.py:143` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:144` | Б | `status = 'uncertain'` |
| `state_store/native_apply.py:148` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:152` | Б | `attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')` |
| `state_store/native_apply.py:155` | Б | `return 'uncertain'` |
| `state_store/native_apply.py:159` | Б | `"""Persist uncertainty before dispatch; cancellation cannot erase ownership."""` |
| `state_store/native_apply.py:168` | Б | `return {'ok': False, 'uncertain': True, 'reason': 'native_client_busy',` |
| `state_store/native_apply.py:173` | Б | `return {'ok': False, 'uncertain': True, 'reason': 'native_ownership_unproven',` |
| `state_store/native_apply.py:176` | Б | `return {'ok': False, 'uncertain': True, 'reason': 'native_attempt_not_retryable',` |
| `state_store/native_apply.py:189` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:195` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:196` | Б | `if _explicit_uncertain(result):` |
| `state_store/native_apply.py:197` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:198` | Б | `if attempt.proven_no_action and not attempt.uncertain:` |
| `state_store/native_apply.py:201` | Б | `attempt.uncertain = True` |
| `state_store/native_apply.py:203` | Б | `result = {**result, 'ok': False, 'uncertain': False}` |
| `state_store/native_apply.py:204` | Б | `uncertain = attempt.uncertain or (attempt.acting and not attempt.proven_no_action and not result.get('ok'))` |
| `state_store/native_apply.py:205` | Б | `status = _finish_receipt(attempt, 'uncertain' if uncertain else 'completed' if result.get('ok') else 'failed')` |
| `state_store/native_apply.py:211` | Б | `status = _finish_receipt(attempt, 'uncertain')` |
| `state_store/native_apply.py:212` | Б | `if status == 'uncertain':` |
| `state_store/native_apply.py:213` | Б | `result = {**result, 'ok': False, 'uncertain': True}` |
| `state_store/native_apply.py:216` | Б | `uncertain = attempt.uncertain or (attempt.acting and not attempt.proven_no_action)` |
| `state_store/native_apply.py:217` | Б | `status = _finish_receipt(attempt, 'uncertain' if uncertain else 'failed')` |
| `state_store/native_apply.py:218` | Б | `if status == 'uncertain' and isinstance(exc, Exception):` |
| `state_store/native_apply.py:221` | Б | `return {'ok': False, 'uncertain': True, 'message': 'Результат отправки не подтверждён; нужна ручная сверка'}` |
| `debug_trace.py:199` | И | `self.application_outcome = "unknown"` |
| `debug_trace.py:318` | И | `def finish(self, *, ok: bool, message: str = "", failure_stage: str = "", application_outcome: str = "unknown") -> None:` |
| `debug_trace.py:327` | И | `application_outcome=application_outcome,` |
| `debug_trace.py:341` | И | `f"Application outcome: {application_outcome}",` |
| `debug_trace.py:371` | И | `self.application_outcome = application_outcome` |
| `debug_trace.py:380` | И | `"application_outcome": application_outcome,` |
| `debug_trace.py:387` | И | `def update_application_outcome(self, outcome: str) -> None:` |
| `debug_trace.py:388` | И | `"""Reflect late uncertainty observed while finishing diagnostic writes."""` |
| `debug_trace.py:389` | И | `if not self.finished or outcome == self.application_outcome:` |
| `debug_trace.py:392` | И | `previous = self.application_outcome` |
| `debug_trace.py:397` | И | `self.application_outcome = outcome` |
| `debug_trace.py:398` | И | `self.event("APPLICATION_OUTCOME_CORRECTED", application_outcome=outcome, previous_outcome=previous)` |
| `hh_chat_responder.py:1086` | вне HH delivery | `detail["notification_uncertain"] = True` |
| `hh_chat_responder.py:1087` | вне HH delivery | `repository.finish(chat_id, owner, "uncertain", remember)` |
| `hh_chat_responder.py:1107` | вне HH delivery | `detail.update(ok=False, message="send failed or uncertain; inspect state")` |
| `reporting.py:31` | И | `key: run[key] for key in ("execution_status", "application_outcome") if run.get(key)}}` |
| `reporting.py:33` | И | `outcome = {"sent": "отправлен", "uncertain": "не подтверждён; нужна ручная сверка",` |
| `reporting.py:36` | И | `f"Исход отклика: {outcome.get(fields['application_outcome'], 'неизвестен')}")` |
| `reporting.py:73` | И | `fields["application_outcome"] == "sent" and not losses and not run.get("stage_failures")` |
| `reporting.py:75` | И | `fields["application_outcome"] == "uncertain" else "ℹ️")` |
| `state_store/hh_resume_import.py:22` | Б | `or value.get("status") not in {"active", "publishing", "uncertain", "completed", "failed"}):` |
| `state_store/hh_resume_import.py:49` | Б | `if previous and previous.get("status") in ("active", "publishing", "uncertain"):` |
| `state_store/hh_resume_import.py:90` | Б | `self.control.save({"id": self.owner, "status": "uncertain" if self.publishing else "failed"},` |
| `state_store/chat_responder.py:60` | Б | `if item.get("status") not in {"preparing", "acting", "completed", "failed", "uncertain"}:` |
| `state_store/chat_responder.py:169` | Б | `(item["kind"] == "reply" and item["status"] == "uncertain")` |
| `state_store/chat_responder.py:173` | Б | `if previous and (previous["status"] == "uncertain" or` |
| `state_store/chat_responder.py:229` | Б | `if status not in {"completed", "failed", "uncertain"}:` |
| `state_store/chat_responder.py:238` | Б | `outcome = "uncertain" if status == "failed" and item["status"] == "acting" else status` |
| `state_store/chat_responder.py:262` | Б | `item["status"] = "uncertain" if item["status"] == "acting" else "failed"` |
| `outcome.py:16` | Б + И | `DECISION_APPLY_UNCERTAIN = "apply_uncertain"` |
| `outcome.py:38` | Б | `def apply_result_is_uncertain(result: dict) -> bool:` |
| `outcome.py:39` | Б | `"""Preserve the existing uncertain signals across native and GeekJob adapters."""` |
| `outcome.py:40` | Б | `return bool(result.get("uncertain")) or result.get("submission_status") in {"uncertain", "preparing", "acting"}` |
| `outcome.py:43` | И | `def application_outcome(result: dict) -> str:` |
| `outcome.py:45` | И | `return ("uncertain" if apply_result_is_uncertain(result) else` |
| `state_store/geekjob_apply.py:1` | Б | `"""Durable per-vacancy GeekJob submission owners; no automatic uncertain replay."""` |
| `state_store/geekjob_apply.py:16` | Б | `or item.get('status') not in {'preparing', 'acting', 'completed', 'failed', 'uncertain'}` |
| `state_store/geekjob_apply.py:43` | Б | `if previous.get('status') in {'preparing', 'acting', 'completed', 'uncertain'}:` |
| `state_store/geekjob_apply.py:53` | Б | `if status not in {'acting', 'completed', 'failed', 'uncertain'}:` |
| `state_store/hh_ui.py:25` | Б | `if not isinstance(item.get("status"), str) or item["status"] not in {"attempting", "sent", "failed", "uncertain"}:` |
| `state_store/hh_ui.py:121` | Б | `if status not in {"sent", "failed", "uncertain"}:` |
| `state_store/google_forms.py:21` | Б | `TERMINAL_STATUSES = frozenset({"submitted", "submit_uncertain", "already_submitted"})` |
| `state_store/google_forms.py:149` | Б | `if item.get("status") in {SUBMITTING_STATUS, "submit_uncertain"}:` |
| `google_forms/workflow.py:147` | вне HH delivery | `item['status'] = ('submitted' if detail.get('ok') else 'submit_uncertain'` |
| `hh/ui.py:195` | Б | `uncertain = bool(getattr(session, '_hh_recovery_uncertain', False)` |
| `hh/ui.py:198` | Б | `if not (reason or uncertain or getattr(session, '_recovering', False)):` |
| `hh/ui.py:201` | Б | `'possible_external_action' if uncertain else 'recovery_ownership_unavailable')` |
| `hh/ui.py:205` | Б | `self.blocked.hh_uncertain = bool(getattr(self.blocked, 'hh_uncertain', False) or uncertain)` |
| `hh/ui.py:268` | Б | `status = "uncertain"` |
| `hh/ui.py:288` | Б | `log.warning("HH UI warning completion uncertain: %s", type(exc).__name__)` |
| `hh/response_navigation.py:92` | Б | `session._hh_recovery_uncertain = True` |
| `hh_resume_pipeline.py:432` | Б | `if entry.get("completed_reason") == "apply_uncertain":` |
| `hh_resume_pipeline.py:460` | Б | `if entry.get("completed_reason") == "apply_uncertain" and reason != "apply_uncertain":` |
| `hh_resume_pipeline.py:632` | Б | `if entry.get("completed_reason") in {"apply_uncertain", "guard_stop", "manual_hh_guard_stop"}:` |
| `hh/verification.py:1` | Б | `"""Read-only HH reconciliation; browser uncertainty never authorizes a retry."""` |
| `hh/verification.py:115` | Б | `if state.get('owner') != attempt.owner or state.get('status') not in {'acting', 'uncertain', 'completed'}:` |
| `hh/verification.py:263` | Б | `# Verification failure preserves the original uncertain result.` |
| `hh/verification.py:317` | Б | `or getattr(session, '_hh_recovery_uncertain', False) or getattr(session, '_recovering', False)` |
| `hh/verification.py:334` | Б | `if active is not latest or latest.uncertain:` |
| `hh/verification.py:336` | Б | `elif state.get('status') == 'uncertain':` |
| `hh/verification.py:337` | Б | `later = getattr(latest, '_hh_uncertain_session', None)` |
| `hh/verification.py:348` | Б | `async def prepare_uncertain_continuation(session, attempt, result, resume_id, letter, *, trace=None):` |
| `hh/verification.py:417` | Б | `attempt._hh_uncertain_session = record` |
| `hh/verification.py:431` | Б | `def can_continue_uncertain(session, attempt):` |
| `hh/verification.py:432` | Б | `"""A typed session receipt never converts an uncertain outcome into success."""` |
| `hh/verification.py:434` | Б | `record = getattr(attempt, '_hh_uncertain_session', None)` |
| `hh/verification.py:438` | Б | `return (state.get('owner') == attempt.owner and state.get('status') == 'uncertain'` |
| `hh/verification.py:467` | Б | `allowed_stops = {'', 'native_action_or_uncertainty', 'native_attempt_already_acting'}` |
| `hh/verification.py:479` | Б | `# terminal receipt. Acting remains set and completed/uncertain durable` |
| `hh/verification.py:480` | Б | `# receipts stay non-retryable; never reopen a terminal uncertain owner.` |
| `hh/verification.py:484` | Б | `attempt.uncertain = False` |
| `hh/verification.py:486` | Б | `session._hh_recovery_uncertain = False` |
| `hh/verification.py:489` | Б | `return {**result, 'ok': True, 'uncertain': False, 'already_applied': False,` |
| `hh/verification.py:490` | Б | `'submission_status': 'success_confirmed', 'native_receipt_status': status,` |
| `hh/submit_boundary.py:172` | Б | `if (attempt is None or not attempt.acting or attempt.abandoned or attempt.uncertain` |
| `hh/submit_boundary.py:182` | Б | `raise RuntimeError('HH popup receipt ownership is uncertain')` |
| `manual_apply_queue.py:103` | Б | `if item.get("status") not in {"applying", "uncertain"} and created and now - created > MAX_AGE_SECONDS:` |
| `manual_apply_queue.py:112` | Б | `if index < MAX_ITEMS or item.get("status") in {"applying", "uncertain"}}` |
| `manual_apply_queue.py:174` | Б | `if item.get("external_started") or item.get("status") == "uncertain":` |
| `manual_apply_queue.py:240` | Б | `if status == "uncertain":` |
| `manual_apply_queue.py:241` | Б | `status_value = "uncertain"` |
| `manual_apply_queue.py:243` | Б | `status_value = status if status in {"applied", "already_applied"} else "uncertain"` |
| `hh/negotiations.py:50` | Б | `session._hh_recovery_uncertain = True` |
| `hh/vacancy_details.py:78` | Б | `def stop(reason, *, uncertain=False):` |
| `hh/vacancy_details.py:80` | Б | `session._hh_recovery_uncertain = bool(getattr(session, '_hh_recovery_uncertain', False) or uncertain)` |
| `hh/vacancy_details.py:114` | Б | `stop('vacancy_forbidden_ownership_unproven', uncertain=True)` |
| `hh/vacancy_details.py:117` | Б | `for key in ('uncertain', 'acting', 'command_started')) for attempt in retained):` |
| `hh/vacancy_details.py:118` | Б | `stop('possible_external_action', uncertain=True)` |
| `hh/vacancy_details.py:120` | Б | `or getattr(session, '_hh_recovery_uncertain', False)):` |
| `hh/vacancy_details.py:121` | Б | `stop('possible_external_action', uncertain=True)` |
| `hh/vacancy_details.py:128` | Б | `stop('vacancy_forbidden_browser_surface_unproven', uncertain=True)` |
| `hh/vacancy_details.py:131` | Б | `stop('cookie_session_ownership_unproven', uncertain=True)` |
| `hh/vacancy_details.py:260` | Б | `session._hh_recovery_uncertain = True` |
| `habr_career_client.py:380` | Б | `return {"ok": False, "uncertain": True,` |
| `habr_career_client.py:410` | Б | `return {"ok": False, "message": "Результат отправки не подтверждён", "uncertain": True}` |
| `habr_career_client.py:426` | Б | `return {"ok": False, "message": "Результат отправки не подтверждён", "uncertain": True}` |
| `hh/browser.py:177` | Б | `session._hh_recovery_uncertain = False` |
| `hh/search.py:20` | Б | `"""Only trusted GET redirects; action uncertainty is never cleared/retried."""` |
| `hh/search.py:45` | Б | `session._hh_recovery_uncertain = True` |
| `hh/recovery.py:99` | Б | `attempt.uncertain = True` |
| `hh/recovery.py:115` | Б | `self.session._hh_recovery_uncertain = True` |
| `hh/recovery.py:158` | Б | `self.session._hh_recovery_uncertain = True` |
| `hh/recovery.py:286` | Б | `attempt.uncertain = True` |
| `hh/recovery.py:290` | Б | `attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')` |
| `hh/recovery.py:516` | Б | `if state.get('owner') == attempt.owner and state.get('status') == 'completed' and not attempt.uncertain:` |
| `hh/recovery.py:524` | Б | `uncertain: bool = False` |
| `hh/recovery.py:528` | Б | `def __init__(self, reason, *, uncertain=False):` |
| `hh/recovery.py:529` | Б | `self.reason, self.uncertain = reason, uncertain` |
| `hh/recovery.py:627` | Б | `uncertain = False` |
| `hh/recovery.py:632` | Б | `raise RecoveryBlocked('browser_ownership_changed', uncertain=True)` |
| `hh/recovery.py:634` | Б | `raise RecoveryBlocked('recovery_previously_stopped', uncertain=True)` |
| `hh/recovery.py:638` | Б | `raise RecoveryBlocked('cookie_session_ownership_unproven', uncertain=True)` |
| `hh/recovery.py:640` | Б | `raise RecoveryBlocked('auth_request_context_ownership_unproven', uncertain=True)` |
| `hh/recovery.py:644` | Б | `raise RecoveryBlocked('action_monitor_unproven', uncertain=True)` |
| `hh/recovery.py:647` | Б | `raise RecoveryBlocked('blocked_service_worker_policy_unproven', uncertain=True)` |
| `hh/recovery.py:649` | Б | `raise RecoveryBlocked('possible_external_action', uncertain=True)` |
| `hh/recovery.py:653` | Б | `raise RecoveryBlocked('background_worker_ownership_unproven', uncertain=True)` |
| `hh/recovery.py:655` | Б | `raise RecoveryBlocked('service_worker_ownership_unproven', uncertain=True)` |
| `hh/recovery.py:657` | Б | `raise RecoveryBlocked('additional_page_ownership_unproven', uncertain=True)` |
| `hh/recovery.py:659` | Б | `raise RecoveryBlocked('action_boundary_owner_unproven', uncertain=True)` |
| `hh/recovery.py:661` | Б | `if attempt.acting or attempt.command_started or attempt.uncertain:` |
| `hh/recovery.py:662` | Б | `raise RecoveryBlocked('native_action_or_uncertainty', uncertain=True)` |
| `hh/recovery.py:666` | Б | `raise RecoveryBlocked('native_receipt_ownership_unproven', uncertain=True)` |
| `hh/recovery.py:672` | Б | `raise RecoveryBlocked('replacement_possible_external_action', uncertain=True)` |
| `hh/recovery.py:678` | Б | `if attempt.acting or attempt.command_started or attempt.uncertain:` |
| `hh/recovery.py:679` | Б | `raise RecoveryBlocked('native_action_or_uncertainty', uncertain=True)` |
| `hh/recovery.py:721` | Б | `raise RecoveryBlocked('fresh_blank_worker_policy_unproven', uncertain=True)` |
| `hh/recovery.py:726` | Б | `raise RecoveryBlocked('fresh_blank_action_boundary_unproven', uncertain=True)` |
| `hh/recovery.py:755` | Б | `raise RecoveryBlocked('fresh_blank_document_changed', uncertain=True)` |
| `hh/recovery.py:762` | Б | `uncertain = isinstance(exc, RecoveryBlocked) and exc.uncertain` |
| `hh/recovery.py:765` | Б | `uncertain = True` |
| `hh/recovery.py:767` | Б | `uncertain = True` |
| `hh/recovery.py:768` | Б | `if attempt is not None and attempt.uncertain:` |
| `hh/recovery.py:769` | Б | `uncertain = True` |
| `hh/recovery.py:770` | Б | `if attempt is not None and uncertain:` |
| `hh/recovery.py:771` | Б | `attempt.uncertain = True` |
| `hh/recovery.py:774` | Б | `session._hh_recovery_uncertain = uncertain` |
| `hh/recovery.py:790` | Б | `return RecoveryResult(False, reason, uncertain)` |
| `hh/recovery.py:804` | Б | `exc.hh_uncertain = result.uncertain` |
| `agent.py:62` | Б | `apply_result_is_uncertain,` |
| `agent.py:330` | И | `"execution_status", "application_outcome", "zero_apply_diagnosis")})` |
| `agent.py:657` | Б + И | `manual_uncertainty_note = None` |
| `agent.py:658` | Б + И | `def record_manual_uncertain():` |
| `agent.py:659` | Б + И | `nonlocal manual_uncertainty_note` |
| `agent.py:660` | Б + И | `if manual_uncertainty_note is not None:` |
| `agent.py:661` | Б + И | `return manual_uncertainty_note` |
| `agent.py:663` | Б + И | `seen.mark_seen(vacancy.get("id", token), vacancy, "apply_uncertain")` |
| `agent.py:664` | Б + И | `hh_pipeline.mark_terminal(vacancy.get("id", token), "apply_uncertain")` |
| `agent.py:665` | Б + И | `finish("uncertain", note)` |
| `agent.py:667` | Б + И | `evaluation=evaluation, details=details, note="manual_ai:apply_uncertain")` |
| `agent.py:668` | Б + И | `manual_uncertainty_note = note` |
| `agent.py:675` | Б + И | `manual_result = apply_orchestrator.apply_result_with_current_uncertainty(` |
| `agent.py:677` | Б + И | `if apply_result_is_uncertain(manual_result):` |
| `agent.py:678` | Б + И | `manual_result["message"] = record_manual_uncertain()` |
| `agent.py:680` | Б + И | `async def stop_manual_result(result, *, raise_on_uncertain=False):` |
| `agent.py:695` | Б + И | `manual_result = {**manual_result, "ok": False, "uncertain": True}` |
| `agent.py:699` | Б + И | `if raise_on_uncertain and apply_result_is_uncertain(manual_result):` |
| `agent.py:710` | Б + И | `await stop_manual_result({"ok": False, "message": guard_note}, raise_on_uncertain=True)` |
| `agent.py:725` | Б + И | `await stop_manual_result({"ok": False, "message": message}, raise_on_uncertain=True)` |
| `agent.py:749` | Б + И | `await stop_manual_result({"ok": False, "message": message}, raise_on_uncertain=True)` |
| `agent.py:773` | Б + И | `raise_on_uncertain=True)` |
| `agent.py:781` | Б + И | `if apply_result_is_uncertain(apply_result):` |
| `agent.py:782` | Б + И | `apply_result = {**apply_result, "ok": False, "uncertain": True}` |
| `agent.py:783` | Б + И | `manual_note = record_manual_uncertain()` |
| `agent.py:784` | Б + И | `await notify_needs_manual(vacancy, score, reason, note=manual_note, uncertain=True)` |
| `agent.py:785` | Б + И | `return {**apply_result, "ok": False, "uncertain": True, "message": manual_note}` |
| `agent.py:839` | Б + И | `finish("uncertain" if apply_result.get("uncertain") else "failed", message)` |
| `agent.py:847` | Б + И | `return {"ok": False, "uncertain": False, "message": message}` |
| `agent.py:850` | Б + И | `message = record_manual_uncertain()` |
| `agent.py:851` | Б + И | `await notify_needs_manual(vacancy, score, reason, note=message, uncertain=True)` |
| `agent.py:852` | Б + И | `return {**manual_result, "ok": False, "uncertain": True, "message": message}` |
| `agent.py:854` | Б + И | `uncertain = bool(getattr(exc, "hh_uncertain", False) or` |
| `agent.py:856` | Б + И | `await stop_manual_result({"ok": False, "uncertain": uncertain, "reason": "hh_unexpected_ui"})` |
| `agent.py:857` | Б + И | `uncertain = apply_result_is_uncertain(manual_result)` |
| `agent.py:858` | Б + И | `if uncertain:` |
| `agent.py:859` | Б + И | `message = record_manual_uncertain()` |
| `agent.py:860` | Б + И | `await notify_needs_manual(vacancy, score, reason, note=message, uncertain=True)` |
| `agent.py:868` | Б + И | `return {**manual_result, "ok": False, "uncertain": uncertain, "message": message}` |
| `agent.py:871` | Б + И | `record_manual_uncertain()` |
| `agent.py:873` | Б + И | `result = await stop_manual_result({"ok": False, "uncertain": dispatch_started})` |
| `agent.py:874` | Б + И | `if not apply_result_is_uncertain(result):` |
| `agent.py:879` | Б + И | `record_manual_uncertain()` |
| `agent.py:882` | Б + И | `result = await stop_manual_result({"ok": False, "uncertain": dispatch_started, "message": message})` |
| `agent.py:883` | Б + И | `uncertain = apply_result_is_uncertain(result)` |
| `agent.py:884` | Б + И | `if uncertain:` |
| `agent.py:885` | Б + И | `message = record_manual_uncertain()` |
| `agent.py:892` | Б + И | `note=message if uncertain else f"ИИ-отклик упал: {message}. Открой вручную.",` |
| `agent.py:893` | Б + И | `uncertain=uncertain,` |
| `agent.py:896` | Б + И | `print(f"⚠ {message}" if uncertain else f"❌ ИИ-отклик упал: {message}")` |
| `agent.py:897` | Б + И | `return {**result, "ok": False, "uncertain": uncertain, "message": message}` |
| `agent.py:900` | Б | `await stop_manual_result({"ok": False, "uncertain": dispatch_started})` |
| `agent.py:935` | Б | `from hh.verification import can_continue_uncertain` |
| `agent.py:938` | Б | `and can_continue_uncertain(self.client, self.owned_attempt))` |
| `agent.py:943` | Б | `self.result = apply_orchestrator.apply_result_with_current_uncertainty(` |
| `agent.py:945` | Б | `if apply_result_is_uncertain(self.result):` |
| `agent.py:1036` | И | `**({"uncertain": True} if decision == DECISION_APPLY_UNCERTAIN else {})))` |
| `agent.py:1198` | Б | `await flush_hh_uncertain_notifications()` |
| `agent.py:1200` | Б | `async def flush_hh_uncertain_notifications():` |
| `agent.py:1288` | Б | `if (item or {}).get("action") in {"apply_uncertain", "manual_hh_guard_stop"}` |
| `agent.py:1377` | Б | `"apply_uncertain", "manual_hh_guard_stop"}:` |
| `agent.py:1443` | Б + И | `# await remain in place when guard_stop becomes uncertain.` |
| `agent.py:1457` | Б + И | `bucket["uncertain"] = bucket.get("uncertain", 0) + 1` |
| `agent.py:1458` | Б + И | `seen.mark_seen(vid, v, "apply_uncertain")` |
| `agent.py:1459` | Б + И | `hh_pipeline.mark_terminal(vid, "apply_uncertain")` |
| `agent.py:1468` | Б + И | `note="hh:apply_uncertain")` |
| `agent.py:1470` | Б + И | `receipt.notice = lambda: notify_hh_uncertain(receipt, v, score, reason)` |
| `agent.py:1473` | Б + И | `async def notify_hh_uncertain(receipt, v, score, reason):` |
| `agent.py:1495` | Б + И | `await observe_hh_receipts(notify_needs_manual(v, score, reason, note=note, uncertain=True))` |
| `agent.py:1548` | Б + И | `if getattr(exc, "hh_uncertain", False):` |
| `agent.py:1550` | Б + И | `receipt.result = {**receipt.result, "ok": False, "uncertain": True}` |
| `agent.py:1555` | Б + И | `bucket["uncertain"] = bucket.get("uncertain", 0) + 1` |
| `agent.py:1556` | Б + И | `hh_pipeline.mark_terminal(vid, "apply_uncertain")` |
| `agent.py:1558` | Б + И | `"Ручная сверка: исход отклика неизвестен", "apply_uncertain", DECISION_APPLY_UNCERTAIN,` |
| `agent.py:1561` | Б + И | `analytics_note="hh:apply_uncertain", before_notify=halt_hh_browser,` |
| `agent.py:1569` | Б | `if getattr(exc, "hh_uncertain", False):` |
| `agent.py:1571` | Б | `# only the uncertainty branch; never attempt another recovery.` |
| `agent.py:2090` | Б | `receipt.capture({"ok": False, "uncertain": bool(getattr(exc, "hh_uncertain", False))})` |
| `agent.py:2096` | Б | `receipt.capture({"ok": False, "uncertain": bool(getattr(e, "hh_uncertain", False))})` |
| `agent.py:2101` | Б | `incident.hh_uncertain = getattr(e, "hh_uncertain", False)` |
| `agent.py:2210` | Б | `log.info("  %s apply result: vacancy=%s ok=%s uncertain=%s questions=%s", source_label, vid,` |
| `agent.py:2211` | Б | `bool(apply_result.get("ok")), bool(apply_result.get("uncertain")),` |
| `agent.py:2213` | Б + И | `if apply_result_is_uncertain(apply_result):` |
| `agent.py:2214` | Б + И | `apply_result = {**apply_result, "ok": False, "uncertain": True}` |
| `agent.py:2215` | Б + И | `bucket["uncertain"] = bucket.get("uncertain", 0) + 1` |
| `agent.py:2216` | Б + И | `uncertainty_note = (` |
| `agent.py:2220` | Б + И | `hh_pipeline.mark_terminal(vid, "apply_uncertain")` |
| `agent.py:2226` | Б + И | `uncertainty_note += (" Обработка следующих вакансий HH продолжается."` |
| `agent.py:2230` | Б + И | `"apply_uncertain",` |
| `agent.py:2232` | Б + И | `uncertainty_note,` |
| `agent.py:2236` | Б + И | `analytics_note=f"{source}:apply_uncertain",` |
| `agent.py:2524` | Б | `await observe_hh_receipts(notify_hh_uncertain(receipt, v, score, reason))` |
| `agent.py:3012` | И | `from outcome import application_outcome` |
| `agent.py:3014` | И | `print(f"  Application outcome: {application_outcome(result)}")` |
| `agent.py:3211` | И | `if result.get("uncertain"):` |
| `hh/apply.py:821` | Б | `from hh.verification import confirmed_result, prepare_uncertain_continuation, reconcile_post_submit` |
| `hh/apply.py:842` | Б | `# browser and NativeAttempt persists a terminal uncertain receipt.` |
| `hh/apply.py:855` | Б | `uncertain = bool(attempt.uncertain or attempt.acting or attempt.command_started` |
| `hh/apply.py:856` | Б | `or getattr(session, '_hh_recovery_uncertain', False))` |
| `hh/apply.py:857` | Б | `if uncertain:` |
| `hh/apply.py:858` | Б | `attempt.uncertain = True` |
| `hh/apply.py:859` | Б | `return {'ok': False, 'hh_hard_stop': True, 'uncertain': uncertain,` |
| `hh/apply.py:879` | Б | `if await prepare_uncertain_continuation(session, attempt, {},` |
| `hh/apply.py:881` | Б | `return {'ok': False, 'uncertain': True,` |
| `hh/apply.py:889` | Б | `exc.hh_uncertain = recovered.uncertain` |
| `hh/apply.py:892` | Б | `return {"ok": False, "uncertain": recovered.uncertain,` |
| `hh/apply.py:899` | Б | `needs_reconciliation = (not result.get('ok') or bool(result.get('uncertain'))` |
| `hh/apply.py:900` | Б | `or result.get('submission_status') in {'uncertain', 'preparing', 'acting'})` |
| `hh/apply.py:901` | Б | `if needs_reconciliation and await prepare_uncertain_continuation(session, attempt, result,` |
| `hh/apply.py:903` | Б | `return {**result, 'ok': False, 'uncertain': True, 'hh_hard_stop': False,` |
| `hh/apply.py:905` | Б | `explicit_uncertain = bool(result.get("uncertain")) or result.get("submission_status") in {"uncertain", "preparing", "acting"}` |
| `hh/apply.py:906` | Б | `if explicit_uncertain:` |
| `hh/apply.py:907` | Б | `result = {**result, "ok": False, "uncertain": True, "hh_hard_stop": True}` |
| `hh/apply.py:914` | Б | `if attempt.acting or attempt.command_started or attempt.uncertain:` |
| `hh/apply.py:924` | Б | `if recovered.uncertain:` |
| `hh/apply.py:925` | Б | `result.update(ok=False, uncertain=True)` |
| `hh/apply.py:928` | Б | `from hh.verification import can_continue_uncertain, confirmed_result` |
| `hh/apply.py:933` | Б | `if bool(result.get('uncertain')) or result.get('submission_status') in {'uncertain', 'preparing', 'acting'}:` |
| `hh/apply.py:934` | Б | `if can_continue_uncertain(session, owned_attempt):` |
| `hh/apply.py:936` | Б | `# durable uncertain receipt and sticky attempt flag remain intact;` |
| `hh/apply.py:939` | Б | `return {**result, 'ok': False, 'uncertain': True, 'hh_hard_stop': False}` |
| `hh/apply.py:946` | Б | `# Diagnostic scope only: keep every sticky browser/native uncertainty` |
| `hh/apply.py:952` | Б | `'hh_uncertainty_scope': scope}` |
| `hh/apply.py:953` | Б | `return {**result, 'ok': False, 'uncertain': True, 'hh_hard_stop': True}` |
| `hh/apply.py:1729` | Б | `return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind, "uncertain": True, "hh_hard_stop": True}` |
| `hh/apply.py:1764` | Б | `return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind, "uncertain": True, "hh_hard_stop": True}` |
| `analytics.py:20` | И | `from outcome import status_bucket as _status_bucket, status_detail_bucket as _status_detail_bucket, apply_result_is_uncertain` |
| `analytics.py:51` | И | `"apply_attempt", "applied", "manual", "uncertain", "skipped", "failed", "guard_stop",` |
| `analytics.py:88` | И | `if decision == "apply_uncertain":` |
| `analytics.py:89` | И | `return "manual", "apply_uncertain"` |
| `analytics.py:120` | И | `self.uncertain_failures = {}` |
| `analytics.py:210` | И | `if observation is not None and observation.decision_states.get(key, (None,))[0] == "apply_uncertain":` |
| `analytics.py:307` | И | `uncertain = funnel.get("uncertain", run.get("uncertain", 0)) or reasons.get("apply_uncertain", 0)` |
| `analytics.py:308` | И | `if not (new or found or retries or uncertain or reasons or failures or stopped) and run.get("ok") is not False:` |
| `analytics.py:311` | И | `if uncertain:` |
| `analytics.py:312` | И | `lines = [f"Подтверждено откликов: 0; требуется ручная сверка: {uncertain}"]` |
| `analytics.py:335` | И | `("apply_uncertain", "исход отклика не подтверждён"),` |
| `analytics.py:356` | И | `uncertain = funnel.get("uncertain") or run.get("uncertain") or (run.get("reason_breakdown") or {}).get("apply_uncertain")` |
| `analytics.py:357` | И | `outcome = ("unknown" if incomplete else "uncertain" if uncertain else` |
| `analytics.py:362` | И | `"application_outcome": outcome, "zero_apply_diagnosis": zero_apply_diagnosis(run)}` |
| `analytics.py:385` | И | `and not result.get("already_applied") and not apply_result_is_uncertain(result)):` |
| `analytics.py:459` | И | `uncertain = (run.get("funnel") or {}).get("uncertain", run.get("uncertain", 0))` |
| `analytics.py:460` | И | `uncertain = uncertain or (run.get("reason_breakdown") or {}).get("apply_uncertain", 0)` |
| `analytics.py:461` | И | `if field == "applied" and uncertain and not incomplete:` |
| `analytics.py:463` | И | `return f"{confirmed} подтверждено; {uncertain} требуют сверки"` |
| `analytics.py:810` | И | `"execution_status", "application_outcome", "zero_apply_diagnosis") if field in result},` |
| `analytics.py:831` | И | `if decision == "apply_uncertain":` |
| `analytics.py:832` | И | `failure_id = failure_id or observation.uncertain_failures.get(key)` |
| `analytics.py:842` | И | `failure_id = record_failure(stage, RuntimeError("apply_uncertain"),` |
| `analytics.py:843` | И | `reason_code="apply_uncertain", retryable=False,` |
| `analytics.py:845` | И | `observation.uncertain_failures[key] = failure_id` |
| `analytics.py:853` | И | `if previous is not None and previous[0] == "apply_uncertain" and decision != "apply_uncertain":` |
| `analytics.py:855` | И | `if decision == "apply_uncertain" and previous is not None and previous[0] != decision:` |
| `analytics.py:874` | И | `if decision == "apply_uncertain":` |
| `analytics.py:875` | И | `observation.counters[key[0]]["uncertain"] += 1` |
| `analytics.py:1105` | И | `if action == "apply_uncertain":` |
| `analytics.py:1106` | И | `return "apply_uncertain", "historical_seen"` |
| `analytics.py:1252` | И | `"apply_uncertain",` |
| `analytics.py:1387` | И | `# run. Preserve the journal, but report only its sticky uncertainty for` |
| `analytics.py:1392` | И | `uncertain_decisions = {` |
| `analytics.py:1394` | И | `if event.get("event") == "decision" and event.get("decision") == "apply_uncertain"` |
| `analytics.py:1398` | И | `if event.get("event") != "decision" or decision_key(event) not in uncertain_decisions` |
| `analytics.py:1399` | И | `or uncertain_decisions[decision_key(event)] == index]` |
| `analytics.py:1408` | И | `"uncertain": 0,` |
| `analytics.py:1533` | И | `"apply_uncertain",` |
| `analytics.py:1540` | И | `if decision == "apply_uncertain":` |
| `analytics.py:1541` | И | `summary["uncertain"] += 1` |
| `analytics.py:1542` | И | `source_bucket["uncertain"] = source_bucket.get("uncertain", 0) + 1` |
