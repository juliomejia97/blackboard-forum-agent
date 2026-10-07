# Blackboard Forum Agent: Design

## Context

Each week, every IE course in Blackboard Ultra opens one forum per session (for example "SESSION 2. Recap of Session 1 · open until Friday 9 October"). The forum has numbered mandatory questions and optional "Themes". Evaluation covers:

- one own post per question;
- at least N relevant replies to classmates;
- the AI-use declaration, using the sentence from the syllabus.

Julio currently checks all of this by hand. The goal is a daily agent that:

1. reads the forums of the selected courses;
2. prioritizes them according to the agreed rules;
3. drafts posts using the session notes in Notion (*Class Notes -> Course -> Sessions*);
4. records in a "Forum" page inside the session what has already been published.

**The agent never posts to Blackboard.**

Chosen approach: **hybrid**.

- A deterministic Python collector (Playwright + SQLite) produces a daily JSON snapshot.
- A scheduled Claude task reads that snapshot, queries Notion through the MCP connector, drafts, and writes to Notion.

## Rules

1. **Context first.** Before drafting, read the session notes and presentations in Notion. If they do not exist, the thread is marked `missing_context` and nothing is drafted.
2. **Own post first.** If the thread has no own post, the only deliverable is the post draft. No replies to classmates are proposed until the own post exists in Blackboard.
3. **Then classmates.** Pick 2-3 classmate messages worth answering and draft a reply that challenges, extends, or asks for evidence.
4. **Stop criterion (thread `done`).** The thread has an own post and enough relevant replies.
   - Relevant reply: an own reply to a classmate's message with at least `min_reply_words` words (default 30).
   - Per-course configurable threshold: `min_replies` (default 2) and `reply_scope: forum | thread` (default `forum`). With `forum`, replies count across the whole weekly forum.
   - `done` threads are not processed again.
5. **Optional Themes** never block the stop criterion. They are listed as `optional`.
6. **Priority.**
   - `high`: mandatory thread without own post, closing in 48 h or less.
   - `medium`: has an own post but lacks replies, or someone replied to Julio.
   - `low`: everything else.
7. **Forum to Notion session mapping.**
   - "Recap of Session N" maps to the N-th session of the course by date.
   - If the title has no number, map to the last session before the forum's opening date.
   - If nothing matches, the forum is `unmapped`: it appears in the index and is not drafted.
8. **Drafts.**
   - respect the word limit in the prompt;
   - end with the course's AI declaration sentence;
   - use the session notes and Julio's experience (Twilio, AWS, Spark, ClickHouse);
   - are a starting point; Julio rewrites them in his own voice.

### Deviation from the original plan: `missing_deadline` is a flag

The original plan listed `missing_deadline` as a value of `status`. In this design it is a boolean flag on the thread evaluation, because a thread with no deadline still has a meaningful status (`needs_post`, `needs_replies`, `done`, `optional`). `status` takes only those four values.

Priority and status interact as follows:

- `optional` and `done` threads are always `low`.
- A closed forum (`due_at` set and `now > due_at`) is `low`.
- A forum without `due_at` gets priority `medium` and `missing_deadline = true`, unless the thread is `done` or `optional`.
- `needs_post` with a deadline within 48 h is `high`.
- `needs_replies`, or any received reply, is `medium`.
- Anything else is `low`.

Definitions used by the rules engine:

- Own post in a thread: a message with `is_mine` whose `parent_id` is empty or equals the thread's root message.
- Relevant reply: an own message whose parent exists, is not mine, and has `word_count >= min_reply_words`.
- Reply count: with scope `thread`, relevant replies in that thread; with scope `forum`, relevant replies across all threads of the forum.
- Received reply: a classmate message whose parent is one of my messages.

## Location and stack

- New repository: `~/development/ie-university/blackboard-forum-agent/` (`git init`, branch `main`).
- Python 3.12 or later with `uv`, Playwright (Chromium), `pydantic`, `pyyaml`, `pytest`, `ruff`. SQLite from the standard library.
- `.gitignore`: `profile/`, `data/`, `out/`, `*.har`, `config.yaml`. The browser profile and snapshots contain cookies and classmates' posts, so they stay local.

## Architecture

```
blackboard-forum-agent/
  pyproject.toml
  config.example.yaml          # real config.yaml is gitignored
  src/bbagent/
    cli.py                     # bbagent login | capture | collect | status
    config.py                  # pydantic: CourseConfig, AppConfig
    models.py                  # Forum, Thread, Message (pydantic)
    bb_client.py               # Playwright persistent context + GET to JSON endpoints
    parse.py                   # Ultra JSON -> models (pure)
    rules.py                   # status + priority per thread (pure)
    mapping.py                 # forum title -> session number (pure)
    store.py                   # SQLite: messages, threads, runs
    snapshot.py                # writes out/YYYY-MM-DD.json
    notify.py                  # osascript display notification
  tests/
    fixtures/                  # captured, anonymized Ultra JSON
  launchd/com.jmejia.bbagent.plist
  claude/forum-drafter/SKILL.md   # drafter instructions
  docs/superpowers/specs/
  docs/endpoints.md               # spike result
```

