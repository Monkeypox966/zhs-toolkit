import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from zhs.app import main
from zhs.automation import run_until_complete
from zhs.clients.course import ZHSClient
from zhs.clients.exam import ExamCtx
from zhs.common.models import ObjDict
from zhs.common.runtime import SubmissionUncertainError, TaskSummary
from zhs.config import DEFAULT_CONFIG
from zhs.journal import RunJournal
from zhs.workflow import execute_tasks


class AutomaticRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Mock(context={}, courses={}, _completed_ai_knowledge=set())
        self.courses = [{"id": "1", "type": "hike"}, {"id": "2", "type": "hike"}]
        self.wait = Mock()

    def run_plan(self, journal, executor, **kwargs):
        with patch("builtins.print"):
            return run_until_complete(
                journal, self.client, {}, executor, wait=self.wait, max_rounds=6, **kwargs
            )

    def test_failed_course_recovers_after_other_courses_without_replaying_successes(self):
        journal = RunJournal.create(self.root, self.courses, {})
        executor = Mock(
            side_effect=[
                TaskSummary(failed=["temporary"]),
                TaskSummary(succeeded=["2"]),
                TaskSummary(succeeded=["1"]),
            ]
        )
        self.assertEqual(self.run_plan(journal, executor).exit_code, 0)
        self.assertEqual(
            [call.args[1][0]["id"] for call in executor.call_args_list], ["1", "2", "1"]
        )
        self.wait.assert_called_once_with(30)
        self.assertEqual([task["attempts"] for task in journal.data["tasks"]], [2, 1])

    def test_persistent_failure_uses_capped_backoff_and_stays_incomplete(self):
        journal = RunJournal.create(self.root, self.courses[:1], {})
        executor = Mock(return_value=TaskSummary(failed=["offline"]))
        self.assertEqual(self.run_plan(journal, executor).exit_code, 1)
        self.assertEqual(
            [call.args[0] for call in self.wait.call_args_list], [30, 60, 120, 240, 300]
        )
        self.assertEqual(journal.data["status"], "incomplete")

    def test_partial_discovery_adds_missing_courses_and_clears_error(self):
        journal = RunJournal.create(self.root, [], {})
        journal.data["discovery_errors"] = ["offline"]
        journal.save()
        discover = Mock(return_value=(self.courses, []))
        executor = Mock(return_value=TaskSummary(succeeded=["done"]))
        self.assertEqual(self.run_plan(journal, executor, discover=discover).exit_code, 0)
        self.assertEqual(len(journal.data["tasks"]), 2)
        self.assertEqual(executor.call_count, 2)
        self.assertEqual(journal.data["discovery_errors"], [])

    def test_catalog_verification_failure_does_not_replay_completed_course(self):
        journal = RunJournal.create(self.root, self.courses[:1], {})
        executor = Mock(return_value=TaskSummary(succeeded=["1"]))
        discover = Mock(side_effect=[(self.courses[:1], ["offline"]), (self.courses[:1], [])])
        self.assertEqual(self.run_plan(journal, executor, discover=discover).exit_code, 0)
        executor.assert_called_once()

    def test_reauthentication_failure_waits_then_recovers(self):
        journal = RunJournal.create(self.root, self.courses[:1], {})
        prepare = Mock(side_effect=[requests.Timeout("offline"), None])
        executor = Mock(return_value=TaskSummary(succeeded=["1"]))
        self.assertEqual(self.run_plan(journal, executor, prepare=prepare).exit_code, 0)
        executor.assert_called_once()
        self.wait.assert_called_once_with(60)

    def test_interrupt_during_cooldown_keeps_resumable_attempt(self):
        journal = RunJournal.create(self.root, self.courses[:1], {})
        self.wait.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan(journal, Mock(return_value=TaskSummary(failed=["offline"])))
        loaded = RunJournal.load(self.root)
        self.assertEqual(loaded.data["tasks"][0]["attempts"], 1)
        self.assertEqual(loaded.data["automation"]["state"], "waiting")

    def test_retry_failed_does_not_loop_forever_on_unselected_queued_task(self):
        journal = RunJournal.create(self.root, self.courses, {})
        journal.data["tasks"][0]["status"] = "failed"
        journal.save()
        executor = Mock(return_value=TaskSummary(succeeded=["1"]))
        result = self.run_plan(journal, executor, retry_failed=True)
        self.assertEqual(result.exit_code, 1)
        executor.assert_called_once()
        self.wait.assert_not_called()
        self.assertEqual(journal.data["tasks"][1]["status"], "queued")
        self.assertEqual(journal.data["status"], "incomplete")

    def test_uncertain_submission_only_resumes_after_readonly_grade_confirmation(self):
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
        journal.save()
        executor = Mock(return_value=TaskSummary(succeeded=["done"]))

        def recover(client, item):
            executor.assert_not_called()
            if recover.calls == 0:
                recover.calls += 1
                raise SubmissionUncertainError("grading")
            return True, 1, 1

        recover.calls = 0
        with patch("zhs.clients.exam.ExamCtx.recover_submission", side_effect=recover):
            self.assertEqual(self.run_plan(journal, executor).exit_code, 0)
        executor.assert_called_once()
        self.assertEqual(self.client._completed_ai_knowledge, {("1", "2", "3")})
        self.assertEqual(journal.data["tasks"][0]["submissions"][0]["state"], "reviewed")

    def test_unavailable_grades_never_trigger_a_new_submission(self):
        journal = RunJournal.create(self.root, [{"id": "1", "type": "ai", "classId": "2"}], {})
        journal.data["tasks"][0].update(
            status="pending",
            error_type="SubmissionUncertainError",
            submissions=[
                {
                    "id": "a" * 32,
                    "state": "accepted",
                    "course_id": "1",
                    "knowledge_id": "3",
                    "exam_test_id": "4",
                    "exam_paper_id": "5",
                }
            ],
        )
        journal.save()
        executor = Mock()
        with patch(
            "zhs.clients.exam.ExamCtx.recover_submission",
            side_effect=SubmissionUncertainError("not graded"),
        ):
            self.assertEqual(self.run_plan(journal, executor).exit_code, 1)
        executor.assert_not_called()
        self.assertEqual(journal.data["tasks"][0]["submissions"][0]["state"], "accepted")


