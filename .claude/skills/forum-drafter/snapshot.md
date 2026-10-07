# Snapshot schema

`bbagent collect` writes `out/YYYY-MM-DD.json` (local date of `generated_at`). The write is atomic
(temp file plus rename). A run with `session_expired: true` never replaces a good snapshot of the
same day, and a failed run (schema error) writes nothing. Key order is stable.

```
{
  "schema_version": 3,
  "generated_at": "2026-10-07T07:00:00+02:00",
  "session_expired": false,
  "courses": [
    {
      "bb_course_id": "_152245_1",
      "name": "...",
      "notion_course_url": "...",
      "min_replies": 2,
      "reply_scope": "forum",
      "min_reply_words": 30,
      "ai_declaration": "...",
      "pending_threads": 3,
      "highest_priority": "high",        // high | medium | low | null when nothing pending
      "next_due_at": "2026-10-09T23:59:00+02:00",   // earliest open deadline or null
      "threads": [ ...thread... ],
      "record": [ ...record entry... ]
    }
  ]
}
```

With `session_expired: true`, `courses` is empty.

## Thread

Only threads whose status is not `done` appear, ordered by priority (high, medium, low), then due
date (nulls last), then ids.

| Key | Meaning |
|---|---|
| `thread_id`, `forum_id` | LTI ids as strings |
| `forum_title`, `title` | Forum and thread titles |
| `prompt_text` | Thread prompt as plain text |
| `word_limit` | `{"min": n, "max": n}` or null |
| `is_optional` | Theme or optional thread |
| `status` | `needs_post`, `needs_replies`, `optional` |
| `priority` | `high`, `medium`, `low` |
| `flags` | `missing_deadline`, `closed`, `has_own_post` |
| `replies` | `done` (relevant replies counted by the course scope), `required`, `received` |
| `due_at` | ISO datetime or null (forum `endDate`, else parsed from the forum title) |
| `session_number` | Mapped Notion session number or null (`unmapped`) |
| `notion_course_url` | Course page in Notion |
| `my_posts` | Own top-level posts: `id`, `created_at`, `body_text`, `word_count` |
| `my_replies` | Own replies: `id`, `parent_id`, `parent_author_name`, `parent_is_mine` (true for an own comment on an own post; never counts toward progress), `created_at`, `body_text`, `word_count` |
| `candidates` | See below |

## Record

`record` lists ALL threads (status `done` included) of forums that are open or closed at most 7
days ago (a forum without `due_at` counts as open). It is the source for the published-post
sections of the Notion Foro page. Entries are ordered by `forum_id`, `thread_id`. Fields:
`thread_id`, `forum_id`, `forum_title`, `title`, `status` (`needs_post`, `needs_replies`, `done`,
`optional`), `is_optional`, `due_at`, `session_number`, `my_posts`, `my_replies` (same shapes as in
`threads`). No candidates. `threads` is unchanged and holds only pending threads.

## Candidates

Empty unless `flags.has_own_post` is true (rule: own post first). Otherwise, classmates' messages
(instructors excluded) that are either top-level posts or direct replies to one of my messages,
and that are new in this run or that I have not answered yet. Fields: `id`, `parent_id`,
`author_name`, `created_at`, `is_new` (first seen in this run), `is_reply_to_me`, `body_text`,
`word_count`.

On the first run ever, every message is `is_new`. A second run right after produces
`is_new: false` everywhere.
