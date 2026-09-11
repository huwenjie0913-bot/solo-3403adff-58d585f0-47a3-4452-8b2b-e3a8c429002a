"""双语字幕轨道对齐与同步检查。

在同一项目内把两个字幕版本指定为源语言与译文，按**时间区间重叠**与
**相邻顺序**生成一对一 / 一对多 / 多对一（以及多对多）cue 映射，并返回
字段化诊断：未匹配 cue、重叠不足、译文漏条、顺序倒置、说话人标签不一致。

修复候选以**源 cue 边界**为依据，只调整时间与结构，不改写文本：

- ``retime_to_source``：把译文 cue 起止时间对齐到源 cue 边界；
- ``merge_to_source``：一对多时把多条译文 cue 合并到源 cue 跨度；
- ``split_at_source_boundaries``：多对一时在源 cue 边界处拆分译文 cue；
- ``fit_group_to_source``：整组译文 cue 按比例映射到源跨度
  （``candidate_strategy=proportional`` 时替代拆分/合并）。

所有时间运算在帧域进行（整数帧号 + 有理数换算），候选参数同时给出
帧号、毫秒与 SMPTE 时间码。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from .autofix import _balance_tags, _tokenize, _units_text
from .parsing import Cue, join_lines, visible_text
from .schemas import (
    AlignCandidate,
    AlignCueRef,
    AlignIssue,
    AlignSide,
    MappingOut,
    UnmatchedCueOut,
)
from .timecode import Timebase, ceil_pos, format_smpte, half_up

# ---------------------------------------------------------------- 选项

@dataclass(frozen=True)
class AlignOptions:
    """对齐阈值与候选策略。"""

    min_overlap_frames: int = 5          # 判定匹配的最小重叠（帧）
    min_overlap_ratio: float = 0.3       # 重叠比例低于该值报“重叠不足”
    speaker_check: bool = True           # 是否检查说话人标签一致性
    strategy: str = "source_boundaries"  # source_boundaries / proportional

    @classmethod
    def from_ms(cls, min_overlap_ms: int, min_overlap_ratio: float,
                speaker_check: bool, strategy: str, tb: Timebase) -> "AlignOptions":
        # 毫秒阈值换算为帧（向上取整，至少 1 帧：任何正重叠都算匹配）
        frames = max(1, ceil_pos(Fraction(min_overlap_ms) * tb.rate / 1000))
        return cls(frames, min_overlap_ratio, speaker_check, strategy)


# ---------------------------------------------------------------- 说话人标签

_VOICE_TAG_RE = re.compile(r"<v(?:\.[^\s>]*)*\s+([^>]+)>", re.I)
_DASH_LABEL_RE = re.compile(r"^\s*[-–—]\s*(\S{1,12}?)\s*[：:]")
_BRACKET_LABEL_RE = re.compile(r"^\s*【([^】]{1,12})】")
_PLAIN_LABEL_RE = re.compile(
    r"^([^\s：:，。、！？；…,.!?;「」『』【】（）()]{1,12}?)[：:]")


def speakers_of(cue: Cue) -> list[str]:
    """提取 cue 的说话人标签（``<v 张三>``、``- 张三：``、``【张三】``、``张三：``）。"""
    names: list[str] = []
    for line in cue.lines:
        for m in _VOICE_TAG_RE.finditer(line):
            names.append(m.group(1).strip())
        vis = visible_text(line)
        for rx in (_DASH_LABEL_RE, _BRACKET_LABEL_RE, _PLAIN_LABEL_RE):
            m = rx.match(vis)
            if m:
                names.append(m.group(1).strip())
                break
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def _side_speakers(cues: list[Cue]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in cues:
        for sp in speakers_of(c):
            if sp.casefold() not in seen:
                seen.add(sp.casefold())
                out.append(sp)
    return out


# ---------------------------------------------------------------- 分组（时间区间重叠 + 相邻顺序）

def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _span(cues: list[Cue]) -> tuple[int, int]:
    return (min(c.start_frame for c in cues), max(c.end_frame for c in cues))


def _mapping_type(ns: int, nt: int) -> str:
    if ns == 1 and nt == 1:
        return "one_to_one"
    if ns == 1:
        return "one_to_many"
    if nt == 1:
        return "many_to_one"
    return "many_to_many"


def _group(src_cues: list[Cue], tgt_cues: list[Cue], min_overlap_frames: int):
    """按时间区间重叠构建连通分量。

    返回 (排序后源列表, 排序后译文列表, 分量列表[(源位置, 译文位置)],
    未匹配源 cue, 未匹配译文 cue)。分量即映射组：一对重叠关系把源与译文
    cue 连成一组，组内源/译文各自按时间顺序排列。
    """
    S = sorted(src_cues, key=lambda c: (c.start_frame, c.index))
    T = sorted(tgt_cues, key=lambda c: (c.start_frame, c.index))
    parent: dict[tuple, tuple] = {}

    def find(x: tuple) -> tuple:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: tuple, b: tuple) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    linked_s: set[int] = set()
    linked_t: set[int] = set()
    j0 = 0
    for i, s in enumerate(S):
        for j in range(j0, len(T)):
            t = T[j]
            if t.start_frame >= s.end_frame:
                break
            if t.end_frame <= s.start_frame:
                if j == j0:  # t 完全在本源 cue 之前，之后源 cue 更晚，可跳过
                    j0 = j + 1
                continue
            if _overlap(s.start_frame, s.end_frame,
                        t.start_frame, t.end_frame) >= min_overlap_frames:
                union(("s", i), ("t", j))
                linked_s.add(i)
                linked_t.add(j)

    comps: dict[tuple, list] = {}
    for i in sorted(linked_s):
        comps.setdefault(find(("s", i)), ([], []))[0].append(i)
    for j in sorted(linked_t):
        comps.setdefault(find(("t", j)), ([], []))[1].append(j)
    unmatched_s = [c for i, c in enumerate(S) if i not in linked_s]
    unmatched_t = [c for j, c in enumerate(T) if j not in linked_t]
    return S, T, list(comps.values()), unmatched_s, unmatched_t


# ---------------------------------------------------------------- 文本拆分（不改写文本）

_SENT_END = set("。！？!?….")      # 句末标点
_CLAUSE_END = set("，、；：,;:—")  # 从句标点


def _split_text_n(text: str, n: int) -> list[str] | None:
    """把文本拆成 n 段：标点边界优先，宽度比例兜底。无法拆分返回 None。"""
    if n <= 1:
        return [text]
    units = _tokenize(text)
    if len(units) < n:
        return None
    total = sum(u.width for u in units)
    cum = [0]
    for u in units:
        cum.append(cum[-1] + u.width)
    bounds: list[int] = []
    prev = 0
    for k in range(1, n):
        ideal = total * k / n
        lo, hi = prev + 1, len(units) - (n - k)  # 后面还要留 n-k 段
        best_b, best_score = lo, float("inf")
        for b in range(lo, hi + 1):
            score = abs(cum[b] - ideal)
            tail = units[b - 1].text[-1] if units[b - 1].text else ""
            if tail in _SENT_END:
                score -= total        # 句末标点优先
            elif tail in _CLAUSE_END:
                score -= total / 2    # 从句标点次之
            if score < best_score:
                best_score, best_b = score, b
        bounds.append(best_b)
        prev = best_b
    pieces: list[str] = []
    last = 0
    for b in bounds + [len(units)]:
        pieces.append(_units_text(units[last:b]))
        last = b
    return _balance_tags(pieces)  # 各段标签闭合（说话人/样式标签保留）


# ---------------------------------------------------------------- 映射与诊断

@dataclass
class _Mapping:
    id: int
    type: str
    sources: list[Cue]
    targets: list[Cue]
    overlap_frames: int
    ratio_source: float
    ratio_target: float
    issues: list[AlignIssue] = field(default_factory=list)
    candidates: list[AlignCandidate] = field(default_factory=list)


@dataclass
class AlignmentResult:
    mappings: list[_Mapping]
    unmatched_source: list[Cue]
    unmatched_target: list[Cue]


def _timing_dict(start_frame: int, end_frame: int, tb: Timebase) -> dict:
    return {
        "start_frame": start_frame, "end_frame": end_frame,
        "start_ms": tb.frames_to_ms(start_frame),
        "end_ms": tb.frames_to_ms(end_frame),
        "start_tc": format_smpte(tb, start_frame),
        "end_tc": format_smpte(tb, end_frame),
    }


def _idxs(cues: list[Cue]) -> list[int]:
    return [c.index for c in cues]


def _diagnose(m: _Mapping, opts: AlignOptions, tb: Timebase) -> None:
    issues: list[AlignIssue] = []
    if (m.ratio_source < opts.min_overlap_ratio
            or m.ratio_target < opts.min_overlap_ratio):
        issues.append(AlignIssue(
            issue_type="insufficient_overlap", severity="warning",
            message=(f"重叠不足：重叠 {m.overlap_frames} 帧，源侧重叠比例 "
                     f"{m.ratio_source:.2f}、译文侧 {m.ratio_target:.2f}，"
                     f"低于阈值 {opts.min_overlap_ratio}"),
            details={
                "overlap_frames": m.overlap_frames,
                "overlap_ratio_source": m.ratio_source,
                "overlap_ratio_target": m.ratio_target,
                "min_overlap_ratio": opts.min_overlap_ratio,
            }))
    if opts.speaker_check:
        ss, ts = _side_speakers(m.sources), _side_speakers(m.targets)
        if {s.casefold() for s in ss} != {t.casefold() for t in ts}:
            issues.append(AlignIssue(
                issue_type="speaker_mismatch", severity="warning",
                message=(f"说话人标签不一致：源侧 [{'、'.join(ss) or '无'}] "
                         f"vs 译文侧 [{'、'.join(ts) or '无'}]"),
                details={"source_speakers": ss, "target_speakers": ts}))
    m.issues = issues


def _detect_order_inversion(mappings: list[_Mapping]) -> None:
    """顺序倒置：按源时间顺序遍历映射，译文 cue 序号应单调不减。"""
    max_idx: int | None = None
    for m in mappings:
        idxs = _idxs(m.targets)
        lo = min(idxs)
        if max_idx is not None and lo < max_idx:
            m.issues.append(AlignIssue(
                issue_type="order_inversion", severity="error",
                message=(f"顺序倒置：译文第 {lo} 条在译文中排在第 {max_idx} 条之前，"
                         f"与源 cue 顺序不一致"),
                details={"target_indices": idxs,
                         "previous_max_target_index": max_idx}))
        max_idx = max(idxs) if max_idx is None else max(max_idx, max(idxs))


# ---------------------------------------------------------------- 修复候选

def _fit_per_cue(targets: list[Cue], s_span: tuple[int, int],
                 t_span: tuple[int, int], tb: Timebase) -> list[dict]:
    """把译文组内各 cue 从译文跨度按比例映射到源跨度（帧域，至少 1 帧）。"""
    S0, S1 = s_span
    T0, T1 = t_span
    scale = Fraction(S1 - S0, T1 - T0)
    out: list[dict] = []
    for c in targets:
        a = S0 + half_up(Fraction(c.start_frame - T0) * scale)
        b = S0 + half_up(Fraction(c.end_frame - T0) * scale)
        a = min(max(a, S0), S1 - 1)
        b = min(max(b, a + 1), S1)
        out.append({"target_index": c.index,
                    "from": _timing_dict(c.start_frame, c.end_frame, tb),
                    "to": _timing_dict(a, b, tb)})
    return out


def _fit_candidate(m: _Mapping, s_span: tuple[int, int],
                   t_span: tuple[int, int], tb: Timebase) -> AlignCandidate:
    return AlignCandidate(
        id="", action="fit_group_to_source",
        description=(f"本组 {len(m.targets)} 条译文 cue 按比例映射到源跨度 "
                     f"{format_smpte(tb, s_span[0])}–{format_smpte(tb, s_span[1])}"),
        params={
            "target_indices": _idxs(m.targets),
            "source_span": _timing_dict(*s_span, tb),
            "target_span": _timing_dict(*t_span, tb),
            "per_cue": _fit_per_cue(m.targets, s_span, t_span, tb),
        })


def _build_candidates(m: _Mapping, opts: AlignOptions, tb: Timebase) -> None:
    s_span, t_span = _span(m.sources), _span(m.targets)
    cands: list[AlignCandidate] = []
    if m.type == "one_to_one":
        s, t = m.sources[0], m.targets[0]
        if (s.start_frame, s.end_frame) != (t.start_frame, t.end_frame):
            cands.append(AlignCandidate(
                id="", action="retime_to_source",
                description=(f"译文第 {t.index} 条起止时间对齐到源第 {s.index} 条边界 "
                             f"{format_smpte(tb, s.start_frame)}–"
                             f"{format_smpte(tb, s.end_frame)}"),
                params={
                    "target_index": t.index, "source_index": s.index,
                    "from": _timing_dict(t.start_frame, t.end_frame, tb),
                    "to": _timing_dict(s.start_frame, s.end_frame, tb),
                }))
    elif opts.strategy == "proportional":
        if s_span != t_span:
            cands.append(_fit_candidate(m, s_span, t_span, tb))
    elif m.type == "one_to_many":
        s = m.sources[0]
        cands.append(AlignCandidate(
            id="", action="merge_to_source",
            description=(f"译文第 {'、'.join(map(str, _idxs(m.targets)))} 条合并为一条，"
                         f"对齐到源第 {s.index} 条边界 "
                         f"{format_smpte(tb, s.start_frame)}–"
                         f"{format_smpte(tb, s.end_frame)}"),
            params={
                "target_indices": _idxs(m.targets), "source_index": s.index,
                "to": _timing_dict(s.start_frame, s.end_frame, tb),
            }))
    elif m.type == "many_to_one":
        t = m.targets[0]
        pieces = _split_text_n(join_lines(t.lines), len(m.sources))
        if pieces is not None:
            cands.append(AlignCandidate(
                id="", action="split_at_source_boundaries",
                description=(f"译文第 {t.index} 条按源第 "
                             f"{'、'.join(map(str, _idxs(m.sources)))} 条边界"
                             f"拆分为 {len(m.sources)} 条"),
                params={
                    "target_index": t.index,
                    "source_indices": _idxs(m.sources),
                    "segments": [dict(source_index=sc.index,
                                      **_timing_dict(sc.start_frame, sc.end_frame, tb))
                                 for sc in m.sources],
                    "text_pieces": pieces,
                }))
        elif s_span != t_span:  # 文本不可拆时退化为整组比例映射
            cands.append(_fit_candidate(m, s_span, t_span, tb))
    elif s_span != t_span:  # many_to_many
        cands.append(_fit_candidate(m, s_span, t_span, tb))
    m.candidates = cands


# ---------------------------------------------------------------- 对齐主流程

def run_alignment(src_cues: list[Cue], tgt_cues: list[Cue],
                  opts: AlignOptions, tb: Timebase) -> AlignmentResult:
    """生成源/译文 cue 映射、诊断与修复候选（纯函数，同输入同输出）。"""
    S, T, comps, un_s, un_t = _group(src_cues, tgt_cues, opts.min_overlap_frames)
    mappings: list[_Mapping] = []
    for s_pos, t_pos in comps:
        sources = [S[i] for i in s_pos]
        targets = [T[j] for j in t_pos]
        s_span, t_span = _span(sources), _span(targets)
        ov = _overlap(s_span[0], s_span[1], t_span[0], t_span[1])
        m = _Mapping(
            id=0, type=_mapping_type(len(sources), len(targets)),
            sources=sources, targets=targets, overlap_frames=ov,
            ratio_source=round(float(Fraction(ov, s_span[1] - s_span[0])), 4),
            ratio_target=round(float(Fraction(ov, t_span[1] - t_span[0])), 4))
        _diagnose(m, opts, tb)
        _build_candidates(m, opts, tb)
        mappings.append(m)
    mappings.sort(key=lambda m: (_span(m.sources)[0], _span(m.targets)[0]))
    for i, m in enumerate(mappings, 1):
        m.id = i
    _detect_order_inversion(mappings)
    # 候选 id 在映射排序后分配，保证 /align 与 /align/apply 间稳定
    for m in mappings:
        for k, c in enumerate(m.candidates, 1):
            c.id = f"m{m.id}.c{k}"
    return AlignmentResult(mappings=mappings, unmatched_source=un_s,
                           unmatched_target=un_t)


# ---------------------------------------------------------------- 序列化

def _cue_ref(c: Cue, tb: Timebase) -> AlignCueRef:
    return AlignCueRef(
        index=c.index, start_frame=c.start_frame, end_frame=c.end_frame,
        start_ms=tb.frames_to_ms(c.start_frame),
        end_ms=tb.frames_to_ms(c.end_frame),
        start_tc=format_smpte(tb, c.start_frame),
        end_tc=format_smpte(tb, c.end_frame),
        speakers=speakers_of(c))


def _side(cues: list[Cue], tb: Timebase) -> AlignSide:
    s0, e0 = _span(cues)
    return AlignSide(
        indices=_idxs(cues), start_frame=s0, end_frame=e0,
        start_ms=tb.frames_to_ms(s0), end_ms=tb.frames_to_ms(e0),
        start_tc=format_smpte(tb, s0), end_tc=format_smpte(tb, e0),
        speakers=_side_speakers(cues), cues=[_cue_ref(c, tb) for c in cues])


def mapping_out(m: _Mapping, tb: Timebase) -> MappingOut:
    return MappingOut(
        id=m.id, type=m.type,
        source=_side(m.sources, tb), target=_side(m.targets, tb),
        overlap_frames=m.overlap_frames,
        overlap_ms=tb.frames_to_ms(m.overlap_frames),
        overlap_ratio_source=m.ratio_source,
        overlap_ratio_target=m.ratio_target,
        issues=m.issues, fix_candidates=m.candidates)


def unmatched_out(cue: Cue, side: str, tb: Timebase) -> UnmatchedCueOut:
    if side == "source":
        return UnmatchedCueOut(
            cue=_cue_ref(cue, tb), issue_type="missing_translation",
            severity="error",
            message=f"译文漏条：源第 {cue.index} 条没有对应的译文 cue")
    return UnmatchedCueOut(
        cue=_cue_ref(cue, tb), issue_type="unmatched_target",
        severity="warning",
        message=f"译文第 {cue.index} 条未匹配到任何源 cue")


def summarize_alignment(result: AlignmentResult) -> dict:
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_mapping = {"one_to_one": 0, "one_to_many": 0,
                  "many_to_one": 0, "many_to_many": 0}
    issue_total = 0

    def _add(issue_type: str, severity: str) -> None:
        nonlocal issue_total
        issue_total += 1
        by_type[issue_type] = by_type.get(issue_type, 0) + 1
        by_severity[severity] = by_severity.get(severity, 0) + 1

    cand_count = 0
    for m in result.mappings:
        by_mapping[m.type] += 1
        cand_count += len(m.candidates)
        for it in m.issues:
            _add(it.issue_type, it.severity)
    for _ in result.unmatched_source:
        _add("missing_translation", "error")
    for _ in result.unmatched_target:
        _add("unmatched_target", "warning")
    return {
        "mapping_count": len(result.mappings), **by_mapping,
        "unmatched_source_count": len(result.unmatched_source),
        "unmatched_target_count": len(result.unmatched_target),
        "issue_count": issue_total,
        "by_type": by_type, "by_severity": by_severity,
        "candidate_count": cand_count,
    }


# ---------------------------------------------------------------- 候选应用

class UnknownCandidateError(ValueError):
    """选定的候选 id 不存在。"""

    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"未知候选 id: {', '.join(ids)}")


class CandidateConflictError(ValueError):
    """选定候选作用于同一译文 cue，互相冲突。"""

    def __init__(self, target_index: int, first: str, second: str):
        self.target_index = target_index
        self.first = first
        self.second = second
        super().__init__(
            f"候选 {first} 与 {second} 同时作用于译文第 {target_index} 条，"
            f"同一映射组内的候选只能选择一个")


def _candidate_target_indices(cand: AlignCandidate) -> list[int]:
    p = cand.params
    if "target_indices" in p:
        return list(p["target_indices"])
    if "target_index" in p:
        return [p["target_index"]]
    return [pc["target_index"] for pc in p.get("per_cue", [])]


def _retimed(cue: Cue, to: dict) -> Cue:
    return Cue(cue.index, to["start_frame"], to["end_frame"],
               list(cue.lines), cue.identifier, cue.settings)


def apply_alignment(mappings: list[_Mapping], selected_ids: list[str],
                    tgt_cues: list[Cue], tb: Timebase) -> tuple[list[Cue], list[dict]]:
    """把选定候选应用到译文 cue 列表，返回 (新 cue 列表, 已应用候选)。

    只调整时间与结构（拆分/合并/对齐），文本内容原样保留。
    """
    by_id = {c.id: (m, c) for m in mappings for c in m.candidates}
    selected = list(dict.fromkeys(selected_ids))  # 去重保序
    unknown = [cid for cid in selected if cid not in by_id]
    if unknown:
        raise UnknownCandidateError(unknown)

    # 同一译文 cue 只能被一个选定候选作用
    touched: dict[int, str] = {}
    for cid in selected:
        for ix in _candidate_target_indices(by_id[cid][1]):
            if ix in touched:
                raise CandidateConflictError(ix, touched[ix], cid)
            touched[ix] = cid
    selected_set = set(selected)

    tgt_by_index = {c.index: c for c in tgt_cues}
    replace: dict[int, list[Cue]] = {}
    consumed: set[int] = set()
    applied: list[dict] = []
    for m in mappings:
        for cand in m.candidates:
            if cand.id not in selected_set:
                continue
            p = cand.params
            if cand.action == "retime_to_source":
                t = tgt_by_index[p["target_index"]]
                replace[t.index] = [_retimed(t, p["to"])]
            elif cand.action == "merge_to_source":
                idxs = p["target_indices"]
                first = tgt_by_index[idxs[0]]
                lines = [ln for ix in idxs for ln in tgt_by_index[ix].lines]
                replace[idxs[0]] = [Cue(first.index, p["to"]["start_frame"],
                                        p["to"]["end_frame"], lines,
                                        first.identifier, first.settings)]
                consumed.update(idxs[1:])
            elif cand.action == "split_at_source_boundaries":
                t = tgt_by_index[p["target_index"]]
                pieces = p["text_pieces"]
                replace[t.index] = [
                    Cue(t.index, seg["start_frame"], seg["end_frame"],
                        [pieces[k]], t.identifier, t.settings)
                    for k, seg in enumerate(p["segments"])]
            elif cand.action == "fit_group_to_source":
                for pc in p["per_cue"]:
                    t = tgt_by_index[pc["target_index"]]
                    replace[t.index] = [_retimed(t, pc["to"])]
            applied.append({
                "candidate_id": cand.id, "mapping_id": m.id,
                "action": cand.action, "description": cand.description,
                "target_indices": _candidate_target_indices(cand),
            })

    new_cues: list[Cue] = []
    for t in sorted(tgt_cues, key=lambda c: c.index):
        if t.index in consumed:
            continue
        if t.index in replace:
            new_cues.extend(replace[t.index])
        else:  # 未选中的 cue 原样保留
            new_cues.append(Cue(t.index, t.start_frame, t.end_frame,
                                list(t.lines), t.identifier, t.settings))
    for i, c in enumerate(new_cues, 1):
        c.index = i
    return new_cues, applied
