from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bbagent.mapping import due_date_from_title, session_number, word_limit


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("SESSION 2. Recap of Session 1", 1),
        ("session 2. recap of session 1", 1),
        ("Week 3 discussion", 3),
        ("Session 4 forum", 4),
        ("Semana 5 foro", 5),
        ("Sesión 6", 6),
        ("General discussion", None),
        ("", None),
    ],
)
def test_session_number(title, expected):
    assert session_number(title) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Write a short post (200–300 words)", (200, 300)),
        ("Write a short post (200-300 words)", (200, 300)),
        ("Write 200 to 300 words", (200, 300)),
        ("max 150 words", (0, 150)),
        ("Answer in up to 150 words.", (0, 150)),
        ("Discuss the topic", None),
    ],
)
def test_word_limit(text, expected):
    assert word_limit(text) == expected


MADRID = ZoneInfo("Europe/Madrid")
REF = datetime(2026, 10, 6, 12, 0, tzinfo=MADRID)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "SESSION 2. Recap of Session 1 · open until Friday 9 October",
            datetime(2026, 10, 9, 23, 59, tzinfo=MADRID),
        ),
        ("Week 3 · until 9 Oct", datetime(2026, 10, 9, 23, 59, tzinfo=MADRID)),
        ("Week 3 · due 12/10", datetime(2026, 10, 12, 23, 59, tzinfo=MADRID)),
        ("Forum open until 3rd of November", datetime(2026, 11, 3, 23, 59, tzinfo=MADRID)),
        ("Forum due October 9", datetime(2026, 10, 9, 23, 59, tzinfo=MADRID)),
        ("Forum hasta el 9 de octubre", datetime(2026, 10, 9, 23, 59, tzinfo=MADRID)),
        # year rollover: a January date seen in December belongs to next year
        ("until 8 January", datetime(2027, 1, 8, 23, 59, tzinfo=MADRID)),
        ("No deadline here", None),
        ("until 31 February", None),
    ],
)
def test_due_date_from_title(title, expected):
    assert due_date_from_title(title, REF) == expected


def test_due_date_recently_passed_is_kept():
    ref = datetime(2026, 10, 20, tzinfo=MADRID)
    assert due_date_from_title("until 9 October", ref) == datetime(
        2026, 10, 9, 23, 59, tzinfo=MADRID
    )


def test_due_date_year_boundary_backwards():
    ref = datetime(2027, 1, 5, tzinfo=MADRID)
    assert due_date_from_title("until 20 December", ref) == datetime(
        2026, 12, 20, 23, 59, tzinfo=MADRID
    )


def test_due_date_weekday_picks_matching_year():
    # 9 October is a Friday in 2026 but a Saturday in 2027
    ref = datetime(2026, 11, 20, tzinfo=MADRID)
    assert due_date_from_title("until Friday 9 October", ref).year == 2026


def test_due_date_too_old_is_none():
    ref = datetime(2026, 10, 6, tzinfo=MADRID)
    # 1 July 2026 is 97 days before ref, beyond the 2-month grace window
    assert due_date_from_title("until 1 July", ref) == datetime(2027, 7, 1, 23, 59, tzinfo=MADRID)
