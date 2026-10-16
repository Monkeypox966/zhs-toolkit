"""Validate platform payloads before they become executable work."""

import math


class ProtocolError(ValueError):
    pass


def ai_resource_is_video(resource):
    detail = resource.get("resourcesDetail") or {}
    return (detail.get("resourcesType"), detail.get("resourcesDistributeType")) in {
        (1, 3),
        (2, 2),
    }


def ai_resource_complete(resource):
    """Trust the platform's completion flag; its video rule is over 80%, not 100%."""
    if not isinstance(resource, dict) or not isinstance(resource.get("resourcesDetail"), dict):
        raise ProtocolError("AI 资源缺少有效详情")
    status = resource.get("studyStatus")
    return not isinstance(status, bool) and status == 1


def ai_video_resume_at(resource):
    detail = resource.get("resourcesDetail") or {}
    value = detail.get("lastWatchTime")
    if value is None:
        value = resource.get("studyTotalTime", 0)
    try:
        value = float(value)
    except TypeError, ValueError:
        return 0
    return int(value) if math.isfinite(value) and value > 0 else 0


def identifier(value, label="id"):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ProtocolError(f"{label} 必须是非空字符串或整数")
    result = str(value).strip()
    if any(char in result for char in ("/", "\\", "\0")) or result in {".", ".."}:
        raise ProtocolError(f"{label} 包含无效字符")
    return result


def course_record(value, require_type=False):
    if not isinstance(value, dict):
        raise ProtocolError("课程记录必须是对象")
    result = dict(value)
    result["id"] = identifier(value.get("id"), "course id")
    kind = value.get("type")
    if kind not in (None, "zhidao", "hike", "ai") or (require_type and kind is None):
        raise ProtocolError(f"无效或缺失的课程类型：{kind}")
    if kind == "ai":
        result["classId"] = identifier(value.get("classId"), "classId")
    return result


def sheet_questions(payload):
    try:
        parts = payload["data"]["partSheetVos"]
    except (KeyError, TypeError) as error:
        raise ProtocolError("试卷响应缺少 partSheetVos") from error
    if not isinstance(parts, list) or not parts:
        raise ProtocolError("试卷没有有效分段")
    questions, seen = [], set()
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("questionSheetVos"), list):
            raise ProtocolError("试卷分段缺少 questionSheetVos")
        for question in part["questionSheetVos"]:
            if not isinstance(question, dict):
                raise ProtocolError("题目概要必须是对象")
            question_id = identifier(question.get("questionId"), "questionId")
            version = question.get("version", 1)
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ProtocolError("题目版本必须是正整数")
            if question_id in seen:
                raise ProtocolError(f"试卷出现重复题目：{question_id}")
            seen.add(question_id)
            questions.append(dict(question, version=version))
    if not questions:
        raise ProtocolError("试卷没有题目")
    return questions


def validate_answers(question, answers):
    if (
        not isinstance(question, dict)
        or isinstance(question.get("questionType"), bool)
        or question.get("questionType") not in (1, 2, 14)
    ):
        raise ProtocolError("不支持的题目类型")
    options = question.get("optionVos")
    if not isinstance(options, list) or not options:
        raise ProtocolError("题目没有可用选项")
    allowed = {}
    for option in options:
        if not isinstance(option, dict) or "content" not in option:
            raise ProtocolError("题目选项格式无效")
        key = identifier(option.get("id"), "option id")
        if key in allowed:
            raise ProtocolError("题目包含重复选项 ID")
        allowed[key] = option["id"]
    if not isinstance(answers, list) or not answers:
        raise ProtocolError("答案不能为空")
    normalized = [identifier(answer, "answer id") for answer in answers]
    if len(set(normalized)) != len(normalized):
        raise ProtocolError("答案包含重复选项")
    if any(answer not in allowed for answer in normalized):
        raise ProtocolError("答案包含不属于当前题目的选项")
    if question["questionType"] in (1, 14) and len(normalized) != 1:
        raise ProtocolError("单选题或判断题必须且只能选择一个选项")
    return [allowed[answer] for answer in normalized]
