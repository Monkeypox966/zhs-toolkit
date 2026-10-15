import json
import math
import os
import re
import threading
import time
import urllib.request
from base64 import b64decode, b64encode
from datetime import datetime
from functools import partial
from getpass import getpass
from pathlib import Path
from random import randint, random, uniform
from threading import Thread
from urllib.parse import unquote_plus as unquote

import requests
from requests.cookies import RequestsCookieJar
from requests.utils import cookiejar_from_dict

from zhs.clients.ai import Openai as Openai
from zhs.clients.exam import ExamCtx
from zhs.clients.materials import PptToTxt
from zhs.common.crypto import AI_KEY, HOME_KEY, VIDEO_KEY, Cipher, WatchPoint, getEv
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.notifications import barkpusher, pushpluser
from zhs.common.runtime import CaptchaException, OperationFailed, TimeLimitExceeded, course_run
from zhs.common.signing import sign
from zhs.common.transport import api_query, create_session
from zhs.common.utils import HMS, list_to_cookie_jar, progressBar, wipeLine


class ZHSClient:
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

    def fuckCourse(self, course_id: str):
        """
        ### Fuck the whole course
        * `course_id`: `courseId`(Hike) or `recuitAndCourseId`(Zhidao)
        """
        if re.match(
            r".*[a-zA-Z].*", course_id
        ):  # determine if it's a courseId or a recruitAndCourseId
            self.fuckZhidaoCourse(course_id)  # it's a recruitAndCourseId
        else:  # it's a courseId
            self.fuckHikeCourse(course_id)

    def fuckVideo(self, course_id, video_id: str):
        """
        ### Fuck a single video
        * `course_id`: `courseId`(Hike) or `recuitAndCourseId`(Zhidao)
        * `video_id`: `fileId`(Hike) or `videoId`(Zhidao, not visible in URL)
        """
        if re.match(r".*[a-zA-Z].*", course_id):
            self.fuckZhidaoVideo(course_id, video_id)
        else:
            self.fuckHikeVideo(course_id, video_id)

    #############################################
    # for some fucking reasons
    # there are 2 sets of completely different API for hike.zhihuishu.com and studyservice-api.zhihuishu.com
    # so we need to use different methods for different API
    #############################################
    # following are methods for studyservice-api.zhihuishu.com API
    def getZhidaoList(self):
        """
        ### Get all courses of zhidao from server
        """
        if self.courses.zhidao:
            return self.courses.zhidao
        url = "https://onlineservice-api.zhihuishu.com/gateway/t/v1/student/course/share/queryShareCourseInfo"
        self._checkCookies()
        self._sessionReady()
        page = 1  # initial page number
        data = {"status": 0, "pageNo": page, "pageSize": 5}
        r = self.zhidaoQuery(url, data, ok_code=200, key=HOME_KEY).result
        r.default = None
        total = r.totalCount or 0
        self.courses.zhidao = r.courseOpenDtos or []
        for i in range(2, int(math.ceil(total / 5)) + 1):
            data["pageNo"] = i
            r = self.zhidaoQuery(url, data, ok_code=200, key=HOME_KEY).result
            self.courses.zhidao += r.courseOpenDtos
        return self.courses.zhidao

    def getZhidaoContext(self, RAC_id: str, force: bool = False):
        """
        ### fetch context for zhidao course
        * `RAC_id`: `recruitAndCourseId`
        * `force`: force update
        """
        if RAC_id in self.context and not force:
            return self.context[RAC_id]
        self._checkCookies()
        logger.info(f"Getting context for {RAC_id}")

        self._sessionReady()  # set cookies, headers, proxies
        self.session.headers.update(
            {"Origin": "https://studyh5.zhihuishu.com", "Referer": "https://studyh5.zhihuishu.com/"}
        )
        # cross sites login
        self.gologin(RAC_id)

        # get course info, including recruitId, course name, etc
        course = self.queryCourse(RAC_id)
        recruit_id = course.recruitId

        # get chapters
        chapters = self.videoList(RAC_id)
        lesson_ids = []
        videos = ObjDict()
        for chapter in chapters.videoChapterDtos:
            for lesson in chapter.videoLessons:
                lesson_ids.append(lesson.id)
                if "videoId" in lesson:  # this lesson has only one video
                    v = lesson.copy()
                    v.lessonId = lesson.id
                    v.id = 0
                    lesson.videoSmallLessons = [v]
                for v in lesson.videoSmallLessons:
                    v.chapterId = chapter.id
                    videos[v.videoId] = v
        logger.info(f"{len(lesson_ids)} lessons, {len(videos)} videos")

        # get read-before, maybe unneccessary. BUTT hey, it's a POST request
        # self.queryStudyReadBefore(course_id, recruit_id)

        # get study info, including watchState, studyTotalTime
        video_ids = [video.id for video in videos.values() if video.id]
        states = self.queryStudyInfo(lesson_ids, video_ids, recruit_id)
        states.default = ObjDict(default=False)  # set default value for non exist attribute
        for v in videos.values():
            state = states.lv[str(v.id)] or states.lesson[str(v.lessonId)]
            v.watchState, v.studyTotalTime = state.watchState, state.studyTotalTime

        # get most recently viewed video id, probably unneccessary, again, it's a POST request
        self.queryUserRecruitIdLastVideoId(recruit_id)

        # update context
        ctx = ObjDict(
            {
                "course": course,
                "chapters": chapters,
                "videos": videos,
                "cookies": self.session.cookies.copy(),
                "headers": self.session.headers.copy(),
                "fucked_time": 0,
            },
            default={},
        )
        self.context[RAC_id] = ctx
        return ctx

    @course_run
    def fuckZhidaoCourse(self, RAC_id: str):
        """
        * `RAC_id`: `recruitAndCourseId`
        """
        logger.info(f"Fucking Zhidao course {RAC_id}")
        tprint = print if self.tree_view else lambda *a, **k: None

        # load context
        ctx = self.getZhidaoContext(RAC_id)
        course = ctx.course
        chapters = ctx.chapters

        # start fucking
        tprint(f"Fucking Zhidao course: {course.courseInfo.name or course.courseInfo.enName}")
        begin_time = time.time()  # real world time
        prefix = self.prefix  # prefix for tree-like print
        try:
            # 在 nohup 下运行无法获取，进行捕获
            w_lim = os.get_terminal_size().columns - 1  # width limit for terminal output
        except Exception:
            # 考虑直接移除此变量，但是保留原代码风格，故进行赋值
            w_lim = 80
        try:
            for chapter in chapters.videoChapterDtos:
                tprint(prefix)  # extra line as separator
                tprint(f"{prefix}__Fucking chapter {chapter.name}"[:w_lim])
                for lesson in chapter.videoLessons:
                    tprint(prefix * 2)
                    tprint(f"{prefix * 2}__Fucking lesson {lesson.name}"[:w_lim])
                    for video in lesson.videoSmallLessons:
                        tprint(f"{prefix * 3}__Fucking video {video.name}"[:w_lim])
                        try:
                            self.fuckZhidaoVideo(RAC_id, video.videoId)
                        except TimeLimitExceeded as e:
                            logger.info(f"Fucking time limit exceeded: {e}")
                            self._notify("已达到课程时限")
                            tprint(prefix)
                            tprint(f"{prefix}##Fucking time limit exceeded: {e}\n")
                            raise
                        except CaptchaException:
                            logger.info("Captcha required")
                            self._notify("需要提供验证码")
                            tprint(prefix)
                            tprint(f"{prefix}##Captcha required\a\n")
                            raise
                        except Exception as e:
                            self._record_failure(e)
                            logger.exception(e)
                            self._notify(str(e))
                            tprint(f"{prefix * 3}##Failed: {e}"[:w_lim])
        except KeyboardInterrupt:
            logger.info("User interrupted")
            raise
        wipeLine()
        tprint(prefix)
        tprint(
            f"\r{prefix}__{self._course_status()} course {course.courseInfo.name}, cost {time.time() - begin_time:.2f}s\n"
        )

    def fuckZhidaoVideo(self, RAC_id, video_id):
        """
        * `RAC_id`: `recruitAndCourseId`
        * `video_id`: `videoId`
        """
        self._checkCookies()
        ctx = self.getZhidaoContext(RAC_id)
        self._checkTimeLimit(RAC_id)
        self._sessionReady(ctx)
        video = ctx.videos.get(video_id) or ctx.videos.get(str(video_id))
        if video is None and str(video_id).isdigit():
            video = ctx.videos.get(int(video_id))
        if not video:
            raise ValueError(f"Video {video_id} not found")
        played_time = video.studyTotalTime
        watch_state = video.watchState
        if (
            watch_state == 1 and self.end_thre <= 1.0
        ):  # check end_thre in case someone wants to rewatch
            logger.info(f"Video {video.name} already watched")
            return
        # get token id from pre learning note
        token_id = self.prelearningNote(RAC_id, video_id).studiedLessonDto.id
        token_id = b64encode(str(token_id).encode()).decode()

        # get questions
        questions: ObjDict = self.loadVideoPointerInfo(RAC_id, video_id)
        questions.default = None
        questions = questions.questionPoint or []
        questions = sorted(questions, key=lambda x: x.timeSec, reverse=True) if questions else None
        while questions and questions[-1].timeSec <= played_time:
            questions.pop()  # remove questions that are already answered

        # compute end time and make sure to answer all questions
        end_time = max(video.videoSec * self.end_thre, 1.0)
        if questions:
            end_time = max(
                questions[0].timeSec, end_time
            )  # compare last question time with end_time

        # emulating video playing
        self.watchVideo(video.videoId)

        # no idea what it is
        self.threeDimensionalCourseWare(video.videoId)

        # prepare vars
        speed = self.speed or 1.5  # default speed for Zhidao is 1.5
        last_submit = played_time  # last pause time
        elapsed_time = 0  # real world time elapsed
        db_interval = 30  # database report interval
        cache_interval = 18  # cache report interval
        answer = None  # answer flag, do not modify
        report = False  # report flag, do not modify
        pause = 0  # pause flag, do not modify
        wp = WatchPoint()  # watch point, do not modify

        ##### start main event loop, sort of...
        while played_time < end_time:
            self._sleep(1, RAC_id)
            ctx.fucked_time += 1  # for time limit check
            elapsed_time += 1
            played_time = min(
                played_time + speed, end_time
            )  # update video time and make sure not exceeding end_time
            pause = (
                pause or int(random() < 0.0025) * 60
            )  # randomly pause a minute, may avoid detection
            report = report or pause == 60  # report on pause

            ### events
            ## on pause
            if pause:
                pause -= 1
                played_time = last_submit
            ## update watch point
            if not elapsed_time % 2:
                wp.add(played_time)
            ## get questions
            if questions and played_time >= questions[-1].timeSec:
                question = questions.pop()
                try:
                    question = (
                        self.lessonPopoupExam(RAC_id, video_id, question.questionIds)
                        .lessonTestQuestionUseInterfaceDtos[0]
                        .testQuestion
                    )
                    answer = 2  # answer delay time
                    report = True  # set report flag
                except Exception as e:
                    logger.error(f"can't get question detail:\n{e}")
            ## answer questions
            if answer is not None:
                if answer == 0:
                    answer = None  # unset answer flag
                    self.saveLessonPopupExamSaveAnswer(
                        RAC_id, video_id, question.questionId, self.answerZhidao(question)
                    )
                else:
                    pause = pause or 1  # emulate pause on pop quiz
                    answer -= 1
            ## report to database
            if elapsed_time % db_interval == 0 or played_time >= end_time or report:
                report = False  # unset report flag
                wp.add(played_time)
                # now submit to database
                self.saveDatabaseIntervalTimeV2(
                    RAC_id, video_id, played_time, last_submit, wp.get(), token_id
                )
                last_submit = played_time  # update last pause time
                wp.reset(played_time)  # reset watch point
            ## report to cache
            if False and elapsed_time % cache_interval == 0:
                wp.add(played_time)
                self.saveCacheIntervalTime(
                    RAC_id, video_id, played_time, last_submit, wp.get(), token_id
                )
                last_submit = played_time  # update last pause time
                wp.reset(played_time)  # reset watch point
            ### end events
            # print progress bar
            s, e = [60 - pause, 60] if pause else [played_time, end_time]
            # have a glance of when quiz is answered
            action = (
                "pause a minute"
                if pause
                else f"fucking {video.videoId}"
                if answer is None
                else "answering quiz"
            )
            progressBar(s, e, prefix=action, suffix="done", progressbar_view=self.progressbar_view)
        ##### end main event loop
        self._sleep(random() + 1, RAC_id)

    def answerZhidao(self, q: dict):
        """you can override this function to answer questions"""
        q = ObjDict(q)
        a = [
            str(opt.id) for opt in q.questionOptions if opt.result == "1"
        ]  # choose correct answers
        return ",".join(a)

    def zhidaoQuery(
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
        cipher = Cipher(key)
        if setTimeStamp:
            # somehow their timestamps are ending with 000
            _t = int(time.time()) * 1000
            data["dateFormate"] = _t
        logger.debug(
            f"{method} url: {url}\nraw_data: {json.dumps(data, indent=4, ensure_ascii=False)}"
        )
        form = {"secretStr": cipher.encrypt(json.dumps(data))} if encrypt else data

        if setTimeStamp:
            form["dateFormate"] = _t

        # 如果是POST请求，且contentType为json，将data转为json字符串
        if method == "POST" and contentType == "json":
            form = json.dumps(form)
        ret = self._apiQuery(url, data=form, method=method, contentType=contentType)
        if ok_code is not None and ret.code != ok_code:
            ret.default = None
            if ret.code == -12:
                e = CaptchaException("captcha required")
            else:
                e = Exception(
                    f"code: {ret.code} "
                    + f"msg: {ret.message or json.dumps(ret, indent=4, ensure_ascii=False)}"
                )
            logger.error(e)
            raise e
        return ret

    def gologin(self, RAC_id):
        """### cross sites login for zhidao course"""
        login_url = "https://studyservice-api.zhihuishu.com/login/gologin"
        params = {
            "fromurl": f"https://studyh5.zhihuishu.com/videoStudy.html#/studyVideo?recruitAndCourseId={RAC_id}"
        }
        logger.debug(f"GET {login_url}\nparams: {params}\n")
        return self.session.get(login_url, params=params, proxies=self.proxies, timeout=10)

    def queryCourse(self, RAC_id):
        """### query course info for zhidao share course"""
        course_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/queryCourse"
        return self.zhidaoQuery(course_url, {"recruitAndCourseId": RAC_id}).data

    def videoList(self, RAC_id):
        """### query video/chapter list for zhidao share course"""
        videos_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/videolist"
        chapters = self.zhidaoQuery(videos_url, {"recruitAndCourseId": RAC_id}).data
        chapters.default = []  # set default value for non exist attribute
        return chapters

    def queryStudyReadBefore(self, course_id, recruit_id):
        """### query study read before for zhidao share course"""
        read_url = (
            "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/queryStudyReadBefore"
        )
        return self.zhidaoQuery(
            read_url, data={"courseId": course_id, "recruitId": recruit_id}, ok_code=None
        ).data

    def queryStudyInfo(self, lesson_ids: list, video_ids: list, recruit_id):
        """### query study info for zhidao"""
        state_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/queryStuyInfo"  # NOT MY TYPO
        data = {"lessonIds": lesson_ids, "lessonVideoIds": video_ids, "recruitId": recruit_id}
        return self.zhidaoQuery(state_url, data=data).data

    def queryUserRecruitIdLastVideoId(self, recruit_id):
        """### query user recruit id last video id for zhidao"""
        last_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/queryUserRecruitIdLastVideoId"
        return self.zhidaoQuery(last_url, data={"recruitId": recruit_id}).data

    def prelearningNote(self, RAC_id, video_id):
        """### query prelearning note for zhidao"""
        note_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/prelearningNote"

        ctx = self.getZhidaoContext(RAC_id)
        course_id = ctx.chapters.courseId
        recruit_id = ctx.course.recruitId
        video = ctx.videos[video_id]
        data = {
            "ccCourseId": course_id,
            "chapterId": video.chapterId,
            "isApply": 1,
            "lessonId": video.lessonId,  # this.lessonId
            "lessonVideoId": video.id,  # this.smallLessonId
            "recruitId": recruit_id,
            "videoId": video.videoId,
        }
        return self.zhidaoQuery(note_url, data=data).data

    def loadVideoPointerInfo(self, RAC_id, video_id):
        """### query video pointer info for zhidao"""
        event_url = (
            "https://studyservice-api.zhihuishu.com/gateway/t/v1/popupAnswer/loadVideoPointerInfo"
        )
        ctx = self.getZhidaoContext(RAC_id)
        course_id = ctx.chapters.courseId
        recruit_id = ctx.course.recruitId
        video = ctx.videos[video_id]
        data = {
            "lessonId": video.lessonId,
            "lessonVideoId": video.id,
            "recruitId": recruit_id,
            "courseId": course_id,
        }
        return self.zhidaoQuery(event_url, data=data).data

    def lessonPopoupExam(self, RAC_id, video_id, question_ids: list):
        """### query lesson popoup exam for zhidao"""
        getQ_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/popupAnswer/lessonPopupExam"
        ctx = self.getZhidaoContext(RAC_id)
        video = ctx.videos[video_id]
        data = {
            "lessonId": video.lessonId,  # this.lessonId
            "lessonVideoId": video.id,  # this.smallLessonId
            "questionIds": question_ids,
        }
        return self.zhidaoQuery(getQ_url, data).data

    def saveLessonPopupExamSaveAnswer(self, RAC_id, video_id, question_id, answer_ids: str):
        """### save lesson popup exam save answer for zhidao"""
        subQ_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/popupAnswer/saveLessonPopupExamSaveAnswer"
        ctx = self.getZhidaoContext(RAC_id)
        course_id = ctx.chapters.courseId
        recruit_id = ctx.course.recruitId
        video = ctx.videos[video_id]
        data = {
            "courseId": course_id,  # this.courseId,
            "recruitId": recruit_id,  # this.recruitId
            "testQuestionId": question_id,  # this.pageList.testQuestion.questionId
            "isCurrent": "1",  # this.result ...it should be 'isCorrect'... in the name of lord, can somebody teach them eNgLIsH!!
            "lessonId": video.lessonId,  # this.lessonId
            "lessonVideoId": video.id,  # this.smallLessonId
            "answer": answer_ids,  # this.answerStu.join(",")
            "testType": 0,  # always 0
        }
        return self.zhidaoQuery(subQ_url, data).data

    def saveDatabaseIntervalTime(
        self, RAC_id, video_id, played_time, last_submit, watch_point, token_id=None
    ):
        """### save database interval time for zhidao"""
        record_url = (
            "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTime"
        )
        ctx = self.getZhidaoContext(RAC_id)
        recruit_id = ctx.course.recruitId
        video = ctx.videos[video_id]
        raw_ev = [
            recruit_id,
            video.lessonId,  # this.lessonId
            video.id,  # this.smallLessonId
            video.videoId,  # this.videoId
            video.chapterId,  # this.chapterId
            "0",  # this.data.studyStatus, always 0
            int(played_time - last_submit),  # this.playTimes
            int(played_time),  # this.totalStudyTime
            HMS(
                seconds=min(
                    video.videoSec,  # more realistic
                    int(played_time + randint(29, 31)),
                )
            ),
        ]
        if not token_id:
            token_id = self.prelearningNote(RAC_id, video_id).studiedLessonDto.id
            token_id = b64encode(str(token_id).encode()).decode()
        data = {"watchPoint": watch_point, "ev": getEv(raw_ev), "learningTokenId": token_id}
        return self.zhidaoQuery(record_url, data=data).data

    def threeDimensionalCourseWare(self, video_id):
        """### query three dimensional course ware for zhidao"""
        ware_url = (
            "https://studyservice-api.zhihuishu.com/gateway/t/v1/course/threeDimensionalCourseWare"
        )
        params = {"videoId": video_id}
        return self.zhidaoQuery(ware_url, data=params, method="GET").data

    def saveDatabaseIntervalTimeV2(
        self, RAC_id, video_id, played_time, last_submit, watch_point, token_id=None, initial=False
    ):
        """### save database interval time for zhidao"""
        record_url = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"
        ctx = self.getZhidaoContext(RAC_id)
        recruit_id = ctx.course.recruitId
        video = ctx.videos[video_id]
        if initial:  # sometimes a request like this happens, I originally thought it is the initialization request, but I might be wrong
            raw_ev = [
                recruit_id,
                video.chapterId,  # this.chapterId
                ctx.course.courseInfo.courseId,
                video.lessonId,  # this.smallLessonId
                HMS(seconds=min(video.videoSec, int(played_time))),
                int(played_time),
                video.videoId,  # this.videoId
                "0",  # this.data.studyStatus, always 0
                int(played_time),  # this.totalStudyTime
                self.uuid,
            ]
        else:
            raw_ev = [
                recruit_id,
                video.lessonId,  # this.lessonId
                video.id,  # this.smallLessonId
                video.videoId,  # this.videoId
                video.chapterId,  # this.chapterId
                "0",  # this.data.studyStatus, always 0
                int(played_time - last_submit),  # this.playTimes
                int(played_time),  # this.totalStudyTime
                HMS(seconds=min(video.videoSec, int(played_time))),
                self.uuid + "zhs",
            ]
        if not token_id:
            token_id = self.prelearningNote(RAC_id, video_id).studiedLessonDto.id
            token_id = b64encode(str(token_id).encode()).decode()
        data = {
            "ewssw": watch_point,
            "sdsew": getEv(raw_ev),
            "zwsds": token_id,
            "courseId": ctx.course.courseInfo.courseId,
        }
        if initial:
            data.pop("courseId")
        return self.zhidaoQuery(record_url, data=data).data

    def saveCacheIntervalTime(
        self, RAC_id, video_id, played_time, last_submit, watch_point, token_id=None
    ):
        """### save cache interval time for zhidao"""
        cache_url = (
            "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveCacheIntervalTime"
        )
        ctx = self.getZhidaoContext(RAC_id)
        recruit_id = ctx.course.recruitId
        course_id = ctx.chapters.courseId
        video = ctx.videos[video_id]
        #!! NOTICE: content is different from database
        raw_ev = [
            recruit_id,
            video.chapterId,
            course_id,
            video.lessonId,
            HMS(
                seconds=min(
                    video.videoSec,  # more realistic
                    int(played_time + randint(10, 20)),
                )
            ),
            int(played_time),
            video.videoId,
            video.id,
            int(played_time - last_submit),
        ]
        if not token_id:
            token_id = self.prelearningNote(RAC_id, video_id).studiedLessonDto.id
            token_id = b64encode(str(token_id).encode()).decode()
        data = {"watchPoint": watch_point, "ev": getEv(raw_ev), "learningTokenId": token_id}
        return self.zhidaoQuery(cache_url, data=data).data

    # end of zhidao methods
    #############################################
    # following are methods for hike API
    def getHikeList(self):
        """
        ### Get all courses of zhidao from server
        """
        if self.courses.hike:
            return self.courses.hike
        url = "https://hikeservice.zhihuishu.com/student/course/aided/getMyCourseList"
        self._checkCookies()
        self._sessionReady()
        params = {
            "uuid": self.uuid,
            "data": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }
        r = self._apiQuery(url, params, "GET").result
        r.default = None
        self.courses.hike = r.startInngcourseList or []  # I've given up on their eNgLIsH
        return self.courses.hike

    def getHikeContext(self, course_id: str, force: bool = False):
        if course_id in self.context and not force:
            return self.context[course_id]
        self._checkCookies()
        self._sessionReady()  # set cookies, headers, proxies
        self.session.headers.update(
            {"Origin": "https://hike.zhihuishu.com", "Referer": "https://hike.zhihuishu.com/"}
        )
        root = self.queryResourceMenuTree(course_id)
        ctx = ObjDict(
            {
                "root": root,
                "cookies": self.session.cookies.copy(),
                "headers": self.session.headers.copy(),
                "fucked_time": 0,
            },
            default={},
        )
        self.context[course_id] = ctx
        return ctx

    @course_run
    def fuckHikeCourse(self, course_id: str):
        tprint = print if self.tree_view else lambda *a, **k: None
        begin_time = time.time()
        root = self.getHikeContext(course_id).root

        prefix = self.prefix
        logger.info(f"Fucking Hike course {course_id} (total root chapters: {len(root)})")
        tprint(f"Fucking course {course_id} (total root chapters: {len(root)})")
        try:
            for chapter in root:
                self._traverse(course_id, chapter)
        except KeyboardInterrupt:
            logger.info("user interrupted")
            raise
        logger.info(f"{self._course_status()} course {course_id}, cost {time.time() - begin_time}s")
        wipeLine()
        tprint(prefix)
        tprint(
            f"{prefix}__{self._course_status()} course {course_id}, cost {time.time() - begin_time:.2f}s\n"
        )

    def fuckHikeVideo(self, course_id, file_id, prev_time=0):
        self._checkCookies()
        logger.info(f"Fucking Hike video {file_id} of course {course_id}")
        begin_time = time.time()
        ctx = self.getHikeContext(course_id)
        self._checkTimeLimit(course_id)
        self._sessionReady(ctx)  # set cookies, headers, proxies
        # get video info
        file_info = self.stuViewFile(course_id, file_id)

        # emulating video playing
        self.watchVideo(file_info.dataId)

        # getting ready to fuck
        total_time = int(file_info.totalTime)
        start_date = int(time.time() * 1000)
        speed = self.speed or 1.25  # default speed for Hike is 1.25
        interval = 30  # interval between 2 progess reports
        end_time = max(total_time * self.end_thre, 1.0)
        played_time = prev_time  # total video played time
        # start main loop
        stalled_reports = 0
        while played_time < end_time:
            self._sleep(1, course_id)
            ctx.fucked_time += 1
            played_time = min(played_time + speed, end_time)
            # enter branch when video is finished or interval is reached
            if played_time >= end_time or not (int(played_time - prev_time) % interval):
                ret_time = self.saveStuStudyRecord(
                    course_id, file_id, played_time, prev_time, start_date
                )  # report progress
                ret_time = float(ret_time)
                if not math.isfinite(ret_time) or ret_time < 0:
                    raise OperationFailed("服务端返回无效播放进度")
                stalled_reports = stalled_reports + 1 if ret_time <= prev_time else 0
                if stalled_reports >= 3:
                    raise OperationFailed("服务端播放进度连续三次未推进")
                prev_time, played_time = ret_time, ret_time
            progressBar(
                played_time,
                end_time,
                prefix=f"fucking {file_id}",
                suffix="done",
                progressbar_view=self.progressbar_view,
            )
        logger.info(
            f"Fucked video {file_id} of course {course_id}, cost {time.time() - begin_time:.2f}s"
        )
        self._sleep(random() + 1, course_id)

    def fuckFile(self, course_id, file_id):
        self.stuViewFile(course_id, file_id)
        self._sleep(random() * 2 + 1, course_id)

    def _traverse(self, course_id, node: ObjDict, depth=0):
        self._checkTimeLimit(course_id)
        depth += 1
        tprint = print if self.tree_view else lambda *a, **k: None
        try:
            # 在 nohup 下运行无法获取，进行捕获
            w_lim = os.get_terminal_size().columns - 1  # width limit for terminal output
        except Exception:
            # 考虑直接移除此变量，但是保留原代码风格，故进行赋值
            w_lim = 80
        prefix = self.prefix * depth
        if node.childList:  # if childList is not None, then it's a chapter
            chapter = node
            logger.debug(f"Fucking chapter {chapter.id}")
            tprint(prefix)  # separate chapters
            tprint(f"{prefix}__Fucking chapter {chapter.name}"[:w_lim])
            for child in chapter.childList:
                self._traverse(course_id, child, depth=depth)
        else:  # if childList is None, then it's a file
            file = node
            file.studyTime = file.studyTime or 0  # sometimes it's None
            logger.debug(f"Fucking file {file.id}, data type: {file.dataType}")
            tprint(f"{prefix}__Fucking {file.name}"[:w_lim])

            if file.studyTime >= file.totalTime * self.end_thre:
                logger.debug(f"Skipped file {file.id}")
                return

            try:
                if file.dataType == 3:
                    self.fuckHikeVideo(course_id, file.id, file.studyTime)
                elif file.dataType is None:
                    tprint(f"{prefix}##Unsupported file type, may be a quiz"[:w_lim])
                else:
                    self.fuckFile(course_id, file.id)
            except TimeLimitExceeded as e:
                logger.info(f"Time limit exceeded, video {file.id} skipped")
                tprint(f"{prefix}##Time limit exceeded: {e}")
                raise
            except Exception as e:
                self._record_failure(e)
                logger.error(f"Failed to fuck file {file.id} of course {course_id}")
                logger.exception(e)
                tprint(f"{prefix}##Failed: {e}"[:w_lim])

    def hikeQuery(
        self,
        url: str,
        data: dict,
        sig: bool = False,
        ok_code: int = 200,
        setTimeStamp: bool = True,
        method: str = "GET",
    ):
        """set ok_code to None for no check"""
        if setTimeStamp:
            data["_"] = int(time.time() * 1000)  # miliseconds
        if sig:
            for k, v in data.items():
                data[k] = str(v)
            data["signature"] = sign(data)
        ret = self._apiQuery(url, data, method=method)
        if ok_code is not None and int(ret.status) != ok_code:
            ret.default = None
            e = Exception(
                f"{ret.status} {ret.message or json.dumps(ret, indent=4, ensure_ascii=False)}"
            )
            logger.error(e)
            raise e
        return ret

    def queryResourceMenuTree(self, course_id):
        """### get resource menu tree for hike"""
        url = "https://studyresources.zhihuishu.com/studyResources/stuResouce/queryResourceMenuTree"
        params = {"courseId": course_id}
        return self.hikeQuery(url, params).rt

    def stuViewFile(self, course_id, file_id):
        """### get resource menu tree for hike"""
        parse_url = "https://studyresources.zhihuishu.com/studyResources/stuResouce/stuViewFile"
        params = {"courseId": course_id, "fileId": file_id}
        # get video info
        return self.hikeQuery(parse_url, params).rt

    def saveStuStudyRecord(self, course_id, file_id, played_time, prev_time, start_date):
        """### save study record for hike"""
        url = "https://hike-teaching.zhihuishu.com/stuStudy/saveStuStudyRecord"
        params = {
            "uuid": self.uuid,
            "courseId": course_id,
            "fileId": file_id,
            "studyTotalTime": int(played_time - prev_time),
            "startWatchTime": int(prev_time),
            "endWatchTime": int(played_time),
            "startDate": start_date,
            "endDate": int(time.time() * 1000),
        }
        rt = self.hikeQuery(url, params, sig=True, ok_code=200).rt
        if rt is None:
            raise Exception("Failed to save study record")
        return rt

    # end of hike methods
    #######################################
    # shared methods
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

    def getZhidaoAiList(self):
        """
        ### 取得智慧树AI课程列表
        """
        url = (
            "https://onlineservice-api.zhihuishu.com/gateway/t/v1/student/queryStudentAICourseList"
        )
        self._checkCookies()
        self._sessionReady()
        data = {"status": 3}
        ret = self.zhidaoQuery(url, data, ok_code=None, key=HOME_KEY).rt

        return ret

    def getAiKnowlegePoints(self, course_id: int, class_id: int):
        """
        ### 取得智慧树AI课程的知识点
        * `course_id`: 课程ID
        * `class_id`: 班级ID
        """
        url = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/knowledge-study/course-basic"
        self._checkCookies()
        self._sessionReady()
        data = {"courseId": course_id, "classId": class_id}
        ret = self.zhidaoQuery(url, data, ok_code=200, key=AI_KEY, contentType="json").data

        return ret

    def aiResourseComplete(
        self, courseId: int, classId: int, knowledgeId: int, resourcesUid: int, watchUId: int
    ):
        """
        ### 完成智慧树AI课程的资源
        """
        url = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/studyRecord/completed"
        self._checkCookies()
        self._sessionReady()
        data = {
            "courseId": courseId,
            "classId": classId,
            "knowledgeId": knowledgeId,
            "resourcesUid": resourcesUid,
            "watchUId": watchUId,
        }
        ret = self.zhidaoQuery(url, data, ok_code=200, key=AI_KEY, contentType="json").data

        return ret

    def reportAiVideoProcess(
        self,
        courseId: int,
        classId: int,
        fileId: int,
        knowledgeId: int,
        lastWatchTime: int,
        studyTotalTime: int = 10,
        shareCourseId: str = "",
        nodeType: int = 0,
        watchUId: int = 1,
    ):
        """
        ### 上传智慧树AI视频观看进度
        """
        url = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/studyRecord/report"
        self._checkCookies()
        self._sessionReady()

        now = int(time.time() * 1000)
        data = {
            "courseId": courseId,
            "classId": classId,
            "fileId": fileId,
            "knowledgeId": knowledgeId,
            "lastWatchTime": lastWatchTime,
            "studyTotalTime": studyTotalTime,
            "shareCourseId": shareCourseId,
            "nodeType": nodeType,
            "watchUId": watchUId,
            "dateFormate": now,
        }
        try:
            self.zhidaoQuery(url, data, ok_code=200, key=AI_KEY, contentType="json")
            return True
        except Exception as e:
            logger.exception(e)
            raise OperationFailed("Failed to report AI video progress") from e

    def listKnowledgeResources(self, courseId: int, classId: int, knowledgeId: int):
        """
        ### 取得智慧树AI课程的知识点资源列表
        """
        url = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/resources/list-knowledge-resource"
        self._checkCookies()
        self._sessionReady()
        data = {"courseId": courseId, "classId": classId, "knowledgeId": knowledgeId}
        ret = self.zhidaoQuery(url, data, ok_code=200, key=AI_KEY, contentType="json").data

        return ret

    def queryAiExam(self, courseId: int, classId: int, knowledgeId: int) -> dict:
        """
        ### 取得智慧树AI课程的考试信息
        """
        url = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/exam/questions-paper"
        self._checkCookies()
        self._sessionReady()
        data = {
            "scMapId": courseId,
            "courseId": classId,
            "classId": classId,
            "knowledgeId": knowledgeId,
        }

        try:
            ret = self.zhidaoQuery(url, data, ok_code=200, key=AI_KEY, contentType="json").data
            return ret

        except Exception as e:
            logger.exception(e)
            raise OperationFailed("Failed to query AI exam") from e

    def fuckAiVideo(
        self,
        courseId: int,
        classId: int,
        fileId: int,
        knowledgeId: int,
        watchUId: int = 1,
        startAt: int = 0,
    ):
        """
        ### 观看智慧树AI视频
        """
        # 取得视频长度
        url = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/resources-lab/get-video-time"
        self._checkCookies()
        self._checkTimeLimit(courseId)
        self._sessionReady()
        data = {"courseId": courseId, "classId": classId, "videoIdList": [fileId]}

        try:
            ret = self.zhidaoQuery(url, data, ok_code=200, key=AI_KEY, contentType="json").data
            video_length = ret[0].time
        except Exception as e:
            logger.exception(e)
            raise Exception("Failed to get video length")

        self.watchVideo(fileId)

        played_time = startAt
        # 开始观看
        while played_time < video_length:
            self._checkTimeLimit(courseId)
            # 上传视频观看进度
            played_time = min(played_time + (self.speed or 1.5) * 2, video_length)
            try:
                self.reportAiVideoProcess(
                    courseId, classId, fileId, knowledgeId, int(played_time), watchUId=watchUId
                )
            except Exception as e:
                logger.exception(e)
                raise

            self._sleep(2, courseId)

            if self.progressbar_view:
                # print progress bar
                s, e = [played_time, video_length]
                action = f"fucking {fileId}"
                progressBar(s, e, prefix=action, suffix=f"({played_time}/{video_length})")

    @course_run
    def fuckAiCourse(self, courseId: int, classId: int, aiConfig: dict, no_exam: bool = False):
        """
        ### 观看智慧树AI课程
        """
        tree_view = self.tree_view

        logger.info(f"Fucking AI course {courseId} in class {classId}")
        tprint = print if tree_view else lambda *a, **k: None

        # 取得课程信息
        try:
            knowledgePoints = self.getAiKnowlegePoints(courseId, classId)

            tprint(f"Fucking Zhidao AI course: {knowledgePoints.courseName}")

            begin_time = time.time()  # real world time
            prefix = self.prefix  # prefix for tree-like print
            try:
                w_lim = os.get_terminal_size().columns - 1  # width limit for terminal output
            except Exception:
                w_lim = 80

            cakeThemeList = knowledgePoints.cakeThemeList
        except TimeLimitExceeded:
            raise
        except Exception as e:
            self._record_failure(e)
            logger.exception(e)
            raise Exception("Failed to get knowledge points")

        # 遍历主题
        for theme in cakeThemeList:
            tprint(prefix)  # extra line as separator
            tprint(f"{prefix}__Fucking theme {theme.themeName}"[:w_lim])
            # 取得知识点列表
            try:
                knowledgeList = theme.knowledgeList
            except TimeLimitExceeded:
                raise
            except Exception as e:
                self._record_failure(e)
                logger.exception(e)
                tprint(prefix * 2)
                tprint(f"{prefix * 2}__Theme {theme.themeName} has no knowledge points")
                continue

            # 遍历知识点
            for knowledge in knowledgeList:
                self._checkTimeLimit(courseId)
                ppts = []
                ppt_conf = aiConfig.get("ppt_processing", {})
                moonShot_conf = ppt_conf.get("moonShot", {})
                tprint(prefix * 2)  # extra line as separator
                tprint(f"{prefix * 2}__Fucking knowledge point {knowledge.knowledgeName}"[:w_lim])

                # 获取资源列表
                if knowledge.studyProgress < 100:  # knowledge point not fucked
                    try:
                        resources = self.listKnowledgeResources(
                            courseId, classId, knowledge.knowledgeId
                        )
                        resourceList = resources.resourceList
                    except TimeLimitExceeded:
                        raise
                    except Exception as e:
                        self._record_failure(e)
                        logger.exception(e)
                        tprint(prefix * 3)
                        tprint(
                            f"{prefix * 3}__Failed to get resources for knowledge point {knowledge.knowledgeName}"
                        )
                        continue
                    # 遍历资源
                    for resource in resourceList:
                        self._sleep(randint(1, 10) * 0.2, courseId)  # random delay
                        tprint(prefix * 3)  # extra line as separator
                        tprint(
                            f"{prefix * 3}__Fucking resource {resource.resourcesDetail.resourcesName}"[
                                :w_lim
                            ]
                        )

                        # 判断资源类型
                        resourceType = resource.resourcesDetail.resourcesType
                        resourceDistributeType = resource.resourcesDetail.resourcesDistributeType
                        if resource.studyStatus == 1:  # already fucked
                            tprint(prefix * 4)
                            tprint(f"{prefix * 4}__Resource already fucked")

                            if resourceType == 1 and resourceDistributeType == 4:  # ppt
                                pptName = resource.resourcesDetail.resourcesName
                                pptUrl = resource.resourcesDetail.resourcesUrl

                                ppts.append({"name": pptName, "url": pptUrl})
                            continue

                        # text or powerpoint
                        if (resourceType == 2 and resourceDistributeType == 1) or (
                            resourceType == 1 and resourceDistributeType == 4
                        ):
                            try:
                                self.aiResourseComplete(
                                    courseId,
                                    classId,
                                    knowledge.knowledgeId,
                                    resource.resourcesDetail.resourcesUid,
                                    1,
                                )
                                tprint(prefix * 4)
                                tprint(
                                    f"{prefix * 4}__Resource type is {'text' if resourceType == 2 else 'ppt'}, fucked"
                                )

                                resourceType = resource.resourcesDetail.resourcesType
                                resourceDistributeType = (
                                    resource.resourcesDetail.resourcesDistributeType
                                )

                                if resourceType == 1 and resourceDistributeType == 4:  # ppt
                                    pptName = resource.resourcesDetail.resourcesName
                                    pptUrl = resource.resourcesDetail.resourcesUrl

                                    ppts.append({"name": pptName, "url": pptUrl})
                            except TimeLimitExceeded:
                                raise
                            except Exception as e:
                                self._record_failure(e)
                                logger.exception(e)
                                tprint(prefix * 4)
                                tprint(
                                    f"{prefix * 4}__Failed to fuck text/ppt resource {resource.resourcesDetail.resourcesName}"
                                )
                        elif resourceType == 1 and resourceDistributeType == 3:  # video
                            try:
                                self.fuckAiVideo(
                                    courseId,
                                    classId,
                                    resource.resourcesDetail.resourcesFileId,
                                    knowledge.knowledgeId,
                                    1,
                                )
                                tprint(prefix * 4)
                                tprint(
                                    f"{prefix * 4}__Fucked video {resource.resourcesDetail.resourcesName}"
                                )
                            except TimeLimitExceeded:
                                raise
                            except Exception as e:
                                self._record_failure(e)
                                logger.exception(e)
                                tprint(prefix * 4)
                                tprint(
                                    f"{prefix * 4}__Failed to fuck video {resource.resourcesDetail.resourcesName}, {e}"
                                )
                        elif (
                            resourceType == 2 and resourceDistributeType == 2
                        ):  # 新增的条件，处理智慧树课程视频
                            try:
                                # 假设我们使用与普通视频相同的方法来处理这种资源
                                self.fuckAiVideo(
                                    courseId,
                                    classId,
                                    resource.resourcesDetail.resourcesFileId,
                                    knowledge.knowledgeId,
                                    1,
                                )
                                tprint(prefix * 4)
                                tprint(
                                    f"{prefix * 4}__Fucked Zhihuishu course video {resource.resourcesDetail.resourcesName}"
                                )
                            except TimeLimitExceeded:
                                raise
                            except Exception as e:
                                self._record_failure(e)
                                logger.exception(e)
                                tprint(prefix * 4)
                                tprint(
                                    f"{prefix * 4}__Failed to fuck Zhihuishu course video {resource.resourcesDetail.resourcesName}, {e}"
                                )
                        else:
                            tprint(prefix * 4)
                            try:
                                self.aiResourseComplete(
                                    courseId,
                                    classId,
                                    knowledge.knowledgeId,
                                    resource.resourcesDetail.resourcesUid,
                                    1,
                                )
                                tprint(
                                    f"{prefix * 4}__Resource type is {resourceType}, distribute type is {resourceDistributeType}, fucked"
                                )
                            except TimeLimitExceeded:
                                raise
                            except Exception as e:
                                self._record_failure(e)
                                logger.exception(e)
                                tprint(
                                    f"{prefix * 4}__Failed to fuck resource {resource.resourcesDetail.resourcesName}, {e}"
                                )
                            continue

                    tprint(prefix * 3)  # extra line as separator
                    tprint(f"{prefix * 3}__Fucked knowledge point {knowledge.knowledgeName}")
                else:
                    if ppt_conf.get("provide_to_ai", False) and moonShot_conf.get("api_key", ""):
                        try:
                            resources = self.listKnowledgeResources(
                                courseId, classId, knowledge.knowledgeId
                            )
                            resourceList = resources.resourceList
                        except TimeLimitExceeded:
                            raise
                        except Exception as e:
                            self._record_failure(e)
                            logger.exception(e)
                            tprint(prefix * 3)
                            tprint(
                                f"{prefix * 3}__Failed to get resources for knowledge point {knowledge.knowledgeName}"
                            )
                            continue
                        for resource in resourceList:
                            resourceType = resource.resourcesDetail.resourcesType
                            resourceDistributeType = (
                                resource.resourcesDetail.resourcesDistributeType
                            )
                            if resourceType == 1 and resourceDistributeType == 4:  # ppt
                                pptName = resource.resourcesDetail.resourcesName
                                pptUrl = resource.resourcesDetail.resourcesUrl

                                ppts.append({"name": pptName, "url": pptUrl})
                    tprint(prefix * 3)
                    tprint(f"{prefix * 3}__Knowledge point already fucked")

                # 开始测试部分
                tried_count = 0
                max_attempts = aiConfig.get("max_exam_attempts", 5)
                while tried_count < max_attempts:
                    self._checkTimeLimit(courseId)
                    if no_exam:
                        break

                    tprint(prefix * 3)
                    tprint(
                        f"{prefix * 3}__Starting exam for knowledge point {knowledge.knowledgeName}"
                    )
                    exam = self.queryAiExam(courseId, classId, knowledge.knowledgeId)
                    if not exam or not exam.get("paperId"):
                        tprint(prefix * 4)
                        tprint(f"{prefix * 4}__No exam available for this knowledge point")
                        break

                    mastery_score = exam.get("masteryScore", None)
                    if not mastery_score:
                        mastery_score = 0
                    tried_count += 1
                    if mastery_score is not None and mastery_score > 90:
                        tprint(prefix * 4)
                        tprint(
                            f"{prefix * 4}__Mastery score already above 90: {exam['masteryScore']}"
                        )
                        break

                    # 将ppt转为text
                    if ppt_conf.get("provide_to_ai", False) and moonShot_conf.get("api_key", ""):
                        with PptToTxt(
                            moonShotKey=moonShot_conf.get("api_key", ""),
                            baseUrl=moonShot_conf.get("base_url", "https://api.moonshot.cn/v1"),
                            delete_immediately=moonShot_conf.get("delete_after_convert", True),
                        ) as ppt2txt:
                            for ppt in ppts:
                                try:
                                    content = ppt2txt.parseTxt(ppt["url"])
                                    ppt["content"] = content
                                    tprint(prefix * 4)
                                    tprint(f"{prefix * 4}__Converted ppt {ppt['name']} to text")
                                except TimeLimitExceeded:
                                    raise
                                except Exception as e:
                                    self._record_failure(e)
                                    logger.exception(e)
                                    tprint(prefix * 4)
                                    tprint(
                                        f"{prefix * 4}__Failed to convert ppt {ppt['name']}, {e}"
                                    )

                    exam_ctx = ExamCtx(
                        fucker=self,
                        courseId=courseId,
                        knowledgeId=knowledge.knowledgeId,
                        examTestId=exam["examTestId"],
                        examPaperId=exam["paperId"],
                        opExtra={
                            "courseName": knowledgePoints.courseName,
                            "theme": theme.themeName,
                            "knowledgePoint": knowledge.knowledgeName,
                        },
                        progress_view=self.progressbar_view,
                        aiConfig=aiConfig,
                    )

                    is_success, correct_count, total_count = exam_ctx.startFuck(
                        referenceMaterials=ppts
                    )

                    tprint(prefix * 4)
                    tprint(
                        f"{prefix * 4}__Exam attempt: Success: {is_success}, Score: {correct_count}/{total_count}"
                    )

                    if is_success:
                        break

                    # Wait before the next bounded attempt.
                    self._sleep(math.ceil(uniform(0.5, 1.5)), courseId)
                else:
                    self._record_failure(
                        OperationFailed(
                            f"知识点 {knowledge.knowledgeName} 达到 {max_attempts} 次测试上限"
                        )
                    )

                if not no_exam:
                    tprint(prefix * 3)
                    tprint(
                        f"{prefix * 3}__Finished exam attempts for knowledge point {knowledge.knowledgeName}"
                    )

            tprint(prefix * 2)  # extra line as separator
            tprint(f"{prefix * 2}__Fucked theme {theme.themeName}")

            # random sleep to avoid being detected as a bot
            self._sleep(math.ceil(uniform(0.5, 1.5)), courseId)

        tprint(prefix)  # extra line as separator
        tprint(
            f"{prefix}__{self._course_status()} Zhidao AI course {knowledgePoints.courseName} in {time.time() - begin_time:.2f}s"
        )


# Backwards compatible class name for existing integrations.
Fucker = ZHSClient
