"""`bbagent publish`: render a post, run the pre-send checks, and (with --send) post it.

The HTTP write itself lives in `bb_client.post_message`.
"""

import html
import json
import re
from collections.abc import Callable
from typing import Any

from bbagent.bb_client import ForumsApi, PostError, build_post_payload, post_message
from bbagent.config import MODEL_PLACEHOLDER, CourseConfig
from bbagent.parse import SchemaError, count_words, current_user, html_to_text

EXIT_OK = 0
EXIT_API_ERROR = 3
EXIT_REFUSED = 4
EXCERPT_WORDS = 25

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_INLINE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<link>https?://[^)\s]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|(?P<url>https?://[^\s<>]+)"
)
_LINK_ATTRS = 'target="_blank" rel="noopener noreferrer"'
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(https?://[^)\s]+\)")
_BRACKETED = re.compile(r"\[[^\]]*\]")
_CITATION = re.compile(r"\[\d+(?:\s*[,\-–]\s*\d+)*\]")


def _inline(line: str) -> tuple[str, str]:
    """(html, plain text) of one line: **bold**, [text](url) and bare URLs; the rest is escaped."""
    out: list[str] = []
    plain: list[str] = []
    pos = 0
    for m in _INLINE.finditer(line):
        if m.start() < pos:
            continue
        before = line[pos : m.start()]
        out.append(html.escape(before, quote=False))
        plain.append(before)
        end = m.end()
        if m.group("link"):
            label = m.group("label")
            out.append(f'<a href="{html.escape(m.group("link"))}" {_LINK_ATTRS}>')
            out.append(f"{html.escape(label, quote=False)}</a>")
            plain.append(label)
        elif m.group("bold"):
            inner_html, inner_plain = _inline(m.group("bold"))
            out.append(f"<strong>{inner_html}</strong>")
            plain.append(inner_plain)
        else:
            url = m.group("url").rstrip(".,;:!?")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1].rstrip(".,;:!?")
            end = m.start() + len(url)
            out.append(f'<a href="{html.escape(url)}" {_LINK_ATTRS}>')
            out.append(f"{html.escape(url, quote=False)}</a>")
            plain.append(url)
        pos = end
    tail = line[pos:]
    out.append(html.escape(tail, quote=False))
    plain.append(tail)
    return "".join(out), "".join(plain)


