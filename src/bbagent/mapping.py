import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_RECAP = re.compile(r"recap\s+of\s+session\s+(\d+)", re.IGNORECASE)
_SESSION = re.compile(r"(?:session|sesi[oó]n|week|semana)\s+(\d+)", re.IGNORECASE)

_DASH = r"(?:-|–|—|to)"
_RANGE = re.compile(rf"(\d+)\s*{_DASH}\s*(\d+)\s*words?", re.IGNORECASE)
_MAX = re.compile(r"(?:max(?:imum)?|up\s+to|at\s+most|no\s+more\s+than)\s*(\d+)\s*words?", re.I)


def session_number(forum_title: str) -> int | None:
    """Session number a forum refers to; "Recap of Session N" takes precedence."""
    match = _RECAP.search(forum_title) or _SESSION.search(forum_title)
    return int(match.group(1)) if match else None


def word_limit(prompt_text: str) -> tuple[int, int] | None:
    """(min, max) words requested by a prompt, or None when it states no limit."""
    if match := _RANGE.search(prompt_text):
        return int(match.group(1)), int(match.group(2))
    if match := _MAX.search(prompt_text):
        return 0, int(match.group(1))
    return None


_MONTHS = {
    **{m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)},
    **{m: i for i, m in enumerate("ene feb mar abr may jun jul ago sep oct nov dic".split(), 1)},
}
_WEEKDAYS = {
    **{
        d: i
        for i, d in enumerate("monday tuesday wednesday thursday friday saturday sunday".split())
    },
    **{d: i for i, d in enumerate("lunes martes miercoles jueves viernes sabado domingo".split())},
}
_KEYWORD = r"(?:until|due|by|before|deadline|closes?|hasta|antes\s+del?)"
_WEEKDAY = r"(?P<wd>[a-zé]{5,9})?\s*,?\s*"
_DAY_MONTH = r"(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+|de\s+)?(?P<m1>[a-zé]{3,})"
_MONTH_DAY = r"(?P<m2>[a-zé]{3,})\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?"
_NUMERIC = r"(?P<d3>\d{1,2})[/.](?P<m3>\d{1,2})"
_DUE = re.compile(
    rf"{_KEYWORD}\s+(?:el\s+)?{_WEEKDAY}(?:{_DAY_MONTH}|{_MONTH_DAY}|{_NUMERIC})",
    re.IGNORECASE,
)
_MADRID = ZoneInfo("Europe/Madrid")
_GRACE = timedelta(days=62)


def _month(token: str) -> int | None:
    return _MONTHS.get(token.lower()[:3])


def _weekday(token: str | None) -> int | None:
    if not token:
        return None
    return _WEEKDAYS.get(token.lower().replace("é", "e").replace("á", "a"))


def due_date_from_title(title: str, reference: datetime) -> datetime | None:
    """Deadline written in a forum title, as 23:59 Europe/Madrid, or None.

    Accepts "until Friday 9 October", "until 9 Oct", "due 12/10" (day/month). The year is the
    one that puts the date closest to `reference`, ignoring dates more than ~2 months before it.
    """
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=_MADRID)
    for match in _DUE.finditer(title):
        if match["d1"]:
            day, month = int(match["d1"]), _month(match["m1"])
        elif match["d2"]:
            day, month = int(match["d2"]), _month(match["m2"])
        else:
            day, month = int(match["d3"]), int(match["m3"])
        if month is None or not 1 <= month <= 12:
            continue
        weekday = _weekday(match["wd"])
        candidates = []
        for year in (reference.year - 1, reference.year, reference.year + 1):
            try:
                candidates.append(datetime(year, month, day, 23, 59, tzinfo=_MADRID))
            except ValueError:
                continue
        if weekday is not None and any(c.weekday() == weekday for c in candidates):
            candidates = [c for c in candidates if c.weekday() == weekday]
        candidates = [c for c in candidates if c >= reference - _GRACE]
        if candidates:
            return min(candidates, key=lambda c: abs(c - reference))
    return None
