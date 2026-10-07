from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from bbagent.config import CourseConfig
from bbagent.models import Forum, Message, Thread

Status = Literal["needs_post", "needs_replies", "done", "optional"]
Priority = Literal["high", "medium", "low"]

HIGH_PRIORITY_WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class ThreadEvaluation:
    thread_id: str
    status: Status
    priority: Priority
    has_own_post: bool
    reply_count: int
    replies_required: int
    received_replies: int
    missing_deadline: bool
    closed: bool


def _has_own_post(thread: Thread) -> bool:
    return any(
        m.is_mine and (m.parent_id is None or m.parent_id == thread.root_message_id)
        for m in thread.messages
    )


def _relevant_replies(thread: Thread, by_id: dict[str, Message], min_words: int) -> int:
    count = 0
    for m in thread.messages:
        parent = by_id.get(m.parent_id) if m.parent_id else None
        if m.is_mine and parent is not None and not parent.is_mine and m.word_count >= min_words:
            count += 1
    return count


def _received_replies(thread: Thread, by_id: dict[str, Message]) -> int:
    return sum(
        1
        for m in thread.messages
        if not m.is_mine and m.parent_id in by_id and by_id[m.parent_id].is_mine
    )


def _priority(
    status: Status, received: int, due_at: datetime | None, now: datetime
) -> tuple[Priority, bool, bool]:
    """Return (priority, missing_deadline, closed)."""
    closed = due_at is not None and now > due_at
    if status in ("done", "optional") or closed:
        return "low", False, closed
    if due_at is None:
        return "medium", True, False
    if status == "needs_post" and due_at - now <= HIGH_PRIORITY_WINDOW:
        return "high", False, False
    if status == "needs_replies" or received > 0:
        return "medium", False, False
    return "low", False, False


def evaluate_forum(course: CourseConfig, forum: Forum, now: datetime) -> list[ThreadEvaluation]:
    by_id = {m.id: m for t in forum.threads for m in t.messages}
    forum_replies = sum(_relevant_replies(t, by_id, course.min_reply_words) for t in forum.threads)
    results = []
    for thread in forum.threads:
        has_post = _has_own_post(thread)
        reply_count = (
            forum_replies
            if course.reply_scope == "forum"
            else _relevant_replies(thread, by_id, course.min_reply_words)
        )
        received = _received_replies(thread, by_id)
        if thread.is_optional:
            status: Status = "optional"
        elif not has_post:
            status = "needs_post"
        elif reply_count < course.min_replies:
            status = "needs_replies"
        else:
            status = "done"
        priority, missing, closed = _priority(status, received, forum.due_at, now)
        results.append(
            ThreadEvaluation(
                thread_id=thread.id,
                status=status,
                priority=priority,
                has_own_post=has_post,
                reply_count=reply_count,
                replies_required=course.min_replies,
                received_replies=received,
                missing_deadline=missing,
                closed=closed,
            )
        )
    return results
