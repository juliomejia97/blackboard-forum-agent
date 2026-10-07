"""Playwright and HTTP access to Blackboard and the IE forums REST API.

`post_message` is the only write in the codebase: every other request is a GET.
"""

import json
import re
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.sync_api import BrowserContext, Error, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bbagent.config import AppConfig, CourseConfig
from bbagent.parse import SchemaError, parse_forums
from bbagent.snapshot import in_record_window

API_MARKER = "/learn/api/"
LTI_ORIGIN = "https://ie-lti-forums.ie.edu"
LTI_REST_PREFIX = f"{LTI_ORIGIN}/rest/"
FORUM_TOOL_HANDLER = "resource/x-bb-bltiplacement-ie_forums_course_tool"
CONTAINER_HANDLERS = {"resource/x-bb-folder", "resource/x-bb-lesson"}
MAX_FOLDER_DEPTH = 3
MAX_PAGES = 20
AUTH_TIMEOUT_S = 60.0
SESSION_TIMEOUT_MS = 45000
SSO_HOSTS = ("login.microsoftonline.com", "login.microsoft.com")
SSO_HOST_SUFFIX = ".microsoftazuread-sso.com"


def run_browser(config: AppConfig, har_path: Path | None = None) -> None:
    """Open a headed persistent Chromium on the course list and block until it is closed."""
    options: dict = {}
    if har_path is not None:
        har_path.parent.mkdir(parents=True, exist_ok=True)
        options = {"record_har_path": str(har_path), "record_har_content": "embed"}
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(config.profile_dir), headless=False, **options
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{config.base_url}/ultra/course")
        print("Log in with SSO/MFA, then close the browser window")
        try:
            wait_until_closed(context)
        except KeyboardInterrupt:
            context.close()
            raise SystemExit(130) from None
        context.close()


def wait_until_closed(context: BrowserContext) -> None:
    """Block until the context has no open pages.

    Closing the last window does not close a persistent Chromium context on macOS, so the
    context-level "close" event never fires. Waiting on one page's "close" event at a time
    keeps the sync API dispatching events, and pages opened meanwhile are picked up on the
    next iteration. The caller must close the context (which also flushes the HAR).
    """
    while open_pages := [p for p in context.pages if not p.is_closed()]:
        try:
            open_pages[0].wait_for_event("close", timeout=0)
        except Error:
            return


def summarize_har(har_path: Path) -> list[str]:
    """One line per distinct (method, status, path) of JSON requests to the Blackboard REST API."""
    entries = json.loads(har_path.read_text(encoding="utf-8"))["log"]["entries"]
    counts: Counter[tuple[str, int, str]] = Counter()
    for entry in entries:
        request, response = entry["request"], entry["response"]
        if API_MARKER not in request["url"]:
            continue
        if "json" not in response.get("content", {}).get("mimeType", "").lower():
            continue
        path = urlsplit(request["url"]).path
        counts[(request["method"], response["status"], path)] += 1
    return [
        f"{method} {status} {path} (x{n})"
        for (method, status, path), n in sorted(counts.items(), key=lambda kv: (kv[0][2], kv[0]))
    ]


class SessionExpired(Exception):
    """The persistent profile is no longer logged in."""


