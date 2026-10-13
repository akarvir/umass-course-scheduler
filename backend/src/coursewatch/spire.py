import argparse
import json
import re
import time
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from coursewatch.availability import build_bundles
from coursewatch.config import settings

BASE = "https://www.spire.umass.edu"
COMPONENT_PATH = "/heproda/EMPLOYEE/SA/c/COMMUNITY_ACCESS.CLASS_SEARCH.GBL"
GUEST_URL = BASE + "/psp" + COMPONENT_PATH
COMPONENTS = {
    "Lecture": "LEC",
    "Laboratory": "LAB",
    "Discussion": "DIS",
    "Seminar": "SEM",
    "Independent Study": "IND",
    "Studio": "STU",
    "Recitation": "REC",
    "Practicum": "PRC",
    "Tutorial": "TUT",
}


class SourceError(Exception):
    pass


class TermUnavailable(SourceError):
    pass


class CourseUnavailable(SourceError):
    pass


def field(soup, name: str, required=True):
    element = soup.find(id=name)
    value = element.get_text(" ", strip=True) if element else ""
    if required and not value:
        raise SourceError(f"SPIRE response missing {name}")
    return value


def form_data(soup):
    form = soup.find("form", id="CLASS_SEARCH")
    if not form or not form.find("input", attrs={"name": "ICStateNum"}):
        raise SourceError("SPIRE session expired or unexpected form")
    data = {}
    for element in form.select("input[name], select[name], textarea[name]"):
        kind = element.get("type", "")
        if kind in {"button", "submit"} or (
            kind in {"checkbox", "radio"} and not element.has_attr("checked")
        ):
            continue
        if element.name == "select":
            option = element.find("option", selected=True) or element.find("option")
            data[element["name"]] = option.get("value", "") if option else ""
        elif element.name == "textarea":
            data[element["name"]] = element.get_text()
        else:
            data[element["name"]] = element.get("value", "")
    data["ICType"] = "Panel"
    return form.get("action", ""), data


def parse_detail(html: str, identity: dict):
    soup = BeautifulSoup(html, "html.parser")
    if field(soup, "SSR_CLS_DTL_WRK_CLASS_NBR") != identity["class_number"]:
        raise SourceError("SPIRE returned details for the wrong class")
    values = {}
    for name in ["ENRL_CAP", "ENRL_TOT", "AVAILABLE_SEATS"]:
        raw = field(soup, "SSR_CLS_DTL_WRK_" + name).replace(",", "")
        if not re.fullmatch(r"-?\d+", raw):
            raise SourceError("SPIRE seat counts could not be parsed")
        values[name] = int(raw)
    status = field(soup, "SSR_CLS_DTL_WRK_SSR_DESCRSHORT")
    if status not in {"Open", "Closed", "Wait List", "Waitlist", "Cancelled", "Canceled"}:
        raise SourceError("Unrecognized class status")
    required = []
    for element in soup.find_all(id=re.compile(r"^SSR_CLS_DTL_WRK_DESCR\$\d+$")):
        index = element["id"].split("$")[-1]
        if field(soup, f"SSR_CLS_DTL_WRK_DESCRSHORT${index}", False) == "Required":
            label = element.get_text(strip=True)
            if label not in COMPONENTS:
                raise SourceError(f"Unsupported required component: {label}")
            required.append(COMPONENTS[label])
    if not required:
        raise SourceError("SPIRE did not expose required components")
    return {
        **identity,
        "status": status,
        "capacity": values["ENRL_CAP"],
        "enrolled": values["ENRL_TOT"],
        "available_seats": values["AVAILABLE_SEATS"],
        "required_components": required,
        "session": field(soup, "PSXLATITEM_XLATLONGNAME"),
        "restrictions": field(soup, "SSR_CLS_DTL_WRK_SSR_REQUISITE_LONG", False),
        "meetings": [
            e.get_text(" ", strip=True) for e in soup.find_all(id=re.compile(r"^MTG_DAYTIME\$\d+$"))
        ],
    }


