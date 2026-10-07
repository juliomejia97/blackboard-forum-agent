from datetime import UTC, datetime

from bbagent.models import Forum, Message, Thread
from bbagent.store import Store

T0 = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)


def msg(id, mine=False, parent=None, read=True) -> Message:
    return Message(
        id=id,
        thread_id="t1",
        forum_id="f1",
        course_id="c1",
        parent_id=parent,
        author_id="me" if mine else "other",
        author_name="Me" if mine else "Other",
        is_mine=mine,
        body_text="some body",
        word_count=2,
        created_at=T0,
        is_read=read,
    )


def forum(*messages: Message, title="Forum") -> Forum:
    thread = Thread(
        id="t1",
        forum_id="f1",
        course_id="c1",
        title="1. Q",
        prompt_text="prompt",
        word_limit=(100, 150),
        is_optional=False,
        messages=list(messages),
    )
    return Forum(id="f1", course_id="c1", title=title, due_at=T1, threads=[thread])


def count(store: Store, table: str) -> int:
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_upsert_is_idempotent():
    store = Store(":memory:")
    run = store.start_run(T0)
    for _ in range(2):
        store.upsert_forum(forum(msg("1"), msg("2", mine=True)), run, session_number=1, now=T0)
    assert count(store, "forums") == 1
    assert count(store, "threads") == 1
    assert count(store, "messages") == 2


def test_upsert_updates_changed_fields():
    store = Store(":memory:")
    run = store.start_run(T0)
    store.upsert_forum(forum(msg("1", read=False), title="Old"), run, session_number=1, now=T0)
    store.upsert_forum(forum(msg("1", read=True), title="New"), run, session_number=2, now=T1)
    row = store.conn.execute("SELECT title, session_number FROM forums").fetchone()
    assert tuple(row) == ("New", 2)
    assert store.conn.execute("SELECT is_read FROM messages").fetchone()[0] == 1


def test_new_message_ids_only_for_current_run():
    store = Store(":memory:")
    run1 = store.start_run(T0)
    store.upsert_forum(forum(msg("1")), run1, now=T0)
    assert store.new_message_ids("t1", run1) == {"1"}
    run2 = store.start_run(T1)
    store.upsert_forum(forum(msg("1"), msg("2")), run2, now=T1)
    assert store.new_message_ids("t1", run2) == {"2"}
    assert store.new_message_ids("t1", run1) == {"1"}


def test_reupsert_same_run_keeps_new_set():
    store = Store(":memory:")
    run = store.start_run(T0)
    store.upsert_forum(forum(msg("1")), run, now=T0)
    store.upsert_forum(forum(msg("1")), run, now=T0)
    assert store.new_message_ids("t1", run) == {"1"}


def test_new_message_ids_scoped_to_thread():
    store = Store(":memory:")
    run = store.start_run(T0)
    store.upsert_forum(forum(msg("1")), run, now=T0)
    assert store.new_message_ids("other-thread", run) == set()


def test_record_run():
    store = Store(":memory:")
    run = store.start_run(T0)
    store.record_run(run, T1, "ok", "out/2026-10-06.json")
    row = store.conn.execute("SELECT outcome, snapshot_path, finished_at FROM runs").fetchone()
    assert tuple(row) == ("ok", "out/2026-10-06.json", T1.isoformat())


def test_set_evaluation():
    store = Store(":memory:")
    run = store.start_run(T0)
    store.upsert_forum(forum(msg("1")), run, now=T0)
    store.set_evaluation("t1", "needs_post", "high", T1)
    row = store.conn.execute("SELECT last_status, last_priority FROM threads").fetchone()
    assert tuple(row) == ("needs_post", "high")


def test_file_backed_store_persists(tmp_path):
    path = tmp_path / "sub" / "bbagent.db"
    store = Store(path)
    run = store.start_run(T0)
    store.upsert_forum(forum(msg("1")), run, now=T0)
    store.close()
    assert Store(path).new_message_ids("t1", run) == {"1"}
