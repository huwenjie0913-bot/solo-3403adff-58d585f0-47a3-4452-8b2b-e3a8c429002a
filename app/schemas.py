"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# 标签/ASS 覆盖标记：术语译法若包含这类内容，替换时会向字幕注入标签或换行
_TERM_STRUCT_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _reject_structural_text(label: str, value: str) -> None:
    """术语文本只能是单行纯文本：含换行或标签会改变字幕结构，拒绝。"""
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label}不能包含换行符（替换会改变字幕原有换行结构）")
    if _TERM_STRUCT_RE.search(value):
        raise ValueError(
            f"{label}不能包含标签标记（如 <i>…</i>、{{\\an8}}），"
            f"替换会向字幕注入标签")


def _reject_structural_variants(label: str, variants: list[str]) -> None:
    for i, v in enumerate(variants):
        _reject_structural_text(f"{label}[{i}]（{v!r}）", v)


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
    origin_version_id: int | None = Field(
        None, description="派生来源版本 id（自动修复/对齐产生的新版本），原稿为 None")
    provenance: dict[str, Any] | None = Field(
        None, description="派生信息（来源类型、对齐参数、已应用候选等）")
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


# ---------------------------------------------------------------- 双语对齐

class AlignRequest(BaseModel):
    """把同一项目中的两个版本指定为源语言与译文，进行轨道对齐检查。"""

    source_version_id: int = Field(description="源语言字幕版本 id")
    target_version_id: int = Field(description="译文字幕版本 id")
    min_overlap_ms: int = Field(
        200, ge=0, description="判定匹配的最小重叠（毫秒，按项目时间基换算为帧，至少 1 帧）")
    min_overlap_ratio: float = Field(
        0.3, ge=0, le=1, description="重叠比例阈值：任一侧低于该值报“重叠不足”")
    speaker_check: bool = Field(True, description="是否检查说话人标签一致性")
    candidate_strategy: Literal["source_boundaries", "proportional"] = Field(
        "source_boundaries",
        description="候选策略：source_boundaries=以源 cue 边界拆分/合并/对齐；"
                    "proportional=组内按比例映射到源跨度")


class AlignThresholds(BaseModel):
    min_overlap_ms: int
    min_overlap_frames: int = Field(description="min_overlap_ms 换算后的帧数（向上取整，至少 1 帧）")
    min_overlap_ratio: float
    speaker_check: bool
    candidate_strategy: str


class AlignCueRef(BaseModel):
    index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    speakers: list[str] = Field(default_factory=list)


class AlignSide(BaseModel):
    """映射组的一侧（源或译文）：跨度 + 组内各 cue。"""

    indices: list[int]
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    speakers: list[str]
    cues: list[AlignCueRef]


class AlignIssue(BaseModel):
    issue_type: str = Field(
        description="missing_translation / unmatched_target / insufficient_overlap / "
                    "order_inversion / speaker_mismatch")
    severity: Literal["error", "warning"]
    message: str = Field(description="问题原因")
    details: dict[str, Any] = Field(default_factory=dict)


class AlignCandidate(BaseModel):
    id: str = Field(description="候选 id（m{映射号}.c{序号}），apply 接口按 id 选定")
    action: str = Field(
        description="retime_to_source / merge_to_source / "
                    "split_at_source_boundaries / fit_group_to_source")
    description: str
    params: dict[str, Any] = Field(
        default_factory=dict, description="修复参数（帧号 + 毫秒 + SMPTE 时间码）")


class MappingOut(BaseModel):
    id: int
    type: str = Field(description="one_to_one / one_to_many / many_to_one / many_to_many")
    source: AlignSide
    target: AlignSide
    overlap_frames: int
    overlap_ms: int
    overlap_ratio_source: float = Field(description="重叠帧数 / 源组跨度")
    overlap_ratio_target: float = Field(description="重叠帧数 / 译文组跨度")
    issues: list[AlignIssue]
    fix_candidates: list[AlignCandidate]


class UnmatchedCueOut(BaseModel):
    cue: AlignCueRef
    issue_type: str = Field(description="missing_translation（译文漏条）/ unmatched_target")
    severity: str
    message: str


