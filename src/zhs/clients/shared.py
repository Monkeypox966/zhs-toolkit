import json
import math
import os
import time
from base64 import b64encode
from random import randint, random

from zhs.common.crypto import HOME_KEY, VIDEO_KEY, Cipher, WatchPoint, getEv
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import CaptchaException, OperationFailed, TimeLimitExceeded, course_run
from zhs.common.utils import HMS, progressBar, wipeLine


class SharedCourseMixin:
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
        while played_time < end_time or questions:
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
            # Keep a point pending until every question has been processed.
            if questions and played_time >= questions[-1].timeSec:
                self._process_popup_point(RAC_id, video_id, questions[-1])
                questions.pop()
                report = True
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

    def _process_popup_point(self, course_id, video_id, point, max_attempts=3):
        last_error = None
        for attempt in range(max_attempts):
            self._checkTimeLimit(course_id)
            try:
                response = self.lessonPopoupExam(course_id, video_id, point.questionIds)
                entries = response.lessonTestQuestionUseInterfaceDtos
                if not isinstance(entries, list) or not entries:
                    raise OperationFailed("Popup returned no questions")
                for entry in entries:
                    question = entry.testQuestion
                    if not question or not question.questionId:
                        raise OperationFailed("Popup question is incomplete")
                    answers = self.answerZhidao(question)
                    if not answers:
                        raise OperationFailed("Popup has no verified answer")
                    self._checkTimeLimit(course_id)
                    self.saveLessonPopupExamSaveAnswer(
                        course_id, video_id, question.questionId, answers
                    )
                return
            except TimeLimitExceeded, CaptchaException:
                raise
            except Exception as error:
                last_error = error
                if attempt + 1 < max_attempts:
                    self._sleep(min(2**attempt, 4), course_id)
        raise OperationFailed(
            f"Popup failed after {max_attempts} attempts: {last_error}"
        ) from last_error

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
