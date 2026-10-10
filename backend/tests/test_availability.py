from copy import deepcopy
from pathlib import Path

import pytest
from coursewatch.availability import build_bundles, open_bundles
from coursewatch.spire import SourceError, form_data, parse_detail

DETAIL = Path(__file__).parent / "fixtures" / "class-detail.html"


def section(number, name, component="LEC", status="Open", seats=1, meetings=None):
    return {
        "class_number": number,
        "section": name,
        "component": component,
        "status": status,
        "available_seats": seats,
        "required_components": ["LEC", "LAB"],
        "session": "University",
        "meetings": meetings or ["We 9:00AM - 9:50AM"],
    }


def pair():
    return [
        section("10001", "01", meetings=["TuTh 10:00AM - 11:15AM"]),
        section("10002", "01LL", "LAB"),
    ]


def test_live_detail_fixture():
    result = parse_detail(
        DETAIL.read_text(), {"class_number": "12073", "section": "01", "component": "LEC"}
    )
    assert result["available_seats"] == 2
    assert result["capacity"] == 115
    assert result["required_components"] == ["LAB", "LEC"]
    assert "Computer Science majors" in result["restrictions"]


def test_wrong_class_is_not_observation():
    with pytest.raises(SourceError, match="wrong class"):
        parse_detail(DETAIL.read_text(), {"class_number": "99999"})


def test_missing_seat_count_is_not_closed():
    html = DETAIL.read_text().replace('id="SSR_CLS_DTL_WRK_AVAILABLE_SEATS"', 'id="changed"')
    with pytest.raises(SourceError):
        parse_detail(html, {"class_number": "12073"})


def test_single_lecture_needs_no_pairing():
    lecture = section("10001", "01")
    lecture["required_components"] = ["LEC"]
    bundles, issues = build_bundles([lecture])
    assert bundles[0]["open"] and not bundles[0]["inferred"]
    assert not issues


def test_pair_requires_both_components():
    rows = pair()
    bundles, _ = build_bundles(rows)
    assert len(bundles) == 1 and bundles[0]["open"] and bundles[0]["inferred"]
    rows[1]["status"] = "Closed"
    assert not build_bundles(rows)[0][0]["open"]


@pytest.mark.parametrize(
    "status,seats", [("Open", 0), ("Wait List", 1), ("Closed", 1), ("Cancelled", 2), ("Open", -1)]
)
def test_status_and_positive_seats_required(status, seats):
    rows = pair()
    rows[1].update(status=status, available_seats=seats)
    assert not build_bundles(rows)[0][0]["open"]


def test_different_lecture_groups_not_mixed():
    rows = pair()
    rows[1]["section"] = "02LL"
    bundles, issues = build_bundles(rows)
    assert not bundles and issues


def test_different_sessions_not_mixed():
    rows = pair()
    rows[1]["session"] = "University Online"
    assert not build_bundles(rows)[0]


def test_ambiguous_prefix_not_guessed():
    rows = pair() + [section("10003", "0")]
    assert not build_bundles(rows)[0]


def test_conflicting_meetings_excluded():
    rows = pair()
    rows[1]["meetings"] = ["Tu 10:30AM - 11:20AM"]
    assert not build_bundles(rows)[0]


def test_unknown_meeting_times_labeled():
    rows = pair()
    rows[1]["meetings"] = ["TBA"]
    assert build_bundles(rows)[0][0]["schedule_unknown"]


def test_lab_watch_filters_complete_bundles():
    rows = pair()
    rows += [section("10003", "01LM", "LAB")]
    bundles, _ = build_bundles(rows)
    snapshot = {"bundles": bundles}
    assert len(open_bundles(snapshot, "10002")) == 1
    assert open_bundles(snapshot, "99999") == []
    assert len(open_bundles(snapshot)) == 2


def test_missing_required_component_blocks_alerts():
    rows = pair()
    rows[0]["required_components"].append("DIS")
    assert not build_bundles(rows)[0]


def test_expired_session_form_rejected():
    from bs4 import BeautifulSoup

    with pytest.raises(SourceError):
        form_data(BeautifulSoup("<html>Please log in</html>", "html.parser"))


def test_inconsistent_unknown_requirements_block_pair():
    rows = deepcopy(pair())
    rows[0]["required_components"] = []
    assert not build_bundles(rows)[0]
