"""Command-line entry point. Importing this module never logs in or writes files."""

from zhs.cli import build_parser


def option(value, default):
    return default if value is None else value


def parse_proxies(value, configured):
    from urllib.parse import urlsplit

    proxies = dict(configured or {})
    if value is None:
        return proxies
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("代理地址必须为 http(s)://host:port 或 socks5(h)://host:port")
    return {"http": value, "https": value}


def restore_cookies(client, path):
    import json
    from pathlib import Path

    from zhs.common.logging import logger

    if not Path(path).exists():
        return False
    try:
        client.cookies = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        client.getZhidaoList()
    except Exception as error:
        logger.warning(f"Saved cookies unavailable: {error}")
        client.cookies = {}
        client.courses.clear()
        return False
    print("Successfully recovered from saved cookies")
    return True


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.videos and not args.course:
        parser.error("--videos requires --course")
    if args.aicourse and (args.course or args.videos):
        parser.error("--aicourse cannot be combined with --course/--videos")
    if (args.resume or args.retry_failed) and (
        args.course or args.aicourse or args.videos or args.fetch or args.check
    ):
        parser.error("resume/retry cannot be combined with course selection, --fetch or --check")
    if sum(bool(value) for value in (args.check, args.fetch, args.history, args.dry_run)) > 1:
        parser.error("--check, --fetch, --history and --dry-run are mutually exclusive")
    if args.history and (
        args.course or args.aicourse or args.resume or args.retry_failed or args.report
    ):
        parser.error("--history cannot be combined with task selection or --report")
    if (args.check or args.fetch) and (args.course or args.aicourse or args.report):
        parser.error("--check/--fetch cannot be combined with task selection or --report")
    from zhs.common.locking import FileLock
    from zhs.common.utils import getRealPath

    try:
        if args.history:
            return print_history(getRealPath("var/runs"))
        with FileLock(getRealPath("var/locks/runner.lock")):
            return run_application(args)
    except KeyboardInterrupt:
        print("用户已中断；已建立的任务记录可用 --resume 继续")
        return 130
    except Exception as error:
        print(f"运行失败：{error}")
        return 1


def print_history(directory):
    import json
    from pathlib import Path

    for path in sorted(Path(directory).glob("*.json"), reverse=True)[:21]:
        if path.name == "latest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"{data['id']}  {data['status']}  {data.get('counts', {})}")
        except OSError, ValueError, KeyError, TypeError:
            print(f"无法读取记录：{path.name}")
    return 0


