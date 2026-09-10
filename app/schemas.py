"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------- 规则

class Rules(BaseModel):
    """一套可配置的字幕规范。"""

    max_cps: float = Field(20.0, gt=0, description="每秒最大字符数（阅读速度上限）")
    max_chars_per_line: int = Field(40, gt=0, description="单行最大字符数")
    max_lines: int = Field(2, gt=0, description="单条字幕最大行数")
    min_duration_ms: int = Field(1000, ge=0, description="最短显示时间（毫秒）")
    max_duration_ms: int = Field(7000, gt=0, description="最长显示时间（毫秒）")
    min_gap_ms: int = Field(100, ge=0, description="相邻字幕最小间隔（毫秒）")
    max_offset_ms: int = Field(500, ge=0, description="自动修复允许的单点时间偏移上限（毫秒）")
    shot_tolerance_ms: int = Field(0, ge=0, description="镜头切点容差（毫秒），容差内不算跨镜头")


# ---------------------------------------------------------------- 规则模板

class RuleTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rules: Rules = Field(default_factory=Rules)


class RuleTemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    rules: Rules | None = None


class RuleTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    rules: Rules
    created_at: datetime


# ---------------------------------------------------------------- 时间基

class TimebaseSpec(BaseModel):
    """结构化时间基设置；与旧字段 frame_rate 二选一。"""

    fps: float | int | str = Field(
        description="帧率：24 / 25 / 30 / 24000/1001 / 30000/1001 / 60000/1001，"
                    "或任意正浮点（仅 non-drop-frame）")
    drop_frame: bool = Field(False, description="drop-frame（仅 30000/1001、60000/1001 可用）")
    start_timecode: str | None = Field(
        None, description="起始时间码 HH:MM:SS:FF（NDF）或 HH:MM:SS;FF（DF），缺省 00:00:00:00")


class TimebaseOut(BaseModel):
    fps: float = Field(description="帧率浮点近似（旧字段，继续可用）")
    fps_label: str = Field(description="帧率预设标签或 num/den 精确值")
    rate_num: int
    rate_den: int
    drop_frame: bool
    start_timecode: str


# ---------------------------------------------------------------- 项目

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    frame_rate: float | None = Field(
        None, gt=0,
        description="旧字段：浮点帧率（non-drop-frame）；与 timebase 二选一")
    timebase: TimebaseSpec | None = Field(None, description="结构化时间基（推荐）")
    shot_cuts: list[float | int | str | dict] = Field(
        default_factory=list,
        description="镜头切点：数字/纯数字字符串按毫秒；'NNNf' 为帧号；"
                    "'HH:MM:SS:FF' / 'HH:MM:SS;FF' 为 SMPTE 时间码；"
                    "也支持 {ms|frame|timecode} 对象与旧格式 HH:MM:SS.mmm",
    )
    rule_template_id: int | None = Field(None, description="引用的规则模板 id")
    rules: Rules | None = Field(None, description="内联规则（优先于模板）")

    @model_validator(mode="after")
    def _check_frame_source(self) -> "ProjectCreate":
        if self.frame_rate is not None and self.timebase is not None:
            raise ValueError("frame_rate 与 timebase 只能提供其一")
        return self


class ProjectOut(BaseModel):
    id: int
    name: str
    frame_rate: float = Field(description="精确帧率的浮点近似（旧字段）")
    timebase: TimebaseOut
    shot_cuts: list[int] = Field(description="镜头切点（时间线帧号）")
    shot_cuts_ms: list[int] = Field(description="镜头切点（毫秒，半向上取整，旧字段）")
    rules: Rules
    rule_template_id: int | None
    version_count: int
    created_at: datetime


# ---------------------------------------------------------------- 字幕版本

