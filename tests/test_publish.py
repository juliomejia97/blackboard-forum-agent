import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from test_collect import (
    BASE,
    COURSE_ID,
    FakeContext,
    FakeRequest,
    FakeResponse,
    contents_routes,
    load,
    lti_routes,
)

import bbagent.cli as cli
from bbagent.bb_client import (
    LTI_ORIGIN,
    PostError,
    SessionExpired,
    build_post_payload,
    post_message,
)
from bbagent.cli import main
from bbagent.parse import html_to_text
from bbagent.publish import text_problems, to_post_html

LINK = 'target="_blank" rel="noopener noreferrer"'
THREAD = 47441
FORUM = 11763
POSTS_URL = f"{LTI_ORIGIN}/rest/forums/{FORUM}/threads/{THREAD}/posts"
PARENT = 804989  # fixture post by A00000003 (Student 3), not mine
OWN_PARENT = 804918  # fixture post by A00000001 (me)


# --- to_post_html ---------------------------------------------------------------------------


def test_paragraphs_and_line_breaks():
    html, length = to_post_html("One\ntwo\n\n\nThree\r\n")
    assert html == "<p>One<br>two</p><p>Three</p>"
    assert length == len("One\ntwo\nThree")


def test_bold_and_links():
    html, length = to_post_html("A **key** idea, see [the paper](https://a.test/x?y=1&z=2).")
    assert html == (
        f'<p>A <strong>key</strong> idea, see <a href="https://a.test/x?y=1&amp;z=2" {LINK}>'
        "the paper</a>.</p>"
    )
    assert length == len("A key idea, see the paper.")


def test_bare_urls_drop_trailing_punctuation():
    html, _ = to_post_html("Read https://a.test/p. Also (https://b.test/q) now, **https://c.test**")
    assert f'<a href="https://a.test/p" {LINK}>https://a.test/p</a>.' in html
    assert f'(<a href="https://b.test/q" {LINK}>https://b.test/q</a>)' in html
    assert f'<strong><a href="https://c.test" {LINK}>https://c.test</a></strong>' in html


def test_escaping_and_no_raw_html():
    html, length = to_post_html('<b>x</b> & "q" <model> **<i>**')
    assert html == '<p>&lt;b&gt;x&lt;/b&gt; &amp; "q" &lt;model&gt; <strong>&lt;i&gt;</strong></p>'
    assert length == len('<b>x</b> & "q" <model> <i>')


def test_unicode_kept_and_counted_in_characters():
    html, length = to_post_html("Año 2026: modelos fiables — café")
    assert html == "<p>Año 2026: modelos fiables — café</p>"
    assert length == len("Año 2026: modelos fiables — café")


def test_post_length_is_plain_text_with_paragraphs_joined_by_newline():
    # Paragraphs concatenated give 6 characters; the "\n" between them adds one. That is the same
    # +1 as the real sample (2229 reported vs 2228 characters of tag-stripped text).
    html, length = to_post_html("abc\n\ndef")
    assert html == "<p>abc</p><p>def</p>"
    assert length == 7 == len("abcdef") + 1


# --- text_problems --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", "empty"),
        ("  \n\n ", "empty"),
        ("I used <model> here", "<model>"),
        ("Fill [Your example: system]", "[Your example: system]"),
        ("[Sugerencia: algo]", "placeholder"),
        ("[]", "placeholder"),
    ],
)
def test_text_problems_flags(text, expected):
    (problem,) = text_problems(text)
    assert expected in problem


@pytest.mark.parametrize(
    "text", ["As shown [1] and [2, 3] and [4-6].", "See [paper](https://a.test/x) now", "plain"]
)
def test_text_problems_allows_citations_and_links(text):
    assert text_problems(text) == []


# --- post_message / payloads ----------------------------------------------------------------


class PostingRequest(FakeRequest):
    def __init__(self, routes, post_response=None):
        super().__init__(routes)
        self.posts = []
        self.post_response = post_response

    def post(self, url, data=None, headers=None, **kwargs):
        self.posts.append((url, data, headers))
        return self.post_response