def run_application(args):
    import platform
    from functools import partial
    from pathlib import Path

    from zhs.automation import run_until_complete
    from zhs.clients.course import ZHSClient
    from zhs.common.logging import logger
    from zhs.common.storage import atomic_write_json
    from zhs.common.utils import cookie_jar_to_list, getRealPath, showImage
    from zhs.config import load_config
    from zhs.journal import RunJournal
    from zhs.workflow import execute_tasks, fetch_courses, read_execution, resolve_courses

    client = None
    journal = None
    try:
        restart = args.resume or args.retry_failed
        if restart:
            journal = RunJournal.load(getRealPath("var/runs"), restart)
        all_courses = not (args.course or args.aicourse or args.videos)
        if (
            journal is None
            and all_courses
            and not (args.once or args.check or args.fetch or args.dry_run)
        ):
            pointer = Path(getRealPath("var/runs")) / "latest.json"
            if pointer.exists():
                previous_run = RunJournal.load(getRealPath("var/runs"))
                if all(task["video_id"] is None for task in previous_run.data["tasks"]) and (
                    previous_run.data["status"] != "finished"
                    or previous_run.data.get("automation", {}).get("state")
                    in {"running", "waiting"}
                ):
                    journal = previous_run
                    journal.data["scope"] = "all"
        previous = journal.data["options"] if journal else {}
        settings = {
            "speed": option(args.speed, previous.get("speed")),
            "threshold": option(args.threshold, previous.get("threshold")),
            "limit": option(args.limit, previous.get("limit", 0)),
            "no_exam": option(args.noexam, previous.get("no_exam", False)),
        }
        if not args.once and settings["threshold"] is None:
            settings["threshold"] = 1.0
        config = load_config()
        logger.setLevel("DEBUG" if args.debug else config.logLevel or "WARNING")
        proxies = parse_proxies(args.proxy, config.proxies)
        qr_config = config.qr_extra
        show_in_terminal = option(args.show_in_terminal, qr_config.show_in_terminal)
        if show_in_terminal is None:
            show_in_terminal = platform.system() == "Windows"
        client = ZHSClient(
            proxies=proxies,
            speed=settings["speed"],
            end_thre=settings["threshold"],
            limit=settings["limit"],
            pushplus_token=config.pushplus.token if config.pushplus.enable else "",
            bark_token=config.bark.token if config.bark.enable else "",
            tree_view=option(args.tree_view, config.tree_view),
            progressbar_view=option(args.progressbar_view, config.progressbar_view),
            image_path=option(args.image_path, config.image_path),
            cache_policy=config.cache,
        )
        cookies_path = getRealPath("cookies.json")

        def authenticate():
            if config.save_cookies and restore_cookies(client, cookies_path):
                return
            username, password = args.username or config.username, args.password or config.password
            if not args.once and args.qrlogin is not True:
                client.login(username, password, interactive=False)
            elif option(args.qrlogin, config.qrlogin):
                callback = partial(
                    showImage,
                    show_in_terminal=show_in_terminal,
                    ensure_unicode=qr_config.ensure_unicode,
                )
                client.login(use_qr=True, qr_callback=callback)
            else:
                client.login(username, password, interactive=args.once)
            if config.save_cookies:
                atomic_write_json(cookies_path, cookie_jar_to_list(client.cookies))

        while True:
            try:
                authenticate()
                break
            except Exception as error:
                import time

                import requests

                if args.once or not isinstance(error, requests.RequestException):
                    raise
                logger.warning(f"登录网络暂不可用，30 秒后自动重试：{error}")
                time.sleep(30)
        execution_path = getRealPath("execution.json")
        if args.check or args.fetch:
            courses, errors = fetch_courses(client)
            for error in errors:
                print(error)
            if errors:
                return 1
            if args.fetch:
                atomic_write_json(execution_path, courses)
                print(f"已保存 {len(courses)} 门课程到 execution.json")
            else:
                counts = {
                    kind: sum(c["type"] == kind for c in courses)
                    for kind in ("zhidao", "hike", "ai")
                }
                print(f"连接检查通过：{counts}")
            return 0
        if journal is None:
            discovery_errors = []
            if args.aicourse:
                course_id, class_id = args.aicourse
                courses = [{"id": course_id, "classId": class_id, "type": "ai"}]
            elif args.course:
                known = read_execution(execution_path) if Path(execution_path).exists() else []
                courses = []
                for course_id in dict.fromkeys(args.course):
                    matches = [item for item in known if item["id"] == course_id]
                    if len(matches) > 1:
                        raise ValueError(
                            f"课程 {course_id} 在清单中不唯一，请使用明确类型的清单或 -ai"
                        )
                    courses.append(matches[0] if matches else {"id": course_id})
            elif args.once and Path(execution_path).exists():
                courses = read_execution(execution_path)
            else:
                courses, discovery_errors = fetch_courses(client)
            courses = resolve_courses(client, courses)
            if (
                not args.once
                and not args.dry_run
                and Path(getRealPath("var/runs"), "latest.json").exists()
            ):
                candidate = RunJournal.load(getRealPath("var/runs"))
                if candidate.data["status"] != "finished" and candidate.matches_selection(
                    courses, args.videos
                ):
                    candidate.bind_account(client.uuid)
                    journal = candidate
                    journal.data["options"] = settings
            if journal is None:
                journal = RunJournal.create(
                    getRealPath("var/runs"), courses, settings, videos=args.videos
                )
            journal.data["discovery_errors"] = discovery_errors
            journal.data["scope"] = "all" if all_courses and not args.once else "selected"
        else:
            journal.data["options"] = settings
        journal.bind_account(client.uuid)
        journal.save()
        if args.report:
            # Publish the plan first: a broken report destination must fail
            # before any course task starts.
            journal.export(args.report)
        print(f"运行记录：{journal.data['id']}")
        if args.dry_run:
            for task in journal.data["tasks"]:
                print(
                    f"{task['id']}: {task['courses']} video={task['video_id']} status={task['status']}"
                )
            if args.report:
                journal.export(args.report)
            return 1 if journal.data.get("discovery_errors") else 0
        if args.once:
            summary = journal.execute(
                client, config, execute_tasks, retry_failed=bool(args.retry_failed)
            )
        else:

            def prepare():
                authenticate()
                journal.bind_account(client.uuid)

            def discover():
                client.courses.clear()
                return fetch_courses(client)

            summary = run_until_complete(
                journal,
                client,
                config,
                partial(execute_tasks, verify_completion=True),
                prepare=prepare,
                discover=discover
                if journal.data.get("scope") == "all" and not args.retry_failed
                else None,
                report=args.report,
                retry_failed=bool(args.retry_failed),
            )
        print(f"运行结果：{summary}")
        for message in summary.failed + summary.pending:
            print(f"  {message}")
        if args.report:
            journal.export(args.report)
        return summary.exit_code
    except BaseException as error:
        if isinstance(error, Exception):
            logger.exception(error)
        if journal is not None and args.report:
            try:
                journal.export(args.report)
            except (OSError, ValueError) as error:
                logger.error(f"报告导出失败：{error}")
        raise
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