def parse_search_rows(soup):
    links = soup.find_all("a", id=re.compile(r"^DERIVED_CLSRCH_SSR_CLASSNAME_LONG\$\d+$"))
    if not links or len(links) > 80:
        raise SourceError("SPIRE search rows missing or exceed the 80-section limit")
    rows = []
    for link in links:
        match = re.fullmatch(r"([A-Za-z0-9]+)-([A-Z]+)\((\d{5})\)", link.get_text(strip=True))
        if not match:
            raise SourceError("Unexpected section identifier")
        index = link["id"].split("$")[-1]
        status_element = soup.find(id=f"win0divDERIVED_CLSRCH_SSR_STATUS_LONG${index}")
        image = status_element.find("img") if status_element else None
        status = image.get("alt", "") if image else ""
        if status not in {"Open", "Closed", "Wait List", "Waitlist", "Cancelled", "Canceled"}:
            raise SourceError("SPIRE search status missing or unrecognized")
        counts = []
        for name in ("ENRL_CAP", "ENRL_TOT"):
            raw = field(soup, f"UM_DERIVED_SR_{name}${index}").replace(",", "")
            if not re.fullmatch(r"\d+", raw):
                raise SourceError("SPIRE search counts missing or unrecognized")
            counts.append(int(raw))
        meetings = soup.find(id=f"win0divSSR_CLSRCH_MTG1${index}")
        if meetings is None:
            raise SourceError("SPIRE search meetings missing")
        rows.append(
            {
                "action": link["id"],
                "section": match[1],
                "component": match[2],
                "class_number": match[3],
                "status": status,
                "capacity": counts[0],
                "enrolled": counts[1],
                "available_seats": counts[0] - counts[1],
                "search_structure": {
                    "session": field(soup, f"PSXLATITEM_XLATSHORTNAME${index}"),
                    "meetings": meetings.get_text(" ", strip=True),
                },
            }
        )
    if len({r["class_number"] for r in rows}) != len(rows):
        raise SourceError("Duplicate SPIRE class identifiers")
    return rows


def fast_sections(rows, cached, term, subject, catalog_number):
    if not cached or not cached.get("search_counts_validated"):
        return None
    if (cached.get("term"), cached.get("subject"), cached.get("catalog_number")) != (
        term,
        subject,
        catalog_number,
    ):
        return None
    try:
        validated = datetime.fromisoformat(cached["details_validated_at"])
        if validated.tzinfo is None or (datetime.now(UTC) - validated).total_seconds() >= (
            settings().detail_refresh_seconds
        ):
            return None
        old = {section["class_number"]: section for section in cached["sections"]}
        if set(old) != {r["class_number"] for r in rows}:
            return None
        sections = []
        for row in rows:
            previous = old[row["class_number"]]
            if any(
                previous.get(key) != row[key]
                for key in ("section", "component", "search_structure", "capacity")
            ):
                return None
            updated = deepcopy(previous)
            updated.update(
                {k: row[k] for k in ("status", "capacity", "enrolled", "available_seats")}
            )
            sections.append(updated)
        return sections
    except (KeyError, ValueError, TypeError):
        return None


