import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from bbagent import mapping
from bbagent.bb_client import (
    CourseNotFound,
    EndpointError,
    SessionExpired,
    check_session,
    collect_course,
    course_rows,
    favorite_course_ids,
    list_memberships,
    open_context,
    open_forums_api,
    run_browser,
    summarize_har,
)
from bbagent.config import AppConfig, ConfigError, CourseConfig, load_config, validate_config
from bbagent.notify import notify
from bbagent.parse import SchemaError, current_user, instructor_ids, parse_forums, parse_thread
from bbagent.publish import refuse, run_publish, text_problems
from bbagent.rules import evaluate_forum
from bbagent.snapshot import (
    CourseResult,
    ForumResult,
    build_snapshot,
    in_record_window,
    merge_with_existing,
    write_snapshot,
)
from bbagent.store import Store

EXIT_SESSION_EXPIRED = 2
EXIT_SCHEMA_ERROR = 3
EXIT_API_ERROR = 3

DEFAULT_CONFIG = Path("config.yaml")
EXAMPLE_CONFIG = Path("config.example.yaml")


def _load(path: Path) -> AppConfig:
    if path.exists():
        return load_config(path)
    if EXAMPLE_CONFIG.exists():
        return load_config(EXAMPLE_CONFIG)
    return AppConfig()


def _login(config: AppConfig, args: argparse.Namespace) -> int:
    run_browser(config)
    return 0


