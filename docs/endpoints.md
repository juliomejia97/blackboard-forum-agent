# Endpoints (spike result)

Forums are not native Blackboard discussions. They are an IE LTI 1.3 tool hosted at
`https://ie-lti-forums.ie.edu`, launched from Blackboard Ultra. Source: HAR captured on
2026-10-06 (`data/capture.har`, local only). Anonymized responses live in `tests/fixtures/`
(`init.json`, `forums.json`, `thread_launch.json`), produced by `scripts/anonymize_har.py`.

## Auth flow

1. The course content tree lists the tool as a content item whose `contentHandler` is
   `resource/x-bb-bltiplacement-ie_forums_course_tool` (title "IE Forums"):
   `GET /learn/api/v1/courses/{courseId}/contents/ROOT/children` (course id format `_152245_1`).
2. Launch (LTI 1.3 OIDC third-party-initiated login):
   - `GET blackboard.ie.edu/webapps/blackboard/execute/blti/launchPlacement` (302) with query keys
     `blti_placement_id`, `content_id`, `course_id`, `wrapped`, `from_ultra`
     (ids use the Blackboard `_N_1` format; `wrapped=true`, `from_ultra=true`).
   - 302 to `ie-lti-forums.ie.edu/login` with `iss`, `login_hint`, `target_link_uri`,
     `lti_message_hint`, `lti_deployment_id`, `client_id`, `lti_storage_target`.
   - 307 to `developer.blackboard.com/api/v1/gateway/oauth2/jwttoken`, then back to
     `launchPlacement?cmd=...` (keys `cmd`, `course_id`, `client_id`, `login_hint`,
     `lti_message_hint`, `nonce`, `redirect_uri`, `response_type`, `scope`, `state`).
   - `POST ie-lti-forums.ie.edu/app`, then `GET /app?ltisession=...&code=...&state=...`.
3. The SPA then calls `https://ie-lti-forums.ie.edu/rest/*` with an `Authorization` header and
   cookies. The header value is not a cookie; it must be captured from the first `/rest/*`
   request of the launched page (Playwright `page.on("request")` filtered on `/rest/`).
4. Collector plan: run step 2 in the persistent browser context (headless), capture the
   `Authorization` header from the first `/rest/init` request, then call the three endpoints with
   `context.request.get(url, headers={"Authorization": token})`. Cookies come from the context.

State, nonce and code values are one-shot and are not recorded here.

## Launch URL per course (resolved)

All GET, cookie auth, no browser needed:

1. `GET /learn/api/v1/courses/{courseId}/contents/ROOT/children?@view=Summary&limit=100` lists the
   content items (`results[]`, `paging.nextPage`). The forums item has
   `contentHandler = resource/x-bb-bltiplacement-ie_forums_course_tool` (title "IE Forums",
   `id` like `_2942894_1`). In the capture it is a direct child of ROOT next to a syllabus tool, a
   Zoom tool, a file and a folder. The summary view only carries `contentDetail: {handler: ...}`
   without the placement.
2. `GET /learn/api/v1/courses/{courseId}/contents/{contentId}`: `contentDetail[handler]` has
   `placementHandle` (`ie_forums_course_tool`), `launchLink`
   (`/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_149_1&content_id=_2942894_1&course_id=_152245_1&wrapped=true`),
   `placement.id` (`_149_1`, the same for every course of the institution as far as seen),
   `placement.url` (`https://ie-lti-forums.ie.edu/app`).
3. Ultra's click then requests the same URL with `from_ultra=true` appended. `find_forum_tool`
   builds `launchPlacement?blti_placement_id&content_id&course_id&wrapped=true&from_ultra=true`
   from `placement id` (parsed from `launchLink`, else `placement.id`), the content id and the
   course id.

`lti/placements` is not the source: it lists only other tools. If the item sits inside a folder or
lesson, `find_forum_tool` walks `contents/{id}/children` breadth first (3 levels; the folder case
is untested against a real course). If nothing is found, `CourseConfig.forum_launch_url` (path or
full URL, optional in `config.yaml`) is used.

## Session check and course list

- `GET /learn/api/v1/users/me` (cookies): 200 with `id` (`_135045_1`). 401, 403, a non-JSON body or
  a redirect away from `/learn/api/` means the session expired (`SessionExpired`).
- `GET /learn/api/v1/users/{id}/memberships?expand=course.effectiveAvailability,course.permissions,courseRole&includeCount=true&limit=1000`
  (as Ultra's course list does): `results[]` with `course` (`id` = `_N_1`, `courseId` external
  code, `name`, `displayName`, `isAvailable`, `isClosed`, `isOrganization`, `termId`, `term.name`)
  and `courseRole.roleBucket` (`TAKING`). The capture had 58 memberships, 12 of them
  organizations. Terms carry no dates, so "current term" cannot be derived; `bbagent courses`
  hides organizations, unavailable and closed courses, sorts by term name and takes `--term TEXT`.
  Not verified: that `expand=course` alone also returns `term` (the exact Ultra expand is used).

## Endpoints

