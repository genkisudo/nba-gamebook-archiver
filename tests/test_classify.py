from pathlib import Path

from gbwatch.classify import _pdfinfo_date, classify_kind, parse_pdf, pdf_metadata

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_partial_is_in_game_and_finals_are_final():
    partial = parse_pdf(FIXTURES / "MEMCLE_1Q_partial.pdf")
    assert partial.kind == "in_game"
    for name in ("MEMCLE_final.pdf", "WASTOR_final_v2.pdf", "MILMIA_final.pdf"):
        assert parse_pdf(FIXTURES / name).kind == "final"


def test_walter_coward_giannis_and_scores():
    wastor = parse_pdf(FIXTURES / "WASTOR_final_v2.pdf")
    walter = wastor.players["Ja'Kobe Walter"]
    assert walter["MIN"] == "37:52"
    assert walter["ST"] == 2
    assert wastor.teams["Washington Wizards"]["PTS"] == 103
    assert wastor.teams["TORONTO RAPTORS"]["PTS"] == 99
    assert wastor.score["Wizards"] == 103
    assert wastor.score["RAPTORS"] == 99

    mem = parse_pdf(FIXTURES / "MEMCLE_final.pdf")
    coward = mem.players["Cedric Coward"]
    assert coward["MIN"] == "33:26"
    assert coward["PTS"] == 20
    assert mem.teams["Memphis Grizzlies"]["PTS"] == 124
    assert mem.teams["CLEVELAND CAVALIERS"]["PTS"] == 121
    assert mem.score == {"Grizzlies": 124, "CAVALIERS": 121}

    mil = parse_pdf(FIXTURES / "MILMIA_final.pdf")
    giannis = mil.players["Giannis Antetokounmpo"]
    assert giannis["OR"] == 3
    assert giannis["DR"] == 11
    assert giannis["TOT"] == 14
    assert mil.teams["MIAMI HEAT"]["PTS"] == 137


def test_pdfinfo_iso_offset_converts_to_utc():
    assert _pdfinfo_date("2026-11-19T03:11:41Z") == "2026-11-19T03:11:41Z"
    assert _pdfinfo_date("2026-11-19T04:11:41+01:00") == "2026-11-19T03:11:41Z"
    assert _pdfinfo_date("2026-11-18T22:11:41-05:00") == "2026-11-19T03:11:41Z"
    assert _pdfinfo_date("Thu Nov 19 04:11:41 2026 CET") is None


def test_pdf_timestamps():
    mem = parse_pdf(FIXTURES / "MEMCLE_final.pdf")
    meta = pdf_metadata(FIXTURES / "MEMCLE_final.pdf")
    assert meta["pdf_created"] == "2026-11-19T03:11:41Z"
    assert meta["pages"] == 19
    assert pdf_metadata(FIXTURES / "MEMCLE_1Q_partial.pdf")["pdf_created"] == "2026-11-19T00:20:36Z"
    assert pdf_metadata(FIXTURES / "WASTOR_final_v2.pdf")["pdf_created"] == "2026-11-19T04:36:29Z"
    assert pdf_metadata(FIXTURES / "MILMIA_final.pdf")["pages"] == 20
    assert mem.kind == "final"


def test_kind_rules_on_synthetic_text():
    assert classify_kind("Attendance: Not Yet Counted\nFINAL BOX\n") == "in_game"
    assert classify_kind("FINAL BOX\n1st QUARTER ONLY\nAttendance: 19007\n") == "final"
    quarter = "1st QUARTER ONLY\nAttendance: 19007\nSCORE BY PERIOD 1 FINAL\n"
    assert classify_kind(quarter) == "in_game"
    complete = "Attendance: 19007\nSCORE BY PERIOD 1 2 3 4 FINAL\n"
    assert classify_kind(complete) == "final"
