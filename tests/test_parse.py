import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bbagent.parse import SchemaError, count_words, html_to_text, parse_forums, parse_thread

FIXTURES = Path(__file__).parent / "fixtures"
COURSE = "_152245_1"
ME = "A00000001"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def forums_json():
    return load("forums.json")


@pytest.fixture
def launch_json():
    return load("thread_launch.json")


@pytest.fixture
def forum(forums_json):
    return parse_forums(forums_json, COURSE)[0]


def test_html_to_text_blocks_and_entities():
    html = "<p>Hello&nbsp;<strong>big</strong> world</p><p>Tom &amp; Jerry</p>"
    assert html_to_text(html) == "Hello big world\nTom & Jerry"


def test_html_to_text_table_cells_are_separated():
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>one</td><td>two</td></tr></table>"
    assert html_to_text(html) == "| A | B |\n| --- | --- |\n| one | two |"


def test_html_to_text_inline_tags_do_not_split_words():
    assert html_to_text("<p>un<em>believ</em>able</p>") == "unbelievable"


@pytest.mark.parametrize("html", ["", "   ", "<p> </p>", "<br>"])
def test_html_to_text_empty(html):
    assert html_to_text(html) == ""


def test_parse_forums(forum):
    assert forum.id == "11763"
    assert forum.course_id == COURSE
    assert forum.title.startswith("SESSION 2. Recap of Session 1")
    assert forum.max_chars is None
    assert forum.opens_at == datetime.fromtimestamp(1791143602.508, UTC)
    assert forum.due_at == datetime(2026, 10, 9, 23, 59, tzinfo=ZoneInfo("Europe/Madrid"))
    assert [t.id for t in forum.threads] == ["47440", "47441", "47442", "47443"]
    assert all(t.messages == [] and t.forum_id == "11763" for t in forum.threads)
    assert all(t.course_id == COURSE for t in forum.threads)


def test_parse_forums_flags_optional_theme(forum):
    assert [t.is_optional for t in forum.threads] == [False, False, False, True]


def test_parse_forums_end_date_wins(forums_json):
    forums_json["data"][0]["endDate"] = 1791500000000
    forum = parse_forums(forums_json, COURSE)[0]
    assert forum.due_at == datetime.fromtimestamp(1791500000, UTC)


def test_parse_forums_title_with_optional_marks_all_threads(forums_json):
    forums_json["data"][0]["title"] += " (optional)"
    assert all(t.is_optional for t in parse_forums(forums_json, COURSE)[0].threads)


def test_parse_forums_missing_key_raises(forums_json):
    del forums_json["data"][0]["ltiThreads"][2]["title"]
    with pytest.raises(SchemaError) as err:
        parse_forums(forums_json, COURSE)
    assert "data[0].ltiThreads[2].title" in str(err.value)


def test_parse_forums_without_data_raises():
    with pytest.raises(SchemaError):
        parse_forums({}, COURSE)


def test_parse_thread_messages(forum, launch_json):
    thread = parse_thread(launch_json, forum, ME, set())
    assert thread.id == "47441"
    assert thread.forum_id == "11763"
    assert thread.title == "2. A responsibility map"
    assert thread.is_optional is False
    assert thread.prompt_text and "<" not in thread.prompt_text
    assert len(thread.messages) == 27
    top = [m for m in thread.messages if m.parent_id is None]
    assert len(top) == 14
    ids = {m.id for m in thread.messages}
    replies = [m for m in thread.messages if m.parent_id is not None]
    assert all(m.parent_id in ids for m in replies)
    assert all(isinstance(m.id, str) and isinstance(m.author_id, str) for m in thread.messages)
    mine = [m for m in thread.messages if m.is_mine]
    assert len(mine) == 4
    assert sum(1 for m in mine if m.parent_id is None) == 1
    assert all(m.author_id == ME for m in mine)
    assert all(m.word_count == count_words(m.body_text) > 0 for m in thread.messages)
    assert all(m.is_read for m in thread.messages)


def test_parse_thread_instructor_ids(forum, launch_json):
    author = launch_json["data"]["ltiPosts"][0]["creator"]
    thread = parse_thread(launch_json, forum, ME, {author})
    flagged = [m for m in thread.messages if m.is_instructor]
    assert flagged and all(m.author_id == author for m in flagged)


def test_parse_thread_unread_flag(forum, launch_json):
    launch_json["data"]["ltiPosts"][0]["readed"] = False
    thread = parse_thread(launch_json, forum, ME, set())
    assert [m.is_read for m in thread.messages].count(False) == 1


def test_parse_thread_word_limit_from_prompt(forum, launch_json):
    launch_json["data"]["description"] = "<p>Answer in 150-200 words.</p>"
    assert parse_thread(launch_json, forum, ME, set()).word_limit == (150, 200)


def test_parse_thread_optional_by_title(forum, launch_json):
    launch_json["data"]["title"] = "theme B. Extra"
    assert parse_thread(launch_json, forum, ME, set()).is_optional is True


def test_parse_thread_missing_key_raises(forum, launch_json):
    broken = copy.deepcopy(launch_json)
    del broken["data"]["ltiPosts"][3]["postText"]
    with pytest.raises(SchemaError) as err:
        parse_thread(broken, forum, ME, set())
    assert "data.ltiPosts[3].postText" in str(err.value)


TABLE_HTML = (
    "<p>Intro</p><table><tr><th> Tool </th><th>Use | case</th></tr>"
    "<tr><td><b>MLflow</b></td><td>tracking<br>runs</td></tr>"
    "<tr><td>DVC</td></tr></table><p>After table</p>"
)


def test_html_to_text_table_becomes_markdown():
    assert html_to_text(TABLE_HTML) == (
        "Intro\n"
        "| Tool | Use \\| case |\n"
        "| --- | --- |\n"
        "| MLflow | tracking runs |\n"
        "| DVC | |\n"
        "After table"
    )


def test_html_to_text_empty_table_and_nested_table_do_not_crash():
    assert html_to_text("<table></table><p>x</p>") == "x"
    nested = "<table><tr><td>a<table><tr><td>b</td></tr></table></td></tr></table>"
    assert html_to_text(nested) == "| a b |\n| --- |"


def test_count_words_ignores_table_syntax():
    text = html_to_text(TABLE_HTML)
    # Intro, Tool, Use | case (3 tokens), MLflow, tracking runs, DVC, After table
    assert count_words(text) == 1 + 1 + 3 + 1 + 2 + 1 + 2
    assert count_words("plain words here") == 3
    assert count_words("") == 0
