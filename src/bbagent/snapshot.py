import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from bbagent.config import CourseConfig
from bbagent.models import Forum, Message, Thread
from bbagent.rules import ThreadEvaluation

SCHEMA_VERSION = 3
RECORD_DAYS = 7
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class ForumResult:
    forum: Forum
    session_number: int | None
    evaluations: list[ThreadEvaluation]
    new_message_ids: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CourseResult:
    course: CourseConfig
    forums: list[ForumResult]


def in_record_window(due_at: datetime | None, now: datetime) -> bool:
    """Forum open (or without due date) or closed at most RECORD_DAYS ago."""
    return due_at is None or due_at >= now - timedelta(days=RECORD_DAYS)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _is_own_post(thread: Thread, m: Message) -> bool:
    return m.is_mine and (m.parent_id is None or m.parent_id == thread.root_message_id)


def _own_post(m: Message) -> dict:
    return {
        "id": m.id,
        "created_at": _iso(m.created_at),
        "body_text": m.body_text,
        "word_count": m.word_count,
    }


def _own_reply(m: Message, by_id: dict[str, Message]) -> dict:
    parent = by_id.get(m.parent_id) if m.parent_id else None
    return {
        "id": m.id,
        "parent_id": m.parent_id,
        "parent_author_name": parent.author_name if parent else None,
        "parent_is_mine": parent is not None and parent.is_mine,
        "created_at": _iso(m.created_at),
        "body_text": m.body_text,
        "word_count": m.word_count,
    }


def _candidates(thread: Thread, by_id: dict[str, Message], new_ids: set[str]) -> list[dict]:
    answered = {m.parent_id for m in thread.messages if m.is_mine and m.parent_id}
    result = []
    for m in thread.messages:
        if m.is_mine or m.is_instructor:
            continue
        parent = by_id.get(m.parent_id) if m.parent_id else None
        top_level = m.parent_id is None or m.parent_id == thread.root_message_id
        reply_to_me = parent is not None and parent.is_mine
        if not (top_level or reply_to_me):
            continue
        is_new = m.id in new_ids
        if not (is_new or m.id not in answered):
            continue
        result.append(
            {
                "id": m.id,
                "parent_id": m.parent_id,
                "author_name": m.author_name,
                "created_at": _iso(m.created_at),
                "is_new": is_new,
                "is_reply_to_me": reply_to_me,
                "body_text": m.body_text,
                "word_count": m.word_count,
            }
        )
    return result


def _thread_entry(course: CourseConfig, fr: ForumResult, thread: Thread, ev: ThreadEvaluation):
    by_id = {m.id: m for m in thread.messages}
    ordered = sorted(thread.messages, key=lambda m: (m.created_at, m.id))
    sorted_thread = thread.model_copy(update={"messages": ordered})
    limit = (
        {"min": thread.word_limit[0], "max": thread.word_limit[1]} if thread.word_limit else None
    )
    return {
        "thread_id": thread.id,
        "forum_id": fr.forum.id,
        "forum_title": fr.forum.title,
        "title": thread.title,
        "prompt_text": thread.prompt_text,
        "word_limit": limit,
        "is_optional": thread.is_optional,
        "status": ev.status,
        "priority": ev.priority,
        "flags": {
            "missing_deadline": ev.missing_deadline,
            "closed": ev.closed,
            "has_own_post": ev.has_own_post,
        },
        "replies": {
            "done": ev.reply_count,
            "required": ev.replies_required,
            "received": ev.received_replies,
        },
        "due_at": _iso(fr.forum.due_at),
        "session_number": fr.session_number,
        "notion_course_url": course.notion_course_url,
        "my_posts": [_own_post(m) for m in ordered if _is_own_post(thread, m)],
        "my_replies": [
            _own_reply(m, by_id) for m in ordered if m.is_mine and not _is_own_post(thread, m)
        ],
        "candidates": (
            _candidates(sorted_thread, by_id, fr.new_message_ids.get(thread.id, set()))
            if ev.has_own_post
            else []
        ),
    }


