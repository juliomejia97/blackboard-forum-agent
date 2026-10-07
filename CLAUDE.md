# CLAUDE.md

## Project

`bbagent` collects IE Blackboard Ultra forum state into SQLite and a daily JSON snapshot. The `forum-drafter` Claude skill (`.claude/skills/forum-drafter/`) reads that snapshot plus Notion session notes and drafts posts. `bbagent publish --send` is the only code path that writes to Blackboard; every other request is a GET.

## Commands

```sh
uv sync && uv run playwright install chromium     # setup
uv run ruff format . && uv run ruff check . && uv run pytest -q   # format, lint, test
uv run pytest tests/test_rules.py::test_name -q   # single test
uv run bbagent collect [--headed]                 # needs a logged-in profile/ (run `bbagent login` first)
uv run bbagent status                             # latest out/*.json, no network
uv run bbagent publish --course _N_1 --thread ID [--parent ID] --text-file post.md   # dry run; add --send to POST
uv run python scripts/anonymize_har.py [data/capture.har] [tests/fixtures]           # regenerate fixtures
```

Ruff: line length 100, rules `E F I UP B`. Python >= 3.12 (`.python-version` pins 3.14).

`tests/test_bb_client.py::test_wait_until_closed_returns_and_flushes_har` launches real headless Chromium; it needs `playwright install chromium`. All other tests use fakes (`FakeRequest`/`FakeResponse` in `tests/test_collect.py`) and anonymized fixtures in `tests/fixtures/`.

## Architecture

Data source: forums are an IE LTI 1.3 tool at `ie-lti-forums.ie.edu`, not native Blackboard discussions. `bb_client.py` finds the "IE Forums" content item in the course content tree, launches it in the persistent Playwright context (`profile/`), captures the `Authorization` header from the first `/rest/*` request, then calls `/rest/init`, `/rest/forums` and `/rest/forums/{id}/threads/{id}/launch` via `context.request`. Course ids are Blackboard `_N_1`; forum, thread and message ids are numeric LTI ids stored as strings.

Pipeline in `cli.run_collect` / `process_course`:

1. `bb_client.check_session` (`/learn/api/v1/users/me`), then `collect_course` returns raw JSON per course.
2. `parse.py` (pure) turns raw JSON into pydantic models (`models.py`); raises `SchemaError` on shape changes.
3. `mapping.py` (pure) maps a forum title to a session number and parses the deadline from the title ("open until Friday 9 October", 23:59 Europe/Madrid) since `endDate` is usually null.
4. `store.py` upserts into `data/bbagent.db` and tracks per-run `first_seen_run` to compute `is_new`.
5. `rules.py` (pure) computes thread `status` (`needs_post`, `needs_replies`, `done`, `optional`), `priority` and the `missing_deadline` flag.
6. `snapshot.py` writes `out/YYYY-MM-DD.json` atomically. Schema (currently `schema_version: 3`) in `.claude/skills/forum-drafter/snapshot.md`; the drafter skill reads it, so keep the doc in sync with code changes.

`run_collect` takes `check`, `collect`, `notifier`, `now` and `store` as injected callables; tests drive it this way instead of a browser. `publish.py` (`run_publish`) follows the same pattern with an injected `api` and `recollect`.

Rules that span several modules:

- Own post first: no reply candidates until the thread has an own top-level post.
- Relevant reply: own reply to a classmate's message with `>= min_reply_words` words; counted per thread or per forum according to `reply_scope`.
- Snapshot `threads` holds only non-`done` threads; `record` holds all threads of forums open or closed at most 7 days ago.
- A `session_expired` snapshot never replaces a good snapshot of the same day; a schema error writes nothing.

Exit codes are part of the contract with schedulers and the drafter skill: `collect` 0 ok, 1 no or placeholder config, 2 session expired, 3 endpoint/schema change; `publish` adds 4 for refused by checks. `publish --send` makes exactly one POST and never retries.

## Local data

`profile/` (browser cookies), `data/` (SQLite, HAR, logs), `out/` (snapshots) and `config.yaml` are gitignored and contain classmates' posts and session cookies. Never commit them or copy their content into fixtures; use `scripts/anonymize_har.py`. Without `config.yaml`, defaults come from `config.example.yaml`.
