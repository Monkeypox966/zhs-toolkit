"""Authentication expiry, definitive HTTP rejection, and partial-course recovery."""

import json
import tempfile
import unittest
from base64 import b64decode
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import unquote

import requests

from zhs.app import restore_cookies
from zhs.automation import run_until_complete
from zhs.clients.course import ZHSClient
from zhs.clients.exam import ExamCtx
from zhs.common.authentication import passport_form
from zhs.common.checkpoints import track_submissions
from zhs.common.logging import logger
from zhs.common.models import ObjDict
from zhs.common.runtime import AuthenticationError, SubmissionUncertainError, TaskSummary
from zhs.common.storage import atomic_write_json
from zhs.common.utils import cookie_jar_to_list
from zhs.journal import RunJournal


def setUpModule():
    logger.setLevel("CRITICAL")


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.client = ZHSClient(proxies={})
        self.addCleanup(self.client.close)
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.client.session.get = Mock(return_value=self.response)

    def test_passport_form_matches_unicode_uri_and_base64_protocol(self):
        form = passport_form("测试+1@example.test", "p&?#密")
        self.assertEqual(set(form), {"secretStr"})
        encoded = b64decode(form["secretStr"]).decode("ascii")
        self.assertIn("+1@example.test", encoded)
        self.assertIn("&?#", encoded)
        self.assertIn("%E6%B5%8B%E8%AF%95", encoded)
        self.assertEqual(
            json.loads(unquote(encoded)),
            {
                "account": "测试+1@example.test",
                "password": "p&?#密",
                "validate": "",
            },
        )

    def test_unknown_login_status_does_not_read_missing_uuid(self):
        self.client._apiQuery = Mock(return_value={"status": -1})
        with self.assertRaises(AuthenticationError) as caught:
            self.client.login("test-user", "test-password", interactive=False)
        self.assertEqual(caught.exception.status, -1)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("uuid not found", str(caught.exception))
        self.client._apiQuery.assert_called_once()

    def test_success_response_missing_ticket_is_not_treated_as_valid_login(self):
        self.client._apiQuery = Mock(return_value={"status": 1, "uuid": "test-uuid"})
        with self.assertRaises(AuthenticationError):
            self.client.login("test-user", "test-password", interactive=False)
        self.client._apiQuery.assert_called_once()

    def test_network_errors_keep_their_type_for_automatic_retry(self):
        self.client._apiQuery = Mock(side_effect=requests.Timeout("offline"))
        with self.assertRaises(requests.Timeout):
            self.client.login("test-user", "test-password", interactive=False)

    def test_successful_login_uses_secret_form_and_once_ticket_redirect(self):
        self.client._apiQuery = Mock(
            side_effect=[
                {"status": 1, "uuid": "test-uuid", "pwd": "once-ticket"},
                {"rt": {"needAuth": 0}},
            ]
        )

        def get(url, **kwargs):
            if "params" in kwargs:
                self.client.session.cookies.set("CASLOGC", json.dumps({"uuid": "test-uuid"}))
            return self.response

        self.client.session.get.side_effect = get
        self.client.login("test-user", "test-password", interactive=False)
        form = self.client._apiQuery.call_args_list[0].args[1]
        self.assertEqual(set(form), {"secretStr"})
        self.assertEqual(self.client.session.get.call_args.kwargs["params"], {"pwd": "once-ticket"})
        self.assertEqual(self.client.uuid, "test-uuid")

    def test_transient_cookie_check_does_not_discard_login_or_attempt_password(self):
        self.client.cookies = {"CASLOGC": json.dumps({"uuid": "test-uuid"})}
        self.client.getZhidaoList = Mock(side_effect=requests.Timeout("offline"))
        self.client.refresh_login = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            atomic_write_json(path, cookie_jar_to_list(self.client.cookies))
            original = path.read_bytes()
            with self.assertRaises(requests.Timeout):
                restore_cookies(self.client, path)
            self.assertEqual(path.read_bytes(), original)
        self.client.refresh_login.assert_not_called()
        self.assertEqual(self.client.uuid, "test-uuid")

    def test_expired_api_session_tries_existing_sso_before_password(self):
        self.client.cookies = {"CASLOGC": json.dumps({"uuid": "test-uuid"})}
        self.client.getZhidaoList = Mock(side_effect=[http_error(401), []])
        self.client.login = Mock()

        def get(*args, **kwargs):
            self.client.session.cookies.set("fresh", "verified")
            return self.response

        self.client.session.get.side_effect = get
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            atomic_write_json(path, cookie_jar_to_list(self.client.cookies))
            with patch("builtins.print"):
                self.assertTrue(restore_cookies(self.client, path))
            self.assertTrue(
                any(cookie["name"] == "fresh" for cookie in json.loads(path.read_text()))
            )
        self.client.login.assert_not_called()
        self.assertEqual(self.client.getZhidaoList.call_count, 2)

    def test_definitive_401_submission_is_rejected_instead_of_awaiting_grades(self):
        self.client.zhidaoAiExamQuery = Mock(side_effect=http_error(401))
        exam = ExamCtx(self.client, 1, 2, 3, 4, aiConfig={})
        self.addCleanup(exam.close)
        checkpoints = []
        with track_submissions(lambda identity, state, detail: checkpoints.append((state, detail))):
            with self.assertRaises(AuthenticationError) as caught:
                exam.submitExam()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual([state for state, _ in checkpoints], ["submitting", "rejected"])
        self.assertEqual(checkpoints[-1][1]["http_status"], 401)
        self.client.zhidaoAiExamQuery.assert_called_once()

    def test_server_error_submission_remains_uncertain(self):
        self.client.zhidaoAiExamQuery = Mock(side_effect=http_error(500))
        exam = ExamCtx(self.client, 1, 2, 3, 4, aiConfig={})
        self.addCleanup(exam.close)
        with self.assertRaises(SubmissionUncertainError):
            exam.submitExam()
        self.assertEqual(exam._submission_state, "submitting")

    def test_pending_exam_does_not_prevent_other_nodes_from_running(self):
        self.client.tree_view, self.client.progressbar_view = False, False
        self.client._sleep = Mock()
        self.client._blocked_ai_knowledge = {("1", "2", "3")}
        self.client.getAiKnowlegePoints = Mock(
            return_value=ObjDict(
                {
                    "courseName": "test",
                    "cakeThemeList": [
                        {
                            "themeName": "theme",
                            "knowledgeList": [
                                {
                                    "knowledgeId": 3,
                                    "knowledgeName": "blocked",
                                    "studyProgress": 100,
                                },
                                {
                                    "knowledgeId": 4,
                                    "knowledgeName": "available",
                                    "studyProgress": 100,
                                },
                            ],
                        }
                    ],
                }
            )
        )
        self.client.queryAiExam = Mock(
            return_value={"examTestId": 10, "paperId": 20, "masteryScore": 0}
        )
        with patch("zhs.clients.ai_courses.ExamCtx") as factory:
            factory.return_value.startFuck.return_value = (True, 1, 1)
            with self.assertRaises(SubmissionUncertainError):
                self.client.fuckAiCourse("1", "2", {})
            factory.assert_called_once()
            self.assertEqual(factory.call_args.kwargs["knowledgeId"], 4)
        self.client.queryAiExam.assert_called_once_with("1", "2", 4)


class RecoveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Mock(context={}, courses={}, _completed_ai_knowledge=set())

    def plan(self):
        journal = RunJournal.create(self.root, [{"id": "1", "type": "ai", "classId": "2"}], {})
        journal.data["tasks"][0].update(
            status="pending",
            error_type="SubmissionUncertainError",
            submissions=[
                {
                    "id": "a" * 32,
                    "state": "submitting",
                    "course_id": "1",
                    "knowledge_id": "3",
                    "exam_test_id": "4",
                    "exam_paper_id": "5",
                }
            ],
        )
        return journal

    def test_legacy_401_checkpoint_is_migrated_but_timeout_is_not(self):
        for error, expected in (
            (
                "试卷 4 的提交结果无法确认，请核对平台状态；不会自动重复提交：401 Client Error: Unauthorized for url: https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/submit",
                "rejected",
            ),
            ("试卷 4 的提交结果无法确认：timeout", "submitting"),
            (
                "试卷 9 的提交结果无法确认：401 Client Error: Unauthorized for url: https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/submit",
                "submitting",
            ),
            (
                "试卷 4 已尝试提交，后续处理失败：401 Client Error: Unauthorized for url: https://studentexamtest.zhihuishu.com/gateway/t/v1/question/getExamQuestionInfo",
                "submitting",
            ),
        ):
            with self.subTest(expected=expected, error=error):
                journal = self.plan()
                journal.data["tasks"][0]["error"] = error
                journal.save()
                task = RunJournal.load(self.root).data["tasks"][0]
                self.assertEqual(task["submissions"][0]["state"], expected)
                self.assertEqual(task["status"], "failed" if expected == "rejected" else "pending")

    def test_401_during_grade_query_does_not_turn_accepted_submission_into_rejection(self):
        journal = self.plan()
        journal.data["tasks"][0]["submissions"][0]["state"] = "accepted"
        journal.save()
        journal.reconcile_submissions(self.client, reconciler=Mock(side_effect=http_error(401)))
        self.assertEqual(journal.data["tasks"][0]["submissions"][0]["state"], "accepted")

    def test_permanent_login_rejection_is_persisted_and_not_retried_forever(self):
        journal = self.plan()
        journal.save()
        prepare = Mock(side_effect=AuthenticationError("login rejected", status=-1))
        wait, executor = Mock(), Mock()
        with self.assertRaises(AuthenticationError):
            run_until_complete(
                journal, self.client, {}, executor, prepare=prepare, wait=wait, max_rounds=6
            )
        prepare.assert_called_once()
        executor.assert_not_called()
        wait.assert_not_called()
        data = json.loads(journal.path.read_text())
        self.assertEqual(data["automation"]["state"], "authentication_required")
        self.assertEqual(data["automation"]["auth_status"], -1)
        self.assertEqual(data["automation"]["last_error"], "login rejected")
        self.assertIsNone(data["automation"]["next_retry_at"])

    def test_connection_failure_reason_is_visible_during_cooldown(self):
        journal = RunJournal.create(self.root, [{"id": "1", "type": "hike"}], {})
        prepare = Mock(side_effect=[requests.Timeout("temporary network failure"), None])

        def wait(delay):
            data = json.loads(journal.path.read_text())
            self.assertEqual(data["automation"]["state"], "waiting")
            self.assertEqual(data["automation"]["error_type"], "Timeout")
            self.assertEqual(data["automation"]["last_error"], "temporary network failure")
            self.assertIsNotNone(data["automation"]["next_retry_at"])

        with patch("builtins.print"):
            result = run_until_complete(
                journal,
                self.client,
                {},
                Mock(return_value=TaskSummary(succeeded=["1"])),
                prepare=prepare,
                wait=wait,
                max_rounds=2,
            )
        self.assertEqual(result.exit_code, 0)
