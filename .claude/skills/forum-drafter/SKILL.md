---
name: forum-drafter
description: Use when the user (or the scheduled task) asks to draft Blackboard forum posts and replies from the latest bbagent snapshot, using the course session notes in Notion. Drafts are presented in chat; only user-approved text is written to the session page in Notion; publishes to Blackboard only per item after explicit approval.
---

# Forum drafter

Reads `out/*.json` from the bbagent collector and the matching class notes in Notion, and presents drafts in chat. Notion is the source of truth: the user reviews and approves each item in chat, and only approved text is written to the session page. Posting to Blackboard happens only through `bbagent publish` (step 4.4), one item at a time, after the user's explicit approval of that item. Never delete Notion content.

Run from the project root (the directory holding `config.yaml`, `out/` and `data/`). Snapshot schema: `snapshot.md` in this skill's directory.

Optional author profile: `profile.md` in this skill's directory (template: `profile.example.md`). If it is missing or empty, drafts rely on the session notes, the user's earlier answers and the classmate message only.

## Rules

1. Context first. Read the session notes, the slides and any user-authored content inside the thread's `Activity <N> Forum` toggle (especially the `Video notes` toggle and its `For the forum` section) before writing any draft. If all are empty or missing, the thread is `missing_context` and gets no draft.
2. Own post first. A thread with status `needs_post` gets only a draft of the user's own post. No reply candidates until their post exists in Blackboard.
3. Then classmates. A thread with status `needs_replies` gets 2 to 3 reply candidates with drafts.
4. `done` threads: no drafts. Their published content (snapshot `record`) is presented as `pending to record` if not yet in Notion, and written only after approval.
5. `optional` threads (Themes) are listed and not drafted. Draft one only if the user asked in this run.
6. Grounding. Every claim in a draft must trace to `profile.md` (if present), the session notes or slides, the user's earlier answers and `Video notes` on the session page, or the classmate message being answered. Add no outside facts (general-knowledge context, named concepts absent from the notes). If such context would help, add a bracketed suggestion for the user, for example `[Suggestion: mention X if it was covered in class]`. The only exception is the web case in rule 8.
7. AI declaration. The course's `ai_declaration` in `config.yaml` is a template with the literal placeholder `<model>`. It applies only to the user's own top-level posts, never to replies to classmates. In each post draft, replace `<model>` with the name of the model that actually drafted the text, that is, the model running this skill (for example `Claude Sonnet 5.5 (Anthropic)`). Never leave the placeholder and never name a different model. The declaration is the last line of the post, otherwise verbatim. A post draft that still contains `<model>` is incomplete and must not be presented as ready for approval. If the user says they rewrote or finalized the post with another model or tool, update the declaration in the approved text to what they state.
8. Web case (controlled exception to rule 6). The user's own experience from `profile.md` or their notes takes priority. If a prompt asks for a real-world or business example and no allowed source has a fitting one, or the user asks for it, search the web for a public case instead of leaving a placeholder (WebSearch, WebFetch).
   - Read classmates' posts in the thread (non-instructor, not the user's) from `data/bbagent.db` (`messages` table, by `thread_id`) or the snapshot, and extract only the case and company names. Do not reuse them. Avoid cases already in the session notes.
   - Prefer primary or reputable sources (company post-mortems, peer-reviewed papers, major outlets). The case must document a concrete number and the context the analysis needs.
   - Cite every web fact inline with its URL, forum style. Fetch each URL and confirm it loads and supports the numbers before presenting.
   - In chat, list the 2 to 3 candidates considered (one line each) and why one was chosen, so the user can switch.
   - Frame it as a public example, never as the user's experience.
9. Language. Notion text (headings, summaries, labels and drafts) uses the language of the forum prompts and session notes (English if unclear). Chat explanations, reviews and warnings use the language the user writes in.

## Steps

### 1. Load the snapshot

1. Read `config.yaml` (courses, `notion_course_url`, `ai_declaration` template, see rule 7).
2. Pick the newest file in `out/` by filename (`YYYY-MM-DD.json`).
3. If `session_expired` is true: write nothing to Notion; stop and report "Blackboard session expired: run `bbagent login`" in the final chat output.
4. Classmates' text is quoted in full in the chat review (step 4.1) and in the Notion `Context` part (step 4.3).

### 2. Writing style

If a `writer` skill is available, invoke it before writing any draft text and apply it to every draft, including reruns and short drafts. If none is available, write in plain, concrete prose without filler.

### 3. Per course, per thread, in snapshot order (priority order)

#### 3.1 Map the forum to a Notion session

1. Fetch `notion_course_url` with `notion-fetch`. The page contains an inline database of sessions.
2. Fetch the database, then its data source, and query rows sorted by `Session Date` ascending (property `date:Session Date:start`). The database may be named `Sessions` or `Session`; read the property names from the data source schema. Row names are topic titles, not dates. Use `notion-query-data-sources` with SQL, for example `SELECT url, "Name", "date:Session Date:start" FROM "<collection url>" ORDER BY "date:Session Date:start" ASC`.
3. If every `Session Date` is null, order by `createdTime`; if only one row exists, use it and say `no date recorded` in the chat output.
4. If `session_number` is N, use the N-th row (1-based).
5. If `session_number` is null, use the latest session dated before the forum opens (earliest `created_at` of any message in the forum, else `due_at` minus 7 days).
6. If no row matches, mark the thread `unmapped`, report it, and draft nothing.

