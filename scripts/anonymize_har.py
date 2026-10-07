"""Turn the captured HAR into anonymized test fixtures.

Reads the /rest/init, /rest/forums and /rest/forums/{id}/threads/{id}/launch responses and writes
tests/fixtures/{init,forums,thread_launch}.json with every identity replaced by a stable fake.
Prints only counts, never content.

Usage: uv run python scripts/anonymize_har.py [data/capture.har] [tests/fixtures]
"""

import json
import re
import sys
from pathlib import Path

from bbagent.parse import html_to_text

LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut "
    "labore et dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco "
    "laboris nisi aliquip ex ea commodo consequat duis aute irure in reprehenderit voluptate "
    "velit esse cillum fugiat nulla pariatur"
).split()
_TAG = re.compile(r"(<[^>]*>)")
_EDGES = re.compile(r"^(\s*)(.*?)(\s*)$", re.DOTALL)
_BARE_TAG = re.compile(r"<(/?)([a-zA-Z0-9]+)[^>]*?>")


def lorem_words(n: int, offset: int = 0) -> str:
    return " ".join(LOREM[(offset + i) % len(LOREM)] for i in range(n))


def lorem_html(html: str, seed: int = 0) -> str:
    """Replace every text run with lorem words, keeping bare tags and per-run word counts."""
    from html import unescape

    out = []
    offset = seed
    for piece in _TAG.split(html):
        if piece.startswith("<") and piece.endswith(">"):
            out.append(_BARE_TAG.sub(r"<\1\2>", piece))
            continue
        lead, core, trail = _EDGES.match(piece).groups()
        count = len(unescape(core).split())
        if count == 0:
            out.append(piece)
            continue
        out.append(lead + lorem_words(count, offset) + trail)
        offset += count
    result = "".join(out)
    original_count = len(html_to_text(html).split())
    if len(html_to_text(result).split()) != original_count:
        result = f"<p>{lorem_words(original_count, seed)}</p>"
    return result


class Identities:
    def __init__(self, init: dict, forums: dict, launch: dict) -> None:
        current = init["currentUser"]
        users = init["data"]["users"]
        self.enrolled = {u["externalKey"] for u in users}
        self.instructor_flag = {u["externalKey"] for u in users if u["instructor"]}
        self.ids: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        self._students = 0
        self._instructors = 0
        self._register(current, instructor=False)
        for user in users:
            self._register(user["externalKey"], user["instructor"])
        for creator in self._all_creators(forums, launch):
            self._register(creator, creator not in self.enrolled)

    def _all_creators(self, forums: dict, launch: dict):
        for forum in forums["data"]:
            yield forum["creator"]
            for thread in forum["ltiThreads"]:
                yield thread["creator"]
        yield launch["data"]["creator"]
        for post in launch["data"]["ltiPosts"]:
            yield post["creator"]

    def _register(self, original: str, instructor: bool) -> None:
        if original in self.ids:
            return
        if instructor:
            self._instructors += 1
            n = self._instructors
            self.ids[original] = f"I{n:08d}"
            self.names[original] = f"Instructor {n}"
            self.emails[original] = f"instructor{n}@example.com"
        else:
            self._students += 1
            n = self._students
            self.ids[original] = f"A{n:08d}"
            self.names[original] = f"Student {n}"
            self.emails[original] = f"student{n}@example.com"

    def is_staff(self, creator: str) -> bool:
        return creator not in self.enrolled or creator in self.instructor_flag


def anon_envelope(doc: dict, ident: Identities) -> dict:
    doc["currentUser"] = ident.ids[doc["currentUser"]]
    return doc


def anon_init(doc: dict, ident: Identities) -> dict:
    data = doc["data"]
    data["courseId"] = "FAKE-COURSE-001"
    data["courseTitle"] = "Fake Course"
    for i, user in enumerate(data["users"], 1):
        key = user["externalKey"]
        first, _, last = ident.names[key].partition(" ")
        user["id"] = 1000 + i
        user["externalKey"] = ident.ids[key]
        user["uuid"] = f"{i:032x}"
        user["firstName"], user["lastName"] = first, last
        user["email"] = ident.emails[key]
    return anon_envelope(doc, ident)