class Position(BaseModel):
    """显式位置对象：ms / frame / timecode 三选一。"""

    ms: float | int | None = Field(None, description="毫秒（半向上取整到帧）")
    frame: int | None = Field(None, description="时间线绝对帧号（相对起始时间码）")
    timecode: str | None = Field(None, description="HH:MM:SS:FF 或 HH:MM:SS;FF")

    @model_validator(mode="after")
    def _exactly_one(self) -> "Position":
        present = [k for k in ("ms", "frame", "timecode") if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError("位置必须且只能提供 ms、frame、timecode 三者之一")
        return self


# 数字 -> 毫秒；字符串 -> 时间码/'NNNf' 帧号/毫秒时间戳；对象 -> Position
PositionLike = float | int | str | Position


class CueInput(BaseModel):
    """结构化 cue。start/end 接受毫秒数字、'NNNf'、SMPTE 时间码或位置对象。"""

    start: PositionLike = Field(description="开始位置：毫秒 / 帧号 / HH:MM:SS:FF / HH:MM:SS;FF")
    end: PositionLike
    lines: list[str] | str = Field(description="文本行；字符串按单行处理")
    identifier: str | None = Field(None, description="cue 标识符（如 WebVTT identifier）")
    settings: str | None = Field(None, description="cue 设置（位置/对齐等）")

    @model_validator(mode="after")
    def _normalize_lines(self) -> "CueInput":
        if isinstance(self.lines, str):
            self.lines = [self.lines]
        return self


class VersionCreate(BaseModel):
    label: str = Field("v1", min_length=1, max_length=100)
    content: str | None = Field(None, description="SRT 或 WebVTT 字幕全文（与 cues 二选一）")
    cues: list[CueInput] | None = Field(None, description="结构化 cue 列表（与 content 二选一）")
    format: Literal["auto", "srt", "vtt"] = Field(
        "auto", description="content 的字幕格式，auto 为自动识别（仅 content 上传时使用）")

    @model_validator(mode="after")
    def _check_source(self) -> "VersionCreate":
        if (self.content is None) == (self.cues is None):
            raise ValueError("content 与 cues 必须且只能提供其一")
        if self.cues is not None and not self.cues:
            raise ValueError("cues 不能为空")
        return self


class CueOut(BaseModel):
    index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    lines: list[str]
    identifier: str | None = None
    settings: str | None = None


class VersionOut(BaseModel):
    id: int
    project_id: int
    label: str
    format: str
    cue_count: int
    created_at: datetime


class VersionDetail(VersionOut):
    cues: list[CueOut]


# ---------------------------------------------------------------- 质检

class FixCandidate(BaseModel):
    action: str = Field(description="修正动作类型，如 extend_end / trim_end / split_cue / rewrap")
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class Issue(BaseModel):
    cue_index: int
    issue_type: str = Field(description="问题类型，如 overlap / flash / shot_cross / cps_exceeded")
    severity: Literal["error", "warning"]
    message: str = Field(description="问题原因")
    details: dict[str, Any] = Field(default_factory=dict)
    fix_candidates: list[FixCandidate] = Field(default_factory=list)


class QCReport(BaseModel):
    project_id: int
    version_id: int
    generated_at: datetime
    rules: Rules
    timebase: TimebaseOut
    shot_cuts: list[int] = Field(description="镜头切点（时间线帧号）")
    summary: dict[str, Any]
    issues: list[Issue]


# ---------------------------------------------------------------- 自动修复

class AutoFixRequest(BaseModel):
    label: str | None = Field(None, max_length=100, description="新版本标签，缺省自动命名")


class AutoFixResponse(BaseModel):
    new_version_id: int
    label: str
    applied: list[dict[str, Any]] = Field(description="已应用的修复（cue_index 为原版本序号）")
    conflicts: list[dict[str, Any]] = Field(description="无法消解的冲突项，对应字幕保留原稿")
    summary: dict[str, Any]


# ---------------------------------------------------------------- 版本比较

class DiffResponse(BaseModel):
    project_id: int
    from_version: int
    to_version: int
    summary: dict[str, Any]
    changes: list[dict[str, Any]]


# ---------------------------------------------------------------- 时间码换算

class ConvertRequest(BaseModel):
    items: list[PositionLike] = Field(description="待换算位置列表")


class StandaloneConvertRequest(BaseModel):
    """无状态换算：时间基与待换算位置同一请求体。"""

    fps: float | int | str = Field(
        description="帧率：24 / 25 / 30 / 24000/1001 / 30000/1001 / 60000/1001，或任意正浮点")
    drop_frame: bool = False
    start_timecode: str | None = None
    items: list[Any] = Field(description="待换算位置：毫秒数字 / 'NNNf' / SMPTE 时间码 / 位置对象")


class ConvertedPosition(BaseModel):
    input: Any
    frame: int
    ms: int
    ms_exact: str = Field(description="精确毫秒（分数 num/den，无舍入）")
    timecode: str


# ---------------------------------------------------------------- 帧级 JSON 导出

class FrameCueOut(BaseModel):
    index: int
    start_frame: int
    end_frame: int
    duration_frames: int
    start_ms: int
    end_ms: int
    duration_ms: int
    start_ms_exact: str
    end_ms_exact: str
    start_tc: str
    end_tc: str
    duration_tc_frames: int
    lines: list[str]
    identifier: str | None = None
    settings: str | None = None


class FrameExport(BaseModel):
    project_id: int
    version_id: int
    label: str
    format: str
    timebase: TimebaseOut
    cue_count: int
    shot_cuts: list[int]
    cues: list[FrameCueOut]