def render_post(text: str) -> tuple[str, str]:
    """(html, plain text) of a light-Markdown text.

    Paragraphs are separated by blank lines, a single newline becomes <br>, `**bold**` becomes
    <strong>, `[text](url)` and bare http(s) URLs become <a>. Everything else is HTML-escaped.
    Plain text joins lines and paragraphs with "\\n"; link syntax contributes only its label.
    """
    paragraphs_html: list[str] = []
    paragraphs_plain: list[str] = []
    for raw in _PARAGRAPH_SPLIT.split(text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = [_inline(line.strip()) for line in raw.split("\n") if line.strip()]
        if not lines:
            continue
        paragraphs_html.append("<p>" + "<br>".join(h for h, _ in lines) + "</p>")
        paragraphs_plain.append("\n".join(p for _, p in lines))
    return "".join(paragraphs_html), "\n".join(paragraphs_plain)


def to_post_html(text: str) -> tuple[str, int]:
    """(postText HTML, postLength) of a light-Markdown text.

    postLength is the number of characters of the plain text with paragraphs joined by "\\n".
    The forum SPA's own count differs by about one character (a real post reported 2229 for 2228
    characters of tag-stripped text); the server was not observed to validate the field.
    """
    rendered, plain = render_post(text)
    return rendered, len(plain)


def text_problems(text: str) -> list[str]:
    """Reasons why a text must not be posted, judged on the raw source text."""
    if not text.strip():
        return ["text is empty"]
    problems = []
    if MODEL_PLACEHOLDER in text:
        problems.append(f"text still contains {MODEL_PLACEHOLDER}")
    without_links = _MARKDOWN_LINK.sub("", text)
    placeholders = [b for b in _BRACKETED.findall(without_links) if not _CITATION.fullmatch(b)]
    if placeholders:
        problems.append(f"text contains a [...] placeholder: {placeholders[0]}")
    return problems


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _find_thread(forums_json: dict, thread_id: str) -> tuple[dict, dict] | None:
    for forum in forums_json.get("data") or []:
        for thread in forum.get("ltiThreads") or []:
            if str(thread.get("id")) == thread_id:
                return forum, thread
    return None


def _excerpt(post_html: str) -> str:
    words = html_to_text(post_html).split()
    return " ".join(words[:EXCERPT_WORDS]) + (" ..." if len(words) > EXCERPT_WORDS else "")


def _launch_problems(
    data: dict, plain: str, me: str, parent_id: int | None
) -> tuple[list[str], dict | None]:
    """(reasons to refuse, parent post or None) from the thread launch data."""
    problems = []
    posts = [p for p in data.get("ltiPosts") or [] if isinstance(p, dict)]
    mine = [p for p in posts if p.get("creator") == me]
    wanted = _normalize(plain)
    if any(_normalize(html_to_text(str(p.get("postText") or ""))) == wanted for p in mine):
        problems.append("an own message with the same text already exists in the thread")
    allowed = data.get("numMessagesAllowed") or 0
    used = data.get("numMessagesOfUser") or 0
    if allowed > 0 and used >= allowed:
        problems.append(f"message limit reached ({used} of {allowed} allowed)")
    parent = None
    if parent_id is not None:
        parent = next((p for p in posts if str(p.get("id")) == str(parent_id)), None)
        if parent is None:
            problems.append(f"parent message {parent_id} not found in the thread")
        elif parent.get("creator") == me:
            problems.append(f"parent message {parent_id} is authored by you")
    return problems, parent


def refuse(problems: list[str], out: Callable[[str], None]) -> int:
    out("Refused:")
    for problem in problems:
        out(f"  - {problem}")
    return EXIT_REFUSED


def run_publish(
    course: CourseConfig,
    *,
    thread_id: str,
    parent_id: int | None,
    text: str,
    send: bool,
    api: ForumsApi,
    recollect: Callable[[], int],
    status_line: Callable[[], str | None],
    out: Callable[[str], None] = print,
) -> int:
    """Check, show and optionally send a post. SessionExpired and EndpointError propagate.

    Exit codes: 0 dry-run ok or published, 4 refused, 3 POST failed or not verified.
    """
    forums_json = api.get("/rest/forums")
    found = _find_thread(forums_json, thread_id)
    if found is None:
        return refuse([f"thread {thread_id} not found in the forums of {course.name}"], out)
    forum, thread = found
    forum_id = thread.get("idForum", forum.get("id"))
    me = current_user(api.get("/rest/init"))
    launch = api.get(f"/rest/forums/{forum_id}/threads/{thread_id}/launch")
    data = launch.get("data")
    if not isinstance(data, dict):
        raise SchemaError("data")
    rendered, plain = render_post(text)
    problems, parent = _launch_problems(data, plain, me, parent_id)
    if problems:
        return refuse(problems, out)

    length = len(plain)
    payload = build_post_payload(rendered, length, parent_id)
    out(f"Course: {course.name}")
    out(f"Forum: {forum.get('title')}")
    out(f"Thread: {thread.get('title')}")
    if parent is not None:
        first = str(parent.get("creatorName") or "?").split()[0]
        out(f"Reply to: {first}: {_excerpt(str(parent.get('postText') or ''))}")
    out(f"Words: {count_words(plain)}")
    out("Text:")
    out(plain)
    out("Payload:")
    out(json.dumps(payload, ensure_ascii=False, indent=2))
    if not send:
        out("Dry run: nothing sent (add --send to post)")
        return EXIT_OK

    try:
        created = post_message(
            api.request, api.token, forum_id, thread_id, rendered, length, parent_id
        )
    except PostError as exc:
        out(f"POST failed (not retried): {exc}")
        out("The post may or may not exist: check the thread in Blackboard before retrying.")
        return EXIT_API_ERROR
    code = _verify(created, me, parent_id, out)
    if recollect() != EXIT_OK:
        out("Warning: published, but the follow-up collect failed; run `bbagent collect`")
    if line := status_line():
        out(line)
    return code


def _verify(created: dict[str, Any], me: str, parent_id: int | None, out) -> int:
    problems = []
    if created.get("creator") != me:
        problems.append(f"creator is {created.get('creator')!r}, expected {me!r}")
    if parent_id is not None and str(created.get("idParent")) != str(parent_id):
        problems.append(f"idParent is {created.get('idParent')!r}, expected {parent_id}")
    if problems:
        out(f"Posted message {created.get('id')} but verification failed: " + "; ".join(problems))
        out("Check the thread in Blackboard.")
        return EXIT_API_ERROR
    out(f"Published message {created.get('id')}")
    return EXIT_OK
