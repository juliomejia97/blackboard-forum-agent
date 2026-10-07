# bbagent

Daily Blackboard (Ultra) forum monitor. Collects forum state into SQLite and a JSON snapshot; a
scheduled Claude task drafts posts in Notion. Only `bbagent publish --send` writes to Blackboard.

## Setup

```sh
uv sync
uv run playwright install chromium
cp config.example.yaml config.yaml   # edit courses
```

Without `config.yaml`, `config.example.yaml` defaults are used for `base_url`, `profile_dir` and `data_dir`.

## Usage

```sh
uv run bbagent login     # headed Chromium on profile/; log in with SSO/MFA, then close the window
uv run bbagent courses   # list starred course ids, names and terms to fill config.yaml (--all: not only starred, --term TEXT filters)
uv run bbagent collect   # headless: forums -> SQLite -> out/YYYY-MM-DD.json; one line per course
uv run bbagent status    # same per-course lines from the latest out/*.json, no network
uv run bbagent publish --course _N_1 --thread 47441 [--parent 804989] --text-file post.md [--send]
uv run bbagent capture   # like login, recording data/capture.har; prints /learn/api/ endpoints seen
```

`collect` exit codes: 0 ok, 1 no courses configured, 2 session expired (run `login`; a snapshot with
`session_expired: true` is written), 3 endpoint or schema change (no snapshot written). Codes 2 and
3 raise a macOS notification. Only GET requests are sent (`publish --send` is the single exception). The forums launch URL is derived from the
course content tree; if that fails, set `forum_launch_url` on the course in `config.yaml`.

`publish` is a dry run unless `--send` is given: it prints course, forum and thread titles, the parent
author and excerpt, the final text, word count and the JSON payload, after checking against a fresh
thread launch that the thread exists in the course, the text is not already posted by you, the message
limit is not reached, `--parent` exists and is not yours, and the text has no `<model>` or `[...]`
placeholder (citations like `[1]` are fine). Input is light Markdown: blank-line paragraphs, single
newline = line break, `**bold**`, `[text](url)` and bare URLs. Exit codes: 0 dry run ok or published, 2
session expired, 3 API error or unverified result, 4 refused by the checks. `--send` makes exactly one
POST, never retries (check the thread before retrying), verifies creator and parent in the response,
then collects that course only (other courses stay in the day's snapshot).

## Forum drafter (Claude skill)

The skill lives at `.claude/skills/forum-drafter` in the project (`SKILL.md`, `profile.md`). A daily scheduled task
runs it after `collect`: it reads the latest `out/*.json`, reads the course session notes in Notion
(Notion MCP connector), and writes drafts to a `Foro – <forum title>` page under each session page.
Publishing is a separate, explicitly approved step (`bbagent publish`).

Add your own experiences to `.claude/skills/forum-drafter/profile.md`; drafts only use facts listed
there or in the session notes. The skill loads the `writer` skill before drafting.

## Tests

```sh
uv run ruff format . && uv run ruff check . && uv run pytest -q
```
