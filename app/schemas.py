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
        if self.preferred_translation is not None:
            self.preferred_translation = self.preferred_translation.strip()
            if not self.preferred_translation:
                raise ValueError("preferred_translation 不能为空")
        if self.acceptable_variants is not None:
            self.acceptable_variants = _clean_variants(self.acceptable_variants)
        if self.forbidden_variants is not None:
            self.forbidden_variants = _clean_variants(self.forbidden_variants)
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
