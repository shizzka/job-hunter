import seen


def test_stats_from_data_counts_already_applied_as_skipped():
    stats = seen.stats_from_data(
        {
            "123": {"action": "already_applied"},
            "124": {"action": "applied"},
            "125": {"action": "manual_hh"},
        }
    )

    assert stats["total"] == 3
    assert stats["applied"] == 1
    assert stats["manual"] == 1
    assert stats["skipped"] == 2
    assert stats["by_source"]["hh"]["skipped"] == 2
