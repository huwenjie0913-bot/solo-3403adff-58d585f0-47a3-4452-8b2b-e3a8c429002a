"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


# ---------------------------------------------------------------- 项目

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    frame_rate: float = Field(25.0, gt=0, description="帧率，用于时间轴帧对齐")
    shot_cuts: list[float | str] = Field(
        default_factory=list,
        description="镜头切点：数字按毫秒，字符串支持 HH:MM:SS.mmm",
    )
    rule_template_id: int | None = Field(None, description="引用的规则模板 id")
    rules: Rules | None = Field(None, description="内联规则（优先于模板）")


class ProjectOut(BaseModel):
    id: int
    name: str
    frame_rate: float
    shot_cuts: list[int]
    rules: Rules
    rule_template_id: int | None
    version_count: int
    created_at: datetime


# ---------------------------------------------------------------- 字幕版本

class VersionCreate(BaseModel):
    label: str = Field("v1", min_length=1, max_length=100)
    content: str = Field(min_length=1, description="SRT 或 WebVTT 字幕全文")
    format: Literal["auto", "srt", "vtt"] = Field("auto", description="字幕格式，auto 为自动识别")


class CueOut(BaseModel):
    index: int
    start_ms: int
    end_ms: int
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
    frame_rate: float
    shot_cuts: list[int]
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
