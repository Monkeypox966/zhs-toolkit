import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from zhs.clients.ai import Openai
from zhs.clients.course import Fucker
from zhs.clients.exam import ExamCtx
from zhs.common.control import StopRequested, check_stop
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import OperationFailed, SubmissionUncertainError
from zhs.common.storage import atomic_write_json


def setUpModule():
    logger.setLevel("CRITICAL")


class ExamAiTests(unittest.TestCase):
    def setUp(self):
        self.client = Fucker(proxies={})
        self.addCleanup(self.client.close)

    def exam(self):
        instance = ExamCtx(
            self.client, 1, 2, 3, 4, progress_view=False, aiConfig={"enabled": False}
        )
        self.addCleanup(instance.close)
        return instance

    def test_heartbeat_stops_after_sheet_failure(self):
        instance = self.exam()
        instance.readAnswerCache = Mock()
        instance.fucker.zhidaoAiExamQuery = Mock(return_value={})
        instance._heartbeat_client.zhidaoAiExamQuery = Mock(return_value={})
        instance.getSheetContent = Mock(side_effect=RuntimeError("sheet failed"))
        with self.assertRaises(RuntimeError):
            instance.startFuck([])
        self.assertTrue(instance.examStopped)
        self.assertFalse(any(t.name == "exam-heartbeat-3" for t in threading.enumerate()))

    def test_heartbeat_stops_on_keyboard_interrupt(self):
        instance = self.exam()
        instance.readAnswerCache = Mock()
        instance.fucker.zhidaoAiExamQuery = Mock(return_value={})
        instance._heartbeat_client.zhidaoAiExamQuery = Mock(return_value={})
        instance.getSheetContent = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            instance.startFuck([])
        self.assertTrue(instance.examStopped)
        self.assertFalse(any(t.name == "exam-heartbeat-3" for t in threading.enumerate()))

    def test_successful_exam_stops_heartbeat_and_reports_score(self):
        instance = self.exam()
        instance.readAnswerCache = Mock()
        instance.fucker.zhidaoAiExamQuery = Mock(return_value={})
        instance._heartbeat_client.zhidaoAiExamQuery = Mock(return_value={})
        instance.getSheetContent = Mock(return_value=[{"questionId": 1}])
        instance.getQuestionContent = Mock(
            side_effect=[
                ObjDict(
                    {
                        "id": 1,
                        "questionType": 1,
                        "content": "q",
                        "optionVos": [{"id": 9, "content": "x"}],
                    }
                ),
                ObjDict(
                    {
                        "userAnswerVo": [{"isCorrect": 1}],
                        "optionVos": [{"id": 9, "content": "x", "isCorrect": 1}],
                    }
                ),
            ]
        )
        instance.setAnswer = Mock()
        self.assertEqual(instance.startFuck([]), (True, 1, 1))
        self.assertTrue(instance.examStopped)
        self.assertFalse(any(t.name == "exam-heartbeat-3" for t in threading.enumerate()))

    def test_failed_answer_save_prevents_submission(self):
        instance = self.exam()
        instance.readAnswerCache = Mock()
        instance.openExam = Mock()
        instance.getSheetContent = Mock(return_value=[{"questionId": 1}])
        instance.getQuestionContent = Mock(
            return_value=ObjDict(
                {
                    "id": 1,
                    "questionType": 1,
                    "content": "q",
                    "optionVos": [{"id": 9, "content": "x"}],
                }
            )
        )
        instance.saveAnswer = Mock(return_value=False)
        instance.submitExam = Mock()
        with self.assertRaises(OperationFailed):
            instance.startFuck([])
        instance.submitExam.assert_not_called()

    def test_submit_timeout_is_not_retried(self):
        import requests

        instance = self.exam()
        instance.fucker.zhidaoAiExamQuery = Mock(side_effect=requests.Timeout("uncertain"))
        with self.assertRaises(SubmissionUncertainError):
            instance.submitExam()
        self.assertEqual(instance.fucker.zhidaoAiExamQuery.call_count, 1)
        self.assertTrue(instance.examStopped)

    def test_stop_before_submission_leaves_no_submission_checkpoint(self):
        instance = self.exam()
        instance.fucker.zhidaoAiExamQuery = Mock()
        event = threading.Event()
        event.set()
        with patch("zhs.common.control._active", event), self.assertRaises(StopRequested):
            instance.submitExam()
        self.assertIsNone(instance._submission_state)
        instance.fucker.zhidaoAiExamQuery.assert_not_called()

    def test_stop_during_submission_finishes_grading_and_cache(self):
        instance = self.exam()
        event = threading.Event()
        instance.readAnswerCache = Mock()
        instance.openExam = Mock()
        instance.fucker.zhidaoAiExamQuery = Mock(side_effect=lambda *a, **k: event.set())
        instance.getSheetContent = Mock(return_value=[{"questionId": 1}])
        question = {
            "id": 1,
            "questionType": 1,
            "content": "q",
            "optionVos": [{"id": 9, "content": "x", "isCorrect": 1}],
        }
        instance.getQuestionContent = Mock(
            side_effect=[
                ObjDict(question),
                ObjDict(dict(question, userAnswerVo=[{"isCorrect": 1}])),
            ]
        )
        instance.saveAnswer = Mock(return_value=True)
        instance.setAnswer = Mock()
        with patch("zhs.common.control._active", event):
            self.assertEqual(instance.startFuck([]), (True, 1, 1))
            self.assertEqual(instance._submission_state, "reviewed")
            instance.setAnswer.assert_called_once()
            instance.fucker.zhidaoAiExamQuery.assert_called_once()
            with self.assertRaises(StopRequested):
                check_stop()

    def test_legacy_version_one_cache_and_broken_cache_are_handled(self):
        instance = self.exam()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "zhs.clients.exam.getRealPath", side_effect=lambda name: str(Path(directory) / name)
            ):
                cache_dir = Path(instance.getAllAnswerpath()).parent
                atomic_write_json(cache_dir / "old.json", {"10_1": {"answer": "42"}})
                (cache_dir / "broken.json").write_text("{", encoding="utf-8")
                instance.readAnswerCache(3)
                self.assertEqual(instance.getAnswer(10, 1)["answer"], "42")

    def test_single_or_empty_options_have_valid_contract(self):
        instance = self.exam()
        question = {"id": 1, "questionType": 1, "content": "q", "optionVos": []}
        with self.assertRaises(ValueError):
            instance.getQuestionAnswer(question)
        question["optionVos"] = [{"id": 9, "content": "x"}]
        self.assertEqual(instance.getQuestionAnswer(question), ([9], "single option"))

    def test_judgement_failure_falls_back_to_one_random_option(self):
        instance = self.exam()
        instance.op = Mock()
        instance.op.judgementTemplate.side_effect = RuntimeError("provider failed")
        question = {
            "id": 1,
            "questionType": 14,
            "content": "q",
            "optionVos": [{"id": 1, "content": "yes"}, {"id": 2, "content": "no"}],
        }
        answers, source = instance.getQuestionAnswer(question)
        self.assertEqual(source, "random")
        self.assertEqual(len(answers), 1)
        self.assertIn(answers[0], [1, 2])
        self.assertEqual(instance.allAnswerCache, {})

    def test_stream_failure_submits_random_answer_then_retries_with_platform_cache(self):
        question = {
            "id": 101,
            "version": 2,
            "questionType": 1,
            "content": "question",
            "optionVos": [{"id": 10, "content": "A"}, {"id": 20, "content": "B"}],
        }
        graded = copy.deepcopy(question)
        graded["userAnswerVo"] = [{"isCorrect": 0}]
        graded["optionVos"][1]["isCorrect"] = 1
        first = self.exam()
        second = self.exam()
        second.examTestId = 5
        for instance in (first, second):
            instance.openExam = Mock()
            instance.getSheetContent = Mock(return_value=[{"questionId": 101, "version": 2}])
            instance.saveAnswer = Mock(return_value=True)
            instance.op = Mock()
        first.op.generateAnswer.side_effect = ValueError(
            "AI stream ended without a complete answer block"
        )
        first.getQuestionContent = Mock(side_effect=[copy.deepcopy(question), graded])
        self.client.zhidaoAiExamQuery = Mock(return_value={})
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "zhs.clients.exam.getRealPath", side_effect=lambda name: str(Path(directory) / name)
            ),
            patch(
                "zhs.clients.exam.sample", return_value=[question["optionVos"][0]]
            ) as random_pick,
        ):
            # The first submission is wrong; only the returned correct option is cached.
            self.assertEqual(first.startFuck(), (False, 0, 1))
            first.saveAnswer.assert_called_once_with(101, [10])
            cached = json.loads(Path(first.getAllAnswerpath()).read_text())
            self.assertEqual(cached["101_2"]["answer"], "20")
            self.assertEqual(first._submission_state, "reviewed")

            # A new exam attempt loads the course-wide cache, without using AI or random choices.
            next_grade = copy.deepcopy(graded)
            next_grade["userAnswerVo"] = [{"isCorrect": 1}]
            second.getQuestionContent = Mock(side_effect=[copy.deepcopy(question), next_grade])
            self.assertEqual(second.startFuck(), (True, 1, 1))
            second.saveAnswer.assert_called_once_with(101, [20])
            second.op.generateAnswer.assert_not_called()
            random_pick.assert_called_once()
            self.assertEqual(second._submission_state, "reviewed")
        self.assertEqual(self.client.zhidaoAiExamQuery.call_count, 2)

    def test_long_prompt_uses_integer_slice(self):
        client = Openai.__new__(Openai)
        client.encoder = Mock()
        client.encoder.encode.return_value = list(range(28001))
        client.encoder.decode.return_value = "truncated"
        client.useZhidao = False
        client.openaiCompletion = Mock(return_value='```answer\n[{"id": 1, "content": "x"}]\n```')
        self.assertEqual(client.generateAnswer("long input"), [1])
        self.assertEqual(len(client.encoder.decode.call_args.args[0]), 27900)

    def test_stream_handles_empty_choices_and_done(self):
        client = Openai.__new__(Openai)
        response = Mock()
        response.iter_lines.return_value = [b'data: {"choices":[]}', b"data: [DONE]"]
        with self.assertRaises(ValueError):
            client._Openai__parseStream(response, "```answer", "```")

    def test_external_endpoint_does_not_duplicate_v1(self):
        client = Openai.__new__(Openai)
        client.baseUrl = "https://example.test/v1/"
        client.apiKey, client.modelName, client.stream = "test", "model", False
        client.session = MagicMock()
        response = client.session.post.return_value.__enter__.return_value
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        self.assertEqual(client.openaiCompletion("question"), "ok")
        self.assertEqual(
            client.session.post.call_args.args[0], "https://example.test/v1/chat/completions"
        )
