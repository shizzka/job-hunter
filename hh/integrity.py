"""Read-only integrity of one HH browser session after an application (R2).

The application orchestrator asks one question — may a finished result stand,
or must it become «исход не подтверждён»? — instead of reading private state of
the client, page, context, action monitor and context watch itself.
"""
from hh.browser import is_completed_owned_stop


def session_permits_result(hh_client, vacancy_url: str, *, owned_attempt=None) -> bool:
    """True when this client's sticky evidence leaves the result as is; False means uncertain.

    No browser actions or writes. Any doubt (lost ownership, an unknown action, an
    unreadable receipt) answers False: it cannot prove that nothing else was sent.
    """
    if hh_client is None:
        return True
    try:
        if getattr(hh_client, "_hh_recovery_uncertain", False):
            return False
        page = getattr(hh_client, "_page", None)
        context = getattr(hh_client, "_context", None)
        monitor = getattr(page, "_hh_action_monitor", None)
        watch = getattr(context, "_hh_action_watch", None)
        if monitor is not None and (monitor.session is not hh_client or monitor.unknown):
            return False
        if watch is not None and (watch.session is not hh_client or watch.unknown):
            return False
        active = getattr(hh_client, "_external_attempt", None)
        active_owner = None
        earlier_monitor = None
        if owned_attempt is not None:
            earlier_monitor = getattr(owned_attempt.page, "_hh_action_monitor", None)
            earlier_watch = getattr(owned_attempt.context, "_hh_action_watch", None)
            if owned_attempt.page is not None and (
                    earlier_monitor is None or (context is not None and (
                        page is None or monitor is None or monitor.page is not page or page.context is not context
                        or watch is None or watch.context is not context or watch.pages.get(page) is not monitor))):
                return False
            if earlier_monitor is not None and (
                    earlier_monitor.session is not hh_client or earlier_monitor.page is not owned_attempt.page
                    or owned_attempt.page.context is not owned_attempt.context or earlier_monitor.unknown
                    or earlier_watch is None or earlier_watch.session is not hh_client
                    or earlier_watch.context is not owned_attempt.context
                    or earlier_watch.pages.get(owned_attempt.page) is not earlier_monitor
                    or earlier_monitor.attempts.get(owned_attempt.owner) is not owned_attempt
                    or (earlier_monitor is not monitor and earlier_monitor.owners)):
                return False
        if owned_attempt is not None and active is not None and active is not owned_attempt:
            # A later legitimate attempt does not invalidate this earlier
            # receipt. Both identities must be retained by the same context's
            # exact owned registry, including the earlier Page after recovery.
            if (active.client is not hh_client or active.context is not context or active.page is not page
                    or active.repository.source != "hh" or not active.owner or active.uncertain
                    or watch is None or watch.session is not hh_client or watch.context is not context
                    or monitor is None or monitor.page is not page or page.context is not context
                    or watch.pages.get(page) is not monitor
                    or monitor.attempts.get(active.owner) is not active
                    or owned_attempt.client is not hh_client or owned_attempt.context is not context
                    or owned_attempt.repository.source != "hh" or not owned_attempt.owner
                    or earlier_monitor is None or earlier_monitor.session is not hh_client
                    or earlier_monitor.page is not owned_attempt.page or owned_attempt.page.context is not context
                    or earlier_monitor.unknown or earlier_monitor.owners - {active.owner}
                    or watch.pages.get(owned_attempt.page) is not earlier_monitor
                    or earlier_monitor.attempts.get(owned_attempt.owner) is not owned_attempt):
                return False
            active_state = active.repository.get(active.url)
            if (active_state.get("owner") != active.owner or active_state.get("status") not in {"preparing", "acting"}
                    or (active_state.get("status") == "acting") != bool(active.acting)
                    or (monitor.owners and not active.acting)):
                return False
            active_owner = active.owner
        if monitor is not None and monitor.owners and (active_owner is None or monitor.owners - {active_owner}):
            return False
        attempt = owned_attempt if owned_attempt is not None else active
        if attempt is None and monitor is not None:
            attempt = monitor.last_attempt
        if attempt is None:
            return True
        repository = attempt.repository
        if attempt.client is not hh_client or repository.source != "hh" or not attempt.owner:
            return False
        if attempt.context is not context:
            # Shutdown releases references only after the captured browser has
            # died. Accept that release solely for this completed owned task.
            shutdown = getattr(hh_client, "_hh_shutdown_operation", None)
            if context is not None or not isinstance(shutdown, tuple) or len(shutdown) != 3:
                return False
            captured_context, browser, operation = shutdown
            if (captured_context is not attempt.context or browser is None
                    or getattr(browser, "_hh_owner_session", None) is not hh_client
                    or getattr(captured_context, "browser", None) is not browser
                    or any(getattr(hh_client, field, None) is not None for field in
                           ("_context", "_browser", "_page", "_pw", "_cookie_binding"))
                    or not is_completed_owned_stop(operation)):
                return False
            # The old monitor and receipt retain late evidence after release.
            monitor = getattr(attempt.page, "_hh_action_monitor", None)
            watch = getattr(captured_context, "_hh_action_watch", None)
            if monitor is not None and (monitor.session is not hh_client or monitor.unknown or monitor.owners):
                return False
            if watch is not None and (watch.session is not hh_client or watch.unknown):
                return False
        # Native keys validate the exact HH vacancy identity before any read.
        if repository.key(attempt.url) != repository.key(vacancy_url):
            return True
        if attempt.uncertain:
            return False
        receipt = repository.get(attempt.url)
        if receipt.get("owner") != attempt.owner:
            return False
        if receipt.get("status") not in {"failed", "completed"}:
            return False
    except Exception:
        # Losing ownership or access to this receipt cannot prove no action.
        return False
    return True