class ReadonlySubmissionTests(unittest.TestCase):
    def test_recovery_reads_grades_and_caches_platform_answers_without_submitting(self):
        client = ZHSClient(proxies={})
        self.addCleanup(client.close)
        client.zhidaoAiExamQuery = Mock(
            side_effect=[
                {
                    "data": {
                        "partSheetVos": [{"questionSheetVos": [{"questionId": 10, "version": 2}]}]
                    }
                },
                {
                    "data": {
                        "id": 10,
                        "questionType": 1,
                        "content": "q",
                        "userAnswerVo": [{"isCorrect": 0}],
                        "optionVos": [{"id": 20, "content": "A", "isCorrect": 1}],
                    }
                },
            ]
        )
        checkpoint = {
            "id": "a" * 32,
            "state": "submitting",
            "course_id": "1",
            "knowledge_id": "2",
            "exam_test_id": "3",
            "exam_paper_id": "4",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "zhs.clients.exam.getRealPath", side_effect=lambda name: str(Path(directory) / name)
            ):
                self.assertEqual(ExamCtx.recover_submission(client, checkpoint), (False, 0, 1))
                cache = json.loads((Path(directory) / "aiexamAnswer/1/data.json").read_text())
                self.assertEqual(cache["10_2"]["answer"], "20")
        self.assertEqual(
            [call.kwargs["method"] for call in client.zhidaoAiExamQuery.call_args_list],
            ["GET", "GET"],
        )

    def test_successful_execution_waits_for_platform_progress_confirmation(self):
        client = Mock()
        client.getAiKnowlegePoints.return_value = {
            "cakeThemeList": [{"knowledgeList": [{"studyProgress": 50}]}]
        }
        course = {"id": "1", "classId": "2", "type": "ai"}
        with patch("builtins.print"):
            result = execute_tasks(
                client, [course], {"ai": {}}, no_exam=True, verify_completion=True
            )
        self.assertEqual(result.exit_code, 1)
        client.getAiKnowlegePoints.return_value["cakeThemeList"][0]["knowledgeList"][0][
            "studyProgress"
        ] = 100
        self.assertEqual(
            execute_tasks(
                client, [course], {"ai": {}}, no_exam=True, verify_completion=True
            ).exit_code,
            0,
        )


class AutomaticApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ObjDict(copy.deepcopy(DEFAULT_CONFIG), default=None)
        self.config.ai.enabled = False
        self.client = MagicMock()
        self.client.uuid = "test-account"
        self.client._completed_ai_knowledge = set()
        self.client.getZhidaoList.return_value = []
        self.client.getHikeList.return_value = []
        self.client.getZhidaoAiList.return_value = [{"courseId": 1, "classId": 2}]
        self.client.getAiKnowlegePoints.return_value = {"cakeThemeList": []}

    def run_app(self, arguments, restored=True):
        with (
            patch("zhs.config.load_config", return_value=self.config),
            patch("zhs.clients.course.ZHSClient", return_value=self.client),
            patch("zhs.app.restore_cookies", return_value=restored),
            patch("zhs.common.utils.getRealPath", side_effect=lambda name: str(self.root / name)),
            patch("builtins.print"),
        ):
            return main(arguments)

    def test_default_discovers_all_courses_even_with_old_empty_execution_file(self):
        (self.root / "execution.json").write_text("[]")
        self.assertEqual(self.run_app([]), 0)
        self.client.fuckAiCourse.assert_called_once()
        journal = RunJournal.load(self.root / "var/runs")
        self.assertEqual(journal.data["scope"], "all")
        self.assertEqual(journal.data["options"]["threshold"], 1.0)

    def test_default_restart_resumes_same_all_course_plan_and_saved_options(self):
        courses = [
            {"id": "1", "type": "ai", "classId": "2"},
            {"id": "3", "type": "ai", "classId": "4"},
        ]
        journal = RunJournal.create(self.root / "var/runs", courses, {"no_exam": True})
        journal.data.update(scope="all", status="incomplete")
        journal.data["tasks"][0].update(status="succeeded", attempts=1)
        journal.data["tasks"][1].update(status="failed", attempts=1)
        journal.bind_account(self.client.uuid)
        journal.save()
        self.client.getZhidaoAiList.return_value = [
            {"courseId": 1, "classId": 2},
            {"courseId": 3, "classId": 4},
        ]
        self.assertEqual(self.run_app([]), 0)
        self.client.fuckAiCourse.assert_called_once_with(
            "3", "4", aiConfig=self.config.ai, no_exam=True
        )
        loaded = RunJournal.load(self.root / "var/runs")
        self.assertEqual(loaded.data["id"], journal.data["id"])
        self.assertEqual([task["attempts"] for task in loaded.data["tasks"]], [1, 2])

    def test_unattended_login_uses_credentials_without_input_or_qr(self):
        self.config.save_cookies = False
        self.config.username, self.config.password = "test-user", "test-password"
        self.config.qrlogin = True
        self.assertEqual(self.run_app(["--check"], restored=False), 0)
        self.client.login.assert_called_once_with("test-user", "test-password", interactive=False)

    def test_repeated_explicit_course_command_reuses_matching_unfinished_plan(self):
        journal = RunJournal.create(
            self.root / "var/runs", [{"id": "1", "type": "ai", "classId": "2"}], {}
        )
        journal.data["status"] = "incomplete"
        journal.data["tasks"][0].update(status="failed", attempts=1)
        journal.bind_account(self.client.uuid)
        journal.save()
        self.assertEqual(self.run_app(["-ai", "1", "2"]), 0)
        loaded = RunJournal.load(self.root / "var/runs")
        self.assertEqual(loaded.data["id"], journal.data["id"])
        self.assertEqual(loaded.data["tasks"][0]["attempts"], 2)

    def test_restart_during_final_catalog_check_does_not_replay_success(self):
        journal = RunJournal.create(
            self.root / "var/runs", [{"id": "1", "type": "ai", "classId": "2"}], {}
        )
        journal.data.update(
            scope="all", status="finished", automation={"state": "running", "round": 1}
        )
        journal.data["tasks"][0].update(status="succeeded", attempts=1)
        journal.bind_account(self.client.uuid)
        journal.save()
        self.assertEqual(self.run_app([]), 0)
        self.client.fuckAiCourse.assert_not_called()
        loaded = RunJournal.load(self.root / "var/runs")
        self.assertEqual(loaded.data["id"], journal.data["id"])
        self.assertEqual(loaded.data["automation"]["state"], "finished")

    def test_initial_login_network_failure_retries_automatically(self):
        self.config.save_cookies = False
        self.config.username, self.config.password = "test-user", "test-password"
        self.client.login.side_effect = [requests.Timeout("offline"), None]
        with patch("time.sleep") as sleep:
            self.assertEqual(self.run_app(["--check"], restored=False), 0)
        self.assertEqual(self.client.login.call_count, 2)
        sleep.assert_called_once_with(30)