class TextResponse(FakeResponse):
    def text(self):
        return "boom\n  detail"


def test_payload_shapes_match_spec():
    top = build_post_payload("<p>x</p>", 1, None)
    assert top == {"postText": "<p>x</p>", "postLength": 1, "usersCited": []}
    assert list(top) == ["postText", "postLength", "usersCited"]
    reply = build_post_payload("<p>x</p>", 1, 804989)
    assert list(reply) == ["postText", "postLength", "usersCited", "idParent", "quote"]
    assert reply["idParent"] == 804989 and reply["quote"] is None


def test_post_message_request_shape_and_result():
    req = PostingRequest({}, FakeResponse(200, {"id": 9, "creator": "A1"}))
    created = post_message(req, "Bearer t", 11, 22, "<p>é</p>", 1, 7)
    assert created["id"] == 9
    ((url, data, headers),) = req.posts
    assert url == f"{LTI_ORIGIN}/rest/forums/11/threads/22/posts"
    assert headers["Authorization"] == "Bearer t"
    assert headers["Content-Type"] == "application/json;charset=UTF-8"
    assert json.loads(data) == {
        "postText": "<p>é</p>",
        "postLength": 1,
        "usersCited": [],
        "idParent": 7,
        "quote": None,
    }


def test_post_message_errors():
    with pytest.raises(SessionExpired):
        post_message(PostingRequest({}, FakeResponse(401, {})), "t", 1, 2, "<p>x</p>", 1, None)
    with pytest.raises(PostError, match="500: boom detail"):
        post_message(PostingRequest({}, TextResponse(500, {})), "t", 1, 2, "<p>x</p>", 1, None)


# --- CLI end to end with fakes ----------------------------------------------------------------


def launch_with(**changes):
    body = copy.deepcopy(load("thread_launch.json"))
    body["data"].update(changes)
    return body


def created_post(creator="A00000001", parent=0):
    return {"id": 900001, "idThread": THREAD, "idParent": parent, "creator": creator}


class _FixedDatetime:
    @staticmethod
    def now():
        return datetime(2026, 10, 7, 7, 0, tzinfo=UTC)


def guarded(body):
    def route(headers):
        ok = headers and headers.get("Authorization") == "Bearer t1"
        return body if ok else FakeResponse(401, {})

    return route


class Env:
    def __init__(self, monkeypatch, tmp_path, launch=None, post=None):
        routes = {**contents_routes(), **lti_routes("Bearer t1")}
        for other in load("forums.json")["data"][0]["ltiThreads"]:
            if other["id"] != THREAD:  # empty but valid launches so the collect step can parse
                body = launch_with(id=other["id"], title=other["title"], ltiPosts=[])
                routes[f"/rest/forums/{FORUM}/threads/{other['id']}/launch"] = guarded(body)
        if launch is not None:
            routes[f"/rest/forums/{FORUM}/threads/{THREAD}/launch"] = guarded(launch)
        self.request = PostingRequest(routes, post)
        context = FakeContext(self.request, ["Bearer t1"] * 5)

        @contextmanager
        def fake_open(config, headless=True):
            yield context

        monkeypatch.setattr(cli, "open_context", fake_open)
        monkeypatch.setattr(cli, "check_session", lambda ctx, base: "_5_1")
        monkeypatch.setattr(cli, "notify", lambda t, m: None)
        monkeypatch.setattr(cli, "datetime", _FixedDatetime)
        self.tmp = tmp_path
        self.config_path = tmp_path / "config.yaml"
        self.config_path.write_text(
            f"base_url: {BASE}\n"
            f"profile_dir: {tmp_path / 'profile'}\n"
            f"data_dir: {tmp_path / 'data'}\n"
            f"out_dir: {tmp_path / 'out'}\n"
            "courses:\n"
            f"  - bb_course_id: {COURSE_ID}\n"
            "    name: Fake Course\n"
            "    notion_course_url: https://notion.example/course\n"
            '    ai_declaration: "AI <model> used."\n',
            encoding="utf-8",
        )
        self.text = tmp_path / "post.md"

    def run(self, text, *extra, course=COURSE_ID, thread=THREAD):
        self.text.write_text(text, encoding="utf-8")
        return main(
            ["publish", "--config", str(self.config_path), "--course", course]
            + ["--thread", str(thread), "--text-file", str(self.text), *extra]
        )


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path, post=FakeResponse(200, created_post()))


