"""个人设置：称呼、任教年级、出题偏好、出题方式，以及根据出题记录提出的偏好建议。

参考原系统（math-teacher-agent）的"教师偏好"：
- 只保存结构化的偏好，不保存题目正文；
- "偏好学习"默认关闭。开启后只看最近出题记录里的年级、题型、难度和用途，
  同一个选择至少出现 3 次、占到六成以上才提建议；采用之前不改动任何设置；
  不采用的建议，要等同样的选择再多出现 3 次才会重新提；
- 可以导出成 JSON，也可以一次删除全部偏好（出题记录不受影响）。

本机单人使用，存成数据文件夹（config.RUNTIME_DIR）里的 profile.json。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mingti.question_authoring.schemas import GradeLevel, QuestionType

logger = logging.getLogger("mingti.profile")

PURPOSES = ("课堂练习", "课后作业", "单元测验", "高考复习")
RESET_PHRASE = "删除全部偏好"
PROPOSAL_THRESHOLD = 3
PROPOSAL_WINDOW = 20

Purpose = Literal["课堂练习", "课后作业", "单元测验", "高考复习"]
SolutionDetail = Literal["concise", "standard", "detailed"]
AgentMode = Literal["fast", "careful", "strict", "custom"]
ProposalKey = Literal["grade", "question_type", "difficulty", "purpose"]

PROPOSAL_LABELS: dict[str, str] = {
    "grade": "任教年级",
    "question_type": "默认题型",
    "difficulty": "默认难度",
    "purpose": "默认用途",
}
# 出题记录里对应的字段
BRIEF_FIELDS: dict[str, str] = {
    "grade": "grade",
    "question_type": "question_type",
    "difficulty": "target_difficulty",
    "purpose": "teaching_purpose",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Defaults(BaseModel):
    """每次打开界面时输入框的初始选择，以及每道题都附带的要求。"""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType = "single_choice"
    difficulty: int = Field(default=3, ge=1, le=5)
    purpose: Purpose = "课堂练习"
    solution_detail: SolutionDetail = "standard"
    standing_request: str = Field(default="", max_length=300)


class AgentSettings(BaseModel):
    """出题方式：fast/careful/strict 是预设；custom 用下面的方案数、验算和把关模型。"""

    model_config = ConfigDict(extra="forbid")

    mode: AgentMode = "fast"
    # 思考强度：high 在构造题目、审题、规划解法、复核时打开模型的思考模式；max 同样这几步，
    # 但用模型最深的一档思考；medium 只在构造题目、改题和规划解法时打开，审题、复核不开；low 都不开，最快。
    # 4、5 星的题这里选低或中时按 high 出（见 jobs.JobManager.submit）。默认中（用户 11-05 要求）
    thinking: Literal["low", "medium", "high", "max"] = "medium"
    model: str | None = Field(default=None, max_length=120)
    lanes: int = Field(default=1, ge=1, le=3)
    verify: bool = False
    check_model: str | None = Field(default=None, max_length=120)


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    key: ProposalKey
    label: str
    value: str | int
    action: Literal["accept", "reject"]
    count: int
    total: int
    confidence: float
    at: str


class Learning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    feedback: list[Feedback] = Field(default_factory=list)


# 头像：界面把上传的图片裁成正方形、缩到 256×256 以后存成 data URL（几十 KB），空字符串表示没有头像
AVATAR_MAX = 400_000
_AVATAR = re.compile(r"data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+")


def _check_avatar(value: str | None) -> str | None:
    if value and not _AVATAR.fullmatch(value):
        raise ValueError("头像要是 PNG、JPEG、WebP 或 GIF 图片")
    return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 0
    display_name: str = Field(default="", max_length=20)
    avatar: str = Field(default="", max_length=AVATAR_MAX)
    grade: GradeLevel | Literal[""] = ""
    # 教材版本：知识点目录按哪一版整理（人教 A 版 / 人教 B 版，都是 2019 年版）
    textbook: Literal["pep_a", "pep_b"] = "pep_a"
    # 界面：主题（白天 light、夜晚 dark、跟随系统 system）和语言（中文 zh、英文 en；题目内容不翻译）
    theme: Literal["light", "dark", "system"] = "light"
    language: Literal["zh", "en"] = "zh"
    defaults: Defaults = Field(default_factory=Defaults)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    learning: Learning = Field(default_factory=Learning)
    updated_at: str | None = None


class ProfileUpdate(BaseModel):
    """界面一次改一项；没传的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=20)
    avatar: str | None = Field(default=None, max_length=AVATAR_MAX)  # 空字符串 = 去掉头像
    grade: GradeLevel | Literal[""] | None = None
    textbook: Literal["pep_a", "pep_b"] | None = None
    theme: Literal["light", "dark", "system"] | None = None
    language: Literal["zh", "en"] | None = None
    defaults: Defaults | None = None
    agent: AgentSettings | None = None
    learning_enabled: bool | None = None

    @field_validator("avatar")
    @classmethod
    def _avatar_format(cls, value: str | None) -> str | None:
        return _check_avatar(value)


