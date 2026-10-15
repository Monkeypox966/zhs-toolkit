from __future__ import annotations

import copy
import json
import os
import threading
import time
from random import randint, sample
from typing import TYPE_CHECKING

from zhs.clients.ai import Openai
from zhs.common.crypto import EXAM_KEY
from zhs.common.logging import logger
from zhs.common.runtime import OperationFailed, TimeLimitExceeded
from zhs.common.storage import atomic_write_json
from zhs.common.transport import create_session
from zhs.common.utils import getRealPath, progressBar

if TYPE_CHECKING:
    from zhs.clients.course import Fucker


class ExamCtx:
    def __init__(
        self,
        fucker: Fucker,
        courseId: int,
        knowledgeId: int,
        examTestId: int,
        examPaperId: int,
        progress_view: bool = True,
        aiConfig: dict = None,
        opExtra: dict = None,
    ):
        aiConfig = aiConfig or {}
        opExtra = opExtra or {}
        self.fucker = fucker
        self.courseId = courseId
        self.examTestId = examTestId
        self.examPaperId = examPaperId
        self.knowledgeId = knowledgeId
        self.opExtra = opExtra
        self.aiConfig = aiConfig
        self.progress_view = progress_view

        self.answerCache = {}
        self.allAnswerCache = {}  # 存储同课程所有题目的答案
        self.sheetContent = None
        self.timeUpdateIndex = 0
        self.examStopped = False
        self._stop_event = threading.Event()
        self._heartbeat_thread = None
        self._heartbeat_client = copy.copy(fucker)
        self._heartbeat_client.session = create_session(retries=0)
        self._heartbeat_client.session.cookies.update(fucker.session.cookies)

        if aiConfig.get("enabled", False) and not aiConfig.get("use_zhidao_ai", False):
            opConf: dict = aiConfig.get("openai", {})
            self.op = Openai(
                baseUrl=opConf.get("api_base", "https://api.openai.com/v1"),
                apiKey=opConf.get("api_key", ""),
                modelName=opConf.get("model_name", "davinci"),
                stream=aiConfig.get("use_stream", False),
                extra=opExtra,
                proxies=fucker.proxies,
            )

        if aiConfig.get("enabled", False) and aiConfig.get("use_zhidao_ai", False):
            session = create_session()
            session.cookies.update(fucker.session.cookies)
            session.headers.update(fucker.session.headers)
            session.proxies.update(fucker.proxies)
            self.op = Openai(
                useZhidao=True,
                zhiDaosession=session,
                stream=aiConfig.get("use_stream", False),
                extra=opExtra,
            )

    def getAnswerpath(self, examTestId):
        answerpath = getRealPath(f"aiexamAnswer/{self.courseId}/{examTestId}.json")

        # 确保目录存在
        os.makedirs(os.path.dirname(answerpath), exist_ok=True)

        # 如果文件不存在，创建一个包含空字典的 JSON 文件
        if not os.path.exists(answerpath):
            atomic_write_json(answerpath, {})

        return answerpath

    def getAllAnswerpath(self):  # 同课程所有题目的答案
        answerpath = getRealPath(f"aiexamAnswer/{self.courseId}/data.json")

        # 确保目录存在
        os.makedirs(os.path.dirname(answerpath), exist_ok=True)

        # 如果文件不存在，创建一个包含空字典的 JSON 文件
        if not os.path.exists(answerpath):
            atomic_write_json(answerpath, {})

        return answerpath

    def _load_cache(self, path):
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                raise ValueError("answer cache must be an object")
        except (OSError, ValueError) as error:
            logger.warning(f"Ignoring unreadable answer cache {path}: {error}")
            return {}
        normalized = {}
        for key, value in data.items():
            if not isinstance(value, dict) or not isinstance(value.get("answer", ""), str):
                logger.warning(f"Ignoring invalid answer cache entry: {key}")
                continue
            question_id, separator, version_text = key.partition("_")
            try:
                version = int(version_text if separator else value.get("version", 1))
            except ValueError, TypeError:
                continue
            normalized_key = question_id if version == 1 else f"{question_id}_{version}"
            normalized[normalized_key] = dict(value, version=version)
        return normalized

    def readAnswerCache(self, examTestId):
        all_path = self.getAllAnswerpath()
        self.allAnswerCache = self._load_cache(all_path)
        directory = os.path.dirname(all_path)
        files = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".json") and name != "data.json"
        ]
        for path in sorted(files, key=os.path.getmtime):
            self.allAnswerCache.update(self._load_cache(path))
        self.answerCache = self._load_cache(self.getAnswerpath(examTestId))
        return self.answerCache, self.allAnswerCache

    def writeAnswerCacheToDisk(self):
        answerpath = self.getAnswerpath(self.examTestId)
        atomic_write_json(answerpath, self.answerCache)

        allAnswerpath = self.getAllAnswerpath()
        atomic_write_json(allAnswerpath, self.allAnswerCache)

    def getAnswer(self, questionId: int, version: int = 1) -> dict | None:
        key = str(questionId) if version == 1 else f"{questionId}_{version}"
        result = self.allAnswerCache.get(key) or self.allAnswerCache.get(f"{questionId}_{version}")
        return result

    def setAnswer(self, questionId: int, version: int, answer_data: dict):
        self.answerCache[str(questionId) if version == 1 else f"{questionId}_{version}"] = {
            "version": version,
            "question": answer_data.get("question", ""),
            "answer": answer_data.get("answer", ""),
            "answer_content": answer_data.get("answer_content", ""),
            "questionDict": answer_data.get("questionDict", {}),
        }
        self.allAnswerCache[str(questionId) if version == 1 else f"{questionId}_{version}"] = {
            "version": version,
            "question": answer_data.get("question", ""),
            "answer": answer_data.get("answer", ""),
            "answer_content": answer_data.get("answer_content", ""),
            "questionDict": answer_data.get("questionDict", {}),
        }

        self.writeAnswerCacheToDisk()

    def openExam(self, triedTimes: int = 0):
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/openExam"

        data = {
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "courseId": self.courseId,
        }
        try:
            self.fucker.zhidaoAiExamQuery(url, data, ok_code=0, key=EXAM_KEY, method="POST")

        except Exception as e:
            if triedTimes < 3:
                logger.error(f"openExam failed, retrying... {triedTimes} times")
                return self.openExam(triedTimes + 1)
            else:
                logger.error("openExam failed, retried 3 times, giving up...")
                raise e
        # 启动定时更新时间的线程
        self.stop_heartbeat()
        self.examStopped = False
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self.updateExamCostTime,
            args=(10,),
            name=f"exam-heartbeat-{self.examTestId}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return True

    def stop_heartbeat(self):
        self.examStopped = True
        self._stop_event.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=45)
        self._heartbeat_thread = None

    def close(self):
        self.stop_heartbeat()
        self._heartbeat_client.session.close()
        op = getattr(self, "op", None)
        if op is not None:
            op.close()

    def updateExamCostTime(self, heartbeatTime: int = 10):
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/updateUserUsedTime"

        data = {
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "heartbeatTime": heartbeatTime,
        }

        while not self._stop_event.is_set() and not self.examStopped:
            try:
                self.fucker._checkTimeLimit(self.courseId)
                self._heartbeat_client.zhidaoAiExamQuery(
                    url, data, ok_code=0, key=EXAM_KEY, method="POST"
                )
            except TimeLimitExceeded:
                self._stop_event.set()
                return
            except Exception as e:
                logger.error(f"updateExamCostTime failed: {e}")
            finally:
                self.timeUpdateIndex += 1
                if self.timeUpdateIndex % 10 == 0:
                    logger.info(
                        f"Exam {self.examTestId} cost time: {self.timeUpdateIndex * heartbeatTime}s"
                    )

                self._stop_event.wait(heartbeatTime)

    def getSheetContent(self, triedTimes: int = 0) -> list:
        if self.sheetContent is not None:
            return self.sheetContent
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/getExamSheetInfo"

        data = {"examTestId": self.examTestId, "examPaperId": self.examPaperId}

        try:
            ret = self.fucker.zhidaoAiExamQuery(url, data, ok_code=0, key=EXAM_KEY, method="GET")
            self.sheetContent = ret["data"]["partSheetVos"][0]["questionSheetVos"]
        except Exception as e:
            logger.error(f"getSheetContent failed: {e}")

            if triedTimes < 3:
                logger.error(f"getSheetContent failed, retrying... {triedTimes} times")
                return self.getSheetContent(triedTimes + 1)
            else:
                logger.error("getSheetContent failed, retried 3 times, giving up...")
                raise e

        return self.sheetContent

    def getQuestionContent(self, questionId: int, version: int, triedTimes: int = 0) -> dict:
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/question/getExamQuestionInfo"
        data = {
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "questionId": questionId,
            "version": version,
        }

        try:
            ret = self.fucker.zhidaoAiExamQuery(url, data, ok_code=0, key=EXAM_KEY, method="GET")
            return ret["data"]
        except Exception:
            if triedTimes < 3:
                logger.error(f"getQuestionContent failed, retrying... {triedTimes} times")
                return self.getQuestionContent(questionId, version, triedTimes + 1)
            else:
                logger.error("getQuestionContent failed, retried 3 times, giving up...")
                return None

    def saveAnswer(self, questionId: int, answers: list):
        if len(answers) == 0:
            return False
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/answer/saveAnswer"

        data = {
            "recruitId": self.courseId,
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "questionId": questionId,
            "dataVos": None,
            "answer": "#@#".join(str(answer) for answer in answers),
        }

        try:
            self.fucker.zhidaoAiExamQuery(url, data, ok_code=0, key=EXAM_KEY, method="POST")
        except Exception as e:
            logger.error(f"saveAnswer failed: {e}")
            return False

        return True

    def submitExam(self, triedTimes: int = 0):
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/submit"

        data = {
            "examTestId": self.examTestId,
            "courseId": self.courseId,
            "courseType": 8,
            "examPaperId": self.examPaperId,
            "aiKnlowledgeId": self.knowledgeId,
        }

        try:
            self.fucker.zhidaoAiExamQuery(url, data, ok_code=0, key=EXAM_KEY, method="POST")
        except Exception as e:
            logger.error(f"submitExam failed: {e}")
            if triedTimes < 3:
                logger.error(f"submitExam failed, retrying... {triedTimes} times")
                return self.submitExam(triedTimes + 1)
            else:
                logger.error("submitExam failed, retried 3 times, giving up...")
                raise e

        finally:
            self.stop_heartbeat()

        return True

    def getQuestionAnswer(self, questionDict: dict) -> tuple[list, str] | None:
        questionId = questionDict["id"]
        questionType = questionDict["questionType"]
        version = questionDict.get("version", 1)

        # 尝试从缓存中获取答案
        answer = self.getAnswer(questionId, version)
        if answer is not None:
            answer = answer.get("answer", "").split("#@#")

            # 随机睡眠3-5秒，模拟网络延迟
            time.sleep(randint(3, 5))
            return answer, "cached"

        # 答案不存在，获取题目内容，使用AI生成答案
        questionContent = questionDict["content"]

        # 选项，只保留有id和content的选项
        choices = [
            {"id": option["id"], "content": option["content"]}
            for option in questionDict["optionVos"]
            if "id" in option and "content" in option
        ]

        # 选项数量少于2个，答案就是选项内容
        if not choices:
            raise ValueError("Question has no usable options")
        if len(choices) == 1:
            answer = choices[0]["id"]
            return [answer], "single option"

        try:
            # 选项数量大于2个，使用AI生成答案
            op = self.op

            if questionType == 1:
                # 单选题
                prompt = op.singleChoiceTemplate(
                    questionContent, choices, referenceMaterials=self.referenceMaterials
                )
            elif questionType == 2:
                # 多选题
                prompt = op.multipleChoiceTemplate(
                    questionContent, choices, referenceMaterials=self.referenceMaterials
                )
            elif questionType == 14:
                # 判断题
                prompt = op.judgementTemplate(
                    questionContent, choices, referenceMaterials=self.referenceMaterials
                )
            else:
                raise ValueError(f"Unsupported question type: {questionType}")
            answer = op.generateAnswer(prompt)
            return answer, "AI generated"
        except Exception as e:
            logger.warning(f"AI answer generation failed: {e}")
            # 随机生成答案
            if questionType in (1, 14):
                answer = self.select_random_answers(choices, 1)
            elif questionType == 2:
                answer = self.select_random_answers(choices, 2)
            else:
                raise ValueError(f"Unsupported question type: {questionType}") from e

            time.sleep(randint(3, 5))

        return answer, "random"

    def select_random_answers(self, choices, n):
        # 确保 n 不大于 choices 的长度
        n = min(n, len(choices))

        # 随机选择 n 个选项
        selected_choices = sample(choices, n)

        # 返回选中选项的 id
        return [choice["id"] for choice in selected_choices]

    def startFuck(self, referenceMaterials=None) -> tuple[bool, int, int]:
        try:
            return self._run_exam(referenceMaterials or [])
        finally:
            self.close()

    def _run_exam(self, referenceMaterials) -> tuple[bool, int, int]:
        self.referenceMaterials = referenceMaterials

        # 加载答案缓存
        self.readAnswerCache(self.examTestId)

        # 打开考试
        self.openExam()

        # 获取考试试卷内容
        sheetContent = self.getSheetContent()
        if not sheetContent:
            raise ValueError("sheet has no content")

        # 遍历试卷内容，获取每道题目的答案
        index = 0
        total_questions = len(sheetContent)
        if self.progress_view:
            progressBar(index, total_questions, "fucking exam", suffix=f"{index}/{total_questions}")
        for questionDict in sheetContent:
            self.fucker._checkTimeLimit(self.courseId)
            # 获取题目内容
            questionContentDict = self.getQuestionContent(
                questionDict["questionId"], questionDict.get("version", 1)
            )

            if questionContentDict is None:
                logger.error(f"getQuestionContent failed: {questionDict['questionId']}")
                raise OperationFailed("试题读取或答案生成失败，未提交试卷")

            # 获取题目答案
            questionContentDict.version = questionDict.get("version", 1)
            answer, note = self.getQuestionAnswer(questionContentDict)

            if answer is None:
                logger.error(f"getQuestionAnswer failed: {questionDict['questionId']}")
                raise OperationFailed("试题读取或答案生成失败，未提交试卷")

            # 保存答案
            if not self.saveAnswer(questionDict["questionId"], answer):
                raise OperationFailed(f"保存题目 {questionDict['questionId']} 失败")

            if self.progress_view:
                action = "fucking exam"
                index += 1
                progressBar(
                    index, total_questions, action, suffix=f"({index}/{total_questions}) {note}"
                )

        # 提交考试
        self.submitExam()

        # 重新取得试卷内容，检查是否答对了，并更新答案缓存
        total_questions = len(sheetContent)
        correct_questions = 0
        for questionDict in sheetContent:
            self.fucker._checkTimeLimit(self.courseId)
            # 取得版本
            version = questionDict.get("version", 1)
            # 获取题目内容
            questionContentDict = self.getQuestionContent(questionDict["questionId"], version)

            if questionContentDict is None:
                logger.error(f"getQuestionContent failed: {questionDict['questionId']}")
                continue

            # 取得做题结果
            result = questionContentDict.get("userAnswerVo", None)
            if result is None:
                logger.error(f"userAnswerVo not found: {questionDict['questionId']}")
                continue
            # 计算得分
            if result[0]["isCorrect"] == 1:
                correct_questions += 1
            else:
                logger.error(f"Question {questionDict['questionId']} is not correct")

            # 获取题目答案
            answer = [
                {"id": option["id"], "content": option["content"]}
                for option in questionContentDict["optionVos"]
                if option.get("isCorrect", 0) == 1
            ]
            answer_str = "#@#".join([str(option["id"]) for option in answer])
            answer_content_str = "\n".join([option["content"] for option in answer])

            # 保存答案
            answer_dict = {
                "question": questionContentDict.get("content", ""),
                "answer": answer_str,
                "answer_content": answer_content_str,
                "questionDict": questionContentDict,
            }
            self.setAnswer(questionDict["questionId"], version, answer_dict)

        # 如果所有题目都做对了，则退出考试
        if correct_questions == total_questions:
            logger.info(
                f"Exam {self.examTestId} is finished, score: {correct_questions}/{total_questions}"
            )
            return True, correct_questions, total_questions
        else:
            logger.info(
                f"Exam {self.examTestId} is not finished, score: {correct_questions}/{total_questions}"
            )
            return False, correct_questions, total_questions