class EndpointError(Exception):
    """An endpoint answered with an unexpected status or could not be reached or derived."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CourseNotFound(EndpointError):
    """The course contents call answered 404: wrong bb_course_id or no access."""

    def __init__(self, course_id: str) -> None:
        super().__init__(f"Course not found or no access: {course_id} (check config.yaml)", 404)


class ForumToolNotFound(EndpointError):
    """The course content tree has no IE Forums item."""


def _is_login_redirect(url: str, base_url: str) -> bool:
    """A learn API call that ended anywhere but the learn API means an SSO or login redirect."""
    final, base = urlsplit(url), urlsplit(base_url)
    return final.netloc != base.netloc or not final.path.startswith(API_MARKER)


def _learn_get(request: Any, base_url: str, path: str, missing_ok: bool = False) -> Any:
    """GET a learn API path (cookie auth) and return the JSON body; 401/403/login => expired.

    With missing_ok a 404 returns None instead of raising.
    """
    url = urljoin(base_url, path)
    response = request.get(url)
    if response.status in (401, 403) or _is_login_redirect(response.url, base_url):
        raise SessionExpired(path.split("?")[0])
    if missing_ok and response.status == 404:
        return None
    if response.status != 200:
        raise EndpointError(f"GET {path.split('?')[0]} -> {response.status}", response.status)
    try:
        return response.json()
    except ValueError as exc:
        raise SessionExpired(path.split("?")[0]) from exc


def classify_url(url: str, base_url: str) -> str:
    """ "blackboard" for the Blackboard host, "sso" for Microsoft login hosts, else "other"."""
    host = (urlsplit(url).hostname or "").lower()
    if host and host == (urlsplit(base_url).hostname or "").lower():
        return "blackboard"
    if host in SSO_HOSTS or (host and host.endswith(SSO_HOST_SUFFIX)):
        return "sso"
    return "other"


def _is_ultra(url: str, base_url: str) -> bool:
    return classify_url(url, base_url) == "blackboard" and urlsplit(url).path.startswith("/ultra")


def establish_session(context: Any, base_url: str, timeout_ms: int = SESSION_TIMEOUT_MS) -> None:
    """Recreate the Blackboard session cookie via the silent SSO redirect chain.

    The Blackboard auth cookie is a session cookie lost on every Chromium restart, while the
    Entra SSO cookies persist; navigating to /ultra/course lets SSO log the user back in.
    """
    page = context.new_page()
    try:
        try:
            page.goto(
                f"{base_url.rstrip('/')}/ultra/course", wait_until="commit", timeout=timeout_ms
            )
            page.wait_for_url(lambda url: _is_ultra(url, base_url), timeout=timeout_ms)
        except (PlaywrightTimeout, Error):
            pass
        final = page.url
    finally:
        page.close()
    if not _is_ultra(final, base_url):
        raise SessionExpired(
            f"Blackboard login not completed ({classify_url(final, base_url)}): run `bbagent login`"
        )


def check_session(context: Any, base_url: str) -> str:
    """Id of the logged-in Blackboard user (`_N_1`); raises SessionExpired when not logged in."""
    establish_session(context, base_url)
    me = _learn_get(context.request, base_url, "/learn/api/v1/users/me")
    user_id = me.get("id") if isinstance(me, dict) else None
    if not user_id:
        raise EndpointError("GET /learn/api/v1/users/me -> no id")
    return str(user_id)


def list_memberships(context: Any, base_url: str, user_id: str) -> list[dict]:
    """Course memberships (with the course expanded) of a user."""
    expand = "course.effectiveAvailability,course.permissions,courseRole"
    path = f"/learn/api/v1/users/{user_id}/memberships?expand={expand}&includeCount=true&limit=1000"
    data = _learn_get(context.request, base_url, path)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise EndpointError("memberships: no results")
    return results


def _normalize_course_id(raw: Any) -> str | None:
    """`_N_1` from `_N_1` or a bare numeric pk `N`; None for anything else."""
    text = str(raw).strip()
    if re.fullmatch(r"_\d+_1", text):
        return text
    if re.fullmatch(r"\d+", text):
        return f"_{text}_1"
    return None


def parse_favorite_ids(data: Any) -> set[str]:
    """Course ids (`_N_1`) from the favorite.courses preference body.

    `value` is a JSON string of an object {"_N_1": true, ...}; ids set to false are skipped.
    A list of ids is accepted too. Unparseable or empty values give an empty set.
    """
    value = data.get("value") if isinstance(data, dict) else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return set()
    if isinstance(value, dict):
        raw_ids = [k for k, v in value.items() if v]
    elif isinstance(value, list):
        raw_ids = value
    else:
        return set()
    return {i for i in map(_normalize_course_id, raw_ids) if i}


def favorite_course_ids(context: Any, base_url: str) -> set[str]:
    """Ids of the courses starred in Ultra; empty when the preference does not exist (404)."""
    path = "/learn/api/v1/users/me/preferences/favorite.courses"
    return parse_favorite_ids(_learn_get(context.request, base_url, path, missing_ok=True))


def course_rows(
    memberships: list[dict],
    term_filter: str | None = None,
    favorites: set[str] | None = None,
) -> list[tuple[str, str, str]]:
    """(course id, name, term) of available, open, non-organization courses, sorted by term.

    With favorites, only courses whose id is in that set are kept.
    """
    rows = []
    for m in memberships:
        course = m.get("course") or {}
        if favorites is not None and str(course.get("id", "")) not in favorites:
            continue
        if course.get("isOrganization") or not course.get("isAvailable", True):
            continue
        if course.get("isClosed"):
            continue
        term = (course.get("term") or {}).get("name") or ""
        if term_filter and term_filter.lower() not in term.lower():
            continue
        name = course.get("displayName") or course.get("name") or ""
        rows.append((str(course.get("id", "")), name, term))
    return sorted(rows, key=lambda r: (r[2], r[1]))


def build_launch_url(base_url: str, placement_id: str, content_id: str, course_id: str) -> str:
    """launchPlacement URL as Ultra builds it when the "IE Forums" content item is clicked."""
    query = urlencode(
        {
            "blti_placement_id": placement_id,
            "content_id": content_id,
            "course_id": course_id,
            "wrapped": "true",
            "from_ultra": "true",
        }
    )
    return f"{base_url.rstrip('/')}/webapps/blackboard/execute/blti/launchPlacement?{query}"


def _children(request: Any, base_url: str, course_id: str, parent_id: str) -> Iterator[dict]:
    path = (
        f"/learn/api/v1/courses/{course_id}/contents/{parent_id}/children?%40view=Summary&limit=100"
    )
    for _ in range(MAX_PAGES):
        try:
            data = _learn_get(request, base_url, path)
        except EndpointError as exc:
            if exc.status == 404 and parent_id == "ROOT":
                raise CourseNotFound(course_id) from exc
            raise
        yield from data.get("results") or []
        path = (data.get("paging") or {}).get("nextPage")
        if not path:
            return


def _placement_id(detail: Mapping[str, Any]) -> str | None:
    """`blti_placement_id` from the content item's tool detail (launchLink, else placement)."""
    for link in (detail.get("launchLink"), (detail.get("placement") or {}).get("launchLink")):
        if link and (ids := parse_qs(urlsplit(link).query).get("blti_placement_id")):
            return ids[0]
    return (detail.get("placement") or {}).get("id")


