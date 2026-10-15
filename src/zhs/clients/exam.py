from __future__ import annotations

import copy
import json
import os
import threading
from random import sample
from typing import TYPE_CHECKING
from uuid import uuid4

import requests

from zhs.clients.ai import Openai
from zhs.common.checkpoints import submission_checkpoint
from zhs.common.contracts import ProtocolError, identifier, sheet_questions, validate_answers
from zhs.common.control import atomic_operation, check_stop, defer_stop
from zhs.common.crypto import EXAM_KEY
from zhs.common.locking import FileLock
from zhs.common.logging import logger
from zhs.common.runtime import (
    AuthenticationError,
    OperationFailed,
    SubmissionUncertainError,
    TimeLimitExceeded,
)
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
        self._submission_id = uuid4().hex
        self._submission_state = None

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
        answerpath = getRealPath(
            f"aiexamAnswer/{identifier(self.courseId)}/{identifier(examTestId)}.json"
        )

        # 确保目录存在
        os.makedirs(os.path.dirname(answerpath), exist_ok=True)

        # 如果文件不存在，创建一个包含空字典的 JSON 文件
        return answerpath

    def getAllAnswerpath(self):  # 同课程所有题目的答案
        answerpath = getRealPath(f"aiexamAnswer/{identifier(self.courseId)}/data.json")

        # 确保目录存在
        os.makedirs(os.path.dirname(answerpath), exist_ok=True)

        # 如果文件不存在，创建一个包含空字典的 JSON 文件
        return answerpath

    def _load_cache(self, path):
        if not os.path.isfile(path):
            return {}
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
        with FileLock(os.path.join(os.path.dirname(all_path), ".answers.lock"), timeout=30):
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
        all_path = self.getAllAnswerpath()
        with FileLock(os.path.join(os.path.dirname(all_path), ".answers.lock"), timeout=30):
            pending = getattr(self, "_dirty_answers", self.answerCache)
            per_exam = self._load_cache(answerpath)
            all_answers = self._load_cache(all_path)
            per_exam.update(pending)
            all_answers.update(pending)
            atomic_write_json(answerpath, per_exam)
            atomic_write_json(all_path, all_answers)
            self.answerCache = per_exam
            self.allAnswerCache = all_answers
            self._dirty_answers = {}

    def getAnswer(self, questionId: int, version: int = 1) -> dict | None:
        key = str(questionId) if version == 1 else f"{questionId}_{version}"
        result = self.allAnswerCache.get(key) or self.allAnswerCache.get(f"{questionId}_{version}")
        return result

    def setAnswer(self, questionId: int, version: int, answer_data: dict):
        key = identifier(questionId) if version == 1 else f"{identifier(questionId)}_{version}"
        if not answer_data.get("answer"):
            raise ProtocolError("Refusing to cache an empty answer")
        entry = {
            "version": version,
            "question": answer_data.get("question", ""),
            "answer": answer_data["answer"],
            "answer_content": answer_data.get("answer_content", ""),
            "questionDict": answer_data.get("questionDict", {}),
        }
        if not hasattr(self, "_dirty_answers"):
            self._dirty_answers = {}
        self._dirty_answers[key] = entry
        self.answerCache[key] = entry
        self.allAnswerCache[key] = entry
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
            self.sheetContent = sheet_questions(ret)
        except ProtocolError:
            raise
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

    @atomic_operation
    def submitExam(self, triedTimes: int = 0):
        if self._submission_state is not None:
            raise SubmissionUncertainError("本次试卷已尝试提交，请先核对平台状态")
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/submit"
        data = {
            "examTestId": self.examTestId,
            "courseId": self.courseId,
            "courseType": 8,
            "examPaperId": self.examPaperId,
            "aiKnlowledgeId": self.knowledgeId,
        }
        self.fucker._checkTimeLimit(self.courseId)
        try:
            self._checkpoint_submission("submitting")
            self.fucker.zhidaoAiExamQuery(url, data, ok_code=0, key=EXAM_KEY, method="POST")
            self._checkpoint_submission("accepted")
        except (
            requests.exceptions.RequestException,
            ValueError,
            KeyError,
            AttributeError,
        ) as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if isinstance(error, requests.HTTPError) and status in (401, 403):
                self._checkpoint_submission("rejected", http_status=status)
                raise AuthenticationError(
                    f"试卷 {self.examTestId} 的提交被认证接口拒绝（HTTP {status}），需要重新登录后重试",
                    status=status,
                    retryable=True,
                ) from error
            raise SubmissionUncertainError(
                f"试卷 {self.examTestId} 的提交结果无法确认，请核对平台状态；不会自动重复提交：{error}"
            ) from error
        finally:
            self.stop_heartbeat()
        return True

    def _checkpoint_submission(self, state, **result):
        self._submission_state = state
        submission_checkpoint(
            self._submission_id,
            state,
            course_id=str(self.courseId),
            knowledge_id=str(self.knowledgeId),
            exam_test_id=str(self.examTestId),
            exam_paper_id=str(self.examPaperId),
            **result,
        )

    @classmethod
    def recover_submission(cls, client, checkpoint):
        """Read an existing sheet and its grades; never open or submit an exam."""
        instance = cls(
            client,
            checkpoint["course_id"],
            checkpoint["knowledge_id"],
            checkpoint["exam_test_id"],
            checkpoint["exam_paper_id"],
            progress_view=False,
            aiConfig={"enabled": False},
        )
        instance._submission_id = checkpoint["id"]
        instance._submission_state = checkpoint["state"]
        try:
            instance.readAnswerCache(instance.examTestId)
            return instance._read_exam_result(instance.getSheetContent())
        finally:
            instance.close()

    def getQuestionAnswer(self, questionDict: dict) -> tuple[list, str]:
        question_id = questionDict["id"]
        version = questionDict.get("version", 1)
        # Validate the complete option set even when consulting an old cache.
        options = questionDict.get("optionVos") or []
        if not options:
            raise ProtocolError("Question has no usable options")
        probe = [options[0].get("id")] if isinstance(options[0], dict) else []
        validate_answers(questionDict, probe)
        cached = self.getAnswer(question_id, version)
        if cached is not None:
            saved_question = cached.get("questionDict") or {}
            if not isinstance(saved_question, dict):
                saved_question = {}
            stale_content = cached.get("question") and cached["question"] != questionDict.get(
                "content"
            )

            def option_signature(items):
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    return None
                return sorted((str(item.get("id")), str(item.get("content"))) for item in items)

            stale_options = saved_question.get("optionVos") and option_signature(
                saved_question["optionVos"]
            ) != option_signature(questionDict.get("optionVos"))
            if not stale_content and not stale_options:
                try:
                    return validate_answers(
                        questionDict, cached.get("answer", "").split("#@#")
                    ), "cached"
                except ProtocolError:
                    logger.warning(f"Ignoring invalid cached answer for question {question_id}")
        if len(options) == 1:
            return validate_answers(questionDict, [options[0]["id"]]), "single option"
        question_type = questionDict["questionType"]
        template_name = {
            1: "singleChoiceTemplate",
            2: "multipleChoiceTemplate",
            14: "judgementTemplate",
        }[question_type]
        try:
            op = getattr(self, "op", None)
            if op is None:
                raise RuntimeError("AI is disabled and no valid cached answer is available")
            prompt = getattr(op, template_name)(
                questionDict.get("content", ""),
                [{"id": option["id"], "content": option["content"]} for option in options],
                referenceMaterials=getattr(self, "referenceMaterials", []),
            )
            answers = validate_answers(questionDict, op.generateAnswer(prompt))
            return answers, "AI generated"
        except TimeLimitExceeded, SubmissionUncertainError:
            raise
        except Exception as error:
            count = 2 if question_type == 2 else 1
            selected = sample(options, min(count, len(options)))
            answers = validate_answers(questionDict, [option["id"] for option in selected])
            logger.warning(
                f"题目 {question_id} 的 AI 答案生成或校验失败，改为随机选择 {len(answers)} 项：{error}"
            )
            # Only the platform's returned answer key is written to the cache.
            return answers, "random"

    def startFuck(self, referenceMaterials=None) -> tuple[bool, int, int]:
        try:
            return self._run_exam(referenceMaterials or [])
        except Exception as error:
            if self._submission_state in {"submitting", "accepted"} and not isinstance(
                error, SubmissionUncertainError
            ):
                raise SubmissionUncertainError(
                    f"试卷 {self.examTestId} 已尝试提交，后续处理失败，请核对平台状态：{error}"
                ) from error
            raise
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
            questionContentDict["version"] = questionDict.get("version", 1)
            answer, note = self.getQuestionAnswer(questionContentDict)

            if answer is None:
                logger.error(f"getQuestionAnswer failed: {questionDict['questionId']}")
                raise OperationFailed("试题读取或答案生成失败，未提交试卷")

            # 保存答案
            self.fucker._checkTimeLimit(self.courseId)
            if not self.saveAnswer(questionDict["questionId"], answer):
                raise OperationFailed(f"保存题目 {questionDict['questionId']} 失败")

            if self.progress_view:
                action = "fucking exam"
                index += 1
                progressBar(
                    index, total_questions, action, suffix=f"({index}/{total_questions}) {note}"
                )

        # 提交考试
        self.fucker._checkTimeLimit(self.courseId)
        check_stop()
        with defer_stop():
            self.submitExam()
            return self._read_exam_result(sheetContent)

    def _read_exam_result(self, sheetContent):
        # 重新取得试卷内容，检查是否答对了，并更新答案缓存
        total_questions = len(sheetContent)
        correct_questions = 0
        graded_questions = 0
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
            if not isinstance(result, list) or not result or not isinstance(result[0], dict):
                logger.error(f"userAnswerVo not found: {questionDict['questionId']}")
                continue
            # 计算得分
            correct = result[0].get("isCorrect")
            if isinstance(correct, bool) or correct not in (0, 1):
                continue
            graded_questions += 1
            if correct == 1:
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
            if answer:
                try:
                    validate_answers(questionContentDict, [option["id"] for option in answer])
                except ProtocolError:
                    logger.warning("Returned answer key is incomplete or inconsistent; not cached")
                else:
                    self.setAnswer(questionDict["questionId"], version, answer_dict)

        if graded_questions != total_questions:
            raise SubmissionUncertainError(
                f"试卷 {self.examTestId} 已提交，但仅取得 {graded_questions}/{total_questions} 道题的评分；"
                "请核对平台状态，不会自动重复提交"
            )
        self._checkpoint_submission(
            "reviewed", correct_questions=correct_questions, total_questions=total_questions
        )

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
