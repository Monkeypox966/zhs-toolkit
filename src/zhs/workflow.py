"""Build and run course tasks, reporting every outcome to the caller."""

import json
import math
from pathlib import Path

from zhs.common.contracts import ProtocolError, course_record
from zhs.common.logging import logger
from zhs.common.runtime import (
    CourseResolutionError,
    OperationFailed,
    PendingAnswerError,
    TaskSummary,
    TimeLimitExceeded,
)
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
                    "id": course[id_field],
                    "type": kind,
                }
                if kind == "ai":
                    item["classId"] = course["classId"]
                courses.append(course_record(item, require_type=True))
        except Exception as error:
            logger.exception(error)
            errors.append(f"获取 {kind} 课程失败：{error}")
    return courses, errors


def resolve_courses(client, requested):
    requested = [course_record(item) for item in requested]
    unknown = [item for item in requested if item.get("type") is None]
    if not unknown:
        return requested
    catalog, errors = fetch_courses(client)
    result = []
    for item in requested:
        if item.get("type"):
            result.append(item)
            continue
        matches = [course for course in catalog if course["id"] == item["id"]]
        distinct = {(c["type"], c.get("classId")) for c in matches}
        if len(distinct) != 1 or errors:
            detail = "；".join(errors)
            raise CourseResolutionError(
                f"无法唯一确定课程 {item['id']} 的类型，请刷新课程清单或使用 -ai COURSE_ID CLASS_ID。{detail}"
            )
        result.append(matches[0])
    return result


def read_execution(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("execution.json 必须是包含课程 id 的对象列表")
    return [course_record(item) for item in data]


def verify_course_completion(client, course):
    """Re-read platform progress instead of trusting a successful write alone."""

    def progress_number(value):
        try:
            number = float(value)
        except (ValueError, TypeError) as error:
            raise ProtocolError("平台学习进度不是数值") from error
        if isinstance(value, bool) or not math.isfinite(number) or number < 0:
            raise ProtocolError("平台学习进度无效")
        return number

    kind, course_id = course.get("type"), course["id"]
    if kind == "ai":
        data = client.getAiKnowlegePoints(course_id, course["classId"])
        themes = data.get("cakeThemeList")
        if not isinstance(themes, list):
            raise ProtocolError("课程进度缺少主题列表")
        for theme in themes:
            nodes = theme.get("knowledgeList")
            if not isinstance(nodes, list):
                raise ProtocolError("课程进度缺少知识点列表")
            for node in nodes:
                progress = node.get("studyProgress")
                if isinstance(progress, bool) or not isinstance(progress, (int, float)):
                    raise ProtocolError("知识点进度无效")
                if progress_number(progress) < 100:
                    return False
        return True
    if kind == "zhidao":
        videos = client.getZhidaoContext(course_id, force=True).videos
        return all(
            video.get("watchState") == 1
            or (
                isinstance(video.get("videoSec"), (int, float))
                and video["videoSec"] > 0
                and progress_number(video.get("studyTotalTime") or 0)
                >= progress_number(video["videoSec"])
            )
            for video in videos.values()
        )
    if kind == "hike":

        def complete(node):
            children = node.get("childList")
            if children:
                return all(complete(child) for child in children)
            if node.get("dataType") == 3:
                return progress_number(node.get("studyTime") or 0) >= progress_number(
                    node["totalTime"]
                )
            # Non-video resources are acknowledged by stuViewFile during traversal.
            return node.get("dataType") is not None

        return all(complete(node) for node in client.getHikeContext(course_id, force=True).root)
    raise ProtocolError("完成状态校验需要明确的课程类型")


def execute_tasks(
    client,
    courses,
    config,
    no_exam=False,
    videos=None,
    reset_video_deadlines=True,
    verify_completion=False,
):
    summary = TaskSummary()
    pending = list(dict.fromkeys(videos or []))
    video_errors = {}
    video_limits = {}
    for course in courses:
        course_id = str(course["id"])
        label = f"{course.get('type', 'auto')}:{course_id}"
        if videos is not None:
            if reset_video_deadlines:
                client._startCourse(course_id)
            else:
                client._startCourseIfNeeded(course_id)
            # Each video belongs to one of the supplied courses. Retry only those
            # not yet handled; never mutate a list while iterating over it.
            for video_id in pending.copy():
                try:
                    if course.get("type"):
                        client.fuckVideo(
                            course_id,
                            video_id,
                            course_type=course["type"],
                            class_id=course.get("classId"),
                        )
                    else:
                        client.fuckVideo(course_id, video_id)
                except TimeLimitExceeded as error:
                    video_limits[video_id] = f"{label}/{video_id}: {error}"
                except PendingAnswerError as error:
                    summary.pending.append(f"{label}/{video_id}: {error}")
                    summary.error_types.append(type(error).__name__)
                    pending.remove(video_id)
                except Exception as error:
                    logger.exception(error)
                    video_errors[video_id] = (type(error).__name__, str(error))
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
                client.fuckCourse(course_id, aiConfig=config.get("ai", {}), no_exam=no_exam)
            if verify_completion and not verify_course_completion(client, course):
                raise OperationFailed("平台尚未确认全部学习进度，将自动复查并继续未完成内容")
        except TimeLimitExceeded as error:
            summary.skipped.append(f"{label}: {error}")
            print(f"已达到时限：{label}")
        except PendingAnswerError as error:
            summary.pending.append(f"{label}: {error}")
            summary.error_types.append(type(error).__name__)
            print(f"任务待处理：{label}: {error}")
        except Exception as error:
            logger.exception(error)
            summary.failed.append(f"{label}: {error}")
            summary.error_types.append(type(error).__name__)
            print(f"任务失败：{label}: {error}")
        else:
            summary.succeeded.append(label)
    for video_id in pending:
        if video_id in video_limits:
            summary.skipped.append(video_limits[video_id])
            continue
        error_type, detail = video_errors.get(video_id, ("CourseResolutionError", "没有可用课程"))
        summary.failed.append(f"未能处理视频：{video_id}: {detail}")
        summary.error_types.append(error_type)
    return summary