def _record_entries(now: datetime, result: CourseResult) -> list[dict]:
    """All threads (done included) of forums open or closed at most RECORD_DAYS ago."""
    entries = []
    for fr in result.forums:
        due = fr.forum.due_at
        if not in_record_window(due, now):
            continue
        by_thread = {t.id: t for t in fr.forum.threads}
        for ev in fr.evaluations:
            thread = by_thread[ev.thread_id]
            by_id = {m.id: m for m in thread.messages}
            ordered = sorted(thread.messages, key=lambda m: (m.created_at, m.id))
            entries.append(
                {
                    "thread_id": thread.id,
                    "forum_id": fr.forum.id,
                    "forum_title": fr.forum.title,
                    "title": thread.title,
                    "status": ev.status,
                    "is_optional": thread.is_optional,
                    "due_at": _iso(due),
                    "session_number": fr.session_number,
                    "my_posts": [_own_post(m) for m in ordered if _is_own_post(thread, m)],
                    "my_replies": [
                        _own_reply(m, by_id)
                        for m in ordered
                        if m.is_mine and not _is_own_post(thread, m)
                    ],
                }
            )
    entries.sort(key=lambda e: (e["forum_id"], e["thread_id"]))
    return entries


def _course_entry(now: datetime, result: CourseResult) -> dict:
    course = result.course
    threads = []
    for fr in result.forums:
        by_thread = {t.id: t for t in fr.forum.threads}
        for ev in fr.evaluations:
            if ev.status != "done":
                threads.append(_thread_entry(course, fr, by_thread[ev.thread_id], ev))
    threads.sort(
        key=lambda t: (
            _PRIORITY_ORDER[t["priority"]],
            t["due_at"] is None,
            t["due_at"] or "",
            t["forum_id"],
            t["thread_id"],
        )
    )
    dues = sorted(t["due_at"] for t in threads if t["due_at"] and not t["flags"]["closed"])
    return {
        "bb_course_id": course.bb_course_id,
        "name": course.name,
        "notion_course_url": course.notion_course_url,
        "min_replies": course.min_replies,
        "reply_scope": course.reply_scope,
        "min_reply_words": course.min_reply_words,
        "ai_declaration": course.ai_declaration,
        "pending_threads": len(threads),
        "highest_priority": threads[0]["priority"] if threads else None,
        "next_due_at": dues[0] if dues else None,
        "threads": threads,
        "record": _record_entries(now, result),
    }


def build_snapshot(
    now: datetime, course_results: list[CourseResult], session_expired: bool = False
) -> dict:
    """Snapshot consumed by the drafter; documented in .claude/skills/forum-drafter/snapshot.md."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "session_expired": session_expired,
        "courses": [_course_entry(now, r) for r in course_results],
    }


def merge_with_existing(snapshot: dict, out_dir: Path | str) -> dict:
    """Snapshot whose courses replace same-id courses of the day's file and keep the others.

    Used by a single-course collect so it does not drop the other courses from out/YYYY-MM-DD.json.
    An absent, unreadable or session-expired file leaves the snapshot unchanged.
    """
    path = Path(out_dir) / f"{snapshot['generated_at'][:10]}.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return snapshot
    if not isinstance(existing, dict) or existing.get("session_expired") is not False:
        return snapshot
    new = {c["bb_course_id"]: c for c in snapshot["courses"]}
    courses = [new.pop(c["bb_course_id"], c) for c in existing.get("courses", [])]
    courses.extend(new.values())
    return {**snapshot, "courses": courses}


def write_snapshot(snapshot: dict, out_dir: Path | str) -> Path:
    """Write out/YYYY-MM-DD.json atomically; an expired-session run never replaces a good one."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot['generated_at'][:10]}.json"
    if snapshot["session_expired"] and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if existing.get("session_expired") is False:
            return path
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
