from gbwatch.diff import diff_stats


def test_diff_reports_changed_stats_only():
    old = {
        "Cedric Coward": {"MIN": "33:26", "PTS": 20, "ST": 2},
        "Ja'Kobe Walter": {"PTS": 18, "ST": 2},
    }
    new = {
        "Cedric Coward": {"MIN": "33:26", "PTS": 21, "ST": 2},
        "Ja'Kobe Walter": {"PTS": 18, "ST": 2},
    }
    changes = diff_stats(old, new)
    assert changes == [("Cedric Coward", "PTS", 20, 21)]
    assert changes[0].as_dict()["stat"] == "PTS"


def test_diff_includes_added_and_removed_players():
    changes = diff_stats(
        {"A": {"PTS": 1}},
        {"B": {"PTS": 2}},
    )
    assert [(c.player, c.stat, c.old, c.new) for c in changes] == [
        ("A", "PTS", 1, None),
        ("B", "PTS", None, 2),
    ]
