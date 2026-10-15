import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from zhs.clients.course import ZHSClient
from zhs.clients.exam import ExamCtx
from zhs.common.contracts import ProtocolError, course_record, sheet_questions, validate_answers
from zhs.common.runtime import CourseResolutionError, SubmissionUncertainError, TimeLimitExceeded
from zhs.workflow import fetch_courses, resolve_courses

FIXTURES = Path(__file__).parent / "fixtures"


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.question = json.loads((FIXTURES / "question.json").read_text())

    def test_all_sheet_parts_are_read_in_order(self):
        payload = json.loads((FIXTURES / "multipart_sheet.json").read_text())
        self.assertEqual([q["questionId"] for q in sheet_questions(payload)], [101, 102])
        instance = ExamCtx.__new__(ExamCtx)
        instance.sheetContent = None
        instance.examTestId, instance.examPaperId = 1, 2
        instance.fucker = Mock()
        instance.fucker.zhidaoAiExamQuery.return_value = payload
        self.assertEqual(len(instance.getSheetContent()), 2)

    def test_malformed_and_duplicate_sheet_entries_fail(self):
        payloads = [
            {},
            {"data": {"partSheetVos": []}},
            {"data": {"partSheetVos": [{"questionSheetVos": None}]}},
            {
                "data": {
                    "partSheetVos": [{"questionSheetVos": [{"questionId": 1}, {"questionId": "1"}]}]
                }
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                sheet_questions(payload)

    def test_invalid_answers_are_rejected(self):
        for answers in ([], [999], [10, 10], [10, 20], [None], [True], "10"):
            with self.subTest(answers=answers), self.assertRaises(ProtocolError):
                validate_answers(self.question, answers)
        self.assertEqual(validate_answers(self.question, ["10"]), [10])
        self.question["questionType"] = 2
        self.assertEqual(validate_answers(self.question, [10, "20"]), [10, 20])

    def test_duplicate_options_and_unsupported_types_fail(self):
        self.question["optionVos"].append(dict(self.question["optionVos"][0]))
        with self.assertRaises(ProtocolError):
            validate_answers(self.question, [10])
        self.question["questionType"] = 99
        with self.assertRaises(ProtocolError):
            validate_answers(self.question, [10])

    def exam(self):
        exam = ExamCtx.__new__(ExamCtx)
        exam.getAnswer = Mock(return_value=None)
        exam.referenceMaterials = []
        exam.op = Mock()
        return exam

    def test_out_of_options_ai_answer_uses_random_legal_option(self):
        exam = self.exam()
        exam.op.generateAnswer.return_value = [999]
        answers, source = exam.getQuestionAnswer(self.question)
        self.assertEqual(source, "random")
        self.assertEqual(len(answers), 1)
        self.assertIn(answers[0], [10, 20])

    def test_invalid_cached_answer_is_replaced_by_valid_ai_answer(self):
        exam = self.exam()
        exam.getAnswer.return_value = {"answer": "999"}
        exam.op.generateAnswer.return_value = [20]
        self.assertEqual(exam.getQuestionAnswer(self.question), ([20], "AI generated"))

    def test_unchanged_cached_answer_is_validated_and_reused(self):
        exam = self.exam()
        exam.getAnswer.return_value = {"answer": "10"}
        self.assertEqual(exam.getQuestionAnswer(self.question), ([10], "cached"))
        exam.op.generateAnswer.assert_not_called()

    def test_changed_question_content_invalidates_cache(self):
        exam = self.exam()
        exam.getAnswer.return_value = {"answer": "10", "question": "outdated question"}
        exam.op.generateAnswer.return_value = [20]
        self.assertEqual(exam.getQuestionAnswer(self.question)[0], [20])

    def test_disabled_ai_uses_random_fallback(self):
        exam = self.exam()
        del exam.op
        answers, source = exam.getQuestionAnswer(self.question)
        self.assertEqual(source, "random")
        self.assertIn(answers[0], [10, 20])

    def test_multiple_choice_fallback_selects_two_distinct_options(self):
        exam = self.exam()
        exam.op.generateAnswer.side_effect = ValueError(
            "AI stream ended without a complete answer block"
        )
        self.question["questionType"] = 2
        self.question["optionVos"].append({"id": 30, "content": "third"})
        answers, source = exam.getQuestionAnswer(self.question)
        self.assertEqual(source, "random")
        self.assertEqual(len(answers), 2)
        self.assertEqual(len(set(answers)), 2)
        self.assertTrue(set(answers) <= {10, 20, 30})

    def test_deadlines_interrupts_and_uncertain_submissions_do_not_fall_back(self):
        for error in (
            TimeLimitExceeded("limit"),
            KeyboardInterrupt(),
            SubmissionUncertainError("verify"),
        ):
            with self.subTest(error=type(error).__name__):
                exam = self.exam()
                exam.op.generateAnswer.side_effect = error
                with self.assertRaises(type(error)):
                    exam.getQuestionAnswer(self.question)

    def test_invalid_question_does_not_trigger_random_submission(self):
        for change in ({"questionType": 99}, {"optionVos": []}):
            with self.subTest(change=change):
                exam = self.exam()
                with self.assertRaises(ProtocolError):
                    exam.getQuestionAnswer(dict(self.question, **change))
                exam.op.generateAnswer.assert_not_called()

    def test_course_records_reject_path_identifiers(self):
        for value in (None, True, "../course", "C:\\course", ""):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                course_record({"id": value})


class RoutingTests(unittest.TestCase):
    def client(self):
        client = ZHSClient(proxies={})
        self.addCleanup(client.close)
        client.getZhidaoList = Mock(return_value=[])
        client.getHikeList = Mock(return_value=[])
        client.getZhidaoAiList = Mock(return_value=[{"courseId": 123, "classId": 456}])
        return client

    def test_numeric_ai_course_is_resolved_from_metadata(self):
        client = self.client()
        client.fuckAiCourse = Mock()
        client.fuckHikeCourse = Mock()
        client.fuckCourse("123", aiConfig={"enabled": False})
        client.fuckAiCourse.assert_called_once_with("123", "456", {"enabled": False}, no_exam=False)
        client.fuckHikeCourse.assert_not_called()

    def test_missing_or_ambiguous_courses_are_not_guessed(self):
        client = self.client()
        with self.assertRaises(CourseResolutionError):
            resolve_courses(client, [{"id": "999"}])
        client.getHikeList.return_value = [{"courseId": 123}]
        with self.assertRaises(CourseResolutionError):
            resolve_courses(client, [{"id": "123"}])

    def test_typed_records_need_no_network_discovery(self):
        client = self.client()
        expected = {"id": "123", "type": "ai", "classId": "456"}
        self.assertEqual(resolve_courses(client, [expected]), [expected])
        client.getZhidaoAiList.assert_not_called()

    def test_course_catalog_fixture_is_normalized(self):
        client = self.client()
        data = json.loads((FIXTURES / "course_catalog.json").read_text())
        client.getZhidaoList.return_value = data["zhidao"]
        client.getHikeList.return_value = data["hike"]
        client.getZhidaoAiList.return_value = data["ai"]
        courses, errors = fetch_courses(client)
        self.assertEqual(errors, [])
        self.assertEqual([course["type"] for course in courses], ["zhidao", "hike", "ai"])
        self.assertEqual(courses[-1]["classId"], "456")

    def test_missing_ids_and_partial_discovery_do_not_silently_select(self):
        client = self.client()
        client.getHikeList.return_value = [{"courseId": None}]
        courses, errors = fetch_courses(client)
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(courses), 1)
        with self.assertRaises(CourseResolutionError):
            resolve_courses(client, [{"id": "123"}])