class AlignReport(BaseModel):
    project_id: int
    source_version_id: int
    target_version_id: int
    generated_at: datetime
    timebase: TimebaseOut
    thresholds: AlignThresholds
    summary: dict[str, Any]
    mappings: list[MappingOut]
    unmatched_source: list[UnmatchedCueOut] = Field(description="译文漏条（源侧未匹配）")
    unmatched_target: list[UnmatchedCueOut] = Field(description="译文侧未匹配")


class AlignApplyRequest(BaseModel):
    """把选定候选应用到译文版本，保存为关联原版本的新字幕版本。"""

    source_version_id: int
    target_version_id: int
    label: str | None = Field(None, max_length=100, description="新版本标签，缺省自动命名")
    candidate_ids: list[str] | None = Field(
        None, description="选定的候选 id 列表；缺省（null）应用全部候选，空列表表示不应用")
    min_overlap_ms: int = Field(200, ge=0)
    min_overlap_ratio: float = Field(0.3, ge=0, le=1)
    speaker_check: bool = True
    candidate_strategy: Literal["source_boundaries", "proportional"] = "source_boundaries"


class AlignApplyResponse(BaseModel):
    new_version_id: int
    label: str
    origin_version_id: int = Field(description="新版本的派生来源（译文版本 id）")
    applied: list[dict[str, Any]] = Field(description="已应用的候选")
    summary: dict[str, Any]


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


# ---------------------------------------------------------------- 双语术语表

TermSeverity = Literal["error", "warning"]


class TerminologyRuleCreate(BaseModel):
    """术语表条目：源语词条 + 首选译法 + 可接受/禁用变体。"""

    source_term: str = Field(min_length=1, max_length=200, description="源语词条")
    preferred_translation: str = Field(
        min_length=1, max_length=200, description="首选译法（修复候选的替换文本）")
    acceptable_variants: list[str] = Field(
        default_factory=list, description="可接受变体（命中不算错，但不计为首选）")
    forbidden_variants: list[str] = Field(
        default_factory=list, description="禁用变体（命中报“禁用变体”并建议改为首选译法）")
    case_sensitive: bool = Field(
        False, description="大小写敏感：false 时忽略大小写匹配（大小写错误单独报告）")
    whole_word: bool = Field(
        True, description="整词匹配：true 时拉丁词只匹配整词边界（CJK 不受影响）")
    severity: TermSeverity = Field(
        "error", description="该词条问题的严重级别（error / warning）")
    note: str | None = Field(None, max_length=500, description="备注")

    @model_validator(mode="after")
    def _normalize(self) -> "TerminologyRuleCreate":
        self.source_term = self.source_term.strip()
        self.preferred_translation = self.preferred_translation.strip()
        self.acceptable_variants = _clean_variants(self.acceptable_variants)
        self.forbidden_variants = _clean_variants(self.forbidden_variants)
        if not self.source_term:
            raise ValueError("source_term 不能为空")
        if not self.preferred_translation:
            raise ValueError("preferred_translation 不能为空")
        # 术语文本为单行纯文本：含换行/标签会让替换改变字幕结构
        _reject_structural_text("source_term", self.source_term)
        _reject_structural_text("preferred_translation", self.preferred_translation)
        _reject_structural_variants("acceptable_variants", self.acceptable_variants)
        _reject_structural_variants("forbidden_variants", self.forbidden_variants)
        overlap = sorted(set(self.forbidden_variants)
                         & (set(self.acceptable_variants) | {self.preferred_translation}))
        if overlap:
            raise ValueError(f"变体不能同时为禁用与可接受/首选：{', '.join(overlap)}")
        return self


class TerminologyRuleUpdate(BaseModel):
    """局部更新；变体字段给定时整体替换。"""

    source_term: str | None = Field(None, min_length=1, max_length=200)
    preferred_translation: str | None = Field(None, min_length=1, max_length=200)
    acceptable_variants: list[str] | None = None
    forbidden_variants: list[str] | None = None
    case_sensitive: bool | None = None
    whole_word: bool | None = None
    severity: TermSeverity | None = None
    note: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _normalize(self) -> "TerminologyRuleUpdate":
        if self.source_term is not None:
            self.source_term = self.source_term.strip()
            if not self.source_term:
                raise ValueError("source_term 不能为空")
            _reject_structural_text("source_term", self.source_term)
        if self.preferred_translation is not None:
            self.preferred_translation = self.preferred_translation.strip()
            if not self.preferred_translation:
                raise ValueError("preferred_translation 不能为空")
            _reject_structural_text(
                "preferred_translation", self.preferred_translation)
        if self.acceptable_variants is not None:
            self.acceptable_variants = _clean_variants(self.acceptable_variants)
            _reject_structural_variants(
                "acceptable_variants", self.acceptable_variants)
        if self.forbidden_variants is not None:
            self.forbidden_variants = _clean_variants(self.forbidden_variants)
            _reject_structural_variants(
                "forbidden_variants", self.forbidden_variants)
        return self


