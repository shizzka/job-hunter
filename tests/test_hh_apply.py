import hh_client
from hh import apply as hh_apply


def test_legacy_apply_predicates_are_reexported():
    assert hh_client._has_archived_hh_state is hh_apply.has_archived_hh_state
    assert (
        hh_client._looks_like_closed_or_archived_hh
        is hh_apply.looks_like_closed_or_archived_hh
    )
    assert (
        hh_client._looks_like_existing_hh_response
        is hh_apply.looks_like_existing_hh_response
    )
    assert hh_client._looks_like_hh_apply_success is hh_apply.looks_like_hh_apply_success


def test_closed_or_archived_predicate_ignores_generic_html_shell():
    html = "<html><template>Вакансия не принимает отклики</template></html>"

    assert hh_apply.looks_like_closed_or_archived_hh(html) is False


def test_closed_or_archived_predicate_detects_serialized_archived_state():
    html = '<script>window.data = {"archived": true}</script>'

    assert hh_apply.looks_like_closed_or_archived_hh(html) is True


def test_apply_success_includes_existing_response_state():
    assert hh_apply.looks_like_hh_apply_success("Вы уже откликнулись") is True
