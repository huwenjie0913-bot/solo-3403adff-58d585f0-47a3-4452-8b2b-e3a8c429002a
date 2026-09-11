"""剪辑改版字幕重套（re-conform）。

给定原字幕版本与源时间线 → 改版（目标）时间线的剪辑映射段，校验映射本身
（源侧重叠、输入倒序、目标段冲突、未映射区间），再按映射重算每条 cue 的
时间：

- 保留段（段内）字幕按有理数比例换算到改版时间线，变速片段（比例 != 1）
  用 ``Fraction`` 精确换算并落到合法整数帧；
- 完全落在删除段/未映射区间的 cue 进入人工处理候选，默认不删除；
- 跨切点、覆盖多个映射段的 cue 返回字段化诊断与候选：移动、裁切、按标点
  拆分、合并相邻片段、转人工；
- 重套后过短（低于最短显示时间）、改版时间线上相邻 cue 重叠同样给出诊断。

候选 id 在相同输入下重算稳定（``q{cue 序号}.c{候选序号}``）。全部时间
运算在帧域进行（整数帧号 + 有理数换算），候选参数同时给出帧号、毫秒与
SMPTE 时间码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .autofix import _CLAUSE_ENDERS, _SENT_ENDERS, _balance_tags, _split_positions
from .parsing import Cue, join_lines, visible_len
from .timecode import Timebase, ceil_pos, format_smpte, half_up

# ---------------------------------------------------------------- 选项与映射段

@dataclass(frozen=True)
class ConformOptions:
    """重套参数（已换算到帧域）。"""

    cut_tolerance_frames: int
    min_duration_frames: int
    merge_gap_frames: int
    strategy: str  # move / trim / split / merge / manual

    @classmethod
    def from_ms(cls, cut_tolerance_ms: int, min_duration_ms: int,
                merge_gap_ms: int, strategy: str, tb: Timebase) -> "ConformOptions":
        def conv(ms: int) -> int:
            return ceil_pos(Fraction(ms) * tb.rate / 1000)
        return cls(conv(cut_tolerance_ms), max(1, conv(min_duration_ms)),
                   conv(merge_gap_ms), strategy)


@dataclass(frozen=True)
class Segment:
    """一条剪辑映射段（帧域）。target 为 None 表示删除段。"""

    id: int
    s0: int
    s1: int
    t0: int | None
    t1: int | None

    @property
    def deleted(self) -> bool:
        return self.t0 is None

    @property
    def src_len(self) -> int:
        return self.s1 - self.s0

    @property
    def tgt_len(self) -> int:
        return self.t1 - self.t0 if self.t0 is not None and self.t1 is not None else 0

    @property
    def ratio(self) -> Fraction:
        """变速比例（目标帧 / 源帧）；删除段为 0。"""
        if self.deleted or self.src_len == 0:
            return Fraction(0)
        return Fraction(self.tgt_len, self.src_len)

    def map_frame(self, frame: int) -> int:
        """源帧按有理数比例映射到改版帧（半向上取整到合法帧）。"""
        if self.deleted:
            raise ValueError("删除段没有目标时间")
        if frame <= self.s0:
            return self.t0
        if frame >= self.s1:
            return self.t1
        return self.t0 + half_up(Fraction(frame - self.s0) * self.ratio)


# ---------------------------------------------------------------- cue 片段划分

@dataclass
class Part:
    """cue 在源时间线上覆盖的一个片段。"""

    kind: str                 # kept / deleted_segment / unmapped_gap
    segment_id: int | None
    a: int                    # 源区间（帧）
    b: int
    t0: int | None = None     # 映射后的改版区间
    t1: int | None = None

    @property
    def overlap(self) -> int:
        return self.b - self.a


def _parts_of(cue: Cue, segs: list[Segment], tol: int
              ) -> tuple[list[Part], bool, bool]:
    """把 cue 区间按映射段切成片段列表，并做切点容差吸附。

    返回 (parts, snapped_start, snapped_end)。片段按源时间顺序；未被任何
    映射段覆盖的部分计为 unmapped_gap，删除段计为 deleted_segment。
    """
    start, end = cue.start_frame, cue.end_frame
    snapped_s = snapped_e = False
    boundaries = sorted({b for s in segs for b in (s.s0, s.s1)})
    for b in boundaries:
        if 0 < abs(start - b) <= tol:
            start, snapped_s = b, True
        if 0 < abs(end - b) <= tol:
            end, snapped_e = b, True

    hit = sorted((s for s in segs if s.s1 > start and s.s0 < end),
                 key=lambda s: s.s0)
    parts: list[Part] = []
    cur = start
    for s in hit:
        a, b = max(cur, s.s0), min(end, s.s1)
        if a >= b:
            continue
        if a > cur:
            parts.append(Part("unmapped_gap", None, cur, a))
        if s.deleted:
            parts.append(Part("deleted_segment", s.id, a, b))
        else:
            parts.append(Part("kept", s.id, a, b,
                              s.map_frame(a), s.map_frame(b)))
        cur = b
    if cur < end:
        parts.append(Part("unmapped_gap", None, cur, end))
    if not parts:
        parts.append(Part("unmapped_gap", None, start, end))
    return parts, snapped_s, snapped_e


# ---------------------------------------------------------------- 映射段校验

@dataclass
class MapIssue:
    issue_type: str
    severity: str
    message: str
    details: dict = field(default_factory=dict)


def _span_timing(a: int, b: int, tb: Timebase) -> dict:
    return {
        "start_frame": a, "end_frame": b,
        "start_ms": tb.frames_to_ms(a), "end_ms": tb.frames_to_ms(b),
        "start_tc": format_smpte(tb, a), "end_tc": format_smpte(tb, b),
    }


def validate_segments(segs: list[Segment], tb: Timebase
                      ) -> tuple[bool, list[MapIssue]]:
    """校验剪辑映射：倒序、源侧重叠、目标段冲突、未映射区间。

    返回 (是否通过 error 级校验, 问题列表)。warning 不阻断重套。
    """
    issues: list[MapIssue] = []

    # 1) 输入倒序：相邻段（按输入顺序）源起点不增
    for a, b in zip(segs, segs[1:]):
        if b.s0 < a.s0:
            issues.append(MapIssue(
                "order_reversed", "error",
                f"映射段 {b.id} 源起点 {format_smpte(tb, b.s0)} 早于前段 {a.id} "
                f"的起点 {format_smpte(tb, a.s0)}，输入顺序倒置",
                {"segment_id": b.id, "previous_segment_id": a.id,
                 "previous": _span_timing(a.s0, a.s1, tb),
                 "current": _span_timing(b.s0, b.s1, tb)}))

    # 2) 源侧重叠（含删除段）；端点相接允许
    for i, a in enumerate(segs):
        for b in segs[i + 1:]:
            lo, hi = max(a.s0, b.s0), min(a.s1, b.s1)
            if lo < hi:
                issues.append(MapIssue(
                    "source_overlap", "error",
                    f"映射段 {a.id} 与 {b.id} 的源区间重叠 {hi - lo} 帧"
                    f"（{format_smpte(tb, lo)}–{format_smpte(tb, hi)}）",
                    {"segment_ids": [a.id, b.id],
                     "overlap": _span_timing(lo, hi, tb)}))

    # 3) 目标段冲突：两条保留段映射到同一改版区间
    kept = [s for s in segs if not s.deleted]
    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            lo, hi = max(a.t0, b.t0), min(a.t1, b.t1)
            if lo < hi:
                issues.append(MapIssue(
                    "target_conflict", "error",
                    f"映射段 {a.id} 与 {b.id} 的改版（目标）区间重叠 "
                    f"{hi - lo} 帧（{format_smpte(tb, lo)}–"
                    f"{format_smpte(tb, hi)}），同一改版位置被映射了两次",
                    {"segment_ids": [a.id, b.id],
                     "overlap": _span_timing(lo, hi, tb)}))

    # 4) 未映射区间：源时间线上相邻段之间的空隙（未被其它段覆盖）
    ordered = sorted(segs, key=lambda s: (s.s0, s.s1))
    for a, b in zip(ordered, ordered[1:]):
        if a.s1 < b.s0 and not any(
                x.s0 <= a.s1 and x.s1 >= b.s0 for x in segs if x is not a):
            gap = b.s0 - a.s1
            issues.append(MapIssue(
                "unmapped_source", "warning",
                f"源时间线 {format_smpte(tb, a.s1)}–{format_smpte(tb, b.s0)} "
                f"（{gap} 帧）不在任何映射段内，该区间素材视为删除；落入其中的 "
                f"cue 将报“落入删除/未映射区间”",
                {"between_segments": [a.id, b.id],
                 "gap": _span_timing(a.s1, b.s0, tb)}))

    # 5) 改版时间线空隙：源侧严格相邻（中间无删除段）的保留段映射后不衔接，
    #    说明改版插入了黑场/新镜头
    src_sorted = sorted(kept, key=lambda s: (s.s0, s.s1))
    for a, b in zip(src_sorted, src_sorted[1:]):
        between = [x for x in segs
                   if x.s0 >= a.s1 and x.s1 <= b.s0 and x.id != a.id]
        if a.s1 == b.s0 and not between and a.t1 < b.t0:
            gap = b.t0 - a.t1
            issues.append(MapIssue(
                "unmapped_target", "warning",
                f"改版时间线 {format_smpte(tb, a.t1)}–{format_smpte(tb, b.t0)} "
                f"（{gap} 帧）没有任何源段映射（黑场或新增镜头）",
                {"between_segments": [a.id, b.id],
                 "gap": _span_timing(a.t1, b.t0, tb)}))

    valid = not any(i.severity == "error" for i in issues)
    return valid, issues


# ---------------------------------------------------------------- cue 诊断与候选

@dataclass
class Cand:
    id: str
    action: str
    description: str
    params: dict = field(default_factory=dict)
    manual: bool = False


@dataclass
class Row:
    """一条重套结果（改版时间线）。"""

    cue_index: int
    segment_id: int | None
    start_frame: int
    end_frame: int
    lines: list[str]
    identifier: str | None
    settings: str | None
    manual: bool = False
    ratio: Fraction | None = None


@dataclass
class CuePlan:
    cue: Cue
    status: str                 # direct / cross_cut / in_deleted
    parts: list[Part]
    snapped_s: bool
    snapped_e: bool
    issues: list[MapIssue] = field(default_factory=list)
    candidates: list[Cand] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    proposed: Cand | None = None

    @property
    def index(self) -> int:
        return self.cue.index

    @property
    def proposed_id(self) -> str | None:
        return self.proposed.id if self.proposed is not None else None

    def cand(self, cid: str | None) -> Cand | None:
        if not cid:
            return None
        return next((c for c in self.candidates if c.id == cid), None)


def _seg_by_id(segs: list[Segment], sid: int | None) -> Segment | None:
    if sid is None:
        return None
    return next((s for s in segs if s.id == sid), None)


def _split_punctuation(text: str, n: int) -> list[str] | None:
    """把文本按句末/从句标点拆成 n 段（标签闭合）；无合适标点返回 None。"""
    if n <= 1:
        return [text] if visible_len(text) else None
    pos = _split_positions(text, _SENT_ENDERS)
    if len(pos) + 1 < n:
        pos = sorted(set(pos + _split_positions(text, _CLAUSE_ENDERS)))
    if len(pos) + 1 < n:
        return None
    pieces: list[str] = []
    last = 0
    for p in pos[: n - 1]:
        seg = text[last:p].strip()
        if not seg or not visible_len(seg):
            return None
        pieces.append(seg)
        last = p
    tail = text[last:].strip()
    if not tail or not visible_len(tail):
        return None
    pieces.append(tail)
    if len(pieces) != n:
        return None
    return _balance_tags(pieces)


def _extend_within(a: int, b: int, need: int, lo: int, hi: int
                   ) -> tuple[int, int]:
    """在 [lo,hi] 内把 [a,b] 延长到至少 need 帧：优先向后，再整体前移。"""
    if b - a >= need:
        return a, b
    nb = min(hi, a + need)
    na = a
    if nb - na < need:
        na = max(lo, nb - need)
    return na, nb


def _manual_candidate(cue: Cue) -> Cand:
    return Cand("", "manual",
                f"第 {cue.index} 条转人工处理：保留原时间码并标记，待人工确认",
                {"cue_index": cue.index}, manual=True)


def _build_plan(cue: Cue, parts: list[Part], snapped_s: bool, snapped_e: bool,
                segs: list[Segment], opts: ConformOptions, tb: Timebase
                ) -> CuePlan:
    kept_parts = [p for p in parts if p.kind == "kept"]
    deleted_parts = [p for p in parts if p.kind != "kept"]
    plan = CuePlan(cue=cue, status="direct", parts=parts,
                   snapped_s=snapped_s, snapped_e=snapped_e)

    # ---- 完全落在删除段/未映射区间 ----
    if not kept_parts:
        plan.status = "in_deleted"
        kind = ("删除段" if any(p.kind == "deleted_segment" for p in deleted_parts)
                else "未映射区间")
        plan.issues.append(MapIssue(
            "in_deleted_segment", "error",
            f"第 {cue.index} 条字幕完全落入{kind}"
            f"（{format_smpte(tb, parts[0].a)}–{format_smpte(tb, parts[-1].b)}），"
            f"改版后无对应位置",
            {"parts": [{"kind": p.kind, "segment_id": p.segment_id,
                        **_span_timing(p.a, p.b, tb)} for p in parts]}))
        _candidates_deleted(plan, segs, opts, tb)
        _resolve_default(plan, segs, opts, tb)
        return plan

    # ---- 跨切点 / 多段 ----
    multi = len({p.segment_id for p in kept_parts}) > 1
    if multi or deleted_parts:
        plan.status = "cross_cut"
        names = []
        if multi:
            names.append(f"跨越 {len({p.segment_id for p in kept_parts})} 个保留段")
        if deleted_parts:
            names.append("覆盖到删除/未映射区间")
        plan.issues.append(MapIssue(
            "cross_cut", "error",
            f"第 {cue.index} 条字幕{'、'.join(names)}，不能直接整体重套",
            {"segment_ids": sorted({p.segment_id for p in kept_parts}),
             "deleted_parts": [{"kind": p.kind, "segment_id": p.segment_id,
                                **_span_timing(p.a, p.b, tb)}
                               for p in deleted_parts]}))
        _candidates_cross(plan, kept_parts, cue, segs, opts, tb)
    else:
        p = kept_parts[0]
        dur = p.t1 - p.t0
        if dur < opts.min_duration_frames:
            plan.issues.append(MapIssue(
                "duration_too_short", "warning",
                f"第 {cue.index} 条重套后仅 {dur} 帧，短于最短显示时间 "
                f"{opts.min_duration_frames} 帧（{tb.frames_to_ms(dur)}ms < "
                f"{tb.frames_to_ms(opts.min_duration_frames)}ms）",
                {"duration_frames": dur,
                 "min_duration_frames": opts.min_duration_frames,
                 "segment_id": p.segment_id}))
        _candidates_direct(plan, p, segs, opts, tb)

    _resolve_default(plan, segs, opts, tb)
    return plan


def _candidates_direct(plan: CuePlan, part: Part, segs: list[Segment],
                       opts: ConformOptions, tb: Timebase) -> None:
    """保留段内 cue：默认直接重套；过短时给移动候选；并附人工候选。"""
    cue = plan.cue
    seg = _seg_by_id(segs, part.segment_id)
    a, b = part.t0, part.t1
    if b - a < opts.min_duration_frames:
        na, nb = _extend_within(a, b, opts.min_duration_frames, seg.t0, seg.t1)
        if (na, nb) != (a, b) and nb > na:
            plan.candidates.append(Cand(
                "", "move",
                f"移动并延长到最短显示时间 {format_smpte(tb, na)}–"
                f"{format_smpte(tb, nb)}",
                {"cue_index": cue.index, "segment_id": seg.id,
                 "from": _span_timing(a, b, tb),
                 "to": _span_timing(na, nb, tb)}))
    plan.candidates.append(_manual_candidate(cue))


def _candidates_cross(plan: CuePlan, kept_parts: list[Part], cue: Cue,
                      segs: list[Segment], opts: ConformOptions,
                      tb: Timebase) -> None:
    # 主保留段：重叠最大的保留部分
    main = max(kept_parts, key=lambda p: p.overlap)
    main_seg = _seg_by_id(segs, main.segment_id)

    # 1) 移动：整条移到主保留段的映射区间
    plan.candidates.append(Cand(
        "", "move",
        f"整条移动到第 {main_seg.id} 段对应改版区间 "
        f"{format_smpte(tb, main.t0)}–{format_smpte(tb, main.t1)}"
        f"（主保留 {main.overlap} 帧）",
        {"cue_index": cue.index, "segment_id": main_seg.id,
         "from": _span_timing(cue.start_frame, cue.end_frame, tb),
         "to": _span_timing(main.t0, main.t1, tb)}))

    # 2) 裁切：只保留主段重叠
    dropped = cue.duration_frames - main.overlap
    plan.candidates.append(Cand(
        "", "trim",
        f"裁切到第 {main_seg.id} 段 {format_smpte(tb, main.t0)}–"
        f"{format_smpte(tb, main.t1)}（丢弃 {dropped} 帧对应的部分）",
        {"cue_index": cue.index, "segment_id": main_seg.id,
         "keep_source": _span_timing(main.a, main.b, tb),
         "to": _span_timing(main.t0, main.t1, tb)}))

    # 3) 按标点拆分：仅当覆盖 >=2 个保留段（删除洞不能承载文本）；
    #    每个保留段的映射区间一条
    if len(kept_parts) >= 2:
        pieces = _split_punctuation(join_lines(cue.lines), len(kept_parts))
        if pieces is not None:
            plan.candidates.append(Cand(
                "", "split_at_punctuation",
                f"按标点拆分为 {len(kept_parts)} 条，分别落到第 "
                f"{'、'.join(str(p.segment_id) for p in kept_parts)} 段",
                {"cue_index": cue.index,
                 "segments": [{"segment_id": p.segment_id,
                               "source": _span_timing(p.a, p.b, tb),
                               "to": _span_timing(p.t0, p.t1, tb)}
                              for p in kept_parts],
                 "text_pieces": pieces}))

    # 4) 人工
    plan.candidates.append(_manual_candidate(cue))


def _candidates_deleted(plan: CuePlan, segs: list[Segment],
                        opts: ConformOptions, tb: Timebase) -> None:
    cue = plan.cue
    kept = [s for s in segs if not s.deleted]
    if kept:
        seg = min(kept, key=lambda s: min(abs(cue.start_frame - s.s0),
                                          abs(cue.end_frame - s.s1)))
        a = seg.t0 if abs(cue.start_frame - seg.s0) <= abs(cue.end_frame - seg.s1) \
            else seg.t1
        a = min(max(a, seg.t0), max(seg.t0, seg.t1 - 1))
        b = min(a + opts.min_duration_frames, seg.t1)
        if b > a:
            plan.candidates.append(Cand(
                "", "move",
                f"移动到最近的保留段（第 {seg.id} 段）改版位置 "
                f"{format_smpte(tb, a)}–{format_smpte(tb, b)}",
                {"cue_index": cue.index, "segment_id": seg.id,
                 "to": _span_timing(a, b, tb)}))
    plan.candidates.append(_manual_candidate(cue))


def _rows_for_candidate(plan: CuePlan, cand: Cand) -> list[Row]:
    cue = plan.cue
    if cand.manual:
        return [Row(cue.index, None, cue.start_frame, cue.end_frame,
                    list(cue.lines), cue.identifier, cue.settings, manual=True)]
    if cand.action in ("move", "trim"):
        to = cand.params["to"]
        return [Row(cue.index, cand.params.get("segment_id"),
                    to["start_frame"], to["end_frame"],
                    list(cue.lines), cue.identifier, cue.settings)]
    if cand.action == "split_at_punctuation":
        return [
            Row(cue.index, seg_info["segment_id"],
                seg_info["to"]["start_frame"], seg_info["to"]["end_frame"],
                [piece], cue.identifier, cue.settings)
            for seg_info, piece in zip(cand.params["segments"],
                                       cand.params["text_pieces"])]
    if cand.action == "merge_adjacent":
        to = cand.params["to"]
        return [Row(cue.index, cand.params.get("segment_id"),
                    to["start_frame"], to["end_frame"],
                    list(cue.lines), cue.identifier, cue.settings)]
    return []


def _resolve_default(plan: CuePlan, segs: list[Segment], opts: ConformOptions,
                     tb: Timebase) -> None:
    """按默认跨段策略确定 proposed 候选与结果行。"""
    cue = plan.cue
    by_action = {c.action: c for c in plan.candidates}

    if plan.status == "in_deleted":
        # 删除/未映射 cue 默认人工（绝不自动丢弃字幕）
        cand = by_action.get("manual")
        plan.rows = _rows_for_candidate(plan, cand)
        plan.proposed = cand
        return

    if plan.status == "direct":
        p = next(x for x in plan.parts if x.kind == "kept")
        plan.rows = [Row(cue.index, p.segment_id, p.t0, p.t1,
                         list(cue.lines), cue.identifier, cue.settings,
                         ratio=_seg_by_id(segs, p.segment_id).ratio)]
        # 仅当策略为 move 且过短、存在移动候选时才默认改选
        cand = (by_action.get("move")
                if (opts.strategy == "move"
                    and p.t1 - p.t0 < opts.min_duration_frames) else None)
        if cand is not None:
            plan.rows = _rows_for_candidate(plan, cand)
            plan.proposed = cand
        return

    # cross_cut：策略 -> 候选动作（merge 的合并候选在第二遍补充）
    action_map = {"move": "move", "trim": "trim",
                  "split": "split_at_punctuation",
                  "merge": "merge_adjacent", "manual": "manual"}
    wanted = action_map.get(opts.strategy, "move")
    cand = by_action.get(wanted)
    if cand is None:  # split 无标点候选时退化为 move；merge 待第二遍
        cand = by_action.get("move")
    plan.proposed = cand
    plan.rows = _rows_for_candidate(plan, cand) if cand else []


# ---------------------------------------------------------------- 合并候选（第二遍）

def _add_merge_candidates(plans: list[CuePlan], segs: list[Segment],
                          opts: ConformOptions, tb: Timebase) -> None:
    """对默认结果相邻的源 cue 给出合并候选。

    改版时间线上相邻（间隔 <= merge_gap，含重叠）且源序号相邻、都非人工的
    一对 cue 可合并。贪心配对，每条 cue 至多参与一个合并候选。
    """
    by_index = {pl.index: pl for pl in plans}

    def single_row(pl: CuePlan) -> Row | None:
        rs = [r for r in pl.rows if not r.manual]
        return rs[0] if len(rs) == 1 else None

    used: set[int] = set()
    ordered = sorted(by_index)
    for k, idx in enumerate(ordered):
        if idx in used:
            continue
        r = single_row(by_index[idx])
        if r is None:
            continue
        # 源序号相邻的下一条 cue（它同样单条、非人工、改版位置不在前面）
        nxt_row = None
        for jdx in ordered[k + 1:]:
            nxt_row = single_row(by_index[jdx])
            if nxt_row is not None:
                break
        if nxt_row is None or nxt_row.start_frame < r.start_frame:
            continue
        jdx = next(j for j in ordered[k + 1:]
                   if single_row(by_index[j]) is nxt_row)
        gap = nxt_row.start_frame - r.end_frame
        if gap > opts.merge_gap_frames:
            continue
        plan, other = by_index[idx], by_index[jdx]
        a, b = r.start_frame, nxt_row.end_frame
        if b <= a:
            continue
        mc = Cand("", "merge_adjacent",
                  f"与第 {jdx} 条合并为一条（改版后间隔 {gap} 帧），"
                  f"合并区间 {format_smpte(tb, a)}–{format_smpte(tb, b)}",
                  {"cue_index": idx, "merge_with": jdx,
                   "segment_id": r.segment_id,
                   "to": _span_timing(a, b, tb)})
        plan.candidates.append(mc)
        used.add(idx)
        used.add(jdx)
        # 默认策略为 merge 且该 cue 不是直接重套时，改选合并候选
        if opts.strategy == "merge" and plan.status != "direct":
            plan.proposed = mc
            plan.rows = _rows_for_candidate(plan, mc)
            other.rows = []
            other.proposed = None


# ---------------------------------------------------------------- 主流程

@dataclass
class ConformResult:
    segments: list[Segment]
    options: ConformOptions
    mapping_valid: bool
    mapping_issues: list[MapIssue]
    plans: list[CuePlan] = field(default_factory=list)

    @property
    def rows(self) -> list[Row]:
        return sorted((r for pl in self.plans for r in pl.rows if not r.manual),
                      key=lambda r: (r.start_frame, r.cue_index))


def run_conform(cues: list[Cue], segments: list[Segment],
                opts: ConformOptions, tb: Timebase) -> ConformResult:
    """校验映射并为每条 cue 生成重套计划、诊断与候选（纯函数，同输入同输出）。"""
    valid, issues = validate_segments(segments, tb)
    result = ConformResult(segments=segments, options=opts,
                           mapping_valid=valid, mapping_issues=issues)
    if not valid:
        return result

    plans: list[CuePlan] = []
    for cue in cues:
        parts, ss, se = _parts_of(cue, segments, opts.cut_tolerance_frames)
        plans.append(_build_plan(cue, parts, ss, se, segments, opts, tb))

    # 合并候选加入后统一分配稳定候选 id（q{cue}.c{n}）
    _add_merge_candidates(plans, segments, opts, tb)
    for pl in plans:
        for k, c in enumerate(pl.candidates, 1):
            c.id = f"q{pl.index}.c{k}"
    # 兜底：proposed 必须确实是本 cue 的候选
    for pl in plans:
        if pl.proposed is not None and pl.proposed not in pl.candidates:
            pl.proposed = None

    # 改版时间线上相邻结果重叠诊断
    all_rows = sorted(
        (r for pl in plans for r in pl.rows if not r.manual),
        key=lambda r: (r.start_frame, r.cue_index))
    for a, b in zip(all_rows, all_rows[1:]):
        if b.start_frame < a.end_frame:
            plan = next(pl for pl in plans if pl.index == a.cue_index)
            plan.issues.append(MapIssue(
                "target_cue_overlap", "warning",
                f"重套后第 {a.cue_index} 条与第 {b.cue_index} 条在改版时间线重叠 "
                f"{a.end_frame - b.start_frame} 帧",
                {"cue_indices": [a.cue_index, b.cue_index],
                 "at_frame": b.start_frame,
                 "at_tc": format_smpte(tb, b.start_frame)}))

    result.plans = plans
    return result


def summarize_conform(result: ConformResult) -> dict:
    plans = result.plans
    by_status = {"direct": 0, "cross_cut": 0, "in_deleted": 0}
    cue_issues: dict[str, int] = {}
    manual = 0
    for pl in plans:
        by_status[pl.status] += 1
        if any(r.manual for r in pl.rows):
            manual += 1
        for it in pl.issues:
            cue_issues[it.issue_type] = cue_issues.get(it.issue_type, 0) + 1
    return {
        "segment_count": len(result.segments),
        "kept_segment_count": sum(1 for s in result.segments if not s.deleted),
        "deleted_segment_count": sum(1 for s in result.segments if s.deleted),
        "retimed_segment_count": sum(
            1 for s in result.segments if not s.deleted and s.ratio != 1),
        "mapping_issue_count": len(result.mapping_issues),
        "mapping_error_count": sum(
            1 for i in result.mapping_issues if i.severity == "error"),
        "mapping_warning_count": sum(
            1 for i in result.mapping_issues if i.severity == "warning"),
        "cue_count": len(plans),
        **by_status,
        "manual_cue_count": manual,
        "cue_issue_count": sum(cue_issues.values()),
        "cue_issues_by_type": cue_issues,
        "candidate_count": sum(len(pl.candidates) for pl in plans),
        "mapped_cue_count": len(result.rows),
    }


# ---------------------------------------------------------------- 候选应用

class UnknownConformCandidateError(ValueError):
    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"未知候选 id: {', '.join(ids)}")


class ConformCandidateConflictError(ValueError):
    def __init__(self, cue_index: int, first: str, second: str):
        self.cue_index = cue_index
        super().__init__(
            f"候选 {first} 与 {second} 同时作用于第 {cue_index} 条字幕；"
            f"每条字幕只能选择一个候选（合并候选会消费相邻 cue）")


def _fallback_manual(pl: CuePlan) -> Row:
    c = pl.cue
    return Row(pl.index, None, c.start_frame, c.end_frame,
               list(c.lines), c.identifier, c.settings, manual=True)


def apply_conform(result: ConformResult, selected_ids: list[str] | None,
                  ) -> tuple[list[Cue], list[dict]]:
    """按选定候选生成改版 cue 列表。

    selected_ids 为 None 时用默认策略候选；为空列表时所有问题 cue 转人工。
    直接重套的 cue（无选择）按映射自动重算。返回 (新 cue 列表, 应用记录)。
    """
    plans = result.plans
    all_cand = {c.id: (pl, c) for pl in plans for c in pl.candidates}

    if selected_ids is None:
        selected = [pl.proposed.id for pl in plans
                    if pl.proposed is not None and pl.proposed in pl.candidates]
    else:
        selected = list(dict.fromkeys(selected_ids))
        unknown = [cid for cid in selected if cid not in all_cand]
        if unknown:
            raise UnknownConformCandidateError(unknown)

    chosen_by_cue: dict[int, tuple[str, Cand]] = {}
    consumed: set[int] = set()
    for cid in selected:
        pl, cand = all_cand[cid]
        if pl.index in chosen_by_cue:
            raise ConformCandidateConflictError(
                pl.index, chosen_by_cue[pl.index][0], cid)
        chosen_by_cue[pl.index] = (cid, cand)
        if cand.action == "merge_adjacent":
            j = cand.params["merge_with"]
            if j in chosen_by_cue:
                raise ConformCandidateConflictError(j, cid, chosen_by_cue[j][0])
            consumed.add(j)

    out_rows: list[Row] = []
    applied: list[dict] = []

    for pl in plans:
        idx = pl.index
        if idx in consumed and idx not in chosen_by_cue:
            continue
        if idx in chosen_by_cue:
            cid, cand = chosen_by_cue[idx]
            rows = _rows_for_candidate(pl, cand)
            if cand.action == "merge_adjacent":
                j = cand.params["merge_with"]
                other = next(p for p in plans if p.index == j).cue
                rows[0].lines = list(pl.cue.lines) + list(other.lines)
                consumed.add(j)
                applied.append({
                    "candidate_id": cid, "cue_index": idx,
                    "action": "merge_adjacent", "merged_with": j,
                    "segment_id": cand.params.get("segment_id"),
                    "to": cand.params["to"]})
            else:
                rec = {"candidate_id": cid, "cue_index": idx,
                       "action": cand.action, "manual": cand.manual,
                       "segment_id": cand.params.get("segment_id")}
                if "to" in cand.params:
                    rec["to"] = cand.params["to"]
                applied.append(rec)
        else:
            kept = [p for p in pl.parts if p.kind == "kept"]
            if pl.status == "direct" and len(kept) == 1:
                p = kept[0]
                rows = [Row(idx, p.segment_id, p.t0, p.t1,
                            list(pl.cue.lines), pl.cue.identifier,
                            pl.cue.settings)]
                applied.append({"cue_index": idx, "action": "direct_retime",
                                "segment_id": p.segment_id})
            else:
                rows = [_fallback_manual(pl)]
                applied.append({"cue_index": idx, "action": "manual",
                                "reason": pl.status})
        out_rows.extend(rows)

    out_rows.sort(key=lambda r: (r.start_frame, r.cue_index))
    new_cues: list[Cue] = []
    for i, r in enumerate(out_rows, 1):
        end = r.end_frame if r.end_frame > r.start_frame else r.start_frame + 1
        new_cues.append(Cue(i, r.start_frame, end, list(r.lines),
                            r.identifier, r.settings))
    return new_cues, applied


# ---------------------------------------------------------------- 序列化

def _seg_out(s: Segment, tb: Timebase) -> dict:
    r = s.ratio
    return {
        "id": s.id,
        "source": _span_timing(s.s0, s.s1, tb),
        "target": None if s.deleted else _span_timing(s.t0, s.t1, tb),
        "deleted": s.deleted,
        "source_duration_frames": s.src_len,
        "target_duration_frames": s.tgt_len,
        "ratio_num": r.numerator, "ratio_den": r.denominator,
        "retimed": (not s.deleted and r != 1),
    }


def _cue_ref(c: Cue, tb: Timebase) -> dict:
    return {
        "index": c.index,
        "start_frame": c.start_frame, "end_frame": c.end_frame,
        "start_ms": tb.frames_to_ms(c.start_frame),
        "end_ms": tb.frames_to_ms(c.end_frame),
        "start_tc": format_smpte(tb, c.start_frame),
        "end_tc": format_smpte(tb, c.end_frame),
        "lines": list(c.lines),
        "identifier": c.identifier, "settings": c.settings,
    }


def _part_out(p: Part, tb: Timebase) -> dict:
    return {
        "kind": p.kind, "segment_id": p.segment_id,
        "source": _span_timing(p.a, p.b, tb),
        "target": (_span_timing(p.t0, p.t1, tb)
                   if p.t0 is not None else None),
        "overlap_frames": p.overlap,
    }


def _candidate_out(c: Cand) -> dict:
    return {"id": c.id, "action": c.action, "description": c.description,
            "params": c.params}


def _mapped_out(r: Row, tb: Timebase) -> dict:
    return {
        "cue_index": r.cue_index, "segment_id": r.segment_id,
        "start_frame": r.start_frame, "end_frame": r.end_frame,
        "start_ms": tb.frames_to_ms(r.start_frame),
        "end_ms": tb.frames_to_ms(r.end_frame),
        "start_tc": format_smpte(tb, r.start_frame),
        "end_tc": format_smpte(tb, r.end_frame),
        "ratio_num": r.ratio.numerator if r.ratio is not None else None,
        "ratio_den": r.ratio.denominator if r.ratio is not None else None,
        "needs_manual": r.manual,
    }


def _plan_out(pl: CuePlan, tb: Timebase) -> dict:
    return {
        "status": pl.status,
        "cue": _cue_ref(pl.cue, tb),
        "snapped_start": pl.snapped_s,
        "snapped_end": pl.snapped_e,
        "parts": [_part_out(p, tb) for p in pl.parts],
        "issues": [{"issue_type": i.issue_type, "severity": i.severity,
                    "message": i.message, "details": i.details}
                   for i in pl.issues],
        "mapped_cues": [_mapped_out(r, tb) for r in pl.rows],
        "fix_candidates": [_candidate_out(c) for c in pl.candidates],
        "proposed_candidate_id": pl.proposed_id,
    }


def options_out(opts: ConformOptions, *, cut_tolerance_ms: int,
                min_duration_ms: int, merge_gap_ms: int) -> dict:
    return {
        "cut_tolerance_ms": cut_tolerance_ms,
        "cut_tolerance_frames": opts.cut_tolerance_frames,
        "min_duration_ms": min_duration_ms,
        "min_duration_frames": opts.min_duration_frames,
        "merge_gap_ms": merge_gap_ms,
        "merge_gap_frames": opts.merge_gap_frames,
        "cross_segment_strategy": opts.strategy,
    }


def report_dict(result: ConformResult, tb: Timebase, *, project_id: int,
                version_id: int, generated_at, cut_tolerance_ms: int,
                min_duration_ms: int, merge_gap_ms: int,
                timebase: dict | None = None) -> dict:
    return {
        "project_id": project_id,
        "version_id": version_id,
        "generated_at": generated_at,
        "timebase": timebase,
        "options": options_out(
            result.options, cut_tolerance_ms=cut_tolerance_ms,
            min_duration_ms=min_duration_ms, merge_gap_ms=merge_gap_ms),
        "mapping_valid": result.mapping_valid,
        "segments": [_seg_out(s, tb) for s in result.segments],
        "mapping_issues": [
            {"issue_type": i.issue_type, "severity": i.severity,
             "message": i.message, "details": i.details}
            for i in result.mapping_issues],
        "summary": summarize_conform(result),
        "cues": [_plan_out(pl, tb) for pl in result.plans],
    }
