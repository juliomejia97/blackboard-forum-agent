import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bbagent.bb_client import (
    CourseNotFound,
    EndpointError,
    ForumToolNotFound,
    SessionExpired,
    auth_header,
    build_launch_url,
    check_session,
    classify_url,
    collect_course,
    course_rows,
    establish_session,
    favorite_course_ids,
    find_forum_tool,
    forum_ids_to_fetch,
    parse_favorite_ids,
)
from bbagent.cli import main, run_collect
from bbagent.config import AppConfig, CourseConfig
from bbagent.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://blackboard.test"
COURSE_ID = "_100_1"
NOW = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
COURSE = CourseConfig(
    bb_course_id=COURSE_ID,
    name="Fake Course",
    notion_course_url="https://notion.example/course",
    ai_declaration="AI used for drafting.",
)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status=200, body=None, url=""):
        self.status = status
        self.body = body
        self.url = url

    def json(self):
        if self.body is None:
            raise ValueError("not json")
        return self.body


class FakeRequest:
    """Routes GETs by path (query ignored) and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers))
        path = urlsplit(url).path
        route = self.routes.get(path)
        if callable(route):
            route = route(headers)
        if route is None:
            return FakeResponse(404, {}, url)
        if isinstance(route, FakeResponse):
            route.url = route.url or url
            return route
        return FakeResponse(200, route, url)


def contents_routes():
    c = load("contents_children.json")
    base = f"/learn/api/v1/courses/{COURSE_ID}/contents"
    return {
        f"{base}/ROOT/children": c["root"],
        f"{base}/_902_1/children": c["folder"],
        f"{base}/_911_1": c["detail"],
    }


def test_auth_header_only_for_forums_rest_api():
    rest = "https://ie-lti-forums.ie.edu/rest/init"
    assert auth_header(rest, {"authorization": "Bearer x"}) == "Bearer x"
    assert auth_header(rest, {"Authorization": "Bearer y"}) == "Bearer y"
    assert auth_header(rest, {"accept": "*/*"}) is None
    assert auth_header(rest, {"authorization": ""}) is None
    assert auth_header("https://ie-lti-forums.ie.edu/app", {"authorization": "Bearer x"}) is None
    assert auth_header("https://other.test/rest/init", {"authorization": "Bearer x"}) is None


def test_build_launch_url_keys_and_order():
    url = build_launch_url(BASE + "/", "_149_1", "_2942894_1", "_152245_1")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        f"{BASE}/webapps/blackboard/execute/blti/launchPlacement"
    )
    assert parse_qsl(parts.query) == [
        ("blti_placement_id", "_149_1"),
        ("content_id", "_2942894_1"),
        ("course_id", "_152245_1"),
        ("wrapped", "true"),
        ("from_ultra", "true"),
    ]


def test_find_forum_tool_walks_folders_with_get_only():
    request = FakeRequest(contents_routes())
    content_id, url = find_forum_tool(request, BASE, COURSE_ID)
    assert content_id == "_911_1"
    assert url == build_launch_url(BASE, "_777_1", "_911_1", COURSE_ID)
    paths = [urlsplit(u).path for u, _ in request.calls]
    base = f"/learn/api/v1/courses/{COURSE_ID}/contents"
    assert paths == [f"{base}/ROOT/children", f"{base}/_902_1/children", f"{base}/_911_1"]
    assert all(h is None for _, h in request.calls)


def test_find_forum_tool_not_found():
    routes = contents_routes()
    routes[f"/learn/api/v1/courses/{COURSE_ID}/contents/_902_1/children"] = {"results": []}
    with pytest.raises(ForumToolNotFound):
        find_forum_tool(FakeRequest(routes), BASE, COURSE_ID)


class SessionPage:
    def __init__(self, final_url):
        self.url = final_url
        self.closed = False
        self.goto_urls = []

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)

    def wait_for_url(self, predicate, timeout=None):
        if not predicate(self.url):
            raise PlaywrightTimeout("timeout")

    def close(self):
        self.closed = True


class SessionContext:
    def __init__(self, request=None, final_url=f"{BASE}/ultra/course"):
        self.request = request
        self.page = SessionPage(final_url)

    def new_page(self):
        return self.page


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (f"{BASE}/ultra/course", "blackboard"),
        ("https://BLACKBOARD.test/webapps/login", "blackboard"),
        ("https://login.microsoftonline.com/tenant/oauth2", "sso"),
        ("https://login.microsoft.com/x", "sso"),
        ("https://abc.microsoftazuread-sso.com/x", "sso"),
        ("https://evil.example/blackboard.test", "other"),
        ("https://blackboard.test.evil.example/", "other"),
        ("about:blank", "other"),
        ("", "other"),
    ],
)
def test_classify_url(url, kind):
    assert classify_url(url, BASE) == kind


def test_establish_session_ok_closes_page():
    ctx = SessionContext()
    establish_session(ctx, BASE)
    assert ctx.page.goto_urls == [f"{BASE}/ultra/course"]
    assert ctx.page.closed


@pytest.mark.parametrize(
    "final",
    [
        "https://login.microsoftonline.com/tenant/oauth2",
        "https://other.example/",
        f"{BASE}/webapps/login/",
    ],
)
def test_establish_session_expired_when_not_on_ultra(final):
    ctx = SessionContext(final_url=final)
    with pytest.raises(SessionExpired, match="bbagent login"):
        establish_session(ctx, BASE, timeout_ms=1)
    assert ctx.page.closed


def test_check_session_ok_and_expired_variants():
    ok = FakeRequest({"/learn/api/v1/users/me": {"id": "_5_1"}})
    assert check_session(SessionContext(ok), BASE) == "_5_1"
    for response in (
        FakeResponse(401, {}),
        FakeResponse(403, {}),
        FakeResponse(200, None),
        FakeResponse(200, {"id": "x"}, url="https://sso.test/login"),
    ):
        ctx = SessionContext(FakeRequest({"/learn/api/v1/users/me": response}))
        with pytest.raises(SessionExpired):
            check_session(ctx, BASE)


def test_check_session_stops_before_api_when_sso_not_completed():
    request = FakeRequest({})
    ctx = SessionContext(request, final_url="https://login.microsoftonline.com/x")
    with pytest.raises(SessionExpired):
        check_session(ctx, BASE)
    assert request.calls == []


def test_course_rows_filters_and_sorts():
    def membership(id, name, term, **course):
        return {"course": {"id": id, "displayName": name, "term": {"name": term}, **course}}

    rows = course_rows(
        [
            membership("_2_1", "B", "T2"),
            membership("_1_1", "A", "T1"),
            membership("_3_1", "Org", "T1", isOrganization=True),
            membership("_4_1", "Closed", "T1", isClosed=True),
            membership("_5_1", "Off", "T1", isAvailable=False),
        ]
    )
    assert rows == [("_1_1", "A", "T1"), ("_2_1", "B", "T2")]
    assert course_rows([membership("_1_1", "A", "T1")], "t2") == []


def test_parse_favorite_ids_fixture_and_normalization():
    assert parse_favorite_ids(load("favorite_courses.json")) == {"_200_1", "_201_1"}
    assert parse_favorite_ids({"value": '["300", "_301_1", "x"]'}) == {"_300_1", "_301_1"}
    for bad in ({"value": ""}, {"value": "not json"}, {"value": "{}"}, {}, None, []):
        assert parse_favorite_ids(bad) == set()


def test_favorite_course_ids_gets_preference_and_handles_404():
    path = "/learn/api/v1/users/me/preferences/favorite.courses"
    request = FakeRequest({path: load("favorite_courses.json")})
    assert favorite_course_ids(SessionContext(request), BASE) == {"_200_1", "_201_1"}
    assert [urlsplit(u).path for u, _ in request.calls] == [path]
    assert favorite_course_ids(SessionContext(FakeRequest({})), BASE) == set()
    with pytest.raises(SessionExpired):
        favorite_course_ids(SessionContext(FakeRequest({path: FakeResponse(401, {})})), BASE)


def test_course_rows_favorites_filter_combines_with_term():
    def membership(id, name, term):
        return {"course": {"id": id, "displayName": name, "term": {"name": term}}}

    ms = [
        membership("_1_1", "A", "T1"),
        membership("_2_1", "B", "T2"),
        membership("_3_1", "C", "T2"),
    ]
    assert course_rows(ms, favorites={"_1_1", "_2_1"}) == [("_1_1", "A", "T1"), ("_2_1", "B", "T2")]
    assert course_rows(ms, "t2", favorites={"_1_1", "_2_1"}) == [("_2_1", "B", "T2")]
    assert course_rows(ms, favorites=set()) == []
    assert len(course_rows(ms)) == 3


def run_courses(monkeypatch, tmp_path, favorites, argv=()):
    from contextlib import contextmanager

    import bbagent.cli as cli

    @contextmanager
    def fake_open(config, headless=True):
        yield object()

    memberships = [
        {"course": {"id": f"_{n}_1", "displayName": f"C{n}", "term": {"name": "T"}}}
        for n in (200, 201, 202)
    ]
    monkeypatch.setattr(cli, "open_context", fake_open)
    monkeypatch.setattr(cli, "check_session", lambda ctx, base: "_5_1")
    monkeypatch.setattr(cli, "list_memberships", lambda ctx, base, uid: memberships)
    monkeypatch.setattr(cli, "favorite_course_ids", lambda ctx, base: favorites)
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    return main(["courses", "--config", str(config), *argv])


def test_courses_defaults_to_starred(monkeypatch, tmp_path, capsys):
    assert run_courses(monkeypatch, tmp_path, {"_200_1", "_201_1"}) == 0
    assert [ln.split("\t")[0] for ln in capsys.readouterr().out.splitlines()] == [
        "_200_1",
        "_201_1",
    ]


def test_courses_all_ignores_favorites(monkeypatch, tmp_path, capsys):
    assert run_courses(monkeypatch, tmp_path, {"_200_1"}, ["--all"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3


def test_courses_without_favorites_hints_and_exits_1(monkeypatch, tmp_path, capsys):
    assert run_courses(monkeypatch, tmp_path, set()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No starred courses; star them in Blackboard or use --all" in captured.err
    assert run_courses(monkeypatch, tmp_path, set(), ["--all"]) == 0


class FakePage:
    def __init__(self, context):
        self.context = context
        self.handler = None
        self.closed = False

    def on(self, event, handler):
        assert event == "request"
        self.handler = handler

    def goto(self, url, **kwargs):
        self.context.launched.append(url)
        token = self.context.tokens.pop(0)
        if token is not None:
            self.handler(
                type(
                    "Req",
                    (),
                    {
                        "url": "https://ie-lti-forums.ie.edu/rest/init",
                        "headers": {"authorization": token},
                    },
                )
            )

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, request, tokens):
        self.request = request
        self.tokens = list(tokens)
        self.launched = []
        self.pages = []

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page


def lti_routes(valid_token):
    def guarded(body):
        def route(headers):
            if headers and headers.get("Authorization") == valid_token:
                return body
            return FakeResponse(401, {})

        return route

    forums = load("forums.json")
    routes = {
        "/rest/init": guarded(load("init.json")),
        "/rest/forums": guarded(forums),
        "/rest/forums/11763/threads/47441/launch": guarded(load("thread_launch.json")),
    }
    for t in forums["data"][0]["ltiThreads"]:
        routes.setdefault(f"/rest/forums/11763/threads/{t['id']}/launch", guarded({"data": {}}))
    return routes


def test_collect_course_captures_header_and_fetches_everything():
    request = FakeRequest({**contents_routes(), **lti_routes("Bearer t1")})
    context = FakeContext(request, ["Bearer t1"])
    init, forums, launches = collect_course(context, BASE, COURSE, auth_timeout=0.01, now=NOW)
    assert init == load("init.json") and forums == load("forums.json")
    assert set(launches) == {"47440", "47441", "47442", "47443"}
    assert context.launched == [build_launch_url(BASE, "_777_1", "_911_1", COURSE_ID)]
    assert all(p.closed for p in context.pages)
    lti_calls = [(u, h) for u, h in request.calls if "/rest/" in u]
    assert len(lti_calls) == 2 + 4
    assert all(h["Authorization"] == "Bearer t1" for _, h in lti_calls)


def test_collect_course_relaunches_once_on_401():
    request = FakeRequest({**contents_routes(), **lti_routes("Bearer new")})
    context = FakeContext(request, ["Bearer old", "Bearer new"])
    collect_course(context, BASE, COURSE, auth_timeout=0.01, now=NOW)
    assert len(context.launched) == 2

    context = FakeContext(request, ["Bearer old", "Bearer old2"])
    with pytest.raises(SessionExpired):
        collect_course(context, BASE, COURSE, auth_timeout=0.01, now=NOW)
    assert len(context.launched) == 2


def test_collect_course_no_header_captured():
    request = FakeRequest({**contents_routes(), **lti_routes("Bearer t1")})
    context = FakeContext(request, [None])
    with pytest.raises(EndpointError):
        collect_course(context, BASE, COURSE, auth_timeout=0.01, now=NOW)
    assert context.pages[0].closed


def test_collect_course_falls_back_to_configured_launch_url():
    request = FakeRequest(lti_routes("Bearer t1"))
    context = FakeContext(request, ["Bearer t1"])
    cfg = COURSE.model_copy(update={"forum_launch_url": "/launch?blti_placement_id=_1_1"})
    collect_course(context, BASE, cfg, auth_timeout=0.01, now=NOW)
    assert context.launched == [f"{BASE}/launch?blti_placement_id=_1_1"]
    no_tool = {**contents_routes(), **lti_routes("Bearer t1")}
    no_tool[f"/learn/api/v1/courses/{COURSE_ID}/contents/_902_1/children"] = {"results": []}
    with pytest.raises(ForumToolNotFound):
        collect_course(
            FakeContext(FakeRequest(no_tool), ["Bearer t1"]), BASE, COURSE, auth_timeout=0.01
        )


def raw_course():
    forums = load("forums.json")
    launches = {"47441": load("thread_launch.json")}
    return load("init.json"), forums, launches


def make_config(tmp_path, courses=(COURSE,)):
    return AppConfig(
        profile_dir=tmp_path / "profile",
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        courses=list(courses),
    )


def run(tmp_path, check=lambda: "_5_1", collect=lambda c: raw_course(), notes=None):
    notes = [] if notes is None else notes
    store = Store(":memory:")
    code = run_collect(
        make_config(tmp_path),
        check=check,
        collect=collect,
        notifier=lambda t, m: notes.append((t, m)),
        now=NOW,
        store=store,
    )
    return code, store


def test_collect_end_to_end_writes_snapshot(tmp_path, capsys):
    code, store = run(tmp_path)
    assert code == 0
    snap = json.loads((tmp_path / "out" / "2026-10-07.json").read_text(encoding="utf-8"))
    assert list(snap) == ["schema_version", "generated_at", "session_expired", "courses"]
    assert snap["session_expired"] is False
    (course,) = snap["courses"]
    assert list(course) == [
        "bb_course_id",
        "name",
        "notion_course_url",
        "min_replies",
        "reply_scope",
        "min_reply_words",
        "ai_declaration",
        "pending_threads",
        "highest_priority",
        "next_due_at",
        "threads",
        "record",
    ]
    assert course["pending_threads"] == len(course["threads"]) > 0
    assert course["threads"][0]["session_number"] == 1
    assert course["next_due_at"].startswith("2026-10-09T23:59")
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"Fake Course: {course['pending_threads']} pending, "
        f"highest priority {course['highest_priority']}, next due {course['next_due_at']}"
    ]
    run_row = store.conn.execute("SELECT outcome, snapshot_path FROM runs").fetchone()
    assert run_row["outcome"] == "ok" and run_row["snapshot_path"].endswith("2026-10-07.json")


def test_collect_session_expired_exit_2(tmp_path):
    notes = []

    def expired():
        raise SessionExpired("/learn/api/v1/users/me")

    code, store = run(tmp_path, check=expired, notes=notes)
    assert code == 2
    assert len(notes) == 1 and "bbagent login" in notes[0][1]
    snap = json.loads((tmp_path / "out" / "2026-10-07.json").read_text(encoding="utf-8"))
    assert snap["session_expired"] is True and snap["courses"] == []
    assert store.conn.execute("SELECT outcome FROM runs").fetchone()[0] == "session_expired"


def test_collect_schema_error_exit_3_keeps_previous_snapshot(tmp_path):
    code, _ = run(tmp_path)
    assert code == 0
    path = tmp_path / "out" / "2026-10-07.json"
    before = path.read_text(encoding="utf-8")
    notes = []

    def broken(course):
        init, forums, launches = raw_course()
        del forums["data"][0]["title"]
        return init, forums, launches

    code, store = run(tmp_path, collect=broken, notes=notes)
    assert code == 3
    assert len(notes) == 1
    assert path.read_text(encoding="utf-8") == before
    assert store.conn.execute("SELECT outcome FROM runs").fetchone()[0].startswith("schema_error")


def test_collect_endpoint_error_exit_3_without_snapshot(tmp_path):
    def failing(course):
        raise EndpointError("GET /rest/forums -> 500")

    code, _ = run(tmp_path, collect=failing)
    assert code == 3
    assert not (tmp_path / "out").exists()


def test_status_reads_latest_snapshot(tmp_path, capsys):
    run(tmp_path)
    capsys.readouterr()
    config = tmp_path / "config.yaml"
    config.write_text(f"out_dir: {tmp_path / 'out'}\n", encoding="utf-8")
    assert main(["status", "--config", str(config)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("2026-10-07.json generated")
    assert lines[1].startswith("Fake Course: ")


def test_status_without_snapshot(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(f"out_dir: {tmp_path / 'none'}\n", encoding="utf-8")
    assert main(["status", "--config", str(config)]) == 1


def forums_with(*specs):
    """specs: (id, endDate ms or None, title)."""
    return {
        "data": [
            {
                "id": fid,
                "title": title,
                "creationDate": 1791143602508,
                "endDate": end,
                "ltiThreads": [{"id": f"{fid}1", "title": "T", "description": ""}],
            }
            for fid, end, title in specs
        ]
    }


def test_forum_ids_to_fetch_window():
    now = NOW
    day = 86_400_000
    ms = int(now.timestamp() * 1000)
    forums = forums_with(
        ("open", ms + day, "SESSION 1"),
        ("closed_recent", ms - 6 * day, "SESSION 2"),
        ("boundary", ms - 7 * day, "SESSION 3"),
        ("closed_old", ms - 8 * day, "SESSION 4"),
        ("no_due", None, "General"),
    )
    assert forum_ids_to_fetch(forums, now) == {"open", "closed_recent", "boundary", "no_due"}


def test_collect_course_skips_threads_of_old_forums():
    request = FakeRequest({**contents_routes(), **lti_routes("Bearer t1")})
    context = FakeContext(request, ["Bearer t1"])
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    _, _, launches = collect_course(context, BASE, COURSE, auth_timeout=0.01, now=far_future)
    assert launches == {}
    assert [u for u, _ in request.calls if "/launch" in u] == []


def test_collect_course_404_on_contents_is_course_not_found():
    request = FakeRequest(lti_routes("Bearer t1"))
    with pytest.raises(CourseNotFound, match="Course not found or no access: _100_1"):
        collect_course(
            FakeContext(request, ["Bearer t1"]), BASE, COURSE, auth_timeout=0.01, now=NOW
        )


def test_collect_course_not_found_exit_3_message(tmp_path, capsys):
    def missing(course):
        raise CourseNotFound(course.bb_course_id)

    notes = []
    code, _ = run(tmp_path, collect=missing, notes=notes)
    err = capsys.readouterr().err
    assert code == 3
    assert err.strip() == "Course not found or no access: _100_1 (check config.yaml)"
    assert "Schema error" not in err
    assert len(notes) == 1
