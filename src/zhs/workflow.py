"""Build and run course tasks, reporting every outcome to the caller."""

import json
from pathlib import Path

from zhs.common.logging import logger
from zhs.common.runtime import TaskSummary, TimeLimitExceeded
from zhs.config import validate_ai_config


def fetch_courses(client):
    courses, errors = [], []
    sources = (
        ("zhidao", client.getZhidaoList, "secret"),
        ("hike", client.getHikeList, "courseId"),
        ("ai", client.getZhidaoAiList, "courseId"),
    )
    for kind, fetch, id_field in sources:
        try:
            for course in fetch() or []:
                item = {
                    "name": course.get("courseName", ""),
                    "id": str(course[id_field]),
                    "type": kind,
                }
                if kind == "ai":
                    item["classId"] = str(course["classId"])
                courses.append(item)
        except Exception as error:
            logger.exception(error)
            errors.append(f"获取 {kind} 课程失败：{error}")
    return courses, errors


def read_execution(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or any(
        not isinstance(item, dict) or not item.get("id") for item in data
    ):
        raise ValueError("execution.json 必须是包含课程 id 的对象列表")
    for item in data:
        if item.get("type") not in (None, "zhidao", "hike", "ai"):
            raise ValueError(f"未知课程类型：{item.get('type')}")
        if item.get("type") == "ai" and not item.get("classId"):
            raise ValueError("AI 课程缺少 classId")
        item["id"] = str(item["id"])
    return data


def execute_tasks(client, courses, config, no_exam=False, videos=None):
    summary = TaskSummary()
    pending = list(dict.fromkeys(videos or []))
    for course in courses:
        course_id = str(course["id"])
        label = f"{course.get('type', 'auto')}:{course_id}"
        if videos is not None:
            client._startCourse(course_id)
            # Each video belongs to one of the supplied courses. Retry only those
            # not yet handled; never mutate a list while iterating over it.
            for video_id in pending.copy():
                try:
                    client.fuckVideo(course_id, video_id)
                except TimeLimitExceeded as error:
                    summary.skipped.append(f"{label}/{video_id}: {error}")
                    pending.remove(video_id)
                except Exception as error:
                    logger.exception(error)
                else:
                    summary.succeeded.append(f"{label}/{video_id}")
                    pending.remove(video_id)
            continue
        try:
            kind = course.get("type")
            if kind == "ai":
                validate_ai_config(config, no_exam=no_exam)
                client.fuckAiCourse(
                    course_id, course["classId"], aiConfig=config["ai"], no_exam=no_exam
                )
            elif kind == "zhidao":
                client.fuckZhidaoCourse(course_id)
            elif kind == "hike":
                client.fuckHikeCourse(course_id)
            else:
                client.fuckCourse(course_id)
        except TimeLimitExceeded as error:
            summary.skipped.append(f"{label}: {error}")
            print(f"已达到时限：{label}")
        except Exception as error:
            logger.exception(error)
            summary.failed.append(f"{label}: {error}")
            print(f"任务失败：{label}: {error}")
        else:
            summary.succeeded.append(label)
    summary.failed.extend(f"未能处理视频：{video_id}" for video_id in pending)
    return summary
