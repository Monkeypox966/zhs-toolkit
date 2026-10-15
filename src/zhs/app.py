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

    import platform
    from functools import partial
    from pathlib import Path

    from zhs.clients.course import ZHSClient
    from zhs.common.logging import logger
    from zhs.common.storage import atomic_write_json
    from zhs.common.utils import cookie_jar_to_list, getRealPath, showImage
    from zhs.config import load_config
    from zhs.workflow import execute_tasks, fetch_courses, read_execution

    client = None
    try:
        config = load_config()
        logger.setLevel("DEBUG" if args.debug else config.logLevel or "WARNING")
        proxies = parse_proxies(args.proxy, config.proxies)
        qr_config = config.qr_extra
        show_in_terminal = option(args.show_in_terminal, qr_config.show_in_terminal)
        if show_in_terminal is None:
            show_in_terminal = platform.system() == "Windows"
        client = ZHSClient(
            proxies=proxies,
            speed=args.speed,
            end_thre=args.threshold,
            limit=args.limit,
            pushplus_token=config.pushplus.token if config.pushplus.enable else "",
            bark_token=config.bark.token if config.bark.enable else "",
            tree_view=option(args.tree_view, config.tree_view),
            progressbar_view=option(args.progressbar_view, config.progressbar_view),
            image_path=option(args.image_path, config.image_path),
        )
        cookies_path = getRealPath("cookies.json")
        if not (config.save_cookies and restore_cookies(client, cookies_path)):
            if option(args.qrlogin, config.qrlogin):
                callback = partial(
                    showImage,
                    show_in_terminal=show_in_terminal,
                    ensure_unicode=qr_config.ensure_unicode,
                )
                client.login(use_qr=True, qr_callback=callback)
            else:
                client.login(args.username or config.username, args.password or config.password)
            if config.save_cookies:
                atomic_write_json(cookies_path, cookie_jar_to_list(client.cookies))

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

        discovery_errors = []
        if args.aicourse:
            course_id, class_id = args.aicourse
            courses = [{"id": course_id, "classId": class_id, "type": "ai"}]
        elif args.course:
            known = read_execution(execution_path) if Path(execution_path).exists() else []
            by_id = {item["id"]: item for item in known}
            courses = [
                by_id.get(course_id, {"id": course_id}) for course_id in dict.fromkeys(args.course)
            ]
        elif Path(execution_path).exists():
            courses = read_execution(execution_path)
        else:
            courses, discovery_errors = fetch_courses(client)
        summary = execute_tasks(client, courses, config, no_exam=args.noexam, videos=args.videos)
        summary.failed.extend(discovery_errors)
        print(f"运行结果：{summary}")
        for failure in summary.failed:
            print(f"  {failure}")
        return summary.exit_code
    except KeyboardInterrupt:
        print("用户已中断")
        return 130
    except Exception as error:
        logger.exception(error)
        print(f"运行失败：{error}")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
