import json
from datetime import UTC, datetime, timedelta

from bbagent.config import CourseConfig
from bbagent.models import Forum, Message, Thread
from bbagent.rules import evaluate_forum
from bbagent.snapshot import CourseResult, ForumResult, build_snapshot, write_snapshot

NOW = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
CREATED = NOW - timedelta(days=1)
COURSE = CourseConfig(
    bb_course_id="_152245_1",
    name="MBDS",
    notion_course_url="https://notion.example/course",
    ai_declaration="AI used for drafting.",
)


def msg(id, thread="t1", parent=None, mine=False, words=40, instructor=False) -> Message:
    return Message(
        id=id,
        thread_id=thread,
        forum_id="f1",
        course_id="_152245_1",
        parent_id=parent,
        author_id="me" if mine else f"a{id}",
        author_name="Me" if mine else f"Student {id}",
        is_mine=mine,
        body_text=f"body {id}",
        word_count=words,
        created_at=CREATED + timedelta(minutes=int(id)),
        is_instructor=instructor,
    )


def thread(id, messages, title="Q", optional=False) -> Thread:
    return Thread(
        id=id,
        forum_id="f1",
        course_id="_152245_1",
        title=title,
        prompt_text=f"prompt {id}",
        word_limit=(100, 150),
        is_optional=optional,
        messages=messages,
    )


def result(threads, due=NOW + timedelta(days=2), new=None, session=1) -> CourseResult:
    forum = Forum(id="f1", course_id="_152245_1", title="SESSION 2", due_at=due, threads=threads)
    evaluations = evaluate_forum(COURSE, forum, NOW)
    fr = ForumResult(forum, session, evaluations, new or {})
    return CourseResult(COURSE, [fr])


def test_only_non_done_threads_and_stable_keys():
    done = thread(
        "t1",
        [
            msg("1", mine=True),
            msg("2"),
            msg("3", parent="2", mine=True),
            msg("4", parent="2", mine=True),
        ],
    )
    pending = thread("t2", [msg("5", thread="t2")])
    snap = build_snapshot(NOW, [result([done, pending])])
    assert list(snap) == ["schema_version", "generated_at", "session_expired", "courses"]
    course = snap["courses"][0]
    assert [t["thread_id"] for t in course["threads"]] == ["t2"]
    t = course["threads"][0]
    assert t["status"] == "needs_post"
    assert t["priority"] == "high"
    assert t["word_limit"] == {"min": 100, "max": 150}
    assert t["session_number"] == 1
    assert t["notion_course_url"] == "https://notion.example/course"
    assert t["due_at"] == (NOW + timedelta(days=2)).isoformat()
    assert t["flags"]["has_own_post"] is False
    assert t["candidates"] == []
    assert course["pending_threads"] == 1
    assert course["highest_priority"] == "high"


def test_candidates_posts_replies_and_flags():
    t = thread(
        "t1",
        [
            msg("1", mine=True),
            msg("2"),
            msg("3"),
            msg("4", parent="1"),
            msg("5", parent="2", mine=True),
            msg("6", parent="4", instructor=True),
            msg("7", parent="3"),
        ],
    )
    snap = build_snapshot(NOW, [result([t], new={"t1": {"3", "4"}})])
    item = snap["courses"][0]["threads"][0]
    assert item["status"] == "needs_replies"
    assert [m["id"] for m in item["my_posts"]] == ["1"]
    assert [(m["id"], m["parent_id"]) for m in item["my_replies"]] == [("5", "2")]
    cands = {c["id"]: c for c in item["candidates"]}
    # 2 was already answered by me and is not new; instructor 6 is never a candidate;
    # 7 replies to a classmate, not to me
    assert set(cands) == {"3", "4"}
    assert cands["3"]["is_new"] is True
    assert cands["4"]["is_reply_to_me"] is True
    assert cands["3"]["is_reply_to_me"] is False
    assert cands["3"]["author_name"] == "Student 3"
    assert cands["3"]["body_text"] == "body 3"
    assert cands["3"]["word_count"] == 40


def test_ordering_by_priority_then_due():
    a = thread("a", [msg("1", thread="a", mine=True)])  # needs_replies -> medium
    b = thread("b", [])  # needs_post, due in 2 days -> high
    c = thread("c", [], optional=True)  # optional -> low
    snap = build_snapshot(NOW, [result([c, a, b])])
    assert [t["thread_id"] for t in snap["courses"][0]["threads"]] == ["b", "a", "c"]


