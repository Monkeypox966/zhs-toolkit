import json
import math
import os
import time
from datetime import datetime
from random import random

from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import OperationFailed, TimeLimitExceeded, course_run
from zhs.common.signing import sign
from zhs.common.utils import progressBar, wipeLine


class CampusCourseMixin:
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

            if file.dataType == 3 and file.studyTime >= file.totalTime * self.end_thre:
                logger.debug(f"Skipped file {file.id}")
                return

            try:
                if file.dataType == 3:
                    self.fuckHikeVideo(course_id, file.id, file.studyTime)
                elif file.dataType is None:
                    raise OperationFailed(f"资源 {file.id} 缺少类型，尚无法确认完成")
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
