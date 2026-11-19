"""Text import from an external capture (OTB artifacts are not reachable here)."""

import json
import subprocess
from pathlib import Path

from gbwatch.cli import main
from gbwatch.classify import parse_text

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF = FIXTURES / "WASTOR_final_v2.pdf"

# Banner kept in the imported text so the file cannot be mistaken for a real capture.
SYNTHETIC_BANNER = """\
SYNTHETIC TEXT — not an OTB resolver artifact.
Built in this test from pdftotext -layout of tests/fixtures/WASTOR_final_v2.pdf.
Edits: final-box Ja'Kobe Walter ST 2 -> 3, Raptors team ST 8 -> 9,
and one inserted Q1 play-by-play line "03:54   J.Walter STEAL".
"""


def _synthetic_wastor_text() -> str:
    raw = subprocess.check_output(["pdftotext", "-layout", str(PDF), "-"], text=True)
    # Keep form feeds. str.splitlines() would treat them as line breaks.
    lines = raw.split("\n")
    walter_done = team_done = steal_done = False
    edited: list[str] = []
    for line in lines:
        if not walter_done and "Ja'Kobe Walter" in line and "37:52" in line:
            if "6 1 2 2 0" not in line:
                raise AssertionError(f"Walter final-box line did not match: {line!r}")
            # A PF ST TO BS is "6 1 2 2 0"; ST is the first 2.
            line = line.replace("6 1 2 2 0", "6 1 3 2 0", 1)
            walter_done = True
        if not team_done and "240:00 39 92" in line:
            if "22 8" not in line:
                raise AssertionError(f"Raptors total line did not match: {line!r}")
            line = line.replace("22 8", "22 9", 1)
            team_done = True
        if not steal_done and "03:54" in line and "W.Riley Bad Pass TURNOVER" in line:
            edited.append("03:54   J.Walter STEAL")
            steal_done = True
        edited.append(line)
    if not (walter_done and team_done and steal_done):
        raise AssertionError(f"edits applied: walter={walter_done} team={team_done} steal={steal_done}")
    return SYNTHETIC_BANNER + "\n".join(edited)


def test_text_then_pdf_first_final_is_walter_st3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)

    text = _synthetic_wastor_text()
    parsed = parse_text(text)
    assert parsed.kind == "final"
    assert parsed.players["Ja'Kobe Walter"]["ST"] == 3
    assert parsed.players["Ja'Kobe Walter"]["MIN"] == "37:52"
    assert parsed.teams["TORONTO RAPTORS"]["ST"] == 9
    assert "03:54   J.Walter STEAL" in text

    text_path = tmp_path / "wastor_synthetic_st3.txt"
    text_path.write_text(text, encoding="utf-8")

    assert (
        main(
            [
                "import",
                "--game",
                "WASTOR",
                "--date",
                "20261118",
                "--fetched-utc",
                "2026-11-19T03:09:00Z",
                "--source",
                "otb",
                "--text",
                str(text_path),
                "--note",
                "synthetic",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "import",
                "--game",
                "WASTOR",
                "--date",
                "20261118",
                "--fetched-utc",
                "2026-11-19T04:36:00Z",
                "--source",
                "manual",
                "--pdf",
                str(PDF),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["show", "WASTOR", "2026-11-18"]) == 0
    shown = capsys.readouterr().out
    assert "source: otb" in shown
    assert "source=manual" in shown
    assert ".txt" in shown
    assert "Ja'Kobe Walter ST 3 -> 2" in shown
    assert "2026-11-19T03:09:00Z" in shown

    assert main(["alerts", "--since", "0"]) == 0
    alerts = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [alert["type"] for alert in alerts] == ["first_final", "diff"]
    assert alerts[0]["diffs"] == []
    assert alerts[0]["sha256"] != alerts[1]["sha256"]
    walter = [row for row in alerts[1]["diffs"] if row["player"] == "Ja'Kobe Walter" and row["stat"] == "ST"]
    assert walter == [{"player": "Ja'Kobe Walter", "stat": "ST", "old": 3, "new": 2}]
    # The synthetic team total is the only other stat change in that one alert.
    assert alerts[1]["diffs"] == [
        {"player": "Ja'Kobe Walter", "stat": "ST", "old": 3, "new": 2},
        {"player": "TORONTO RAPTORS total", "stat": "ST", "old": 9, "new": 8},
    ]