def test_missing_deadline_flag():
    snap = build_snapshot(NOW, [result([thread("t1", [])], due=None)])
    item = snap["courses"][0]["threads"][0]
    assert item["flags"]["missing_deadline"] is True
    assert item["due_at"] is None


def test_empty_course_and_expired():
    empty = build_snapshot(NOW, [result([])])
    assert empty["courses"][0]["threads"] == []
    assert empty["courses"][0]["highest_priority"] is None
    expired = build_snapshot(NOW, [], session_expired=True)
    assert expired["session_expired"] is True
    assert expired["courses"] == []


def test_build_is_deterministic():
    t = thread("t1", [msg("1", mine=True), msg("2"), msg("3")])
    one = json.dumps(build_snapshot(NOW, [result([t])]))
    two = json.dumps(build_snapshot(NOW, [result([t])]))
    assert one == two


def test_write_snapshot_atomic(tmp_path):
    snap = build_snapshot(NOW, [result([thread("t1", [])])])
    path = write_snapshot(snap, tmp_path / "out")
    assert path == tmp_path / "out" / "2026-10-07.json"
    assert json.loads(path.read_text(encoding="utf-8")) == snap
    assert [p.name for p in path.parent.iterdir()] == ["2026-10-07.json"]


def test_expired_run_does_not_overwrite_good_snapshot(tmp_path):
    good = build_snapshot(NOW, [result([thread("t1", [])])])
    path = write_snapshot(good, tmp_path)
    expired = build_snapshot(NOW + timedelta(hours=1), [], session_expired=True)
    assert write_snapshot(expired, tmp_path) == path
    assert json.loads(path.read_text(encoding="utf-8")) == good


def test_good_run_replaces_expired_snapshot(tmp_path):
    write_snapshot(build_snapshot(NOW, [], session_expired=True), tmp_path)
    good = build_snapshot(NOW + timedelta(hours=1), [result([thread("t1", [])])])
    path = write_snapshot(good, tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["session_expired"] is False


def test_record_includes_done_threads_with_own_messages():
    done = thread(
        "t1",
        [
            msg("1", mine=True),
            msg("2"),
            msg("3", parent="2", mine=True),
            msg("4", parent="2", mine=True),
        ],
    )
    pending = thread("t2", [msg("5", thread="t2")])
    course = build_snapshot(NOW, [result([done, pending])])["courses"][0]
    assert [t["thread_id"] for t in course["threads"]] == ["t2"]
    rec = {e["thread_id"]: e for e in course["record"]}
    assert set(rec) == {"t1", "t2"}
    assert rec["t1"]["status"] == "done"
    assert [p["id"] for p in rec["t1"]["my_posts"]] == ["1"]
    assert [r["id"] for r in rec["t1"]["my_replies"]] == ["3", "4"]
    assert rec["t1"]["my_replies"][0]["parent_author_name"] == "Student 2"
    assert "candidates" not in rec["t1"]
    assert rec["t2"]["my_posts"] == []


def test_record_window_excludes_forums_closed_over_7_days():
    t = thread("t1", [msg("1", mine=True)])
    old = build_snapshot(NOW, [result([t], due=NOW - timedelta(days=8))])
    assert old["courses"][0]["record"] == []
    recent = build_snapshot(NOW, [result([t], due=NOW - timedelta(days=6))])
    assert len(recent["courses"][0]["record"]) == 1
    no_due = build_snapshot(NOW, [result([t], due=None)])
    assert len(no_due["courses"][0]["record"]) == 1


def test_schema_version_is_3():
    assert build_snapshot(NOW, [])["schema_version"] == 3


def test_parent_is_mine_flags_own_comment_on_own_post_and_it_is_not_progress():
    t = thread(
        "t1",
        [
            msg("1", mine=True),
            msg("2"),
            msg("3", parent="1", mine=True),
            msg("4", parent="2", mine=True),
        ],
    )
    snap = build_snapshot(NOW, [result([t])])
    item = snap["courses"][0]["threads"][0]
    flags = {m["id"]: m["parent_is_mine"] for m in item["my_replies"]}
    assert flags == {"3": True, "4": False}
    assert item["replies"]["done"] == 1
    rec = snap["courses"][0]["record"][0]
    assert {m["id"]: m["parent_is_mine"] for m in rec["my_replies"]} == flags