def find_forum_tool(request: Any, base_url: str, course_id: str) -> tuple[str, str]:
    """(content_id, launch_url) of the IE Forums item of a course, via GET learn API calls.

    Walks /contents/ROOT/children breadth first (folders and lessons up to MAX_FOLDER_DEPTH),
    finds the item whose contentHandler is the forums tool, then reads GET /contents/{id}: its
    contentDetail holds the placement id (`launchLink` / `placement.id`).
    """
    queue = [("ROOT", 0)]
    while queue:
        parent_id, depth = queue.pop(0)
        for item in _children(request, base_url, course_id, parent_id):
            handler = item.get("contentHandler")
            if handler == FORUM_TOOL_HANDLER:
                content_id = str(item["id"])
                detail = _learn_get(
                    request, base_url, f"/learn/api/v1/courses/{course_id}/contents/{content_id}"
                )
                tool = (detail.get("contentDetail") or {}).get(FORUM_TOOL_HANDLER) or {}
                if placement_id := _placement_id(tool):
                    return content_id, build_launch_url(
                        base_url, placement_id, content_id, course_id
                    )
            elif handler in CONTAINER_HANDLERS and depth < MAX_FOLDER_DEPTH:
                queue.append((str(item["id"]), depth + 1))
    raise ForumToolNotFound(f"No IE Forums item in course {course_id}")


def auth_header(url: str, headers: Mapping[str, str]) -> str | None:
    """Authorization value of a request to the forums REST API, else None."""
    if not url.startswith(LTI_REST_PREFIX):
        return None
    for name, value in headers.items():
        if name.lower() == "authorization" and value:
            return value
    return None


def _launch_and_capture(context: Any, launch_url: str, timeout_s: float) -> str:
    """Navigate the forums launch in a new page and return the first Authorization header."""
    page = context.new_page()
    captured: list[str] = []

    def on_request(req: Any) -> None:
        if not captured and (value := auth_header(req.url, req.headers)):
            captured.append(value)

    page.on("request", on_request)
    nav_error = ""
    try:
        try:
            page.goto(launch_url, wait_until="commit", timeout=timeout_s * 1000)
        except Error as exc:
            nav_error = f" ({str(exc).splitlines()[0]})"
        deadline = time.monotonic() + timeout_s
        while not captured and time.monotonic() < deadline:
            page.wait_for_timeout(200)
    finally:
        page.close()
    if not captured:
        raise EndpointError(f"No Authorization header captured from {LTI_ORIGIN}/rest/{nav_error}")
    return captured[0]


def forum_ids_to_fetch(forums_json: dict, now: datetime) -> set[str]:
    """Ids of forums open, without due date, or closed at most RECORD_DAYS ago (snapshot record)."""
    return {f.id for f in parse_forums(forums_json, "") if in_record_window(f.due_at, now)}


