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

    import requests

    from zhs.common.logging import logger
    from zhs.common.runtime import AuthenticationError

    if not Path(path).exists():
        return False
    try:
        client.cookies = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        client.courses.clear()
        client.getZhidaoList()
    except Exception as error:
        if isinstance(error, requests.RequestException):
            response = getattr(error, "response", None)
            if getattr(response, "status_code", None) not in (401, 403):
                raise
            try:
                if client.refresh_login():
                    from zhs.common.storage import atomic_write_json
                    from zhs.common.utils import cookie_jar_to_list

                    atomic_write_json(path, cookie_jar_to_list(client.cookies))
                    print("已通过现有 SSO 会话自动刷新登录态")
                    return True
            except AuthenticationError:
                pass
            except requests.RequestException as refresh_error:
                if getattr(getattr(refresh_error, "response", None), "status_code", None) not in (
                    401,
                    403,
                ):
                    raise
        logger.warning(f"Saved cookies unavailable: {error}")
        client.cookies = {}
        client.courses.clear()
        return False
    print("Successfully recovered from saved cookies")
    return True


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "stop" and vars(args) != vars(parser.parse_args(["stop"])):
        parser.error("stop 不能与学习、登录或报告参数组合使用")
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
    from zhs.common.control import StopRequested, managed_runner, stop_runner
    from zhs.common.utils import getRealPath

    try:
        if args.history:
            return print_history(getRealPath("var/runs"))
        if args.command == "stop":
            stopped = stop_runner(getRealPath("var/locks"))
            print(
                "运行实例已退出；下次启动会恢复已保存进度" if stopped else "当前没有正在运行的实例"
            )
            return 0
        with managed_runner(
            getRealPath("var/locks"), replace=not (args.check or args.fetch or args.dry_run)
        ):
            if not (args.check or args.fetch or args.dry_run or args.once):
                print("正在启动：自动检查登录、恢复进度并处理未完成内容。", flush=True)
            return run_application(args)
    except StopRequested:
        print("\n已响应停止请求并保存状态；下次直接运行 uv run zhs 将自动继续。", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\n收到运行中断信号，进度已保存；下次直接运行 uv run zhs 将自动继续。", flush=True)
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

    from zhs.automation import record_interruption, record_recovery_error, run_until_complete
    from zhs.clients.course import ZHSClient
    from zhs.common.control import check_stop, sleep
    from zhs.common.logging import logger
    from zhs.common.runtime import AuthenticationError
    from zhs.common.storage import atomic_write_json
    from zhs.common.utils import cookie_jar_to_list, getRealPath, showImage
    from zhs.config import load_config
    from zhs.journal import RunJournal
    from zhs.workflow import execute_tasks, fetch_courses, read_execution, resolve_courses

    client = None
    journal = None
    automatically_resumed = False
    try:
        check_stop()
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
                if previous_run.data["status"] != "finished" or previous_run.data.get(
                    "automation", {}
                ).get("state") in {"running", "waiting"}:
                    journal = previous_run
                    automatically_resumed = True
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
            show_in_terminal = platform.system() not in {"Windows", "Darwin"}
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
        qr_required = option(args.qrlogin, config.qrlogin)

        def verify_account():
            if journal is not None:
                try:
                    journal.bind_account(client.uuid)
                except ValueError as error:
                    raise AuthenticationError(str(error)) from error

        def authenticate():
            nonlocal qr_required
            check_stop()
            if config.save_cookies and restore_cookies(client, cookies_path):
                verify_account()
                return
            username, password = args.username or config.username, args.password or config.password
            if not qr_required:
                if not username or not password:
                    if args.qrlogin is False:
                        raise AuthenticationError(
                            "未配置账号密码；省略 --no-qrlogin 可自动扫码登录"
                        )
                    qr_required = True
                else:
                    try:
                        client.login(username, password, interactive=False)
                    except AuthenticationError:
                        if args.qrlogin is False:
                            raise
                        print("账号登录未成功，正在自动切换扫码登录……")
                        qr_required = True
            if qr_required:
                print("需要重新登录，请使用智慧树 App 扫描二维码并确认；登录后将自动继续。")
                callback = partial(
                    showImage,
                    show_in_terminal=show_in_terminal,
                    ensure_unicode=qr_config.ensure_unicode,
                )
                while True:
                    client.login(use_qr=True, qr_callback=callback)
                    try:
                        verify_account()
                    except AuthenticationError:
                        print("扫码账号与原任务不一致，请使用原账号重新扫码。")
                        client.cookies = {}
                        client.session.cookies.clear()
                        client.courses.clear()
                        continue
                    # Verify actual course access before replacing saved login state.
                    client.courses.clear()
                    client.getZhidaoList()
                    break
                print("扫码登录成功，正在继续原任务。")
            verify_account()
            if config.save_cookies:
                atomic_write_json(cookies_path, cookie_jar_to_list(client.cookies))

        while True:
            try:
                authenticate()
                break
            except Exception as error:
                import requests

                retryable = isinstance(error, requests.RequestException) or (
                    isinstance(error, AuthenticationError) and error.retryable
                )
                delay = getattr(error, "retry_after", None) or 30
                if journal is not None:
                    record_recovery_error(
                        journal, error, delay=delay, terminal=not retryable or args.once
                    )
                if args.once or not retryable:
                    raise
                logger.warning(f"登录暂不可用，{delay} 秒后自动重试：{error}")
                sleep(delay)
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
        if automatically_resumed:
            print("已自动恢复上次未完成任务，保留已完成结果和缓存。", flush=True)
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
        if isinstance(error, KeyboardInterrupt) and journal is not None:
            record_interruption(journal, error)
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
