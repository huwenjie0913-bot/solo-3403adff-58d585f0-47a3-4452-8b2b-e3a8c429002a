"""双语术语一致性检查与精确替换。

复用 :mod:`align` 的**时间区间重叠映射**：每个映射组（含未匹配源 cue）
按术语表逐条核对，报告四类问题：

- ``term_untranslated``：组内源词出现，但译文没有任何首选译法/可接受变体；
- ``term_inconsistent``：同一术语跨组（或同组内）译法不统一；
- ``term_forbidden_variant``：译文中出现禁用变体；
- ``term_case_error``：大小写敏感条目下，变体只在忽略大小写时命中。

可修复问题生成**精确替换候选**：只替换译文行内与变体逐字一致的原始
文本片段，不改时间码、标签与换行；片段内含样式标签时不出候选。
候选 id 形如 ``t{映射组号}.r{规则id}.c{序号}``（未匹配源 cue 组号为 0），
在相同阈值与术语表下重算保持稳定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .align import AlignOptions, AlignmentResult, run_alignment
from .parsing import Cue, strip_tags
from .schemas import (
    TermCueRef,
    TermFixCandidate,
    TermFragment,
    TermIssue,
    TerminologyReport,
    TerminologyRuleOut,
    TermThresholds,
    TimebaseOut,
)
from .timecode import Timebase, format_smpte

CONTEXT_RADIUS = 15  # 上下文片段在命中处前后各取的字符数


# ---------------------------------------------------------------- 匹配

def _needs_boundary(ch: str) -> bool:
    return bool(re.match(r"[A-Za-z0-9]", ch))


def _compile_variant(text: str, whole_word: bool, flags: int) -> re.Pattern:
    pat = re.escape(text)
    if whole_word:
        if _needs_boundary(text[0]):
            pat = r"(?<![A-Za-z0-9])" + pat
        if _needs_boundary(text[-1]):
            pat = pat + r"(?![A-Za-z0-9])"
    return re.compile(pat, flags)


@dataclass(frozen=True)
class _Variant:
    text: str
    category: str  # preferred / acceptable / forbidden


@dataclass
class _Frag:
    """命中片段（位置按去标签可见文本计）。"""

    text: str
    cue_index: int
    line_index: int
    start: int
    end: int
    context: str

    def out(self) -> TermFragment:
        return TermFragment(
            text=self.text, cue_index=self.cue_index, line_index=self.line_index,
            start=self.start, end=self.end, context=self.context)


def _make_frag(text: str, cue: Cue, li: int, visible: str,
               start: int, end: int) -> _Frag:
    a, b = max(0, start - CONTEXT_RADIUS), min(len(visible), end + CONTEXT_RADIUS)
    return _Frag(text=text, cue_index=cue.index, line_index=li,
                 start=start, end=end, context=visible[a:b].strip())


def _line_fragments(rule: TerminologyRuleOut, cue: Cue) -> list[tuple[int, _Frag]]:
    """在 cue 各行查找源词，返回 (行号, 片段)。跨行词条不匹配。"""
    flags = 0 if rule.case_sensitive else re.IGNORECASE
    pat = _compile_variant(rule.source_term, rule.whole_word, flags)
    out: list[tuple[int, _Frag]] = []
    for li, line in enumerate(cue.lines):
        visible = strip_tags(line)
        for m in pat.finditer(visible):
            out.append((li, _make_frag(m.group(0), cue, li, visible,
                                       m.start(), m.end())))
    return out


@dataclass
class _Hit:
    """译文中的一个变体命中。"""

    frag: _Frag
    variant: str               # 命中的已知变体（规范拼写）
    category: str              # preferred / acceptable / forbidden
    case_wrong: bool = False   # 仅忽略大小写时命中（大小写错误）


def _scan_target(rule: TerminologyRuleOut, cues: list[Cue]) -> list[_Hit]:
    """在译文 cue 中查找首选译法/可接受/禁用变体的全部命中（去重叠）。"""
    variants = [_Variant(rule.preferred_translation, "preferred")]
    variants += [_Variant(v, "acceptable") for v in rule.acceptable_variants]
    variants += [_Variant(v, "forbidden") for v in rule.forbidden_variants]

    exact_flags = 0 if rule.case_sensitive else re.IGNORECASE
    exact_pats = [(_compile_variant(v.text, rule.whole_word, exact_flags), v)
                  for v in variants]
    # 大小写敏感时再用忽略大小写扫一遍，用于发现“大小写错误”
    ci_pats = ([(_compile_variant(v.text, rule.whole_word, re.IGNORECASE), v)
                for v in variants] if rule.case_sensitive else [])

    hits: list[_Hit] = []
    for cue in cues:
        for li, line in enumerate(cue.lines):
            visible = strip_tags(line)

            def collect(pats, case_wrong):
                found = []
                for pat, v in pats:
                    for m in pat.finditer(visible):
                        found.append((m.start(), m.end(), v, m.group(0), case_wrong))
                return found

            # 最早开始优先、同起点较长者优先，贪心选择不重叠命中
            chosen: list[tuple[int, int, _Variant, str, bool]] = []
            raw = collect(exact_pats, False)
            raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
            for s, e, v, txt, cw in raw:
                if chosen and s < chosen[-1][1] and e > chosen[-1][0]:
                    continue
                chosen.append((s, e, v, txt, cw))
            covered = [(s, e) for s, e, *_ in chosen]

            if ci_pats:
                ci = collect(ci_pats, True)
                ci.sort(key=lambda x: (x[0], -(x[1] - x[0])))
                for s, e, v, txt, cw in ci:
                    if any(s < ce and e > cs for cs, ce in covered):
                        continue  # 已被规范大小写命中覆盖
                    chosen.append((s, e, v, txt, cw))
                    covered.append((s, e))

            chosen.sort(key=lambda x: x[0])
            for s, e, v, txt, cw in chosen:
                hits.append(_Hit(frag=_make_frag(txt, cue, li, visible, s, e),
                                 variant=v.text, category=v.category,
                                 case_wrong=cw))
    return hits


# ---------------------------------------------------------------- 原始文本定位

_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _raw_span(line: str, start: int, end: int) -> tuple[int, int] | None:
    """把去标签文本上的 (start, end) 映射到原始行；区间内含标签则不可替换。"""
    mapping: list[int] = []
    pos = 0
    while pos < len(line):
        m = _TAG_RE.match(line, pos)
        if m:
            pos = m.end()
            continue
        mapping.append(pos)
        pos += 1
    if end > len(mapping) or start < 0:
        return None
    a, b = mapping[start], mapping[end - 1] + 1
    if line[a:b] != strip_tags(line)[start:end]:
        return None
    return a, b


# ---------------------------------------------------------------- 检查主流程

@dataclass
class _GroupRecord:
    """一组（一个映射组）内某条术语的使用情况。"""

    mapping_id: int
    source_frags: list[_Frag]
    hits: list[_Hit]


@dataclass
class _Run:
    rules: list[TerminologyRuleOut]
    tb: Timebase
    lines: dict[tuple[int, int], str] = field(default_factory=dict)
    seq: dict[tuple[int, int], int] = field(default_factory=dict)

    def next_cid(self, mapping_id: int, rule_id: int) -> str:
        key = (mapping_id, rule_id)
        self.seq[key] = self.seq.get(key, 0) + 1
        return f"t{mapping_id}.r{rule_id}.c{self.seq[key]}"

    def cue_ref(self, c: Cue) -> TermCueRef:
        return TermCueRef(
            index=c.index, start_frame=c.start_frame, end_frame=c.end_frame,
            start_ms=self.tb.frames_to_ms(c.start_frame),
            end_ms=self.tb.frames_to_ms(c.end_frame),
            start_tc=format_smpte(self.tb, c.start_frame),
            end_tc=format_smpte(self.tb, c.end_frame),
            lines=list(c.lines))

    def make_candidate(self, mapping_id: int, rule: TerminologyRuleOut,
                       hit: _Hit, replacement: str) -> TermFixCandidate | None:
        line = self.lines.get((hit.frag.cue_index, hit.frag.line_index))
        if line is None:
            return None
        span = _raw_span(line, hit.frag.start, hit.frag.end)
        if span is None:
            return None
        a, b = span
        found = line[a:b]
        return TermFixCandidate(
            id=self.next_cid(mapping_id, rule.id),
            action="replace_term",
            description=(f"译文第 {hit.frag.cue_index} 条：{hit.frag.text!r} "
                         f"→ {replacement!r}（术语 {rule.source_term!r}）"),
            cue_index=hit.frag.cue_index, line_index=hit.frag.line_index,
            start=a, end=b, found=found, replacement=replacement,
            preview_line=line[:a] + replacement + line[b:])


def _issue(rule: TerminologyRuleOut, issue_type: str, message: str,
           mapping_id: int | None, source_cues: list[Cue],
           target_cues: list[Cue], run: _Run,
           source_frags: list[_Frag] | None = None,
           target_frags: list[_Frag] | None = None,
           reasons: dict | None = None,
           candidates: list[TermFixCandidate] | None = None) -> TermIssue:
    return TermIssue(
        issue_type=issue_type, severity=rule.severity, message=message,
        rule=rule, mapping_id=mapping_id,
        source_cues=[run.cue_ref(c) for c in source_cues],
        target_cues=[run.cue_ref(c) for c in target_cues],
        source_fragments=[f.out() for f in (source_frags or [])],
        target_fragments=[f.out() for f in (target_frags or [])],
        reasons=reasons or {}, fix_candidates=candidates or [])


def _hit_candidates(run: _Run, mapping_id: int, rule: TerminologyRuleOut,
                    hits: list[_Hit], replacement_of) -> list[TermFixCandidate]:
    cands: list[TermFixCandidate] = []
    for h in hits:
        cand = run.make_candidate(mapping_id, rule, h, replacement_of(h))
        if cand is not None:
            cands.append(cand)
    return cands


def _blocking_issue(run: _Run, rule: TerminologyRuleOut, mapping_id: int,
                    source_cues: list[Cue], target_cues: list[Cue],
                    src_frags: list[_Frag], hits: list[_Hit]) -> TermIssue | None:
    """生成禁用变体 / 大小写错误 / 源词未译问题（含精确替换候选）。"""
    forbidden = [h for h in hits if h.category == "forbidden" and not h.case_wrong]
    if forbidden:
        frags = [h.frag for h in forbidden]
        cands = _hit_candidates(run, mapping_id, rule, forbidden,
                                lambda h: rule.preferred_translation)
        return _issue(
            rule, "term_forbidden_variant",
            (f"术语 {rule.source_term!r}：译文第 "
             f"{'、'.join(str(f.cue_index) for f in frags)} 条出现禁用变体 "
             f"{'、'.join(repr(x) for x in sorted({h.variant for h in forbidden}))}，"
             f"应使用首选译法 {rule.preferred_translation!r}"),
            mapping_id, source_cues, target_cues, run,
            source_frags=src_frags, target_frags=frags,
            reasons={"source_term": rule.source_term,
                     "forbidden_variants": sorted({h.variant for h in forbidden}),
                     "forbidden_fragments": sorted({f.text for f in frags}),
                     "preferred_translation": rule.preferred_translation,
                     "variants_present": sorted({h.frag.text for h in hits})},
            candidates=cands)

    case_hits = [h for h in hits if h.case_wrong]
    if case_hits:
        frags = [h.frag for h in case_hits]

        def repl(h: _Hit) -> str:
            # 禁用变体的错误大小写 → 首选译法；首选/可接受变体 → 规范拼写
            return rule.preferred_translation if h.category == "forbidden" else h.variant

        cands = _hit_candidates(run, mapping_id, rule, case_hits, repl)
        return _issue(
            rule, "term_case_error",
            (f"术语 {rule.source_term!r}：大小写错误，命中片段 "
             f"{'、'.join(repr(f.text) for f in frags)} 与规范写法 "
             f"{'、'.join(repr(h.variant) for h in case_hits)} 大小写不一致"),
            mapping_id, source_cues, target_cues, run,
            source_frags=src_frags, target_frags=frags,
            reasons={"source_term": rule.source_term,
                     "expected": [h.variant for h in case_hits],
                     "found": [f.text for f in frags],
                     "preferred_translation": rule.preferred_translation},
            candidates=cands)

    exact = [h for h in hits if not h.case_wrong]
    if not exact:
        present = " ".join(
            strip_tags(ln).strip() for c in target_cues for ln in c.lines).strip()
        variants_note = (f"或可接受变体 {rule.acceptable_variants}"
                         if rule.acceptable_variants else "")
        return _issue(
            rule, "term_untranslated",
            (f"术语 {rule.source_term!r}：源语词在映射组 m{mapping_id} 出现 "
             f"{len(src_frags)} 次，但译文未使用首选译法 "
             f"{rule.preferred_translation!r}{variants_note}"),
            mapping_id, source_cues, target_cues, run,
            source_frags=src_frags,
            reasons={"source_term": rule.source_term,
                     "preferred_translation": rule.preferred_translation,
                     "acceptable_variants": list(rule.acceptable_variants),
                     "expected_any": [rule.preferred_translation,
                                      *rule.acceptable_variants],
                     "target_text": present})
    return None


def _canonical(rule: TerminologyRuleOut,
               records: list[_GroupRecord]) -> str:
    """确定标准译法：首选译法优先；否则取跨组数最多、同频按最早组序者。"""
    groups_using: dict[str, list[int]] = {}
    for rec in records:
        for f in {h.variant for h in rec.hits if not h.case_wrong}:
            groups_using.setdefault(f, []).append(rec.mapping_id)
    if rule.preferred_translation in groups_using:
        return rule.preferred_translation
    best = None
    for form, ids in groups_using.items():
        if (best is None or len(ids) > len(groups_using[best])
                or (len(ids) == len(groups_using[best])
                    and min(ids) < min(groups_using[best]))):
            best = form
    return best  # type: ignore[return-value]


def run_terminology(src_cues: list[Cue], tgt_cues: list[Cue],
                    rules: list[TerminologyRuleOut], opts: AlignOptions,
                    tb: Timebase, *, project_id: int,
                    source_version_id: int, target_version_id: int,
                    timebase_out: TimebaseOut, min_overlap_ms: int
                    ) -> tuple[TerminologyReport, AlignmentResult]:
    """生成术语一致性报告（纯函数，同输入同输出）；同时返回底层对齐结果。"""
    alignment = run_alignment(src_cues, tgt_cues, opts, tb)
    run = _Run(rules=rules, tb=tb)
    for c in tgt_cues:
        for li, ln in enumerate(c.lines):
            run.lines[(c.index, li)] = ln

    groups: list[tuple[int, list[Cue], list[Cue]]] = [
        (m.id, m.sources, m.targets) for m in alignment.mappings]
    records: dict[int, dict[int, _GroupRecord]] = {}
    issues: list[TermIssue] = []

    # 每个映射组预先扫出两侧片段
    for gid, source_cues, target_cues in groups:
        records[gid] = {}
        for rule in rules:
            src_frags = [f for c in source_cues for _, f in _line_fragments(rule, c)]
            if not src_frags:
                continue
            records[gid][rule.id] = _GroupRecord(
                gid, src_frags, _scan_target(rule, target_cues))

    # 逐组生成阻断类问题；其余进入一致性统计
    clean: dict[int, list[_GroupRecord]] = {r.id: [] for r in rules}
    for gid, source_cues, target_cues in groups:
        for rule in rules:
            rec = records[gid].get(rule.id)
            if rec is None:
                continue
            issue = _blocking_issue(run, rule, gid, source_cues, target_cues,
                                    rec.source_frags, rec.hits)
            if issue is not None:
                issues.append(issue)
            else:
                clean[rule.id].append(rec)

    # 跨组 / 同组译法一致性
    for rule in rules:
        recs = clean[rule.id]
        if not recs:
            continue
        canonical = _canonical(rule, recs)
        usage: dict[str, int] = {}
        for rec in recs:
            for h in rec.hits:
                if not h.case_wrong:
                    usage[h.variant] = usage.get(h.variant, 0) + 1
        cue_lookup = {gid: (s, t) for gid, s, t in groups}
        for rec in recs:
            source_cues, target_cues = cue_lookup[rec.mapping_id]
            bad = [h for h in rec.hits
                   if not h.case_wrong and h.variant != canonical]
            if not bad:
                continue
            frags = [h.frag for h in bad]
            forms = sorted({h.variant for h in rec.hits if not h.case_wrong})
            cands = _hit_candidates(run, rec.mapping_id, rule, bad,
                                    lambda h: rule.preferred_translation)
            suffix = ("（首选译法）" if canonical == rule.preferred_translation
                      else f"；术语表首选译法为 {rule.preferred_translation!r}")
            issues.append(_issue(
                rule, "term_inconsistent",
                (f"术语 {rule.source_term!r}：映射组 m{rec.mapping_id} 使用译法 "
                 f"{forms}，标准用法为 {canonical!r}{suffix}"),
                rec.mapping_id, source_cues, target_cues, run,
                source_frags=rec.source_frags, target_frags=frags,
                reasons={"source_term": rule.source_term,
                         "canonical": canonical,
                         "preferred_translation": rule.preferred_translation,
                         "used_in_group": forms,
                         "usage_across_groups": usage},
                candidates=cands))

    # 未匹配源 cue：源词出现即“源词未译”（无译文 cue，无修复候选）
    for c in alignment.unmatched_source:
        for rule in rules:
            src_frags = [f for _, f in _line_fragments(rule, c)]
            if not src_frags:
                continue
            issues.append(_issue(
                rule, "term_untranslated",
                (f"术语 {rule.source_term!r}：源语第 {c.index} 条无对应译文 cue，"
                 f"源词出现 {len(src_frags)} 次（译文漏条）"),
                None, [c], [], run, source_frags=src_frags,
                reasons={"source_term": rule.source_term,
                         "preferred_translation": rule.preferred_translation,
                         "acceptable_variants": list(rule.acceptable_variants),
                         "expected_any": [rule.preferred_translation,
                                          *rule.acceptable_variants],
                         "missing_translation": True}))

    issues.sort(key=lambda i: (i.mapping_id is None, i.mapping_id or 0,
                               i.rule.id, i.issue_type))

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    cand_count = 0
    for i in issues:
        by_type[i.issue_type] = by_type.get(i.issue_type, 0) + 1
        by_severity[i.severity] = by_severity.get(i.severity, 0) + 1
        cand_count += len(i.fix_candidates)
    summary = {
        "mapping_count": len(alignment.mappings),
        "unmatched_source_count": len(alignment.unmatched_source),
        "unmatched_target_count": len(alignment.unmatched_target),
        "issue_count": len(issues),
        "by_type": by_type,
        "by_severity": by_severity,
        "fixable_candidate_count": cand_count,
    }

    report = TerminologyReport(
        project_id=project_id, source_version_id=source_version_id,
        target_version_id=target_version_id,
        generated_at=datetime.now(timezone.utc),
        timebase=timebase_out,
        thresholds=TermThresholds(
            min_overlap_ms=min_overlap_ms,
            min_overlap_frames=opts.min_overlap_frames,
            min_overlap_ratio=opts.min_overlap_ratio,
            rule_count=len(rules)),
        rules=list(rules), summary=summary,
        mapping_count=len(alignment.mappings), issues=issues)
    return report, alignment


# ---------------------------------------------------------------- 候选索引 / 预览 / 应用

class UnknownTermCandidateError(ValueError):
    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"未知候选 id: {', '.join(ids)}")


class TermCandidateConflictError(ValueError):
    def __init__(self, cue_index: int, line_index: int, first: str, second: str):
        self.cue_index = cue_index
        self.line_index = line_index
        super().__init__(
            f"候选 {first} 与 {second} 在译文第 {cue_index} 条第 "
            f"{line_index + 1} 行的替换区间重叠，不能同时应用")


class TermCandidateStaleError(ValueError):
    def __init__(self, cid: str, expected: str, actual: str):
        self.cid = cid
        super().__init__(
            f"候选 {cid} 已失效：期望片段 {expected!r}，原文为 {actual!r}")


def index_candidates(report: TerminologyReport) -> dict[str, TermFixCandidate]:
    return {c.id: c for i in report.issues for c in i.fix_candidates}


def _select(report: TerminologyReport,
            candidate_ids: list[str] | None) -> list[TermFixCandidate]:
    by_id = index_candidates(report)
    if candidate_ids is None:
        return list(by_id.values())
    unknown = [cid for cid in dict.fromkeys(candidate_ids) if cid not in by_id]
    if unknown:
        raise UnknownTermCandidateError(unknown)
    return [by_id[cid] for cid in dict.fromkeys(candidate_ids)]


def preview_terminology(report: TerminologyReport, tgt_cues: list[Cue],
                        candidate_ids: list[str]) -> list[dict]:
    """返回选定候选的逐行替换预览（不落库）。"""
    lines_by = {(c.index, li): ln for c in tgt_cues
                for li, ln in enumerate(c.lines)}
    items: list[dict] = []
    for cand in _select(report, candidate_ids):
        before = lines_by[(cand.cue_index, cand.line_index)]
        if before[cand.start:cand.end] != cand.found:
            raise TermCandidateStaleError(
                cand.id, cand.found, before[cand.start:cand.end])
        items.append({
            "candidate_id": cand.id, "cue_index": cand.cue_index,
            "line_index": cand.line_index,
            "before_line": before, "after_line": cand.preview_line,
            "found": cand.found, "replacement": cand.replacement})
    return items


def apply_terminology(report: TerminologyReport, tgt_cues: list[Cue],
                      candidate_ids: list[str] | None
                      ) -> tuple[list[Cue], list[dict]]:
    """把选定候选应用到译文 cue，返回 (新 cue 列表, 已应用替换)。

    只替换行内片段，时间码、标签与换行（行数/行序）原样保留。
    同一行的替换区间不得重叠；替换按位置自后向前进行。
    """
    selected = _select(report, candidate_ids)

    # 区间冲突检测（按行）
    by_line: dict[tuple[int, int], list[TermFixCandidate]] = {}
    for cand in selected:
        by_line.setdefault((cand.cue_index, cand.line_index), []).append(cand)
    for (cue_ix, li), cands in by_line.items():
        ordered = sorted(cands, key=lambda c: (c.start, c.end))
        for a, b in zip(ordered, ordered[1:]):
            if b.start < a.end:
                raise TermCandidateConflictError(cue_ix, li, a.id, b.id)

    new_cues = [Cue(c.index, c.start_frame, c.end_frame, list(c.lines),
                    c.identifier, c.settings) for c in tgt_cues]
    lines_by = {c.index: c.lines for c in new_cues}
    applied: list[dict] = []
    for (cue_ix, li), cands in by_line.items():
        line = lines_by[cue_ix][li]
        for cand in sorted(cands, key=lambda c: c.start, reverse=True):
            if line[cand.start:cand.end] != cand.found:
                raise TermCandidateStaleError(
                    cand.id, cand.found, line[cand.start:cand.end])
            line = line[:cand.start] + cand.replacement + line[cand.end:]
            applied.append({
                "candidate_id": cand.id, "cue_index": cue_ix,
                "line_index": li, "found": cand.found,
                "replacement": cand.replacement,
                "rule_id": int(cand.id.split(".")[1][1:])})
        lines_by[cue_ix][li] = line
    applied.sort(key=lambda a: (a["cue_index"], a["line_index"]))
    return new_cues, applied