def _capture(config: AppConfig, args: argparse.Namespace) -> int:
    har_path = config.data_dir / "capture.har"
    run_browser(config, har_path)
    if not har_path.exists():
        print(f"No HAR written at {har_path}", file=sys.stderr)
        return 1
    lines = summarize_har(har_path)
    summary = "\n".join(lines)
    (config.data_dir / "capture-endpoints.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary or "No JSON requests to /learn/api/ found")
    return 0


def summary_line(course: dict) -> str:
    """One status line from a snapshot course entry."""
    return (
        f"{course['name']}: {course['pending_threads']} pending, "
        f"highest priority {course['highest_priority'] or '-'}, "
        f"next due {course['next_due_at'] or '-'}"
    )


def process_course(
    course_cfg: CourseConfig,
    raw: tuple[dict, dict, dict[str, dict]],
    store: Store,
    run_id: int,
    now: datetime,
) -> CourseResult:
    """Parse the raw JSON of a course, persist it and evaluate the rules."""
    init_json, forums_json, launches = raw
    me = current_user(init_json)
    instructors = instructor_ids(init_json, forums_json)
    forum_results = []
    for forum in parse_forums(forums_json, course_cfg.bb_course_id):
        if not in_record_window(forum.due_at, now):
            continue  # not fetched by collect_course; keep stored data untouched
        threads = [
            parse_thread(launches[t.id], forum, me, instructors) if t.id in launches else t
            for t in forum.threads
        ]
        forum = forum.model_copy(update={"threads": threads})
        session = mapping.session_number(forum.title)
        store.upsert_forum(forum, run_id, session, now)
        evaluations = evaluate_forum(course_cfg, forum, now)
        for ev in evaluations:
            store.set_evaluation(ev.thread_id, ev.status, ev.priority, now)
        new_ids = {t.id: store.new_message_ids(t.id, run_id) for t in forum.threads}
        forum_results.append(ForumResult(forum, session, evaluations, new_ids))
    return CourseResult(course_cfg, forum_results)


def run_collect(
    config: AppConfig,
    *,
    check: Callable[[], Any],
    collect: Callable[[CourseConfig], tuple[dict, dict, dict[str, dict]]],
    notifier: Callable[[str, str], None],
    now: datetime,
    store: Store,
    merge: bool = False,
) -> int:
    run_id = store.start_run(now)

    def finish(outcome: str, path: Path | None = None) -> None:
        store.record_run(run_id, datetime.now().astimezone(), outcome, str(path) if path else None)

    try:
        check()
        results = [process_course(c, collect(c), store, run_id, now) for c in config.courses]
    except SessionExpired:
        notifier("Blackboard", "Blackboard session expired: run `bbagent login`")
        path = write_snapshot(build_snapshot(now, [], session_expired=True), config.out_dir)
        finish("session_expired", path)
        print("Session expired: run `bbagent login`", file=sys.stderr)
        return EXIT_SESSION_EXPIRED
    except CourseNotFound as exc:
        notifier("Blackboard", f"bbagent: {exc}")
        finish(f"course_not_found: {exc}")
        print(str(exc), file=sys.stderr)
        return EXIT_SCHEMA_ERROR
    except (SchemaError, EndpointError) as exc:
        notifier("Blackboard", f"bbagent: endpoint changed ({exc})")
        finish(f"schema_error: {exc}")
        print(f"Schema error: {exc}", file=sys.stderr)
        return EXIT_SCHEMA_ERROR
    collected = build_snapshot(now, results)
    # single-course run: keep the other courses of today's file
    snapshot = merge_with_existing(collected, config.out_dir) if merge else collected
    path = write_snapshot(snapshot, config.out_dir)
    finish("ok", path)
    for course in collected["courses"]:
        print(summary_line(course))
    return 0


def _collect(config: AppConfig, args: argparse.Namespace) -> int:
    if not config.courses:
        print("No courses in config.yaml (see `bbagent courses`)", file=sys.stderr)
        return 1
    try:
        validate_config(config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    store = Store(config.data_dir / "bbagent.db")
    now = datetime.now().astimezone()
    try:
        with open_context(config, headless=not args.headed) as context:
            return run_collect(
                config,
                check=lambda: check_session(context, config.base_url),
                collect=lambda c: collect_course(context, config.base_url, c, now=now),
                notifier=notify,
                now=now,
                store=store,
            )
    finally:
        store.close()


def _thread_status_line(config: AppConfig, course: CourseConfig, thread_id: str, day: str):
    """Status line of a thread from the day's snapshot, or None when it is not there."""
    try:
        snapshot = json.loads((config.out_dir / f"{day}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in snapshot.get("courses", []):
        if entry.get("bb_course_id") != course.bb_course_id:
            continue
        for rec in entry.get("record", []):
            if rec["thread_id"] == thread_id:
                return (
                    f"{course.name} / {rec['title']}: status {rec['status']}, "
                    f"own posts {len(rec['my_posts'])}, own replies {len(rec['my_replies'])}"
                )
    return None


def _publish(config: AppConfig, args: argparse.Namespace) -> int:
    course = next((c for c in config.courses if c.bb_course_id == args.course), None)
    if course is None:
        return refuse([f"course {args.course} is not in config.yaml"], print)
    try:
        validate_config(config)
        text = args.text_file.read_text(encoding="utf-8")
    except (ConfigError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if problems := text_problems(text):
        return refuse(problems, print)
    store = Store(config.data_dir / "bbagent.db")
    now = datetime.now().astimezone()
    try:
        with open_context(config, headless=not args.headed) as context:

            def recollect() -> int:
                single = config.model_copy(update={"courses": [course]})
                return run_collect(
                    single,
                    check=lambda: check_session(context, config.base_url),
                    collect=lambda c: collect_course(context, config.base_url, c, now=now),
                    notifier=notify,
                    now=now,
                    store=store,
                    merge=True,
                )

            try:
                check_session(context, config.base_url)
                api = open_forums_api(context, config.base_url, course)
                return run_publish(
                    course,
                    thread_id=str(args.thread),
                    parent_id=args.parent,
                    text=text,
                    send=args.send,
                    api=api,
                    recollect=recollect,
                    status_line=lambda: _thread_status_line(
                        config, course, str(args.thread), now.isoformat()[:10]
                    ),
                )
            except SessionExpired:
                print("Session expired: run `bbagent login`", file=sys.stderr)
                return EXIT_SESSION_EXPIRED
            except (SchemaError, EndpointError) as exc:
                print(f"API error: {exc}", file=sys.stderr)
                return EXIT_API_ERROR
    finally:
        store.close()


def _status(config: AppConfig, args: argparse.Namespace) -> int:
    files = sorted(config.out_dir.glob("*.json"))
    if not files:
        print(f"No snapshot in {config.out_dir}", file=sys.stderr)
        return 1
    snapshot = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"{files[-1].name} generated {snapshot['generated_at']}")
    if snapshot["session_expired"]:
        print("Session expired: run `bbagent login`")
        return 0
    for course in snapshot["courses"]:
        print(summary_line(course))
    return 0


def _courses(config: AppConfig, args: argparse.Namespace) -> int:
    with open_context(config, headless=not args.headed) as context:
        try:
            user_id = check_session(context, config.base_url)
            favorites = None if args.all else favorite_course_ids(context, config.base_url)
            if favorites is not None and not favorites:
                print("No starred courses; star them in Blackboard or use --all", file=sys.stderr)
                return 1
            rows = course_rows(
                list_memberships(context, config.base_url, user_id), args.term, favorites
            )
        except SessionExpired:
            print("Session expired: run `bbagent login`", file=sys.stderr)
            return EXIT_SESSION_EXPIRED
        except EndpointError as exc:
            print(f"Endpoint error: {exc}", file=sys.stderr)
            return EXIT_SCHEMA_ERROR
    for course_id, name, term in rows:
        print(f"{course_id}\t{name}\t{term}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bbagent", description="Blackboard forum agent")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "login": (_login, "Open Chromium to log in with SSO/MFA"),
        "capture": (_capture, "Record a HAR while browsing and summarize API endpoints"),
        "courses": (_courses, "List your starred courses (id, name, term) to fill config.yaml"),
        "collect": (_collect, "Collect forums, evaluate rules and write out/YYYY-MM-DD.json"),
        "publish": (_publish, "Dry-run (or --send) one post/reply to a thread; the only write"),
        "status": (_status, "Show per-course status from the latest snapshot (no network)"),
    }
    for name, (_, help_text) in commands.items():
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name in ("courses", "collect", "publish"):
            cmd.add_argument("--headed", action="store_true", help="show the browser window")
        if name == "publish":
            cmd.add_argument("--course", required=True, help="bb_course_id from config.yaml")
            cmd.add_argument("--thread", required=True, type=int, help="thread id")
            cmd.add_argument("--parent", type=int, help="message id to reply to")
            cmd.add_argument("--text-file", required=True, type=Path)
            cmd.add_argument("--send", action="store_true", help="really POST (default: dry run)")
        if name == "courses":
            cmd.add_argument("--term", help="only terms whose name contains this text")
            cmd.add_argument(
                "--all", action="store_true", help="include courses that are not starred"
            )
    args = parser.parse_args(argv)
    return commands[args.command][0](_load(args.config), args)


if __name__ == "__main__":
    sys.exit(main())
