import json
import math
import re
import threading
import time
import urllib.request
from base64 import b64decode
from functools import partial
from getpass import getpass
from pathlib import Path
from threading import Thread
from urllib.parse import unquote_plus as unquote

import requests
from requests.cookies import RequestsCookieJar
from requests.utils import cookiejar_from_dict

from zhs.clients.ai import Openai as Openai
from zhs.clients.ai_courses import AICourseMixin
from zhs.clients.campus import CampusCourseMixin
from zhs.clients.exam import ExamCtx as ExamCtx
from zhs.clients.materials import PptToTxt as PptToTxt
from zhs.clients.shared import SharedCourseMixin
from zhs.common.crypto import VIDEO_KEY
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.notifications import barkpusher, pushpluser
from zhs.common.runtime import OperationFailed, TimeLimitExceeded
from zhs.common.transport import api_query, create_session
from zhs.common.utils import list_to_cookie_jar


class ZHSClient(SharedCourseMixin, CampusCourseMixin, AICourseMixin):
    def __init__(
        self,
        cookies: dict = None,
        headers: dict = None,
        proxies: dict = None,
        limit: int = 0,
        speed: float = None,
        end_thre: float = None,
        pushplus_token: str = "",
        bark_token: str = "",
        tree_view: bool = True,
        progressbar_view: bool = True,
        image_path: str = "",
        cache_policy: dict = None,
    ):
        """
        ### Fucker Class
        * `cookies`: dict, optional, cookies to use for the session
        * `headers`: dict, optional, headers to use for the session
        * `proxies`: dict, optional, proxies to use for the session
        * `limit`: int, optional, time limit for each course, in minutes (default is 0), auto resets on fuck*Course methods call
        * `speed`: float, optional, video playback speed
        * `end_thre`: float, optional, threshold to stop the fucker, overloaded when there are questions left unanswered
        * `tree_view` :bool, optional, print the tree progress view of the course
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        for name, value in (("speed", speed), ("end_thre", end_thre)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        logger.debug(
            f"created a Fucker {id(self)}, limit: {limit}, speed: {speed}, end_thre: {end_thre}"
        )

        self.uuid = None  # actually it's not a uuid, but a random string
        self.cookies = cookies or {}
        self.proxies = urllib.request.getproxies() if proxies is None else proxies
        self.headers = headers or {
            "Accept": "*/*",
            "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="101", "Google Chrome";v="101"',
            "sec-ch-ua-mobile": "?0",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36",
            "sec-ch-ua-platform": "macOS",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-GB,en;q=0.9",
        }
        self.session = create_session()
        self._course_started = {}
        self._active_course_id = None
        self._course_failures = None
        self._watch_threads = []
        self._stop_event = threading.Event()

        logger.debug(f"proxies: {self.proxies}")
        logger.debug(f"headers: {self.headers}")

        self.limit = abs(limit)  # time limit for fucking, in minutes
        self.speed = speed and max(speed, 0.1)  # video play speed, Falsy values for default
        self.end_thre = (
            max(end_thre or 0, 0.0) or 0.91
        )  # video play end threshold, above this will be considered as finished
        self.prefix = "  |"  # prefix for tree view
        self.context = ObjDict(default=None)  # context for methods
        self.courses = ObjDict(default=None)  # store courses info
        self._pushplus = (
            partial(pushpluser, token=pushplus_token)
            if pushplus_token
            else lambda *args, **kwargs: None
        )
        self._bark = (
            partial(barkpusher, token=bark_token) if bark_token else lambda *args, **kwargs: None
        )
        self.tree_view = tree_view
        self.progressbar_view = progressbar_view
        self.image_path = image_path
        self.cache_policy = dict(cache_policy or {})

    @property  # cannot directly manipulate _cookies property, we need to parse uuid from cookies
    def cookies(self) -> RequestsCookieJar:
        return self._cookies

    @cookies.setter
    def cookies(self, cookies: dict | list | RequestsCookieJar):
        if isinstance(cookies, dict):
            cookies = cookiejar_from_dict(cookies)
        elif isinstance(cookies, list):
            cookies = list_to_cookie_jar(cookies)

        self._cookies = cookies
        logger.debug(f"received cookies: {self.cookies}")
        if cookies:
            try:
                self.uuid = json.loads(unquote(cookies["CASLOGC"]))["uuid"]
                self._cookies[f"exitRecod_{self.uuid}"] = "2"
            except KeyError:
                raise ValueError("Cookies invalid")
        logger.debug(f"set cookies: {self._cookies}")

    def login(
        self,
        username: str = None,
        password: str = None,
        interactive: bool = True,
        use_qr: bool = False,
        qr_callback: callable = None,
    ):
        """* `interactive`: whether to use interactive mode to login"""
        if use_qr:
            if not callable(qr_callback):
                raise ValueError("callable qr_callback is required when use_qr is True")
            self._qrlogin(qr_callback)
            return
        while not username or not password:
            if not interactive:
                raise ValueError("username or password being empty")
            if not username:
                username = input("Username: ")
            else:
                print(f"Username: {username}")
            if not password:
                password = getpass("Password: ")
        # urls
        login_page = "https://passport.zhihuishu.com/login?service=https://onlineservice-api.zhihuishu.com/login/gologin"
        valid_url = "https://passport.zhihuishu.com/user/validateAccountAndPassword"
        check_url = "https://appcomm-user.zhihuishu.com/app-commserv-user/userInfo/checkNeedAuth"
        self._sessionReady()  # set cookies, headers, proxies
        self.session.headers.update(
            {"Origin": "https://passport.zhihuishu.com", "Referer": login_page}
        )
        try:
            self.session.get(login_page, proxies=self.proxies, timeout=10)
            form = {"account": username, "password": password}
            user_info = self._apiQuery(valid_url, form)  # get uuid and pwd
            if user_info.status == 1:
                pass  # success
            elif user_info.status == -2:
                raise ValueError("Username or password invalid")
            elif user_info.status == -4:
                raise Exception("Multiple invalid attempts, requires captcha")
            elif user_info.status == -9:
                raise Exception("Account requires SMS verification")
            else:
                pass  # unknown error, just try to ignore
            need_auth = self._apiQuery(check_url, {"uuid": user_info.uuid}).rt.needAuth
            if need_auth:
                raise Exception("Account need auth, please login using browser to pass auth")
            params = {"account": username, "pwd": user_info.pwd, "validate": 0}
            self.session.get(login_page, params=params, proxies=self.proxies, timeout=10)
            self.cookies = self.session.cookies.copy()
            if not self.cookies:
                raise Exception("No cookies found")

            logger.info("Login successful")
        except Exception as e:
            logger.exception(e)
            raise Exception(f"Login failed: {e}")

    def _qrlogin(self, qr_callback, max_attempts=3):
        """Refresh expired QR codes without unbounded recursion."""
        login_page = "https://passport.zhihuishu.com/login?service=https://onlineservice-api.zhihuishu.com/login/gologin"
        qr_page = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrImg"
        query_page = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrInfo"
        self._sessionReady()
        for attempt in range(max_attempts):
            with self.session.get(qr_page, timeout=10) as response:
                response.raise_for_status()
                data = response.json()
            qr_token = data["qrToken"]
            image = b64decode(data["img"])
            if self.image_path:
                directory = Path(self.image_path).expanduser()
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{time.strftime('%Y-%m-%dT%H-%M-%S')}.png").write_bytes(image)
            qr_callback(image)
            logger.debug(f"QR login received, token{qr_token}")
            scanned = False
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if self._stop_event.wait(0.5):
                    raise InterruptedError("Client closed")
                with self.session.get(
                    query_page, params={"qrToken": qr_token}, timeout=10
                ) as response:
                    response.raise_for_status()
                    message = ObjDict(response.json(), default=None)
                if message.status == -1:
                    continue
                if message.status == 0:
                    if not scanned:
                        print("QR Scanned")
                        scanned = True
                elif message.status == 1:
                    with self.session.get(
                        login_page,
                        params={"pwd": message.oncePassword},
                        proxies=self.proxies,
                        timeout=10,
                    ) as response:
                        response.raise_for_status()
                    self.cookies = self.session.cookies.copy()
                    self._checkCookies()
                    logger.info("Login successful")
                    return
                elif message.status == 2:
                    break
                elif message.status == 3:
                    raise RuntimeError("Login canceled")
                else:
                    raise RuntimeError(f"Unknown QR response: {message.msg}")
            print(f"QR code expired ({attempt + 1}/{max_attempts})")
        raise TimeLimitExceeded(f"QR login expired after {max_attempts} attempts")

    def fuckWhatever(self, aiConfig=None, no_exam=False):
        from zhs.workflow import execute_tasks, fetch_courses

        courses, errors = fetch_courses(self)
        config = {"ai": aiConfig or {"enabled": False}}
        result = execute_tasks(self, courses, config, no_exam=no_exam)
        result.failed.extend(errors)
        if result.failed:
            raise OperationFailed(str(result))
        return result

    def fuckCourse(
        self, course_id: str, course_type=None, class_id=None, aiConfig=None, no_exam=False
    ):
        from zhs.workflow import resolve_courses

        item = {"id": str(course_id)}
        if course_type:
            item["type"] = course_type
        if class_id is not None:
            item["classId"] = class_id
        course = resolve_courses(self, [item])[0]
        if course["type"] == "zhidao":
            return self.fuckZhidaoCourse(course["id"])
        if course["type"] == "hike":
            return self.fuckHikeCourse(course["id"])
        return self.fuckAiCourse(course["id"], course["classId"], aiConfig or {}, no_exam=no_exam)

    def fuckVideo(self, course_id, video_id: str, course_type=None, class_id=None):
        from zhs.workflow import resolve_courses

        item = {"id": str(course_id)}
        if course_type:
            item["type"] = course_type
        if class_id is not None:
            item["classId"] = class_id
        course = resolve_courses(self, [item])[0]
        if course["type"] == "zhidao":
            return self.fuckZhidaoVideo(course["id"], video_id)
        if course["type"] == "hike":
            return self.fuckHikeVideo(course["id"], video_id)
        return self.process_ai_video(course["id"], course["classId"], video_id)

    def watchVideo(
        self, video_id
    ):  # it's probably unnecessary but let's keep it to fool those idiots
        headers = self.session.headers.copy()
        cookies = self.session.cookies.copy()
        parse_url = "https://newbase.zhihuishu.com/video/initVideo"

        def watch():
            try:
                if self._stop_event.is_set():
                    return
                with requests.get(
                    parse_url,
                    params={
                        "jsonpCallBack": "result",
                        "videoID": str(video_id),
                        "_": int(time.time() * 1000),
                    },
                    cookies=cookies,
                    headers=headers,
                    proxies=self.proxies,
                    timeout=10,
                ) as response:
                    response.raise_for_status()
                    match = re.match(r"^result\((.*)\)$", response.text)
                    if not match:
                        raise ValueError("Invalid video metadata response")
                    url = ObjDict(json.loads(match.group(1))).result.lines[0].lineUrl
                if self._stop_event.is_set():
                    return
                with requests.get(
                    url,
                    headers=headers,
                    cookies=cookies,
                    proxies=self.proxies,
                    timeout=(5, 10),
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    next(response.iter_content(chunk_size=8192), None)
            except Exception as e:
                logger.error(f"Failed to watch video {video_id}")
                logger.exception(e)

        self._watch_threads = [thread for thread in self._watch_threads if thread.is_alive()]
        watch_thread = Thread(target=watch, name=f"video-{video_id}", daemon=True)
        self._watch_threads.append(watch_thread)
        watch_thread.start()

    def _apiQuery(self, url: str, data: dict, method: str = "POST", contentType: str = "form"):
        method = method.upper()
        logger.debug(
            f"{method} url: {url}\ndata: {json.dumps(data, indent=4, ensure_ascii=False)}\n"
            + f"headers: {json.dumps(self.headers, indent=4)}\n"
            + f"cookies: {self.session.cookies}\n"
            + f"proxies: {json.dumps(self.session.proxies, indent=4)}"
        )
        ret = api_query(
            self.session, url, data, method=method, content_type=contentType, proxies=self.proxies
        )
        logger.debug(json.dumps(ret, indent=4, ensure_ascii=False))
        return ret

    def _checkCookies(self):
        if not self._cookies:
            e = Exception("No cookies found, please login first")
            logger.error(e)
            raise e

    def _checkTimeLimit(self, cid):
        started = self._course_started.setdefault(str(cid), time.monotonic())
        if self.limit and time.monotonic() - started >= self.limit * 60:
            raise TimeLimitExceeded(f"{self.limit} minutes")

    def _startCourse(self, cid):
        self._course_started[str(cid)] = time.monotonic()

    def _startCourseIfNeeded(self, cid):
        self._course_started.setdefault(str(cid), time.monotonic())

    def reset_deadlines(self):
        self._course_started.clear()

    def _sleep(self, seconds, cid):
        self._checkTimeLimit(cid)
        if self.limit:
            remaining = self.limit * 60 - (time.monotonic() - self._course_started[str(cid)])
            seconds = min(seconds, max(0, remaining))
        if self._stop_event.wait(seconds):
            raise InterruptedError("Client closed")
        self._checkTimeLimit(cid)

    def _record_failure(self, error):
        if self._course_failures is not None:
            self._course_failures.append(str(error))

    def _course_status(self):
        if self._course_failures:
            return f"Finished with {len(self._course_failures)} failures in"
        return "Completed"

    def _notify(self, content):
        for push in (self._pushplus, self._bark):
            try:
                push("ZHS Toolkit", str(content))
            except Exception as error:
                logger.warning(f"Notification failed: {error}")

    def close(self):
        self._stop_event.set()
        deadline = time.monotonic() + 10
        for thread in self._watch_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        self.session.close()

    def _sessionReady(self, ctx: dict = None):
        ctx = ObjDict(ctx or {}, recursive=False, default=False)
        self.session.cookies = ctx.cookies or self.cookies.copy()
        self.session.headers = ctx.headers or self.headers.copy()
        self.session.proxies = self.proxies.copy()

    def zhidaoAiExamQuery(
        self,
        url: str,
        data: dict,
        encrypt: bool = True,
        ok_code: int = 0,
        setTimeStamp: bool = True,
        method: str = "POST",
        key=VIDEO_KEY,
        contentType: str = "form",
    ):
        """set ok_code to None for no check"""

        self._checkCookies()
        self._sessionReady()
        return self.zhidaoQuery(url, data, encrypt, ok_code, setTimeStamp, method, key, contentType)


Fucker = ZHSClient
