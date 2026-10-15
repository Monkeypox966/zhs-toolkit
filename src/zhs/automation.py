"""Keep unfinished tasks moving, with cooling-off and read-only submission recovery."""

import time

from zhs.common.logging import logger
from zhs.common.runtime import TaskSummary


def _restore_completed_knowledge(client, journal):
    completed = set(getattr(client, "_completed_ai_knowledge", set()))
    for task in journal.data["tasks"]:
        for item in task.get("submissions", []):
            total = item.get("total_questions", 0)
            if item["state"] != "reviewed" or not total or item.get("correct_questions") != total:
                continue
            for course in task["courses"]:
                if course["type"] == "ai" and course["id"] == item.get("course_id"):
                    completed.add((course["id"], course["classId"], item["knowledge_id"]))
    client._completed_ai_knowledge = completed


def run_until_complete(
    journal,
    client,
    config,
    executor,
    *,
    prepare=None,
    discover=None,
    report=None,
    wait=None,
    max_rounds=0,
    retry_delay=30,
    max_retry_delay=300,
    retry_failed=False,
):
    """Zero max_rounds means keep recovering until complete or explicitly stopped."""
    wait = wait or time.sleep
    result = TaskSummary()
    round_number, delay = 0, retry_delay
    previous_progress = None
    selected_ids = {
        task["id"]
        for task in journal.data["tasks"]
        if not retry_failed or task["status"] in {"failed", "pending"}
    }
    while True:
        if round_number:
            journal.data["automation"] = {
                "round": round_number,
                "state": "waiting",
                "retry_in_seconds": delay,
            }
            journal.save()
            if report:
                journal.export(report)
            print(f"仍有未完成内容，{delay:g} 秒后自动恢复；Ctrl+C 可停止")
            wait(delay)
        round_number += 1
        if prepare:
            try:
                prepare()
            except Exception as error:
                logger.warning(f"连接恢复暂未成功：{error}")
                result = TaskSummary(failed=[f"连接恢复失败：{error}"])
                if max_rounds and round_number >= max_rounds:
                    return result
                delay = min(max_retry_delay, max(retry_delay, delay * 2))
                continue
        client.reset_deadlines()
        if any(journal._unresolved_submission(task) for task in journal.data["tasks"]):
            journal.reconcile_submissions(client)
        _restore_completed_knowledge(client, journal)
        journal.data["automation"] = {"round": round_number, "state": "running"}
        old_discovery_errors = list(journal.data.get("discovery_errors", []))
        result = journal.execute(client, config, executor, retry_failed=retry_failed)
        if discover:
            courses, errors = discover()
            journal.extend_courses(courses, errors)
            for error in old_discovery_errors:
                if error in result.failed:
                    result.failed.remove(error)
            result.failed.extend(errors)
        remaining = any(
            task["status"] not in {"succeeded", "skipped"} for task in journal.data["tasks"]
        ) or bool(journal.data.get("discovery_errors"))
        unfinished = (
            any(
                task["status"] not in {"succeeded", "skipped"}
                for task in journal.data["tasks"]
                if task["id"] in selected_ids
            )
            if retry_failed
            else remaining
        )
        journal.data["status"] = "incomplete" if remaining else "finished"
        journal.data["automation"]["state"] = "waiting" if unfinished else "finished"
        journal.save()
        if report:
            journal.export(report)
        print(f"自动执行第 {round_number} 轮：{result}")
        if not unfinished or (max_rounds and round_number >= max_rounds):
            if unfinished and result.exit_code == 0:
                result.failed.append("仍有未完成任务或课程发现错误")
            return result
        progress = (
            sum(task["status"] == "succeeded" for task in journal.data["tasks"]),
            sum(
                item["state"] == "reviewed"
                for task in journal.data["tasks"]
                for item in task.get("submissions", [])
            ),
        )
        delay = retry_delay if progress != previous_progress else min(max_retry_delay, delay * 2)
        previous_progress = progress
        # A new round must refresh server progress instead of replaying a cached context.
        client.context.clear()
        client.courses.clear()