def _clean_variants(variants: list[str]) -> list[str]:
    """去空白、去空串、去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


class TerminologyRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    source_term: str
    preferred_translation: str
    acceptable_variants: list[str]
    forbidden_variants: list[str]
    case_sensitive: bool
    whole_word: bool
    severity: str
    note: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- 双语术语一致性检查

class TerminologyCheckRequest(BaseModel):
    source_version_id: int = Field(description="源语言字幕版本 id")
    target_version_id: int = Field(description="译文字幕版本 id")
    min_overlap_ms: int = Field(
        200, ge=0, description="时间重叠映射的最小重叠（毫秒），复用双语对齐阈值")
    min_overlap_ratio: float = Field(
        0.3, ge=0, le=1, description="重叠比例阈值（低于该值的映射组仍核对术语，但标记重叠不足）")
    rule_ids: list[int] | None = Field(
        None, description="只使用指定术语条目；缺省（null）使用项目全部条目")


class TermThresholds(BaseModel):
    min_overlap_ms: int
    min_overlap_frames: int = Field(description="换算后的帧数（向上取整，至少 1 帧）")
    min_overlap_ratio: float
    rule_count: int = Field(description="本次检查实际使用的术语条目数")


class TermFragment(BaseModel):
    """一个实际命中片段及其在原文中的位置（按去标签可见文本计）。"""

    text: str = Field(description="实际片段（命中的源词/译法/变体原文）")
    cue_index: int = Field(description="所在 cue 序号（该侧版本内）")
    line_index: int = Field(description="所在行序号（cue 内）")
    start: int = Field(description="片段在该行去标签可见文本中的起始字符位置")
    end: int = Field(description="片段结束字符位置（不含）")
    context: str = Field(description="去标签后的上下文片段（命中处前后各取若干字）")


class TermCueRef(BaseModel):
    """cue 引用：帧号 + 毫秒 + SMPTE 时间码 + 行文本。"""

    index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    lines: list[str]


class TermFixCandidate(BaseModel):
    id: str = Field(description="候选 id（t{组号}.{规则id}.{序号}），预览/应用按 id 选定")
    action: str = Field(description="replace_term：不改时间码/标签/换行的精确替换")
    description: str
    cue_index: int = Field(description="待替换译文 cue 序号")
    line_index: int
    start: int = Field(description="替换起点（该行原始文本字符位置）")
    end: int = Field(description="替换终点（不含）")
    found: str = Field(description="实际片段（与原文逐字一致才可应用）")
    replacement: str = Field(description="替换文本（首选译法，与源词大小写形式一致）")
    preview_line: str = Field(description="替换后该行预览")


class TermIssue(BaseModel):
    issue_type: str = Field(
        description="term_untranslated / term_inconsistent / "
                    "term_forbidden_variant / term_case_error")
    severity: str
    message: str = Field(description="字段化原因说明")
    rule: TerminologyRuleOut
    mapping_id: int | None = Field(
        None, description="对应的时间重叠映射组号；未匹配源 cue 为 null")
    source_cues: list[TermCueRef] = Field(description="该组源侧 cue")
    target_cues: list[TermCueRef] = Field(description="该组译文侧 cue（未匹配源 cue 为空）")
    source_fragments: list[TermFragment] = Field(
        default_factory=list, description="源词实际命中片段")
    target_fragments: list[TermFragment] = Field(
        default_factory=list, description="译文实际命中片段（禁用变体/错误大小写/不一致译法）")
    reasons: dict[str, Any] = Field(
        default_factory=dict, description="字段化原因（期望/实际/标准译法/各组用法…）")
    fix_candidates: list[TermFixCandidate] = Field(default_factory=list)


class TerminologyReport(BaseModel):
    project_id: int
    source_version_id: int
    target_version_id: int
    generated_at: datetime
    timebase: TimebaseOut
    thresholds: TermThresholds
    rules: list[TerminologyRuleOut] = Field(description="本次检查使用的术语规则快照")
    summary: dict[str, Any]
    mapping_count: int = Field(description="时间重叠映射组数（复用双语对齐）")
    issues: list[TermIssue]


class TerminologyPreviewItem(BaseModel):
    candidate_id: str
    cue_index: int
    line_index: int = Field(description="替换所在行（cue 内行号，0 起）")
    before_line: str = Field(description="替换前行（原文，含标签/换行不变）")
    after_line: str = Field(description="替换后行预览")
    found: str
    replacement: str


class TerminologyPreviewRequest(BaseModel):
    source_version_id: int
    target_version_id: int
    candidate_ids: list[str] = Field(description="待预览的候选 id 列表")
    min_overlap_ms: int = Field(200, ge=0)
    min_overlap_ratio: float = Field(0.3, ge=0, le=1)
    rule_ids: list[int] | None = None


class TerminologyPreviewResponse(BaseModel):
    items: list[TerminologyPreviewItem]


class TerminologyApplyRequest(BaseModel):
    source_version_id: int
    target_version_id: int
    candidate_ids: list[str] | None = Field(
        None, description="选定的候选 id；缺省（null）应用全部可修复候选，空列表表示不应用")
    label: str | None = Field(None, max_length=100, description="新版本标签，缺省自动命名")
    min_overlap_ms: int = Field(200, ge=0)
    min_overlap_ratio: float = Field(0.3, ge=0, le=1)
    rule_ids: list[int] | None = None


class TerminologyApplyResponse(BaseModel):
    new_version_id: int
    label: str
    origin_version_id: int = Field(description="新版本的派生来源（译文版本 id）")
    applied: list[dict[str, Any]] = Field(description="已应用的精确替换")
    summary: dict[str, Any]


# ---------------------------------------------------------------- 剪辑改版字幕重套

ConformStrategy = Literal["move", "trim", "split", "merge", "manual"]


class ConformSegmentInput(BaseModel):
    """一条剪辑映射：源时间线区间 -> 改版（目标）时间线区间。

    目标区间起止相等（或均不提供）表示**删除段**：该段源素材在改版中被剪掉。
    位置接受毫秒数字 / 'NNNf' 帧号 / SMPTE 时间码 / 位置对象。
    """

    source_start: PositionLike = Field(description="源时间线起点（毫秒 / 帧号 / 时间码）")
    source_end: PositionLike = Field(description="源时间线终点（毫秒 / 帧号 / 时间码）")
    target_start: PositionLike | None = Field(
        None, description="改版时间线起点；与 target_end 同时省略表示删除段")
    target_end: PositionLike | None = Field(
        None, description="改版时间线终点；与 target_start 相等表示删除段")

    @model_validator(mode="after")
    def _check_target_pair(self) -> "ConformSegmentInput":
        if (self.target_start is None) != (self.target_end is None):
            raise ValueError("target_start 与 target_end 必须同时提供，或同时省略（删除段）")
        return self


class ConformRequest(BaseModel):
    """按剪辑映射把原字幕版本重套到改版时间线（检查 + 生成计划与候选，不落库）。"""

    version_id: int = Field(description="原字幕版本 id")
    segments: list[ConformSegmentInput] = Field(
        ..., min_length=1, description="剪辑映射段（按源时间线先后给出）")
    cut_tolerance_ms: int = Field(
        0, ge=0, description="切点容差：cue 边界距切点不超过该值视为未跨切点")
    min_duration_ms: int = Field(
        1000, ge=0, description="重套后最短显示时间；变速压缩导致更短时报“时长过短”")
    merge_gap_ms: int = Field(
        0, ge=0, description="合并间隔：改版后相邻 cue 间隔不超过该值时给出合并候选")
    cross_segment_strategy: ConformStrategy = Field(
        "move",
        description="跨段默认处理策略：move=移动 / trim=裁切 / split=按标点拆分 / "
                    "merge=合并相邻 / manual=转人工")


class ConformOptionsOut(BaseModel):
    cut_tolerance_ms: int
    cut_tolerance_frames: int
    min_duration_ms: int
    min_duration_frames: int
    merge_gap_ms: int
    merge_gap_frames: int
    cross_segment_strategy: ConformStrategy


class ConformSegmentOut(BaseModel):
    id: int = Field(description="映射段序号（按请求中的顺序，1 起，候选/快照引用此 id）")
    source: dict[str, Any]
    target: dict[str, Any] | None = Field(None, description="删除段为 null")
    deleted: bool
    source_duration_frames: int
    target_duration_frames: int
    ratio_num: int = Field(description="变速比例分子（目标帧/源帧，1/1 为原速）")
    ratio_den: int
    retimed: bool = Field(description="是否变速片段（比例 != 1）")


class ConformMapIssue(BaseModel):
    issue_type: str = Field(
        description="source_overlap / order_reversed / target_conflict / "
                    "unmapped_source / unmapped_target")
    severity: Literal["error", "warning"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConformCueRef(BaseModel):
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


class ConformPartOut(BaseModel):
    """cue 在源时间线上覆盖的一个片段：映射段或未映射（删除）区间。"""

    kind: Literal["kept", "deleted_segment", "unmapped_gap"]
    segment_id: int | None = Field(None, description="命中的映射段 id；未映射区间为 null")
    source: dict[str, Any] = Field(description="cue 落在该段内的源区间（帧/毫秒/时间码）")
    target: dict[str, Any] | None = Field(None, description="映射后的改版区间；删除/未映射为 null")
    overlap_frames: int = Field(description="cue 与该片段的源侧重叠帧数")


class ConformCueIssue(BaseModel):
    issue_type: str = Field(
        description="cross_cut / in_deleted_segment / duration_too_short / target_cue_overlap")
    severity: Literal["error", "warning"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConformCandidateOut(BaseModel):
    id: str = Field(description="候选 id（q{cue 序号}.c{候选序号}），preview/apply 按 id 选定")
    action: str = Field(description="move / trim / split_at_punctuation / merge_adjacent / manual")
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class ConformMappedCue(BaseModel):
    """默认策略下 cue 重套后的一条结果（改版时间线）。"""

    cue_index: int = Field(description="来源 cue 序号（拆分时多条结果指向同一 cue）")
    segment_id: int | None = Field(None, description="映射来源段 id；人工处理为 null")
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    ratio_num: int | None = None
    ratio_den: int | None = None
    needs_manual: bool = False


class ConformCueResult(BaseModel):
    status: Literal["direct", "cross_cut", "in_deleted"]
    cue: ConformCueRef
    snapped_start: bool = Field(description="起点是否按切点容差吸附到切点")
    snapped_end: bool
    parts: list[ConformPartOut] = Field(description="cue 覆盖的源片段（含删除/未映射区间）")
    issues: list[ConformCueIssue]
    mapped_cues: list[ConformMappedCue] = Field(
        description="默认跨段策略下的重套结果（直接重套时为 1 条）")
    fix_candidates: list[ConformCandidateOut] = Field(default_factory=list)
    proposed_candidate_id: str | None = Field(
        None, description="默认策略选定的候选 id；直接重套/无候选为 null")


class ConformReport(BaseModel):
    project_id: int
    version_id: int
    generated_at: datetime
    timebase: TimebaseOut
    options: ConformOptionsOut
    mapping_valid: bool = Field(
        description="映射段是否通过 error 级校验；false 时不生成 cue 重套结果")
    segments: list[ConformSegmentOut]
    mapping_issues: list[ConformMapIssue]
    summary: dict[str, Any]
    cues: list[ConformCueResult] = Field(
        default_factory=list, description="逐条 cue 的重套计划（mapping_valid=false 时为空）")


class _ConformSelectionBase(BaseModel):
    version_id: int
    segments: list[ConformSegmentInput]
    cut_tolerance_ms: int = 0
    min_duration_ms: int = 1000
    merge_gap_ms: int = 0
    cross_segment_strategy: ConformStrategy = "move"


class ConformPreviewRequest(_ConformSelectionBase):
    candidate_ids: list[str] | None = Field(
        None, description="选定的候选 id；缺省（null）用默认策略候选，空列表表示全部转人工")


class ConformPreviewOutcome(BaseModel):
    action: str = Field(description="direct_retime / move / trim / split_at_punctuation / "
                                    "merge_adjacent / manual")
    candidate_id: str | None = None
    segment_id: int | None
    before: dict[str, Any]
    after: dict[str, Any]
    ratio_num: int | None = None
    ratio_den: int | None = None
    needs_manual: bool = False
    lines: list[str]


class ConformPreviewItem(BaseModel):
    cue_index: int
    before: dict[str, Any] = Field(description="改版前时间码（原版本）")
    outcomes: list[ConformPreviewOutcome] = Field(
        description="改版后结果（拆分/合并时可能为多条；合并时含相邻 cue 文本）")
    merged_with: list[int] = Field(
        default_factory=list, description="合并候选时被并入的相邻 cue 序号")


class ConformPreviewResponse(BaseModel):
    project_id: int
    version_id: int
    timebase: TimebaseOut
    options: ConformOptionsOut
    items: list[ConformPreviewItem]


class ConformApplyRequest(_ConformSelectionBase):
    label: str | None = Field(None, max_length=100, description="新版本标签，缺省自动命名")
    candidate_ids: list[str] | None = Field(
        None, description="选定的候选 id；缺省（null）用默认策略候选，空列表表示全部转人工")


class ConformApplyResponse(BaseModel):
    new_version_id: int
    label: str
    origin_version_id: int = Field(description="新版本的派生来源（原字幕版本 id）")
    applied: list[dict[str, Any]]
    summary: dict[str, Any]


# ---------------------------------------------------------------- 字幕样式兼容性校核

StyleTargetFormat = Literal["srt", "vtt"]

# 各目标格式的缺省渲染配置（与常见播放器实现对齐）
DEFAULT_SRT_TAGS = ["i", "b", "u", "font"]
DEFAULT_VTT_TAGS = ["i", "b", "u", "c", "v", "lang", "ruby", "rt", "font"]
DEFAULT_TAG_ATTRIBUTES: dict[str, list[str]] = {
    "font": ["color", "face", "size"],
    "c": ["class"],
    "v": ["voice"],
    "lang": ["lang"],
}
DEFAULT_CUE_SETTINGS = ["vertical", "line", "position", "size", "align", "region"]


class RenderConfig(BaseModel):
    """目标播放器渲染配置：格式、允许的内联标签/属性、嵌套深度、说话人、settings。

    允许标签/属性/cue settings 省略时按目标格式给缺省值（SRT 仅 i/b/u/font，
    不允许说话人标签与 cue settings；WebVTT 允许完整内部标签集与全部 settings）。
    """

    target_format: StyleTargetFormat = Field(
        description="目标播放器字幕格式：srt / vtt")
    allowed_tags: list[str] | None = Field(
        None, description="允许的内联标签白名单（小写，如 i、b、u、font、c、v、lang）")
    allowed_attributes: dict[str, list[str]] = Field(
        default_factory=dict,
        description="按标签声明允许的属性，如 {\"font\": [\"color\"], \"c\": [\"class\"]}")
    max_nesting_depth: int = Field(
        3, ge=0, description="内联标签最大嵌套深度（0 表示不允许任何嵌套）")
    allow_speaker_tags: bool | None = Field(
        None, description="是否允许说话人标签（<v …>）；缺省 SRT=false、VTT=true")
    allowed_cue_settings: list[str] | None = Field(
        None, description="WebVTT cue settings 可用字段；缺省 SRT=[]、VTT=全部六个")

    @model_validator(mode="after")
    def _apply_defaults(self) -> "RenderConfig":
        defaults = (DEFAULT_SRT_TAGS if self.target_format == "srt"
                    else DEFAULT_VTT_TAGS)
        if self.allowed_tags is None:
            self.allowed_tags = list(defaults)
        else:
            self.allowed_tags = [t.lower() for t in self.allowed_tags]
        if self.allow_speaker_tags is None:
            self.allow_speaker_tags = self.target_format == "vtt"
        if self.allowed_cue_settings is None:
            self.allowed_cue_settings = (
                list(DEFAULT_CUE_SETTINGS) if self.target_format == "vtt" else [])
        else:
            self.allowed_cue_settings = [s.lower() for s in self.allowed_cue_settings]
        self.allowed_attributes = {
            k.lower(): [a.lower() for a in v]
            for k, v in (self.allowed_attributes or {}).items()}
        # 用户未显式声明的标签按各格式常规属性集给缺省
        for tag, attrs in DEFAULT_TAG_ATTRIBUTES.items():
            if tag in self.allowed_tags and tag not in self.allowed_attributes:
                self.allowed_attributes[tag] = list(attrs)
        return self

    @classmethod
    def default_srt(cls) -> "RenderConfig":
        return cls(target_format="srt")

    @classmethod
    def default_vtt(cls) -> "RenderConfig":
        return cls(target_format="vtt")


class StylePosition(BaseModel):
    """行列位置：行序号（cue 内 0 起）与行内字符偏移。"""

    line_index: int
    start: int = Field(description="行内起始字符偏移（含标签的原始文本）")
    end: int = Field(description="行内结束字符偏移（不含）")


class StyleFragment(BaseModel):
    """问题涉及的可见文本片段（位置按去标签可见文本计）。"""

    line_index: int
    text: str
    start: int
    end: int


class StyleFixCandidate(BaseModel):
    id: str = Field(description="候选 id（s{cue 序号}.c{序号}），预览/应用按 id 选定")
    action: str = Field(
        description="repair_structure / unwrap_tag / remove_attribute / "
                    "remove_override / remove_timestamp_tag / remove_setting / "
                    "drop_settings")
    description: str
    cue_index: int
    preview_lines: list[str] = Field(description="应用后的行预览（时间码不变）")
    preview_settings: str | None = None
    preview_identifier: str | None = None
    preserves: list[str] = Field(
        description="应用后保持不变的语义：timecode / visible_text / line_breaks")


class StyleIssue(BaseModel):
    cue_index: int
    issue_type: str = Field(
        description="unclosed_tag / stray_closing_tag / crossed_tag / style_cross_line / "
                    "nesting_too_deep / unknown_attribute / conflicting_style / "
                    "unsupported_tag / speaker_not_allowed / unsupported_override / "
                    "unsupported_timestamp_tag / unsupported_setting / "
                    "invalid_setting_value / settings_unsupported / "
                    "identifier_unsupported / malformed_tag / roundtrip_drift")
    severity: Literal["error", "warning"]
    message: str = Field(description="问题原因")
    positions: list[StylePosition] = Field(
        default_factory=list, description="行列位置（标签/属性在原始行中的偏移）")
    fragments: list[StyleFragment] = Field(
        default_factory=list, description="涉及的可见文本片段")
    details: dict[str, Any] = Field(default_factory=dict)
    fix_candidates: list[StyleFixCandidate] = Field(default_factory=list)


class StyleCueRef(BaseModel):
    index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    lines: list[str] = Field(description="原始行（含标签）")
    visible_lines: list[str] = Field(description="去标签后的可见文本行")
    identifier: str | None = None
    settings: str | None = None


class StyleCueResult(BaseModel):
    cue: StyleCueRef
    issues: list[StyleIssue]


class StyleCheckRequest(BaseModel):
    version_id: int
    config: RenderConfig


class StyleReport(BaseModel):
    project_id: int
    version_id: int
    generated_at: datetime
    source_format: str = Field(description="版本原始格式（srt / vtt / json）")
    target_format: str = Field(description="渲染配置声明的目标格式")
    timebase: TimebaseOut
    config: RenderConfig = Field(description="本次校核使用的渲染配置快照")
    summary: dict[str, Any]
    cues: list[StyleCueResult] = Field(description="逐条 cue 的诊断与修复候选")


class _StyleSelectionBase(BaseModel):
    version_id: int
    config: RenderConfig


class StylePreviewRequest(_StyleSelectionBase):
    candidate_ids: list[str] = Field(description="待预览的候选 id 列表")


class StylePreviewItem(BaseModel):
    candidate_id: str
    cue_index: int
    action: str
    description: str
    before_lines: list[str]
    before_settings: str | None = None
    before_identifier: str | None = None
    after_lines: list[str]
    after_settings: str | None = None
    after_identifier: str | None = None
    preserves: list[str]


class StylePreviewResponse(BaseModel):
    items: list[StylePreviewItem]


class StyleApplyRequest(_StyleSelectionBase):
    candidate_ids: list[str] | None = Field(
        None, description="选定的候选 id；缺省（null）应用全部可修复候选，空列表表示不应用")
    label: str | None = Field(None, max_length=100, description="新版本标签，缺省自动命名")
    output_format: Literal["auto", "srt", "vtt"] = Field(
        "auto", description="新版本序列格式；auto 取渲染配置目标格式")


class StyleApplyResponse(BaseModel):
    new_version_id: int
    label: str
    origin_version_id: int = Field(description="新版本的派生来源（被校核版本 id）")
    output_format: str
    applied: list[dict[str, Any]]
    summary: dict[str, Any]
