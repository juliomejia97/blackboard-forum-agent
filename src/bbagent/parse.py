import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from bbagent.mapping import due_date_from_title, word_limit
from bbagent.models import Forum, Message, Thread

_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
_CELL = {"td", "th"}
_SEPARATOR_LINE = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+$")


def _table_markdown(rows: list[list[str]]) -> list[str]:
    """Markdown lines of a table; the first row is the header, cells are padded to equal width."""
    rows = [r for r in rows if r]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    lines = []
    for i, row in enumerate(rows):
        cells = [c.replace("|", "\\|") for c in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return lines


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._table_depth = 0
        self._rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
                self._cell = None
                self.parts.append("\n")
        elif self._table_depth:
            if tag == "tr" and self._table_depth == 1:
                self._close_cell()
                self._rows.append([])
            elif tag in _CELL and self._table_depth == 1:
                self._close_cell()
                if not self._rows:
                    self._rows.append([])
                self._cell = []
            else:
                self._append(" ")
        else:
            self._break(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0:
                self._close_cell()
                self.parts.append("\n".join(_table_markdown(self._rows)) + "\n")
            return
        if self._table_depth:
            if tag in _CELL and self._table_depth == 1:
                self._close_cell()
            elif tag in _BLOCK or tag in _CELL:
                self._append(" ")
        else:
            self._break(tag)

    def _close_cell(self) -> None:
        if self._cell is not None:
            text = re.sub(r"[\s\xa0]+", " ", "".join(self._cell)).strip()
            self._rows[-1].append(text)
            self._cell = None

    def _append(self, text: str) -> None:
        if self._cell is not None:
            self._cell.append(text)

    def _break(self, tag: str) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._table_depth:
            self._append(data)
        else:
            self.parts.append(data)

    def close(self) -> None:
        super().close()
        while self._table_depth:  # unterminated table: flush what was collected
            self.handle_endtag("table")


def html_to_text(html: str) -> str:
    """Visible text of an HTML fragment; block tags become newlines, inline tags vanish.

    A `<table>` becomes a Markdown table (first row = header, cells stripped, pipes escaped).
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (
        re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(extractor.parts).split("\n")
    )
    return "\n".join(line for line in lines if line)


def count_words(text: str) -> int:
    """Words of `text`, ignoring Markdown table pipes and separator rows."""
    total = 0
    for line in text.split("\n"):
        if _SEPARATOR_LINE.match(line):
            continue
        if line.startswith("|") and line.endswith("|") and len(line) > 1:
            cells = re.split(r"(?<!\\)\|", line[1:-1])
            total += sum(len(c.replace("\\|", "|").split()) for c in cells)
        else:
            total += len(line.split())
    return total


class SchemaError(Exception):
    """The LTI forums JSON does not have the expected shape; `path` locates the problem."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Missing or invalid key: {path}")
        self.path = path


def _get(obj: Any, key: str, path: str) -> Any:
    full = f"{path}.{key}" if path else key
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError(full)
    return obj[key]


def _list(obj: Any, key: str, path: str) -> list:
    value = _get(obj, key, path)
    if not isinstance(value, list):
        raise SchemaError(f"{path}.{key}" if path else key)
    return value


def _datetime(value: Any, path: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SchemaError(path)
    return datetime.fromtimestamp(value / 1000, UTC)


def _end_date(value: Any, path: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchemaError(path) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return _datetime(value, path)


def _is_optional(thread_title: str, forum_title: str) -> bool:
    return (
        thread_title.strip().lower().startswith("theme")
        or "optional" in thread_title.lower()
        or "optional" in forum_title.lower()
    )


def _thread_from_summary(item: Any, path: str, forum_id: str, course_id: str, forum_title: str):
    title = str(_get(item, "title", path))
    return Thread(
        id=str(_get(item, "id", path)),
        forum_id=forum_id,
        course_id=course_id,
        title=title,
        prompt_text=html_to_text(str(_get(item, "description", path) or "")),
        is_optional=_is_optional(title, forum_title),
    )


def parse_forums(forums_json: dict, course_id: str) -> list[Forum]:
    """Forums of a course with their threads (without posts) from GET /rest/forums."""
    forums = []
    for i, item in enumerate(_list(forums_json, "data", "")):
        path = f"data[{i}]"
        forum_id = str(_get(item, "id", path))
        title = str(_get(item, "title", path))
        opens_at = _datetime(_get(item, "creationDate", path), f"{path}.creationDate")
        end = _end_date(item.get("endDate"), f"{path}.endDate")
        threads = [
            _thread_from_summary(t, f"{path}.ltiThreads[{j}]", forum_id, course_id, title)
            for j, t in enumerate(_list(item, "ltiThreads", path))
        ]
        description = html_to_text(str(item.get("description") or ""))
        forums.append(
            Forum(
                id=forum_id,
                course_id=course_id,
                title=title,
                description=description,
                max_chars=item.get("maxCharactersForPost") or None,
                due_at=end or due_date_from_title(title, opens_at),
                opens_at=opens_at,
                threads=threads,
            )
        )
    return forums


def parse_thread(
    launch_json: dict, forum: Forum, current_user: str, instructor_ids: set[str]
) -> Thread:
    """Thread with all its posts from GET /rest/forums/{id}/threads/{id}/launch."""
    data = _get(launch_json, "data", "")
    thread_id = str(_get(data, "id", "data"))
    title = str(_get(data, "title", "data"))
    prompt = html_to_text(str(_get(data, "description", "data") or ""))
    messages = []
    for i, post in enumerate(_list(data, "ltiPosts", "data")):
        path = f"data.ltiPosts[{i}]"
        author = str(_get(post, "creator", path))
        text = html_to_text(str(_get(post, "postText", path)))
        parent = _get(post, "idParent", path)
        messages.append(
            Message(
                id=str(_get(post, "id", path)),
                thread_id=thread_id,
                forum_id=forum.id,
                course_id=forum.course_id,
                parent_id=str(parent) if parent else None,
                author_id=author,
                author_name=str(_get(post, "creatorName", path)),
                is_mine=author == current_user,
                body_text=text,
                word_count=count_words(text),
                created_at=_datetime(_get(post, "creationDate", path), f"{path}.creationDate"),
                is_read=bool(_get(post, "readed", path)),
                is_instructor=author in instructor_ids,
            )
        )
    messages.sort(key=lambda m: (m.created_at, m.id))
    return Thread(
        id=thread_id,
        forum_id=forum.id,
        course_id=forum.course_id,
        title=title,
        prompt_text=prompt,
        word_limit=word_limit(prompt) or word_limit(forum.description),
        is_optional=_is_optional(title, forum.title),
        messages=messages,
    )


def instructor_ids(init_json: dict, forums_json: dict) -> set[str]:
    """Ids (`A########`) treated as instructors.

    Users flagged `instructor` in /rest/init, plus the creators of forums and threads (the
    instructor is not listed in `users`).
    """
    ids = {
        str(u["externalKey"])
        for u in _list(_get(init_json, "data", ""), "users", "data")
        if isinstance(u, dict) and u.get("instructor") and u.get("externalKey")
    }
    for forum in _list(forums_json, "data", ""):
        if isinstance(forum, dict):
            if forum.get("creator"):
                ids.add(str(forum["creator"]))
            for t in forum.get("ltiThreads") or []:
                if isinstance(t, dict) and t.get("creator"):
                    ids.add(str(t["creator"]))
    return ids


def current_user(init_json: dict) -> str:
    """Viewer id (`A########`) from /rest/init."""
    return str(_get(init_json, "currentUser", ""))