def test_dry_run_prints_everything_and_sends_nothing(env, capsys):
    assert env.run("Hello **world**\nsecond line [1]") == 0
    out = capsys.readouterr().out
    assert "Course: Fake Course" in out
    assert "Forum: SESSION 2. Recap of Session 1" in out
    assert "Thread: 2. A responsibility map" in out
    assert "Words: 5" in out
    assert "Hello world\nsecond line [1]" in out
    payload = {
        "postText": "<p>Hello <strong>world</strong><br>second line [1]</p>",
        "postLength": len("Hello world\nsecond line [1]"),
        "usersCited": [],
    }
    assert json.dumps(payload, ensure_ascii=False, indent=2) in out
    assert "Dry run" in out
    assert env.request.posts == []
    assert not (env.tmp / "out").exists()  # no collect either


def test_dry_run_reply_shows_parent_first_name_and_excerpt(env, capsys):
    assert env.run("Reply text", "--parent", str(PARENT)) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("Reply to: "))
    assert line.startswith("Reply to: Student: ")
    excerpt = line.split(": ", 2)[2]
    assert 0 < len(excerpt.replace(" ...", "").split()) <= 25
    assert '"idParent": 804989' in out and '"quote": null' in out
    assert env.request.posts == []


def refused(env, capsys, text="A fine post", *extra, **kw):
    assert env.run(text, *extra, **kw) == 4
    captured = capsys.readouterr()
    assert env.request.posts == []
    return captured.out


def test_refuses_course_not_in_config(env, capsys):
    assert "not in config.yaml" in refused(env, capsys, course="_999_1")


def test_refuses_unknown_thread(env, capsys):
    assert "thread 1 not found" in refused(env, capsys, thread=1)


@pytest.mark.parametrize(
    "text,reason", [("", "empty"), ("<model> wrote this", "<model>"), ("Fill [X here]", "[X here]")]
)
def test_refuses_bad_text_without_network(env, capsys, text, reason):
    assert reason in refused(env, capsys, text)
    assert env.request.calls == []


def test_refuses_duplicate_own_text(env, capsys):
    posts = load("thread_launch.json")["data"]["ltiPosts"]
    own = next(p for p in posts if p["creator"] == "A00000001" and p["idParent"] == 0)
    text = "  " + html_to_text(own["postText"]).replace("\n", "  \n ").upper()
    assert "same text already exists" in refused(env, capsys, text)


def test_other_authors_same_text_is_not_a_duplicate(env, capsys):
    posts = load("thread_launch.json")["data"]["ltiPosts"]
    other = next(p for p in posts if p["creator"] == "A00000003" and p["idParent"] == 0)
    assert env.run(html_to_text(other["postText"])) == 0


def test_refuses_message_limit(monkeypatch, tmp_path, capsys):
    env = Env(monkeypatch, tmp_path, launch=launch_with(numMessagesOfUser=7, numMessagesAllowed=7))
    assert "limit reached (7 of 7" in refused(env, capsys)


def test_unlimited_when_allowed_is_zero_or_null(monkeypatch, tmp_path):
    for allowed in (0, None):
        env = Env(monkeypatch, tmp_path, launch=launch_with(numMessagesAllowed=allowed))
        assert env.run("A fine post") == 0


def test_refuses_missing_parent_and_own_parent(env, capsys):
    assert "parent message 1 not found" in refused(env, capsys, "x ok", "--parent", "1")
    assert "authored by you" in refused(env, capsys, "x ok", "--parent", str(OWN_PARENT))