class ForumsApi:
    """Authenticated GET access to the forums REST API of one course.

    Captures the LTI Authorization header with a launch and re-launches once on a 401.
    """

    def __init__(self, context: Any, launch_url: str, auth_timeout: float) -> None:
        self.request = context.request
        self._context = context
        self._launch_url = launch_url
        self._auth_timeout = auth_timeout
        self._relaunched = False
        self.token = _launch_and_capture(context, launch_url, auth_timeout)

    def get(self, path: str) -> dict:
        while True:
            response = self.request.get(
                f"{LTI_ORIGIN}{path}",
                headers={"Authorization": self.token, "Accept": "application/json"},
            )
            if response.status == 401:
                if self._relaunched:
                    raise SessionExpired(path)
                self._relaunched = True
                self.token = _launch_and_capture(
                    self._context, self._launch_url, self._auth_timeout
                )
                continue
            if response.status != 200:
                raise EndpointError(f"GET {path} -> {response.status}")
            try:
                return response.json()
            except ValueError as exc:
                raise EndpointError(f"GET {path} -> not JSON") from exc


def open_forums_api(
    context: Any, base_url: str, course_cfg: CourseConfig, auth_timeout: float = AUTH_TIMEOUT_S
) -> ForumsApi:
    """Resolve the forums launch URL of a course and capture the Authorization header."""
    try:
        _, launch_url = find_forum_tool(context.request, base_url, course_cfg.bb_course_id)
    except EndpointError:
        if not course_cfg.forum_launch_url:
            raise
        launch_url = urljoin(base_url, course_cfg.forum_launch_url)
    return ForumsApi(context, launch_url, auth_timeout)


def collect_course(
    context: Any,
    base_url: str,
    course_cfg: CourseConfig,
    auth_timeout: float = AUTH_TIMEOUT_S,
    now: datetime | None = None,
) -> tuple[dict, dict, dict[str, dict]]:
    """(init_json, forums_json, {thread_id: launch_json}) of a course; GET requests only."""
    api = open_forums_api(context, base_url, course_cfg, auth_timeout)
    init_json = api.get("/rest/init")
    forums_json = api.get("/rest/forums")
    launches: dict[str, dict] = {}
    data = forums_json.get("data")
    if not isinstance(data, list):
        raise SchemaError("data")
    wanted = forum_ids_to_fetch(forums_json, now or datetime.now().astimezone())
    for i, forum in enumerate(data):
        if str(forum.get("id")) not in wanted:
            continue
        for j, thread in enumerate(forum.get("ltiThreads") or []):
            try:
                forum_id, thread_id = forum["id"], thread["id"]
            except (KeyError, TypeError) as exc:
                raise SchemaError(f"data[{i}].ltiThreads[{j}].id") from exc
            launches[str(thread_id)] = api.get(
                f"/rest/forums/{forum_id}/threads/{thread_id}/launch"
            )
    return init_json, forums_json, launches


class PostError(EndpointError):
    """The POST of a message failed or its response could not be read; it is never retried."""


def build_post_payload(html: str, length: int, parent_id: int | None) -> dict:
    """Request body of a post: top-level without idParent/quote, a reply with both."""
    payload: dict[str, Any] = {"postText": html, "postLength": length, "usersCited": []}
    if parent_id is not None:
        payload["idParent"] = parent_id
        payload["quote"] = None
    return payload


def _body_summary(response: Any, limit: int = 300) -> str:
    try:
        text = response.text()
    except Exception:  # noqa: BLE001 - diagnostic only
        text = ""
    return " ".join(str(text).split())[:limit]


def post_message(
    request_ctx: Any,
    auth: str,
    forum_id: int | str,
    thread_id: int | str,
    html: str,
    length: int,
    parent_id: int | None,
) -> dict:
    """POST one message to a thread and return the created post. No retries.

    Raises SessionExpired on 401 (nothing was created) and PostError on any other failure.
    """
    url = f"{LTI_ORIGIN}/rest/forums/{forum_id}/threads/{thread_id}/posts"
    body = json.dumps(build_post_payload(html, length, parent_id), ensure_ascii=False)
    response = request_ctx.post(
        url,
        data=body,
        headers={
            "Authorization": auth,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        },
    )
    if response.status == 401:
        raise SessionExpired(f"POST /rest/forums/{forum_id}/threads/{thread_id}/posts")
    if response.status not in (200, 201):
        raise PostError(f"POST -> {response.status}: {_body_summary(response)}", response.status)
    try:
        created = response.json()
    except ValueError as exc:
        raise PostError(
            f"POST -> {response.status} but the response is not JSON", response.status
        ) from exc
    if isinstance(created, dict) and "id" not in created and isinstance(created.get("data"), dict):
        created = created["data"]
    if not isinstance(created, dict):
        raise PostError(f"POST -> {response.status}: response is not an object", response.status)
    return created


@contextmanager
def open_context(config: AppConfig, headless: bool = True) -> Iterator[BrowserContext]:
    """Persistent Chromium context on the configured profile."""
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(config.profile_dir), headless=headless)
        try:
            yield context
        finally:
            context.close()