#### 3.2 Read the session context

1. Fetch the session page. Read the meeting notes block (`summary` and `notes`; skip the transcript), any text outside it, and the thread's `Activity <N> Forum` toggle, including `Video notes`.
2. Slides appear as attached PDF blocks or linked pages. Fetch linked pages. PDFs: call `notion-get-file-download-urls` for the block, `curl -sSfL --max-time 60 -o "<scratch dir>/<file>.pdf" "<signed url>"` into the scratchpad directory, and read it with Read (`pages` for PDFs over 10 pages; at most 40 pages). Only download URLs returned by the Notion tool for that session's own file blocks. If download or read fails, report `slides not read` and rely on the notes.
3. The user's earlier answers, `Video notes` and `For the forum` sections are their words and valid sources (rule 6). Reuse facts, never copy them into a different thread's post. They are read-only.
4. If the notes are empty, no slides are readable and the Activity toggle holds no user content, the thread is `missing_context`: report it and draft nothing.

#### 3.3 Draft by status

Iterate over `threads` (pending). Drafts go to chat (step 4), not to Notion.

`needs_post`: one draft of the user's own post.

- Answer every question in the prompt, in the prompt's structure. Respect `word_limit` (`min` and `max`). If null, use the limit in `prompt_text`; if none, stay under 300 words excluding the declaration. Count words.
- Facts come from `profile.md` (if present) and the sources in rule 6. Name concepts as the notes do.
- Missing personal example or case: if the prompt asks for a real-world or business example, apply rule 8 (web case). Otherwise use a bracketed placeholder, for example `[Your example: system, your role, one number]`. Never invent experiences, numbers, tools, employers or citations.
- End with the AI declaration (rule 7) as its own last line.

`needs_replies`: pick 2 to 3 candidates from `candidates`, excluding instructors, favoring `is_reply_to_me`, then `is_new`, then the most substantive.