def _anon_thread(thread: dict, ident: Identities, label: int) -> None:
    creator = thread["creator"]
    if not ident.is_staff(creator):
        thread["title"] = f"Thread {label}"
        thread["description"] = lorem_html(thread["description"], label)
    thread["creator"] = ident.ids[creator]


def anon_forums(doc: dict, ident: Identities) -> dict:
    for n, forum in enumerate(doc["data"], 1):
        forum["externalCourseKey"] = "FAKE-EXT-COURSE-001"
        for t, thread in enumerate(forum["ltiThreads"], 1):
            _anon_thread(thread, ident, t)
            for post in thread["ltiPosts"]:
                _anon_post(post, ident)
        if not ident.is_staff(forum["creator"]):
            forum["title"] = f"Forum {n}"
            forum["description"] = lorem_html(forum["description"], n)
        forum["creator"] = ident.ids[forum["creator"]]
    return anon_envelope(doc, ident)


def _anon_post(post: dict, ident: Identities) -> None:
    creator = post["creator"]
    post["postText"] = lorem_html(post["postText"], post["id"])
    post["postLength"] = len(html_to_text(post["postText"]))
    post["creator"] = ident.ids[creator]
    post["creatorName"] = ident.names[creator]
    post["usersCited"] = []
    post["ltiPostAttachments"] = []


def anon_launch(doc: dict, ident: Identities) -> dict:
    thread = doc["data"]
    _anon_thread(thread, ident, 1)
    for post in thread["ltiPosts"]:
        _anon_post(post, ident)
    return anon_envelope(doc, ident)


def _bodies(har: dict) -> dict[str, dict]:
    found = {}
    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "ie-lti-forums.ie.edu/rest/" not in url:
            continue
        path = url.split("ie-lti-forums.ie.edu", 1)[1].split("?", 1)[0]
        text = entry["response"]["content"].get("text")
        if text and entry["response"]["status"] == 200:
            found[path] = json.loads(text)
    return found


def _shingles(text: str, size: int = 5) -> set[str]:
    words = text.lower().split()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def verify(originals: dict, outputs: dict[str, str], ident: Identities) -> dict[str, int]:
    """Count leaks of original identifiers and post text in the written fixtures."""
    blob = "\n".join(outputs.values())
    users = originals["init"]["data"]["users"]
    secrets = set(ident.ids)
    secrets |= {u["email"] for u in users} | {u["uuid"] for u in users}
    secrets |= {n for u in users for n in (u["firstName"], u["lastName"]) if len(n) > 3}
    leaked_ids = sum(1 for s in secrets if s in blob)
    original_posts = originals["launch"]["data"]["ltiPosts"]
    shingles = set().union(*(_shingles(html_to_text(p["postText"])) for p in original_posts))
    new_text = " ".join(
        html_to_text(p["postText"]) for p in json.loads(outputs["launch"])["data"]["ltiPosts"]
    )
    leaked_text = len(shingles & _shingles(new_text))
    return {
        "original_identifiers_checked": len(secrets),
        "identifiers_found_in_fixtures": leaked_ids,
        "post_5grams_checked": len(shingles),
        "post_5grams_found_in_fixtures": leaked_text,
    }


def main(argv: list[str]) -> int:
    har_path = Path(argv[1]) if len(argv) > 1 else Path("data/capture.har")
    out_dir = Path(argv[2]) if len(argv) > 2 else Path("tests/fixtures")
    bodies = _bodies(json.loads(har_path.read_text(encoding="utf-8")))
    launch_path = next(p for p in bodies if re.fullmatch(r"/rest/forums/\d+/threads/\d+/launch", p))
    originals = {
        "init": bodies["/rest/init"],
        "forums": bodies["/rest/forums"],
        "launch": bodies[launch_path],
    }
    ident = Identities(originals["init"], originals["forums"], originals["launch"])
    copy = json.loads(json.dumps(originals))
    anonymized = {
        "init": anon_init(copy["init"], ident),
        "forums": anon_forums(copy["forums"], ident),
        "launch": anon_launch(copy["launch"], ident),
    }
    names = {"init": "init.json", "forums": "forums.json", "launch": "thread_launch.json"}
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for key, doc in anonymized.items():
        outputs[key] = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        (out_dir / names[key]).write_text(outputs[key], encoding="utf-8")
    for label, count in verify(originals, outputs, ident).items():
        print(f"{label}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