class SpireClient:
    def __init__(self):
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=40,
            headers={"User-Agent": "UMassCourseWatch/0.1"},
            event_hooks={"request": [self.before_http_request]},
        )
        self.soup = None
        self.last_request = 0.0
        self.request_count = 0
        self.before_request = None
        self.cached_snapshot = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, url, **kwargs):
        if urlparse(url).hostname != urlparse(BASE).hostname:
            raise SourceError("Unexpected SPIRE form host")
        response = self.client.request(method, url, **kwargs)
        response.raise_for_status()
        if urlparse(str(response.url)).hostname != urlparse(BASE).hostname:
            raise SourceError("SPIRE redirected outside the guest interface")
        self.soup = BeautifulSoup(response.text, "html.parser")
        return response.text

    def before_http_request(self, request):
        if request.url.host != urlparse(BASE).hostname:
            raise SourceError("Unexpected SPIRE redirect host")
        time.sleep(
            max(
                0,
                settings().spire_request_interval_seconds - (time.monotonic() - self.last_request),
            )
        )
        if self.before_request:
            self.before_request()
        self.last_request = time.monotonic()
        self.request_count += 1

    def start(self):
        self.request(
            "POST", GUEST_URL, data={"userid": "GUEST", "pwd": "GUEST", "timezoneOffset": "240"}
        )
        frame = self.soup.find("iframe", id="ptifrmtgtframe")
        if not frame:
            raise SourceError("SPIRE guest portal did not expose class search")
        self.request("GET", urljoin(BASE, frame["src"]))
        form_data(self.soup)

    def action(self, action, **overrides):
        url, data = form_data(self.soup)
        data.update(overrides)
        data["ICAction"] = action
        return self.request("POST", urljoin(BASE, url), data=data)

    def collect(self, term, subject, catalog_number):
        self.start()
        select = self.soup.find("select", id="UM_DERIVED_SA_UM_TERM_DESCR")
        if not select:
            raise SourceError("SPIRE term selector missing")
        option = next(
            (o for o in select.find_all("option") if o.get_text(" ", strip=True) == term), None
        )
        if not option:
            raise TermUnavailable(f"{term} is not published in SPIRE guest search yet")
        self.action(
            "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH",
            **{
                "UM_DERIVED_SA_UM_TERM_DESCR": option["value"],
                "CLASS_SRCH_WRK2_SUBJECT$108$": subject,
                "CLASS_SRCH_WRK2_CATALOG_NBR$8$": catalog_number,
                "CLASS_SRCH_WRK2_SSR_EXACT_MATCH1": "E",
                "CLASS_SRCH_WRK2_SSR_OPEN_ONLY$chk": "N",
                "CLASS_SRCH_WRK2_SSR_OPEN_ONLY": "N",
            },
        )
        content = self.soup.get_text(" ", strip=True)
        year, season = term.split(" ", 1)
        if f"{season} {year}" not in field(self.soup, "DERIVED_CLSRCH_SSS_PAGE_KEYDESCR"):
            raise SourceError("SPIRE returned results for the wrong term")
        if "The search returns no results" in content or "No classes found" in content:
            raise CourseUnavailable("No sections published for this course")
        # Require the requested course identity somewhere in the result heading.
        if not re.search(rf"\b{re.escape(subject)}\s+{re.escape(catalog_number)}\b", content):
            raise SourceError("SPIRE result course identity could not be verified")
        rows = parse_search_rows(self.soup)
        sections = fast_sections(rows, self.cached_snapshot, term, subject, catalog_number)
        mode = "search"
        if sections is not None:
            bundles, issues = build_bundles(sections)
            previously_open = {b["key"] for b in self.cached_snapshot["bundles"] if b["open"]}
            newly_open = {b["key"] for b in bundles if b["open"]} - previously_open
            if newly_open:
                sections = None
        if sections is None:
            mode = "details"
            sections = []
            for row in rows:
                identity = {key: row[key] for key in ("section", "component", "class_number")}
                html = self.action(row["action"])
                detail = parse_detail(html, identity)
                detail["search_structure"] = row["search_structure"]
                sections.append(detail)
                self.action("CLASS_SRCH_WRK2_SSR_PB_BACK")
            bundles, issues = build_bundles(sections)
            counts_validated = all(
                all(
                    detail[key] == row[key]
                    for key in ("status", "capacity", "enrolled", "available_seats")
                )
                for row, detail in zip(rows, sections, strict=True)
            )
            validated_at = datetime.now(UTC).isoformat()
        else:
            counts_validated = True
            validated_at = self.cached_snapshot["details_validated_at"]
        return {
            "collection_mode": mode,
            "details_validated_at": validated_at,
            "search_counts_validated": counts_validated,
            "term": term,
            "subject": subject,
            "catalog_number": catalog_number,
            "sections": sections,
            "bundles": bundles,
            "issues": issues,
            "pairing_note": "Related sections inferred from section labels; "
            "verify compatibility and eligibility in SPIRE.",
        }


def main():
    parser = argparse.ArgumentParser(description="Read one course from public SPIRE guest search")
    parser.add_argument("catalog_number")
    parser.add_argument("--term", default="2027 Spring")
    parser.add_argument("--subject", default="COMPSCI")
    args = parser.parse_args()
    try:
        with SpireClient() as client:
            print(
                json.dumps(client.collect(args.term, args.subject, args.catalog_number), indent=2)
            )
    except (SourceError, httpx.HTTPError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
