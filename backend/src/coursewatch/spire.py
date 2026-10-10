import argparse
import json
import re
import time
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


class SpireClient:
    def __init__(self):
        self.client = httpx.Client(
            follow_redirects=True, timeout=40, headers={"User-Agent": "UMassCourseWatch/0.1"}
        )
        self.soup = None
        self.last_request = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, url, **kwargs):
        if urlparse(url).hostname != urlparse(BASE).hostname:
            raise SourceError("Unexpected SPIRE form host")
        time.sleep(
            max(
                0,
                settings().spire_request_interval_seconds - (time.monotonic() - self.last_request),
            )
        )
        self.last_request = time.monotonic()
        response = self.client.request(method, url, **kwargs)
        response.raise_for_status()
        if urlparse(str(response.url)).hostname != urlparse(BASE).hostname:
            raise SourceError("SPIRE redirected outside the guest interface")
        self.soup = BeautifulSoup(response.text, "html.parser")
        return response.text

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
        self.action("UM_DERIVED_SA_UM_TERM_DESCR", UM_DERIVED_SA_UM_TERM_DESCR=option["value"])
        self.action("CLASS_SRCH_WRK2_SUBJECT$108$", **{"CLASS_SRCH_WRK2_SUBJECT$108$": subject})
        self.action(
            "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH",
            **{
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
        links = self.soup.find_all("a", id=re.compile(r"^DERIVED_CLSRCH_SSR_CLASSNAME_LONG\$\d+$"))
        if not links:
            raise SourceError("SPIRE result rows missing; search may be interrupted")
        if len(links) > 80:
            raise SourceError("More than 80 sections; narrow collection before proceeding")
        identities = []
        for link in links:
            match = re.fullmatch(r"([A-Za-z0-9]+)-([A-Z]+)\((\d{5})\)", link.get_text(strip=True))
            if not match:
                raise SourceError("Unexpected section identifier")
            identities.append(
                {
                    "action": link["id"],
                    "section": match[1],
                    "component": match[2],
                    "class_number": match[3],
                }
            )
        sections = []
        for row in identities:
            action = row.pop("action")
            html = self.action(action)
            sections.append(parse_detail(html, row))
            self.action("CLASS_SRCH_WRK2_SSR_PB_BACK")
        bundles, issues = build_bundles(sections)
        return {
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
