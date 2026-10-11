from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from coursewatch.models import now
from coursewatch.spire import SourceError, SpireClient, fast_sections, parse_search_rows

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "search-results.html").read_text()
DETAIL = (FIXTURES / "class-detail.html").read_text()


class FixtureClient(SpireClient):
    def __init__(self, search=SEARCH, seat_mismatch=False):
        super().__init__()
        self.search = search
        self.seat_mismatch = seat_mismatch
        self.actions = []

    def start(self):
        self.request_count += 2
        self.soup = BeautifulSoup(
            '<select id="UM_DERIVED_SA_UM_TERM_DESCR">'
            '<option value="2267">2026 Fall</option></select>',
            "html.parser",
        )

    def action(self, action, **kwargs):
        self.request_count += 1
        self.actions.append(action)
        if action == "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH":
            self.soup = BeautifulSoup(self.search, "html.parser")
        if action.startswith("DERIVED_CLSRCH_SSR_CLASSNAME_LONG$"):
            index = int(action.split("$")[-1])
            row = parse_search_rows(BeautifulSoup(self.search, "html.parser"))[index]
            soup = BeautifulSoup(DETAIL, "html.parser")
            fields = {
                "CLASS_NBR": row["class_number"],
                "ENRL_CAP": row["capacity"],
                "ENRL_TOT": row["enrolled"],
                "AVAILABLE_SEATS": row["available_seats"],
                "SSR_DESCRSHORT": row["status"],
            }
            if self.seat_mismatch:
                fields["AVAILABLE_SEATS"] = 0
            for name, value in fields.items():
                soup.find(id="SSR_CLS_DTL_WRK_" + name).string = str(value)
            return str(soup)
        return str(self.soup)


def collect(search=SEARCH, cached=None, seat_mismatch=False):
    with FixtureClient(search, seat_mismatch) as client:
        client.cached_snapshot = cached
        result = client.collect("2026 Fall", "COMPSCI", "220")
        return result, client.request_count


def test_live_search_fixture_counts_status_and_identity():
    rows = parse_search_rows(BeautifulSoup(SEARCH, "html.parser"))
    assert rows[0]["class_number"] == "12073"
    assert rows[0]["available_seats"] == 2 and rows[0]["status"] == "Open"
    assert rows[1]["class_number"] == "12075" and rows[1]["available_seats"] == 0
    assert rows[1]["status"] == "Closed"


def test_repeat_check_uses_search_only_without_mutating_cache():
    initial, requests = collect()
    original = deepcopy(initial)
    result, fast_requests = collect(cached=initial)
    assert requests == 9 and fast_requests == 5
    assert initial["search_counts_validated"] and result["collection_mode"] == "search"
    assert result["sections"] == initial["sections"] and initial == original
    assert result["details_validated_at"] == initial["details_validated_at"]


def test_new_opening_is_confirmed_with_detail_counts():
    initial, _ = collect()
    soup = BeautifulSoup(SEARCH, "html.parser")
    soup.find(id="UM_DERIVED_SR_ENRL_TOT$1").string = "34"
    soup.find(id="win0divDERIVED_CLSRCH_SSR_STATUS_LONG$1").find("img")["alt"] = "Open"
    result, requests = collect(str(soup), cached=initial, seat_mismatch=True)
    assert result["collection_mode"] == "details" and requests == 9
    assert not result["search_counts_validated"]
    assert not any(b["open"] for b in result["bundles"])


def test_expired_details_require_refresh():
    initial, _ = collect()
    initial["details_validated_at"] = (now() - timedelta(hours=2)).isoformat()
    result, requests = collect(cached=initial)
    assert result["collection_mode"] == "details" and requests == 9


@pytest.mark.parametrize("change", ["session", "schedule", "capacity", "section"])
def test_changed_structure_requires_details(change):
    initial, _ = collect()
    soup = BeautifulSoup(SEARCH, "html.parser")
    if change == "session":
        soup.find(id="PSXLATITEM_XLATSHORTNAME$0").string = "Winter"
    elif change == "schedule":
        soup.find(id="win0divSSR_CLSRCH_MTG1$0").string = "TBA"
    elif change == "capacity":
        soup.find(id="UM_DERIVED_SR_ENRL_CAP$0").string = "116"
    else:
        soup.find(id="DERIVED_CLSRCH_SSR_CLASSNAME_LONG$0").string = "03-LEC(12073)"
    result, requests = collect(str(soup), cached=initial)
    assert result["collection_mode"] == "details" and requests == 9


def test_new_removed_or_wrong_course_cache_is_not_reused():
    initial, _ = collect()
    rows = parse_search_rows(BeautifulSoup(SEARCH, "html.parser"))
    assert fast_sections(rows[:1], initial, "2026 Fall", "COMPSCI", "220") is None
    initial["catalog_number"] = "250"
    assert fast_sections(rows, initial, "2026 Fall", "COMPSCI", "220") is None


def test_detail_counts_that_disagree_disable_fast_path():
    initial, _ = collect(seat_mismatch=True)
    assert not initial["search_counts_validated"]
    result, requests = collect(cached=initial)
    assert result["collection_mode"] == "details" and requests == 9


@pytest.mark.parametrize(
    "name", ["UM_DERIVED_SR_ENRL_CAP$0", "win0divDERIVED_CLSRCH_SSR_STATUS_LONG$0"]
)
def test_incomplete_search_is_error_not_closed(name):
    soup = BeautifulSoup(SEARCH, "html.parser")
    soup.find(id=name).decompose()
    with pytest.raises(SourceError):
        collect(str(soup))


def test_duplicate_class_rows_rejected():
    soup = BeautifulSoup(SEARCH, "html.parser")
    soup.find(id="DERIVED_CLSRCH_SSR_CLASSNAME_LONG$1").string = "01-LEC(12073)"
    with pytest.raises(SourceError, match="Duplicate"):
        parse_search_rows(soup)


def test_shared_request_gate_also_paces_redirects(monkeypatch):
    from types import SimpleNamespace

    import httpx

    monkeypatch.setattr(
        "coursewatch.spire.settings", lambda: SimpleNamespace(spire_request_interval_seconds=0)
    )
    gated = []

    def response(request):
        if request.url.path == "/first":
            return httpx.Response(302, headers={"location": "/second"})
        return httpx.Response(200, text="<html>done</html>")

    with SpireClient() as client:
        client.client.close()
        client.client = httpx.Client(
            transport=httpx.MockTransport(response),
            follow_redirects=True,
            event_hooks={"request": [client.before_http_request]},
        )
        client.before_request = lambda: gated.append(True)
        client.request("GET", "https://www.spire.umass.edu/first")
        assert client.request_count == 2 and len(gated) == 2


def test_redirect_outside_spire_is_rejected_before_request(monkeypatch):
    from types import SimpleNamespace

    import httpx

    monkeypatch.setattr(
        "coursewatch.spire.settings", lambda: SimpleNamespace(spire_request_interval_seconds=0)
    )
    visited = []

    def response(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.org/"})

    with SpireClient() as client:
        client.client.close()
        client.client = httpx.Client(
            transport=httpx.MockTransport(response),
            follow_redirects=True,
            event_hooks={"request": [client.before_http_request]},
        )
        with pytest.raises(SourceError, match="redirect host"):
            client.request("GET", "https://www.spire.umass.edu/first")
        assert len(visited) == 1