## Data flow

1. **07:00, launchd runs `bbagent collect`.**
   1. Opens the persistent context in `profile/`, headless.
   2. GET `/learn/api/public/v1/users/me` to check the session. On 401 or an SSO redirect: macOS notification "Blackboard session expired: run `bbagent login`", writes a snapshot with `session_expired: true`, exits with code 2.
   3. For each course in `config.yaml`: forums, then threads, then messages through the endpoints documented in `docs/endpoints.md`.
   4. `parse`, then `store.upsert`. Messages whose `author_id` equals the current user are marked as own.
   5. `rules.evaluate_forum(course_cfg, forum, now)` computes `status` in `{needs_post, needs_replies, done, optional}`, `priority`, and the `missing_deadline` flag.
   6. `snapshot` writes only threads that are not `done`, with:
      - the prompt;
      - classmate messages that are new since the last run or still without an own reply;
      - published own posts and replies (for the record);
      - the mapped `session_number` and `notion_course_url`.
2. **07:30, scheduled Claude task runs the `forum-drafter` skill.**
   1. Reads the latest snapshot. If `session_expired`, it only updates the index with the notice.
   2. For each thread, in priority order:
      - finds the session in the course's sessions inline DB (ordered by date, index N);
      - reads its notes and presentations;
      - creates or updates the child page "Forum - <forum title>".
   3. Updates the "Foros hoy" page with one line per course: pending threads, highest priority, closing date, and link.
3. **Julio** reviews in Notion, rewrites, and publishes in Blackboard. The next run detects the post and advances the state.

### Notion "Forum" page format (inside the session)

- Header: forum, closing date, the course's reply rule, and progress (for example "Replies: 1/2").
- Per thread, ordered by priority:
  - status;
  - prompt (summary);
  - **My published post** (verbatim copy from the snapshot, only if it exists);
  - **Post draft** (only if `needs_post`);
  - **Reply candidates**: author, message excerpt, why it is worth answering, and a draft (only if `needs_replies`);
  - **My published replies**.
- The drafter rewrites only its own draft blocks. It never touches blocks Julio has edited: draft blocks carry a marker, and if the content differs from what was written, the block is left as is.

## Data source (spike result)

Forums are not native Blackboard discussions. They are an IE LTI 1.3 tool at `https://ie-lti-forums.ie.edu`, launched from Blackboard through `/webapps/blackboard/execute/blti/launchPlacement`. After the launch, the tool's SPA calls three JSON endpoints (`/rest/init`, `/rest/forums`, `/rest/forums/{id}/threads/{id}/launch`) with an `Authorization` header plus cookies. The collector launches the tool once per course in the persistent browser context, captures the header from the first `/rest/*` request, and reuses it through Playwright's `context.request`. Details and open questions: `docs/endpoints.md`.

Consequences for the design:

- Ids are LTI ids (numeric, stored as strings); the Blackboard course id (`_152245_1`) only identifies the course to launch.
- The forum has no structured deadline (`endDate` is null). The deadline is parsed from the forum title ("open until Friday 9 October") by `mapping.due_date_from_title`, as 23:59 Europe/Madrid. `endDate` wins when present. A title without a parsable date gives `missing_deadline`.
- Top-level posts have `idParent` 0; the parser maps it to `parent_id = None`.
- The session check is `GET /learn/api/v1/users/me` with the profile's cookies (401, 403, non-JSON or an SSO redirect means expired). The forums launch URL is derived from the course content tree (see `docs/endpoints.md`).
- Snapshot format: `docs/snapshot.md`.

## SQLite schema (`data/bbagent.db`)

- `messages(id PK, thread_id, forum_id, course_id, parent_id, author_id, author_name, is_mine, is_read, body_text, word_count, created_at, first_seen_at, first_seen_run)`
- `threads(id PK, forum_id, course_id, title, prompt_text, word_limit, is_optional, last_status, last_priority, updated_at)`
- `forums(id PK, course_id, title, due_at, session_number)`
- `runs(id PK, started_at, finished_at, outcome, snapshot_path)`

## Error handling

| Case | Behavior |
|---|---|
| Expired session | Notification, `session_expired` snapshot, exit 2. The drafter shows the notice in "Foros hoy". |
| Endpoint changes (404 or invalid schema) | `parse` raises `SchemaError` with the path. Recorded in `runs.outcome` with a notification and exit 3. The previous snapshot is not overwritten. |
| Course without new forum | Empty snapshot for that course. The index says "no pending items". |
| Forum without closing date | `missing_deadline = true`, priority `medium`. |
| Notion session not found | `unmapped` in the index, no drafting. |
| Empty session notes | `missing_context` in the Forum page, no drafting. |
| Claude app closed at 07:30 | The task runs when the app opens (scheduled task behavior). The snapshot remains available. |

## Out of scope

- Posting to Blackboard.
- Running in the cloud or without the Mac.
- Push notifications beyond macOS notifications.
- Courses without the weekly forum structure.
