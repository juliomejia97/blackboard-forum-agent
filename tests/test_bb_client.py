from pathlib import Path

from bbagent.bb_client import summarize_har

FIXTURE = Path(__file__).parent / "fixtures" / "sample.har"


def test_summarize_har_groups_and_filters():
    assert summarize_har(FIXTURE) == [
        "GET 200 /learn/api/v1/courses/_1_1/contents (x2)",
        "POST 204 /learn/api/v1/telemetry (x1)",
        "GET 401 /learn/api/v1/users/me (x1)",
    ]


def test_wait_until_closed_returns_and_flushes_har(tmp_path):
    from playwright.sync_api import sync_playwright

    from bbagent.bb_client import wait_until_closed

    har = tmp_path / "t.har"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            headless=True,
            record_har_path=str(har),
            record_har_content="embed",
        )
        ctx.pages[0].goto("about:blank")
        ctx.new_page().close()
        ctx.pages[0].close()
        wait_until_closed(ctx)
        ctx.close()
    assert har.exists()


def test_wait_until_closed_ignores_closed_pages_still_listed():
    from bbagent.bb_client import wait_until_closed

    class FakePage:
        def __init__(self, closed):
            self.closed = closed
            self.waited = False

        def is_closed(self):
            return self.closed

        def wait_for_event(self, event, timeout):
            self.waited = True
            self.closed = True

    class FakeContext:
        def __init__(self, pages):
            self.pages = pages

    stale, live = FakePage(True), FakePage(False)
    wait_until_closed(FakeContext([stale, live]))
    assert live.waited and not stale.waited
    wait_until_closed(FakeContext([stale]))