All responses share the envelope `{data, permissions, currentUser, currentRole}`. `currentUser`
is the viewer's id in `A########` format (same format as `creator`); `currentRole` was
`ContextLearner`.

### GET /rest/init

`data`: `courseId` (external key, not the Blackboard id), `courseTitle`, `groups` (`{}`),
`users[]`.

User: `id` (int), `externalKey` (`A########`, the id used by `creator`/`currentUser`), `creationDate`
(null), `uuid`, `firstName`, `lastName`, `email`, `status` (`OK`), `instructor` (bool).

Observed: 32 users, none with `instructor: true`. Forum and thread creators (the instructor) are
not in `users`. The collector therefore treats creators of forums and threads, plus users with
`instructor: true`, as instructors.

Collector uses: `currentUser` (own id). `users` is only needed for the instructor flag.

### GET /rest/forums

`data[]` forum: `id` (int), `externalCourseKey`, `title`, `description` (HTML), `creationDate`
(epoch ms), `creator`, `visibility`, `startDate` (null), `endDate` (null in the sample),
`courseGroups`, `gradeable`, `allow*` flags, `maxPosts` (0), `maxCharactersForPost` (0 means no
limit), `status` (`OK`), `permissions`, `ltiThreads[]`, `isUserSubscribed`, `isFavorite`,
`hasUnreadedMessages`, `totalPosts`, `totalUnreaded`.

`ltiThreads[]` item: `id` (int), `idForum`, `title`, `description` (HTML, the prompt),
`creationDate`, `creator`, `visibility`, `startDate`, `endDate`, `readOnly`, `status`,
`permissions`, `ltiPosts` (always `[]` here), `totalPosts`, `totalUnreaded`, `numMessagesOfUser`,
`numMessagesAllowed`.

The deadline lives only in the forum title ("... open until Friday 9 October"), `endDate` is null.
`mapping.due_date_from_title` parses it (23:59 Europe/Madrid); `endDate` wins when present.

Collector uses: forum `id`, `title`, `description`, `creationDate` (as `opens_at` and as the
reference for the title year), `endDate`, `maxCharactersForPost`; thread `id`, `title`,
`description`.

### GET /rest/forums/{forumId}/threads/{threadId}/launch

`data`: thread fields as above plus `ltiPosts[]`.

Post: `id` (int), `idThread`, `quote`, `idParent`, `postText` (HTML), `postLength`, `creationDate`
(epoch ms), `creator`, `status`, `readed`, `flagged`, `liked`, `numLikes`, `threadTitle`,
`creatorName` ("First Last"), `ltiPostAttachments`, `usersCited`.

Observed: top-level posts have `idParent: 0` and `quote: 0` (not null). Replies carry the parent
post id in `idParent` and `quote: null`. Replies to replies exist. Posts in the thread: 27, of
which 14 top-level. The posts are fully inlined (no paging seen).

Collector uses: `id`, `idParent` (0 becomes `None`), `postText` (stripped to text),
`creationDate`, `creator`, `creatorName`, `readed`.

### POST /rest/forums/{forumId}/threads/{threadId}/posts

The only write, used by `bbagent publish --send` through `bb_client.post_message`. Headers: the
captured `Authorization` value and `Content-Type: application/json;charset=UTF-8`. Verified from a
real capture and the forum SPA source.

Top-level post body (`idParent` and `quote` omitted):

```json
{"postText": "<p>...</p>", "postLength": 123, "usersCited": []}
```

Reply body: the same plus `"idParent": <parent message id, int>, "quote": null`.

`postText` uses only `<p>`, `<br>`, `<strong>` and `<a href target="_blank" rel="noopener noreferrer">`.
`postLength` is the plain-text length with paragraphs joined by `\n` (a real post reported 2229
for 2228 characters of tag-stripped text, so the SPA's count differs by about one; the server was not
observed to validate it). The response is the created post (`id`, `idThread`, `quote`, `idParent`,
`postText`, `postLength`, `creationDate`, `creator`, `status`, ...); `publish` checks that `creator`
is the current user and, for a reply, that `idParent` is the requested parent.

The launch GET also gives `numMessagesOfUser` and `numMessagesAllowed` (0 or null means unlimited),
used by the pre-send checks. The POST is never retried. The SPA also has PUT (edit) and DELETE
endpoints for posts; they are deliberately not implemented. A 401 means the post was not created
(`SessionExpired`).

## Open questions

- Token lifetime of the `Authorization` header is unknown. Plan: one launch per course per run, and
  re-launch on a 401 once.
- Whether `/rest/forums` returns all weeks' forums for a course or only visible ones, and whether
  older forums keep `ltiPosts: []`. Only one forum was captured.
- Whether `endDate` is ever populated (epoch ms is assumed; ISO strings are also accepted).
- Thread pagination for threads with many posts is unobserved.
- The collector fetches every thread of every forum returned by `/rest/forums` (all weeks), so
  requests per course grow with the number of weeks. Rate limits are unknown; it issues 2 + (number of threads) requests per course, plus 2 to 3 learn API calls to find the tool.