def test_send_top_level_posts_once_verifies_and_collects(env, capsys):
    assert env.run("Hello **world**", "--send") == 0
    ((url, data, headers),) = env.request.posts
    assert url == POSTS_URL
    assert headers["Authorization"] == "Bearer t1"
    assert headers["Content-Type"] == "application/json;charset=UTF-8"
    assert json.loads(data) == {
        "postText": "<p>Hello <strong>world</strong></p>",
        "postLength": len("Hello world"),
        "usersCited": [],
    }
    out = capsys.readouterr().out
    assert "Published message 900001" in out
    assert "Fake Course: " in out  # run_collect summary line
    assert "Fake Course / 2. A responsibility map: status " in out
    snap = json.loads((env.tmp / "out" / "2026-10-07.json").read_text(encoding="utf-8"))
    assert [c["bb_course_id"] for c in snap["courses"]] == [COURSE_ID]
    # collect ran for the course after the post: /rest/init fetched again
    assert sum(1 for u, _ in env.request.calls if u.endswith("/rest/init")) >= 2


def test_send_reply_payload_and_idparent_verification(monkeypatch, tmp_path, capsys):
    env = Env(monkeypatch, tmp_path, post=FakeResponse(201, created_post(parent=PARENT)))
    assert env.run("Reply", "--parent", str(PARENT), "--send") == 0
    ((_, data, _),) = env.request.posts
    assert json.loads(data) == {
        "postText": "<p>Reply</p>",
        "postLength": 5,
        "usersCited": [],
        "idParent": PARENT,
        "quote": None,
    }
    assert "Published message 900001" in capsys.readouterr().out


@pytest.mark.parametrize(
    "created,extra",
    [
        (created_post(creator="A00000099"), []),
        (created_post(parent=1), ["--parent", str(PARENT)]),
    ],
)
def test_send_verification_mismatch_exit_3(monkeypatch, tmp_path, capsys, created, extra):
    env = Env(monkeypatch, tmp_path, post=FakeResponse(200, created))
    assert env.run("Reply", *extra, "--send") == 3
    assert "verification failed" in capsys.readouterr().out
    assert len(env.request.posts) == 1


def test_send_post_error_exit_3_no_retry_no_collect(monkeypatch, tmp_path, capsys):
    env = Env(monkeypatch, tmp_path, post=TextResponse(500, {}))
    assert env.run("Hello", "--send") == 3
    out = capsys.readouterr().out
    assert "POST failed (not retried)" in out and "check the thread" in out
    assert len(env.request.posts) == 1
    assert not (env.tmp / "out").exists()


def test_send_session_expired_on_post_exit_2(monkeypatch, tmp_path, capsys):
    env = Env(monkeypatch, tmp_path, post=FakeResponse(401, {}))
    assert env.run("Hello", "--send") == 2
    assert "bbagent login" in capsys.readouterr().err
    assert len(env.request.posts) == 1


def test_session_expired_on_check_exit_2(env, monkeypatch):
    def expired(ctx, base):
        raise SessionExpired("x")

    monkeypatch.setattr(cli, "check_session", expired)
    assert env.run("Hello", "--send") == 2
    assert env.request.posts == []


def test_collect_after_publish_keeps_other_courses(env):
    out_dir = env.tmp / "out"
    out_dir.mkdir()
    existing = {
        "generated_at": "2026-10-07T00:00:00",
        "session_expired": False,
        "courses": [{"bb_course_id": "_other_1", "name": "Other"}],
    }
    (out_dir / "2026-10-07.json").write_text(json.dumps(existing), encoding="utf-8")
    assert env.run("Hello", "--send") == 0
    snap = json.loads((out_dir / "2026-10-07.json").read_text(encoding="utf-8"))
    assert [c["bb_course_id"] for c in snap["courses"]] == ["_other_1", COURSE_ID]
