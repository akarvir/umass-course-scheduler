import itertools
import re
from datetime import datetime

DAY_PATTERN = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)+"
TIME_PATTERN = r"\d{1,2}:\d{2}[AP]M"


def meeting_intervals(value: str):
    match = re.fullmatch(rf"({DAY_PATTERN})\s+({TIME_PATTERN})\s*-\s*({TIME_PATTERN})", value)
    if not match:
        return None
    days = set(re.findall(r"Mo|Tu|We|Th|Fr|Sa|Su", match[1]))
    times = []
    for part in (match[2], match[3]):
        parsed = datetime.strptime(part, "%I:%M%p")
        times.append(parsed.hour * 60 + parsed.minute)
    return days, *times


def no_conflicts(sections):
    for a, b in itertools.combinations(sections, 2):
        for first, second in itertools.product(a["meetings"], b["meetings"]):
            left, right = meeting_intervals(first), meeting_intervals(second)
            if left and right and left[0] & right[0] and left[1] < right[2] and right[1] < left[2]:
                return False
    return True


def build_bundles(sections: list[dict]):
    """Infer related components from unambiguous section prefixes within a session."""
    bundles = []
    issues = set()
    for primary in sections:
        if primary["component"] not in {"LEC", "SEM", "IND", "STU", "LAB", "DIS"}:
            continue
        required = set(primary["required_components"])
        # Lecture is the anchor when a lecture is required.
        if "LEC" in required and primary["component"] != "LEC":
            continue
        if "SEM" in required and "LEC" not in required and primary["component"] != "SEM":
            continue
        if not required or primary["component"] not in required:
            issues.add("Required components could not be resolved")
            continue
        siblings = [s for s in sections if s["session"] == primary["session"]]
        anchors = [s for s in siblings if s["component"] == primary["component"]]
        groups = [[primary]]
        for component in sorted(required - {primary["component"]}):
            matches = []
            for candidate in siblings:
                if candidate["component"] != component:
                    continue
                possible = [a for a in anchors if candidate["section"].startswith(a["section"])]
                if len(possible) == 1 and possible[0]["class_number"] == primary["class_number"]:
                    matches.append(candidate)
            if not matches:
                issues.add(f"No unambiguous {component} pairing for {primary['section']}")
                break
            groups.append(matches)
        else:
            if not all(groups):
                continue
            for combo in itertools.product(*groups):
                if len({s["class_number"] for s in combo}) != len(combo) or not no_conflicts(combo):
                    continue
                numbers = sorted(s["class_number"] for s in combo)
                bundles.append(
                    {
                        "key": ":".join(numbers),
                        "class_numbers": numbers,
                        "open": all(
                            s["status"] == "Open" and s["available_seats"] > 0 for s in combo
                        ),
                        "inferred": len(combo) > 1,
                        "schedule_unknown": any(
                            not s["meetings"]
                            or any(meeting_intervals(m) is None for m in s["meetings"])
                            for s in combo
                        ),
                    }
                )
                if len(bundles) >= 500:
                    issues.add("Pair list limited to 500 combinations")
                    return bundles, sorted(issues)
    return list({b["key"]: b for b in bundles}.values()), sorted(issues)


def open_bundles(snapshot: dict, class_number: str = ""):
    return [
        b
        for b in snapshot["bundles"]
        if b["open"] and (not class_number or class_number in b["class_numbers"])
    ]