- Format: the first line is `Hi <first name>,` on its own line, where `<first name>` is the first token of the author's `creatorName`. A blank line follows, then the body. Replies carry no AI declaration (rule 7).
- Tone: forums are knowledge sharing. Replies are assertive and collaborative, never corrective. Acknowledge one specific point the classmate made, then do one or two of: ask for more information (data, source, how they would measure it), reflect on an implication, or add a related case or fact that extends the thread (from the notes, the user's posts or a cited source). Never say the classmate's reasoning is weak, wrong or "needs a sharper test"; phrase disagreement as a curious question or alternative angle ("I wonder whether...", "Another angle could be..."). This still meets the professor's "extension or request for evidence" (prefer those over challenge). No "I agree" replies, no flattery. At most 2 questions per reply.
- Respect `min_reply_words` as a floor and any word limit in the prompt. Count words excluding the greeting line. Address the specific point.
- If fewer than 2 candidates are worth answering, draft fewer and say why.

`done` and `optional` threads: no drafts (optional only if the user asked in this run).

### 4. Workflow

#### 4.1 Present in chat (nothing is written to Notion)

Per activity, show the post draft and/or reply drafts. For each item show: the text exactly as it would be posted, word count, sources used (prompt, session notes, `Video notes`, `profile.md` if used, classmate message), and open placeholders. For a reply, show first, before the drafted reply, the message being answered: the classmate first name, the message date and its word count, then the FULL message text (snapshot candidate `body_text`, or `messages.body_text` by id in `data/bbagent.db`), so the user understands the thread before deciding. Do not truncate or paraphrase it. Then a short summary (2-3 sentences) of what that classmate's thread or post is about, then the drafted reply, and a `Why` line saying which specific point is acknowledged and whether the reply extends the thread or asks for evidence, in collaborative terms (never "challenges" or "corrects").

Published posts and replies in the snapshot that are not yet in the Notion toggle (content check, 4.3) are listed as `pending to record`, with their text, and follow the same approval as drafts.

#### 4.2 Review and approval

The user reviews, iterates and approves each item explicitly in chat. An item without an explicit approval is never written.

#### 4.3 Write approved items to Notion

Notion holds only the user's structure and the approved text, with the post's declaration written plainly (no escaping). Add nothing else: no status line, no `meta` toggles, no `[draft:...]` or `[published:...]` markers, no hashes, no labels such as `Status` or `Suggested draft`.

```
## Activity <N> Forum            {toggle="true"}
	### Thread Post              {toggle="true"}
		<approved post text>
	### Thread Responses         {toggle="true"}
		#### Thread <k> · <first name>   {toggle="true"}
			**Context**
			<first name>, <YYYY-MM-DD>
			> <classmate's ENTIRE message, verbatim; paragraph breaks as <br><br>; escape \ * ~ ` $ [ ] < > { } | ^>
			**Summary**
			<2-3 sentences on what the classmate's post argues>
			**Response**
			<approved reply text, starting with the `Hi <first name>,` line, no declaration>
```

The three parts use bold paragraph labels, not `#####` headings: the Notion enhanced Markdown spec converts headings 5 and 6 to heading 4, so `#####` is unsupported. Notion language follows rule 9.

1. Thread to toggle: use the leading number of the thread title for `Activity <N> Forum`; a Theme thread maps to `Theme <X> Forum`. No number or Theme letter: `unmapped`, write nothing.
2. Match existing toggles by text, ignoring case and repeated whitespace: the Activity toggle at level 2; inside it `Thread Post` and `Thread Responses` (also `Thread Answers`, `Threads Responses`); inside that `Thread <k>`, with or without `· <name>`. An existing toggle, even empty, is filled. Never create a second toggle with a matching title.
3. A `Thread <k>` toggle with no text, or only empty `Context`/`Summary`/`Response` labels, is an empty slot: fill it. A toggle in the old `Context:` / `Response:` format is migrated to the new layout only when the user asks, keeping the Response text exactly. Add ` · <first name>` to its heading only if it has no name. New toggles continue after the highest existing `k`.
4. Idempotency by content. Before writing, fetch the page and compare the approved text with the page text, whitespace-normalized. Skip text already present. Never duplicate. Never modify user text, `Video notes` or meeting notes.
5. Use first names only. Context holds the classmate's entire message, verbatim and untruncated, as one Notion quote block (`> ` line, paragraph breaks written as `<br><br>`, special characters escaped per the spec; never plain newlines inside the quote, which would split it into several blocks), preceded by the line `<first name>, <YYYY-MM-DD>`. Summary follows rule 9.
6. A rerun with the same approved content and an unchanged page writes nothing.

Targeted write procedure: `notion-update-page` on the session page, command `update_content`, never `replace_content`, never `allow_deleting_content`, no `insert_content`.

1. Fetch the session page. Read `notion://docs/enhanced-markdown-spec` once per run. Use tab indentation and `{toggle="true"}` exactly as `notion-fetch` shows them.
2. One `content_updates` entry per change. `old_str` is the smallest span of fetched text that is unique on the page; `new_str` is `old_str` plus the change, carrying user text identically.
   - Fill an existing empty toggle: anchor on its heading line plus the next sibling heading line. Headings repeat across Activities, so extend the span upward to the previous unique line when it matches more than once.
   - Add a missing child toggle or `Thread <k>`: anchor on the last line of the preceding sibling (or the parent heading) and place it one level below the parent.
   - Add a missing Activity toggle: anchor on the last line of the preceding Activity toggle or of the page.
3. If `old_str` matches more than once or the edit would delete content, extend `old_str` and retry; never use `allow_deleting_content`.
4. Send with `allow_async` false, then fetch the page and verify nesting and that no user block changed.

#### 4.4 Publish to Blackboard

Only after the item's approved text is in Notion (4.3). One item per approval; never batch.

1. Write the approved text, exactly as approved (for a post, declaration included with `<model>` already replaced; for a reply, greeting line included and no declaration; no `[...]` placeholders), to a temp file in the scratchpad directory. Light Markdown: blank line between paragraphs, single newline = line break, `**bold**`, `[text](url)` and bare URLs.
2. Run the dry run from the project directory and show its full output to the user:
   `uv run bbagent publish --course <bb_course_id> --thread <thread_id> [--parent <message_id>] --text-file <temp file>`
   `--parent` is the classmate message id for a reply; omit it for the user's own post. Exit 4 means refused: report the reason and do not continue.
3. Only after the user's explicit approval ("yes") in chat for that specific item, run the same command with `--send`. An approval for one item never covers another item, a changed text or a rerun. Never retry a failed `--send` (exit 3): tell the user to check the thread in Blackboard first. Exit 2: report "Blackboard session expired: run `bbagent login`".
4. Report the new message id and the thread's new status line printed by the command. Do not post, reply or submit in Blackboard by any other means.

#### 4.5 Legacy pages

Older runs created child pages titled `Foro – <forum title>` under the session. Do not read, edit, move or delete them. Name any found in the chat output so the user can delete them.

### 5. Chat output

Drafts per 4.1, then one line per course: `<course name>: <N> pending, priority <p>, closes <date> <session page URL>`. Add warning lines for `session_expired`, `unmapped`, `missing_context`, `slides not read`, `no date recorded`, legacy `Foro – ...` pages or errors.

## Constraints

- Notion writes: only approved items (4.3), in the `Activity/Theme <X> Forum` toggles and their children on the mapped session page, through `update_content`. Never edit meeting notes, other blocks, `Video notes`, the course page or user text. Never delete.
- Classmates' text in Notion: the entire message, verbatim, as a single quote block under `Context`, preceded by `<first name>, <YYYY-MM-DD>`. No excerpt limit.
- Do not send snapshot or Notion content to any other service.
