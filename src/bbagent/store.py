import sqlite3
from datetime import datetime
from pathlib import Path

from bbagent.models import Forum

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    snapshot_path TEXT
);
CREATE TABLE IF NOT EXISTS forums (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    title TEXT NOT NULL,
    due_at TEXT,
    session_number INTEGER
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    forum_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    word_limit TEXT,
    is_optional INTEGER NOT NULL,
    last_status TEXT,
    last_priority TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    forum_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    parent_id TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    is_mine INTEGER NOT NULL,
    is_read INTEGER NOT NULL,
    body_text TEXT NOT NULL,
    word_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    first_seen_run INTEGER REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Store:
    """SQLite persistence for forums, threads, messages and collector runs."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def start_run(self, started_at: datetime) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (started_at.isoformat(),)
            )
        return int(cursor.lastrowid)

    def record_run(
        self, run_id: int, finished_at: datetime, outcome: str, snapshot_path: str | None = None
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, outcome = ?, snapshot_path = ? WHERE id = ?",
                (finished_at.isoformat(), outcome, snapshot_path, run_id),
            )

    def upsert_forum(
        self,
        forum: Forum,
        run_id: int,
        session_number: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Idempotent. A message keeps the run and time at which it was first seen."""
        stamp = (now or datetime.now().astimezone()).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO forums (id, course_id, title, due_at, session_number)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET course_id = excluded.course_id,"
                " title = excluded.title, due_at = excluded.due_at,"
                " session_number = excluded.session_number",
                (forum.id, forum.course_id, forum.title, _iso(forum.due_at), session_number),
            )
            for thread in forum.threads:
                limit = (
                    f"{thread.word_limit[0]}-{thread.word_limit[1]}" if thread.word_limit else None
                )
                self.conn.execute(
                    "INSERT INTO threads (id, forum_id, course_id, title, prompt_text, word_limit,"
                    " is_optional, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET forum_id = excluded.forum_id,"
                    " course_id = excluded.course_id, title = excluded.title,"
                    " prompt_text = excluded.prompt_text, word_limit = excluded.word_limit,"
                    " is_optional = excluded.is_optional, updated_at = excluded.updated_at",
                    (
                        thread.id,
                        thread.forum_id,
                        thread.course_id,
                        thread.title,
                        thread.prompt_text,
                        limit,
                        int(thread.is_optional),
                        stamp,
                    ),
                )
                for m in thread.messages:
                    self.conn.execute(
                        "INSERT INTO messages (id, thread_id, forum_id, course_id, parent_id,"
                        " author_id, author_name, is_mine, is_read, body_text, word_count,"
                        " created_at, first_seen_at, first_seen_run)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(id) DO UPDATE SET parent_id = excluded.parent_id,"
                        " author_name = excluded.author_name, is_mine = excluded.is_mine,"
                        " is_read = excluded.is_read, body_text = excluded.body_text,"
                        " word_count = excluded.word_count",
                        (
                            m.id,
                            m.thread_id,
                            m.forum_id,
                            m.course_id,
                            m.parent_id,
                            m.author_id,
                            m.author_name,
                            int(m.is_mine),
                            int(m.is_read),
                            m.body_text,
                            m.word_count,
                            m.created_at.isoformat(),
                            stamp,
                            run_id,
                        ),
                    )

    def new_message_ids(self, thread_id: str, run_id: int) -> set[str]:
        """Ids of the thread's messages first seen during `run_id`."""
        rows = self.conn.execute(
            "SELECT id FROM messages WHERE thread_id = ? AND first_seen_run = ?",
            (thread_id, run_id),
        )
        return {row["id"] for row in rows}

    def set_evaluation(
        self, thread_id: str, status: str, priority: str, now: datetime | None = None
    ) -> None:
        stamp = (now or datetime.now().astimezone()).isoformat()
        with self.conn:
            self.conn.execute(
                "UPDATE threads SET last_status = ?, last_priority = ?, updated_at = ?"
                " WHERE id = ?",
                (status, priority, stamp, thread_id),
            )
