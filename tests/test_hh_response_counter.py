import html
import json

import pytest

import hh_response_counter


def _page_model(*, total: int, page_count: int, filter_name: str, deleted: int = 0) -> str:
    payload = (
        f'"items":[],"total":{total},"readOnlyInterval":180,'
        f'"pageCount":"{page_count}","filterInUse":"{filter_name}",'
        '"applicantNegotiationsCounters":{'
        '"new":{"all":0},'
        f'"total":{{"all":119,"deleted":{deleted},"archived":0}}'
        "}"
    )
    return f'<div data-state="{html.escape(payload, quote=True)}"></div>'


def test_parse_active_negotiations_page():
    result = hh_response_counter.parse_negotiations_page(
        _page_model(total=119, page_count=6, filter_name="active", deleted=7),
        expected_filter="active",
    )

    assert result == {
        "total": 119,
        "page_count": 6,
        "filter": "active",
        "deleted": 7,
    }


def test_parse_archived_negotiations_page():
    result = hh_response_counter.parse_negotiations_page(
        _page_model(total=1611, page_count=81, filter_name="archived"),
        expected_filter="archived",
    )

    assert result == {
        "total": 1611,
        "page_count": 81,
        "filter": "archived",
    }


def test_parse_requires_matching_filter():
    with pytest.raises(hh_response_counter.HHResponseCounterError):
        hh_response_counter.parse_negotiations_page(
            _page_model(total=119, page_count=6, filter_name="active"),
            expected_filter="archived",
        )


def test_save_snapshot_persists_total_and_delta(tmp_path):
    first = hh_response_counter.save_snapshot(
        profile_name="qa",
        home_dir=str(tmp_path),
        active={"total": 119, "page_count": 6, "deleted": 7},
        archived={"total": 1611, "page_count": 81},
        fetched_at="2026-09-11T10:00:00+03:00",
    )
    second = hh_response_counter.save_snapshot(
        profile_name="qa",
        home_dir=str(tmp_path),
        active={"total": 121, "page_count": 7, "deleted": 7},
        archived={"total": 1612, "page_count": 81},
        fetched_at="2026-09-11T11:00:00+03:00",
    )

    assert first["total"] == 1737
    assert first["delta"] == {}
    assert second["total"] == 1740
    assert second["delta"] == {
        "active": 2,
        "archived": 1,
        "deleted": 0,
        "total": 3,
    }
    assert json.loads(
        (tmp_path / hh_response_counter.SNAPSHOT_FILENAME).read_text(encoding="utf-8")
    ) == second