def _current(profile: Profile, key: str):
    if key == "grade":
        return profile.grade
    if key == "question_type":
        return profile.defaults.question_type
    if key == "difficulty":
        return profile.defaults.difficulty
    return profile.defaults.purpose


def _valid(key: str, value) -> bool:
    if key == "grade":
        return value in ("高一", "高二", "高三")
    if key == "question_type":
        return value in ("single_choice", "multiple_choice", "fill_blank", "open_response")
    if key == "difficulty":
        return isinstance(value, int) and 1 <= value <= 5
    return value in PURPOSES


def proposals(profile: Profile, briefs: list[dict]) -> list[dict]:
    """briefs：最近的出题要求，新的在前。只在开启偏好学习时提建议。"""
    if not profile.learning.enabled:
        return []
    found: list[dict] = []
    for key, field in BRIEF_FIELDS.items():
        values = [b.get(field) for b in briefs[:PROPOSAL_WINDOW]]
        values = [v for v in values if v not in (None, "")]
        if not values:
            continue
        value, count = Counter(values).most_common(1)[0]
        total = len(values)
        if count < PROPOSAL_THRESHOLD or count / total < 0.6:
            continue
        if not _valid(key, value) or value == _current(profile, key):
            continue
        rejected = [
            f.count
            for f in profile.learning.feedback
            if f.key == key and f.value == value and f.action == "reject"
        ]
        if rejected and count < max(rejected) + PROPOSAL_THRESHOLD:
            continue
        # 出现得越多、占比越高，把握越大；封顶 0.95，留出"可能看错"的余地
        confidence = min(0.95, (count / total) * (1 - 0.5 ** (count / 2)))
        found.append(
            {
                "id": f"{key}:{value}",
                "key": key,
                "label": PROPOSAL_LABELS[key],
                "value": value,
                "count": count,
                "total": total,
                "confidence": round(confidence, 2),
            }
        )
    return found


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self) -> Profile:
        if not self.path.exists():
            return Profile()
        try:
            return Profile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            logger.warning("个人设置文件读不出来，按默认设置处理：%s", exc)
            return Profile()

    def _save(self, profile: Profile) -> Profile:
        saved = profile.model_copy(update={"version": profile.version + 1, "updated_at": _now()})
        temp = self.path.with_suffix(".tmp")
        temp.write_text(saved.model_dump_json(indent=2), encoding="utf-8")
        temp.replace(self.path)
        return saved

    def update(self, changes: ProfileUpdate) -> Profile:
        with self._lock:
            data = self.load().model_dump()
            for field, value in changes.model_dump(exclude_none=True).items():
                if field == "learning_enabled":
                    data["learning"]["enabled"] = value
                else:
                    data[field] = value
            return self._save(Profile.model_validate(data))

    def review(self, proposal: dict, action: Literal["accept", "reject"]) -> Profile:
        with self._lock:
            profile = self.load()
            data = profile.model_dump()
            key, value = proposal["key"], proposal["value"]
            if action == "accept":
                if key == "grade":
                    data["grade"] = value
                elif key == "difficulty":
                    data["defaults"]["difficulty"] = value
                else:
                    data["defaults"][key] = value
            data["learning"]["feedback"].insert(
                0,
                {
                    "id": f"{proposal['id']}@{profile.version}",
                    "key": key,
                    "label": proposal["label"],
                    "value": value,
                    "action": action,
                    "count": proposal["count"],
                    "total": proposal["total"],
                    "confidence": proposal["confidence"],
                    "at": _now(),
                },
            )
            data["learning"]["feedback"] = data["learning"]["feedback"][:50]
            return self._save(Profile.model_validate(data))

    def reset(self) -> Profile:
        with self._lock:
            return self._save(Profile(version=self.load().version))

    def export(self) -> dict:
        return json.loads(self.load().model_dump_json())
