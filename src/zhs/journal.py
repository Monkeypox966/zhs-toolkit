"""Durable task states, restart selection and machine-readable run reports."""

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from zhs.common.checkpoints import track_submissions
from zhs.common.locking import FileLock
from zhs.common.runtime import TaskSummary
from zhs.common.storage import atomic_write_json


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def validate_options(options):
    if not isinstance(options, dict) or set(options) - {"speed", "threshold", "limit", "no_exam"}:
        raise ValueError("运行选项包含未知字段")
    if not isinstance(options.get("no_exam", False), bool):
        raise ValueError("no_exam 必须是布尔值")
    limit = options.get("limit", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("运行时限必须是非负整数")
    for key in ("speed", "threshold"):
        value = options.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} 必须是有限正数")


class RunJournal:
    def __init__(self, path, data, persisted=True):
        self.path = Path(path)
        self.data = data
        self._revision = data.get("revision", data.get("updated_at")) if persisted else None

    @classmethod
    def create(cls, directory, courses, options, videos=None):
        from zhs.common.contracts import course_record, identifier

        courses = [course_record(course, require_type=True) for course in courses]
        validate_options(options)
        if videos is not None:
            videos = list(dict.fromkeys(identifier(video, "video id") for video in videos))
            if not courses or not videos:
                raise ValueError("视频计划必须包含课程和视频")
        directory = Path(directory)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        groups = (
            [([course], None) for course in courses]
            if videos is None
            else [(courses, str(video)) for video in dict.fromkeys(videos)]
        )
        tasks = [
            {
                "id": str(index + 1),
                "courses": copy.deepcopy(group),
                "video_id": video,
                "status": "queued",
                "attempts": 0,
                "error": None,
                "error_type": None,
                "duration_seconds": 0.0,
                "started_at": None,
                "finished_at": None,
                "history": [],
                "submissions": [],
            }
            for index, (group, video) in enumerate(groups)
        ]
        data = {
            "schema_version": 1,
            "id": run_id,
            "created_at": timestamp(),
            "updated_at": timestamp(),
            "status": "queued",
            "options": dict(options),
            "tasks": tasks,
            "discovery_errors": [],
        }
        journal = cls(directory / f"{run_id}.json", data, persisted=False)
        journal.save()
        atomic_write_json(directory / "latest.json", {"id": run_id})
        return journal

    @classmethod
    def load(cls, directory, run_id="latest"):
        directory = Path(directory)
        if run_id == "latest":
            run_id = json.loads((directory / "latest.json").read_text(encoding="utf-8"))["id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", run_id):
            raise ValueError("无效的运行记录 ID")
        path = directory / f"{run_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or data.get("id") != run_id
            or not isinstance(data.get("tasks"), list)
            or not isinstance(data.get("options"), dict)
        ):
            raise ValueError("运行记录格式无效或版本不支持")
        validate_options(data["options"])
        if data.get("status") not in {"queued", "running", "finished", "incomplete", "interrupted"}:
            raise ValueError("运行记录状态无效")
        if not isinstance(data.get("discovery_errors", []), list) or any(
            not isinstance(error, str) for error in data.get("discovery_errors", [])
        ):
            raise ValueError("课程发现错误格式无效")
        allowed = {"queued", "running", "succeeded", "failed", "pending", "skipped", "interrupted"}
        from zhs.common.contracts import course_record, identifier

        seen = set()
        for task in data["tasks"]:
            if not isinstance(task, dict) or task.get("status") not in allowed:
                raise ValueError("运行记录包含无效任务")
            task_id = identifier(task.get("id"), "task id")
            if task_id in seen or not isinstance(task.get("courses"), list) or not task["courses"]:
                raise ValueError("运行记录包含重复或空任务")
            seen.add(task_id)
            task["id"] = task_id
            if (
                isinstance(task.get("attempts"), bool)
                or not isinstance(task.get("attempts"), int)
                or task["attempts"] < 0
            ):
                raise ValueError("运行记录的尝试次数无效")
            task["courses"] = [
                course_record(course, require_type=True) for course in task["courses"]
            ]
            if task.get("video_id") is not None:
                task["video_id"] = identifier(task["video_id"], "video id")
            if not isinstance(task.get("history", []), list):
                raise ValueError("任务尝试历史格式无效")
            task.setdefault("history", [])
            duration = task.get("duration_seconds", 0)
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration < 0
            ):
                raise ValueError("任务耗时无效")
            outcomes = task.get("outcomes", {})
            if (
                not isinstance(outcomes, dict)
                or set(outcomes) - {"succeeded", "failed", "pending", "skipped"}
                or any(
                    not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                    for items in outcomes.values()
                )
            ):
                raise ValueError("任务结果格式无效")
            submissions = task.setdefault("submissions", [])
            if not isinstance(submissions, list):
                raise ValueError("提交检查点格式无效")
            submission_ids = set()
            for item in submissions:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", item["id"])
                    or item["id"] in submission_ids
                    or item.get("state") not in {"submitting", "accepted", "reviewed", "rejected"}
                ):
                    raise ValueError("提交检查点格式无效")
                submission_ids.add(item["id"])
                # Earlier versions classified explicit auth rejections as unknown
                # submissions. Match the exact exam and submit endpoint only.
                if item["state"] == "submitting" and item.get("exam_test_id"):
                    legacy = re.search(
                        rf"试卷\s+{re.escape(str(item['exam_test_id']))}\s+的提交结果无法确认"
                        r"[^\n]*?(401|403) Client Error:[^\n]*?"
                        r"https://studentexamtest\.zhihuishu\.com/gateway/t/v1/exam/user/submit(?:\s|$)",
                        str(task.get("error") or ""),
                    )
                    if legacy:
                        item.update(
                            state="rejected",
                            http_status=int(legacy.group(1)),
                            rejection_source="legacy_http_error",
                        )
                        task.update(status="failed", error_type="AuthenticationError")
            if task["status"] == "running":
                task["status"] = "interrupted"
                task["error"] = "上次进程未正常结束，任务状态需要重新确认"
                if task["attempts"] and not any(
                    isinstance(entry, dict) and entry.get("attempts") == task["attempts"]
                    for entry in task["history"]
                ):
                    task["history"].append(
                        {
                            "status": "interrupted",
                            "attempts": task["attempts"],
                            "error": task["error"],
                            "error_type": "ProcessInterrupted",
                            "started_at": task.get("started_at"),
                            "finished_at": None,
                            "duration_seconds": None,
                            "recovered_at": timestamp(),
                        }
                    )
            if cls._unresolved_submission(task):
                task.update(
                    status="pending",
                    error="上次试卷提交或评分尚未确认，等待恢复查询",
                    error_type="SubmissionUncertainError",
                )
        return cls(path, data)

    def _submission_recorder(self, task):
        def record(submission_id, state, details):
            checkpoints = task.setdefault("submissions", [])
            item = next((entry for entry in checkpoints if entry["id"] == submission_id), None)
            if item is None:
                item = {"id": submission_id, **details}
                checkpoints.append(item)
            else:
                item.update(details)
            item.update(state=state, updated_at=timestamp())
            # This write must succeed before the submit request is sent.
            self._save()

        return record

    def reconcile_submissions(self, client, reconciler=None):
        """Resolve uncertain submissions using read-only grade queries."""
        from zhs.clients.exam import ExamCtx
        from zhs.common.logging import logger

        reconciler = reconciler or ExamCtx.recover_submission
        with FileLock(self.path.with_suffix(".lock")):
            self._check_revision()
            for task in self.data["tasks"]:
                for item in task.get("submissions", []):
                    if item["state"] in {"reviewed", "rejected"}:
                        continue
                    try:
                        _, correct, total = reconciler(client, item)
                        if (
                            isinstance(correct, bool)
                            or isinstance(total, bool)
                            or not isinstance(correct, int)
                            or not isinstance(total, int)
                            or total <= 0
                            or not 0 <= correct <= total
                        ):
                            raise ValueError("平台评分尚不完整")
                    except Exception as error:
                        logger.warning(f"试卷 {item.get('exam_test_id')} 等待评分确认：{error}")
                        item["last_check_error"] = str(error)
                        item["last_checked_at"] = timestamp()
                    else:
                        item.update(
                            state="reviewed",
                            correct_questions=correct,
                            total_questions=total,
                            updated_at=timestamp(),
                        )
                        item.pop("last_check_error", None)
                if (
                    task.get("error_type") == "SubmissionUncertainError"
                    and task.get("submissions")
                    and not self._unresolved_submission(task)
                ):
                    task.update(status="failed", error=None, error_type=None)
            self._save()

    def extend_courses(self, courses, discovery_errors):
        """Merge a fresh catalog into an all-course plan without losing attempts."""
        from zhs.common.contracts import course_record

        courses = [course_record(course, require_type=True) for course in courses]
        with FileLock(self.path.with_suffix(".lock")):
            self._check_revision()
            known = {
                (course["type"], course["id"], course.get("classId"))
                for task in self.data["tasks"]
                for course in task["courses"]
            }
            used_ids = {task["id"] for task in self.data["tasks"]}
            next_id = len(used_ids)
            for course in courses:
                key = (course["type"], course["id"], course.get("classId"))
                if key in known:
                    continue
                next_id += 1
                while str(next_id) in used_ids:
                    next_id += 1
                self.data["tasks"].append(
                    {
                        "id": str(next_id),
                        "courses": [course],
                        "video_id": None,
                        "status": "queued",
                        "attempts": 0,
                        "error": None,
                        "error_type": None,
                        "duration_seconds": 0.0,
                        "started_at": None,
                        "finished_at": None,
                        "history": [],
                        "submissions": [],
                    }
                )
                known.add(key)
                used_ids.add(str(next_id))
            self.data["discovery_errors"] = list(discovery_errors)
            self._save()

    @staticmethod
    def _unresolved_submission(task):
        return any(
            item["state"] in {"submitting", "accepted"} for item in task.get("submissions", [])
        )

    def save(self):
        with FileLock(self.path.with_suffix(".lock")):
            self._check_revision()
            self._save()

    def _check_revision(self):
        if self.path.exists():
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("revision", current.get("updated_at")) != self._revision:
                raise ValueError("运行记录已被其他实例更新，请重新加载后再操作")
        elif self._revision is not None:
            raise ValueError("运行记录已被移除，请重新建立计划")

    def _save(self):
        self.data["updated_at"] = timestamp()
        revision = uuid4().hex
        self.data["revision"] = revision
        self.data["counts"] = {
            status: sum(t["status"] == status for t in self.data["tasks"])
            for status in (
                "queued",
                "running",
                "succeeded",
                "failed",
                "pending",
                "skipped",
                "interrupted",
            )
        }
        atomic_write_json(self.path, self.data)
        self._revision = revision

    def validate_report_path(self, path):
        target = Path(path)
        if target.resolve().is_relative_to(self.path.parent.resolve()):
            raise ValueError("报告需导出到运行记录目录之外，避免覆盖任务状态")
        if target.exists():
            try:
                previous = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError("报告目标已存在且不是运行报告，拒绝覆盖") from error
            if (
                not isinstance(previous, dict)
                or previous.get("schema_version") != 1
                or not isinstance(previous.get("id"), str)
                or not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", previous["id"])
                or not isinstance(previous.get("tasks"), list)
                or not isinstance(previous.get("options"), dict)
            ):
                raise ValueError("报告目标已存在且不是运行报告，拒绝覆盖")

    def export(self, path):
        self.validate_report_path(path)
        atomic_write_json(path, self.data)

    def bind_account(self, account_id):
        if not isinstance(account_id, str) or not account_id:
            return
        digest = hashlib.sha256(account_id.encode()).hexdigest()
        if self.data.get("account") not in (None, digest):
            raise ValueError("运行记录属于其他账号，不能直接续跑")
        self.data["account"] = digest

    def matches_selection(self, courses, videos=None):
        def group_key(group, video):
            return (
                tuple(
                    sorted(
                        (item["type"], str(item["id"]), str(item.get("classId", "")))
                        for item in group
                    )
                ),
                None if video is None else str(video),
            )

        expected = (
            {group_key([course], None) for course in courses}
            if videos is None
            else {group_key(courses, video) for video in videos}
        )
        return expected == {
            group_key(task["courses"], task["video_id"]) for task in self.data["tasks"]
        }

    def execute(self, client, config, executor, retry_failed=False, allow_partial=False):
        with FileLock(self.path.with_suffix(".lock")):
            self._check_revision()
            return self._execute(
                client, config, executor, retry_failed=retry_failed, allow_partial=allow_partial
            )

    def _execute(self, client, config, executor, retry_failed=False, allow_partial=False):
        eligible = (
            {"failed", "pending"}
            if retry_failed
            else {"queued", "failed", "pending", "skipped", "interrupted"}
        )
        aggregate = TaskSummary()
        executed = set()
        self.data["status"] = "running"
        self._save()
        try:
            if client is not None and any(
                task.get("video_id") is not None for task in self.data["tasks"]
            ):
                client.reset_deadlines()
            for task in self.data["tasks"]:
                if task["status"] not in eligible:
                    continue
                needs_verification = task.get(
                    "error_type"
                ) == "SubmissionUncertainError" or self._unresolved_submission(task)
                unresolved = [
                    item
                    for item in task.get("submissions", [])
                    if item["state"] in {"submitting", "accepted"}
                ]
                partial = (
                    allow_partial
                    and client is not None
                    and bool(unresolved)
                    and task["video_id"] is None
                    and len(task["courses"]) == 1
                    and task["courses"][0]["type"] == "ai"
                    and all(
                        item.get("knowledge_id")
                        and item.get("course_id") == task["courses"][0]["id"]
                        for item in unresolved
                    )
                )
                if needs_verification and not partial:
                    task.update(
                        status="pending",
                        error="试卷已有提交检查点，等待查询提交及评分状态，确认前不会重复提交",
                        error_type="SubmissionUncertainError",
                    )
                    aggregate.pending.append(task.get("error") or "提交状态需要人工核实")
                    executed.add(task["id"])
                    continue
                executed.add(task["id"])
                task.update(
                    status="running",
                    error=None,
                    error_type=None,
                    started_at=timestamp(),
                    finished_at=None,
                    duration_seconds=0.0,
                )
                task["attempts"] = task.get("attempts", 0) + 1
                self._save()
                started = time.monotonic()
                previous_blocked = (
                    getattr(client, "_blocked_ai_knowledge", set()) if client is not None else set()
                )
                if client is not None:
                    client._blocked_ai_knowledge = (
                        {
                            (item["course_id"], task["courses"][0]["classId"], item["knowledge_id"])
                            for item in unresolved
                        }
                        if partial
                        else set()
                    )
                try:
                    video_options = (
                        {"reset_video_deadlines": False} if task["video_id"] is not None else {}
                    )
                    with track_submissions(self._submission_recorder(task)):
                        result = executor(
                            client,
                            task["courses"],
                            config,
                            no_exam=self.data["options"].get("no_exam", False),
                            videos=None if task["video_id"] is None else [task["video_id"]],
                            **video_options,
                        )
                    if not isinstance(result, TaskSummary):
                        raise TypeError("任务执行器必须返回 TaskSummary")
                    if not any((result.succeeded, result.failed, result.pending, result.skipped)):
                        result.failed.append("执行器未返回任何已确认的任务结果")
                        result.error_types.append("EmptyTaskResult")
                    if self._unresolved_submission(task):
                        result.pending.append("试卷提交或评分状态待核实，不会自动重新执行该任务")
                        result.error_types.insert(0, "SubmissionUncertainError")
                    for field in ("succeeded", "failed", "pending", "skipped"):
                        getattr(aggregate, field).extend(getattr(result, field))
                    task["outcomes"] = {
                        field: list(getattr(result, field))
                        for field in ("succeeded", "failed", "pending", "skipped")
                    }
                    task["status"] = (
                        "failed"
                        if result.failed
                        else "pending"
                        if result.pending
                        else "skipped"
                        if result.skipped
                        else "succeeded"
                    )
                    task["error"] = (
                        "\n".join(result.failed + result.pending + result.skipped) or None
                    )
                    task["error_type"] = result.error_types[0] if result.error_types else None
                except BaseException as error:
                    task.update(
                        status="interrupted"
                        if isinstance(error, (KeyboardInterrupt, SystemExit))
                        else "failed",
                        error=str(error),
                        error_type=type(error).__name__,
                    )
                    raise
                finally:
                    if client is not None:
                        client._blocked_ai_knowledge = previous_blocked
                    task["finished_at"] = timestamp()
                    task["duration_seconds"] = round(time.monotonic() - started, 3)
                    task.setdefault("history", []).append(
                        {
                            key: task[key]
                            for key in (
                                "status",
                                "attempts",
                                "error",
                                "error_type",
                                "started_at",
                                "finished_at",
                                "duration_seconds",
                            )
                        }
                    )
                    self._save()
            aggregate.failed.extend(self.data.get("discovery_errors", []))
            for task in self.data["tasks"]:
                if task["id"] not in executed and task["status"] in {"succeeded", "skipped"}:
                    status = task["status"]
                    getattr(aggregate, status).extend(
                        task.get("outcomes", {}).get(status) or [f"任务 {task['id']}"]
                    )
                if task["id"] not in executed and task["status"] in {
                    "failed",
                    "pending",
                    "queued",
                    "interrupted",
                }:
                    aggregate.failed.append(
                        f"任务 {task['id']} 仍为 {task['status']}，请使用 --resume"
                    )
            self.data["status"] = (
                "incomplete"
                if any(
                    t["status"] in {"failed", "pending", "queued", "interrupted"}
                    for t in self.data["tasks"]
                )
                or self.data.get("discovery_errors")
                else "finished"
            )
            return aggregate
        except BaseException as error:
            self.data["status"] = (
                "interrupted"
                if isinstance(error, (KeyboardInterrupt, SystemExit))
                else "incomplete"
            )
            raise
        finally:
            self._save()
