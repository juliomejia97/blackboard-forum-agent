from datetime import UTC, datetime, timedelta

import pytest

from bbagent.config import CourseConfig
from bbagent.models import Forum, Message, Thread
from bbagent.rules import evaluate_forum

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
WORDS_OK = 40
WORDS_SHORT = 10


def course(scope="forum", min_replies=2, min_words=30) -> CourseConfig:
    return CourseConfig(
        bb_course_id="c1",
        name="Course",
        notion_course_url="https://example.test",
        min_replies=min_replies,
        reply_scope=scope,
        min_reply_words=min_words,
        ai_declaration="x",
    )


def msg(id, thread="t1", parent=None, mine=False, words=WORDS_OK) -> Message:
    return Message(
        id=id,
        thread_id=thread,
        forum_id="f1",
        course_id="c1",
        parent_id=parent,
        author_id="me" if mine else f"a-{id}",
        author_name="Me" if mine else f"Author {id}",
        is_mine=mine,
        body_text="w " * words,
        word_count=words,
        created_at=NOW,
    )


def thread(id="t1", messages=(), optional=False, root=None) -> Thread:
    return Thread(
        id=id,
        forum_id="f1",
        course_id="c1",
        title=id,
        is_optional=optional,
        root_message_id=root,
        messages=list(messages),
    )


def forum(threads, due_in_hours: float | None = 24) -> Forum:
    due = None if due_in_hours is None else NOW + timedelta(hours=due_in_hours)
    return Forum(id="f1", course_id="c1", title="F", due_at=due, threads=threads)


def ev(cfg, f, thread_id="t1"):
    return {e.thread_id: e for e in evaluate_forum(cfg, f, NOW)}[thread_id]


def test_needs_post_high_when_due_within_48h():
    e = ev(course(), forum([thread(messages=[msg("r", mine=False)], root="r")], 24))
    assert (e.status, e.priority, e.has_own_post) == ("needs_post", "high", False)


def test_needs_post_low_when_due_after_48h():
    e = ev(course(), forum([thread()], 72))
    assert (e.status, e.priority) == ("needs_post", "low")


def test_needs_post_high_at_exactly_48h():
    e = ev(course(), forum([thread()], 48))
    assert e.priority == "high"


def test_needs_replies_thread_scope_ignores_other_threads():
    t1 = thread("t1", [msg("p1", "t1", mine=True), msg("c1", "t1")], root=None)
    t2 = thread(
        "t2",
        [msg("p2", "t2", mine=True), msg("c2", "t2"), msg("r2", "t2", "c2", mine=True)],
    )
    t1.messages.append(msg("r1", "t1", "c1", mine=True))
    cfg = course(scope="thread", min_replies=2)
    e = ev(cfg, forum([t1, t2]))
    assert (e.status, e.reply_count, e.replies_required) == ("needs_replies", 1, 2)
    assert e.priority == "medium"


def test_forum_scope_counts_across_threads_and_done():
    t1 = thread("t1", [msg("p1", "t1", mine=True), msg("c1", "t1"), msg("r1", "t1", "c1", True)])
    t2 = thread("t2", [msg("p2", "t2", mine=True), msg("c2", "t2"), msg("r2", "t2", "c2", True)])
    f = forum([t1, t2])
    for tid in ("t1", "t2"):
        e = ev(course(scope="forum"), f, tid)
        assert (e.status, e.reply_count, e.priority) == ("done", 2, "low")


def test_forum_scope_needs_replies_when_threshold_not_met():
    t1 = thread("t1", [msg("p1", "t1", mine=True), msg("c1", "t1"), msg("r1", "t1", "c1", True)])
    e = ev(course(scope="forum"), forum([t1]))
    assert (e.status, e.reply_count) == ("needs_replies", 1)


def test_short_reply_not_counted():
    t = thread(
        messages=[
            msg("p", mine=True),
            msg("c"),
            msg("r", parent="c", mine=True, words=WORDS_SHORT),
        ]
    )
    e = ev(course(min_replies=1), forum([t]))
    assert (e.status, e.reply_count) == ("needs_replies", 0)


def test_reply_to_own_message_not_counted():
    t = thread(messages=[msg("p", mine=True), msg("r", parent="p", mine=True)])
    e = ev(course(min_replies=1), forum([t]))
    assert (e.status, e.reply_count) == ("needs_replies", 0)


def test_reply_to_unknown_parent_not_counted():
    t = thread(messages=[msg("p", mine=True), msg("r", parent="ghost", mine=True)])
    e = ev(course(min_replies=1), forum([t]))
    assert e.reply_count == 0


def test_own_post_requires_root_or_no_parent():
    t = thread(messages=[msg("root"), msg("x", parent="root", mine=True)], root="root")
    assert ev(course(), forum([t])).has_own_post is True
    t2 = thread(messages=[msg("c"), msg("x", parent="c", mine=True)], root="root")
    assert ev(course(), forum([t2])).has_own_post is False


def test_optional_never_blocks():
    e = ev(course(), forum([thread(optional=True)], 10))
    assert (e.status, e.priority) == ("optional", "low")


def test_received_reply_gives_medium():
    t = thread(messages=[msg("p", mine=True), msg("c", parent="p")])
    e = ev(course(min_replies=0), forum([t], 200))
    assert (e.status, e.received_replies, e.priority) == ("done", 1, "low")
    e = ev(course(min_replies=1), forum([t], 200))
    assert (e.status, e.received_replies, e.priority) == ("needs_replies", 1, "medium")


@pytest.mark.parametrize(
    ("status_thread", "expected_priority", "expected_flag"),
    [
        ("needs_post", "medium", True),
        ("done", "low", False),
        ("optional", "low", False),
    ],
)
def test_missing_deadline(status_thread, expected_priority, expected_flag):
    if status_thread == "needs_post":
        t = thread()
    elif status_thread == "done":
        t = thread(messages=[msg("p", mine=True)])
    else:
        t = thread(optional=True)
    cfg = course(min_replies=0)
    e = ev(cfg, forum([t], None))
    assert (e.status, e.priority, e.missing_deadline) == (
        status_thread,
        expected_priority,
        expected_flag,
    )


def test_closed_forum_is_low():
    e = ev(course(), forum([thread()], -1))
    assert (e.status, e.priority, e.closed) == ("needs_post", "low", True)
