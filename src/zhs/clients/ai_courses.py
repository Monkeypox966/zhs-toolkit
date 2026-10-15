import math
import os
import time
from random import randint, uniform

from zhs.clients.exam import ExamCtx
from zhs.clients.materials import PptToTxt
from zhs.common.crypto import AI_KEY, HOME_KEY
from zhs.common.logging import logger
from zhs.common.runtime import OperationFailed, PendingAnswerError, TimeLimitExceeded, course_run
from zhs.common.utils import progressBar


class AICourseMixin:
    def process_ai_video(self, course_id, class_id, video_id):
        from zhs.common.runtime import CourseResolutionError

        self._checkTimeLimit(course_id)
        knowledge = self.getAiKnowlegePoints(course_id, class_id)
        matches = []
        for theme in knowledge.cakeThemeList or []:
            for node in theme.knowledgeList or []:
                self._checkTimeLimit(course_id)
                resources = self.listKnowledgeResources(course_id, class_id, node.knowledgeId)
                for resource in resources.resourceList or []:
                    detail = resource.resourcesDetail
                    is_video = (
                        detail.get("resourcesType"),
                        detail.get("resourcesDistributeType"),
                    ) in ((1, 3), (2, 2))
                    if is_video and str(detail.get("resourcesFileId", "")) == str(video_id):
                        matches.append(node.knowledgeId)
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1:
            raise CourseResolutionError(f"AI 视频 {video_id} 不存在或对应多个知识点")
        return self.fuckAiVideo(course_id, class_id, video_id, matches[0])

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

                try:
                    self._complete_ai_exams(
                        courseId,
                        classId,
                        knowledge,
                        aiConfig,
                        ppts,
                        knowledgePoints.courseName,
                        theme.themeName,
                        prefix,
                        tprint,
                        no_exam,
                    )
                except TimeLimitExceeded:
                    raise
                except PendingAnswerError as error:
                    self._course_pending.append(error)
                    logger.warning(f"知识点 {knowledge.knowledgeId} 的测试等待恢复：{error}")
                except Exception as error:
                    self._record_failure(error)
                    logger.exception(error)

            tprint(prefix * 2)  # extra line as separator
            tprint(f"{prefix * 2}__Fucked theme {theme.themeName}")

            # random sleep to avoid being detected as a bot
            self._sleep(math.ceil(uniform(0.5, 1.5)), courseId)

        tprint(prefix)  # extra line as separator
        tprint(
            f"{prefix}__{self._course_status()} Zhidao AI course {knowledgePoints.courseName} in {time.time() - begin_time:.2f}s"
        )

    def _complete_ai_exams(
        self,
        courseId,
        classId,
        knowledge,
        aiConfig,
        ppts,
        course_name,
        theme_name,
        prefix,
        tprint,
        no_exam=False,
    ):
        key = (str(courseId), str(classId), str(knowledge.knowledgeId))
        completed = getattr(self, "_completed_ai_knowledge", set())
        if key in completed:
            return
        ppt_conf = aiConfig.get("ppt_processing", {})
        moonShot_conf = ppt_conf.get("moonShot", {})
        # 开始测试部分
        tried_count = 0
        max_attempts = aiConfig.get("max_exam_attempts", 5)
        while tried_count < max_attempts:
            self._checkTimeLimit(courseId)
            if no_exam:
                break

            tprint(prefix * 3)
            tprint(f"{prefix * 3}__Starting exam for knowledge point {knowledge.knowledgeName}")
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
                tprint(f"{prefix * 4}__Mastery score already above 90: {exam['masteryScore']}")
                break

            # 将ppt转为text
            if ppt_conf.get("provide_to_ai", False) and moonShot_conf.get("api_key", ""):
                with PptToTxt(
                    moonShotKey=moonShot_conf.get("api_key", ""),
                    baseUrl=moonShot_conf.get("base_url", "https://api.moonshot.cn/v1"),
                    delete_immediately=moonShot_conf.get("delete_after_convert", True),
                    local_max_cache_files=self.cache_policy.get("max_files", 500),
                    local_max_cache_size_mb=self.cache_policy.get("max_size_mb", 1024),
                    local_max_age_days=self.cache_policy.get("max_age_days", 30),
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
                            tprint(f"{prefix * 4}__Failed to convert ppt {ppt['name']}, {e}")

            exam_ctx = ExamCtx(
                fucker=self,
                courseId=courseId,
                knowledgeId=knowledge.knowledgeId,
                examTestId=exam["examTestId"],
                examPaperId=exam["paperId"],
                opExtra={
                    "courseName": course_name,
                    "theme": theme_name,
                    "knowledgePoint": knowledge.knowledgeName,
                },
                progress_view=self.progressbar_view,
                aiConfig=aiConfig,
            )

            is_success, correct_count, total_count = exam_ctx.startFuck(referenceMaterials=ppts)

            tprint(prefix * 4)
            tprint(
                f"{prefix * 4}__Exam attempt: Success: {is_success}, Score: {correct_count}/{total_count}"
            )

            if is_success:
                completed.add(key)
                self._completed_ai_knowledge = completed
                break

            # Wait before the next bounded attempt.
            self._sleep(math.ceil(uniform(0.5, 1.5)), courseId)
        else:
            self._record_failure(
                OperationFailed(f"知识点 {knowledge.knowledgeName} 达到 {max_attempts} 次测试上限")
            )

        if not no_exam:
            tprint(prefix * 3)
            tprint(
                f"{prefix * 3}__Finished exam attempts for knowledge point {knowledge.knowledgeName}"
            )
