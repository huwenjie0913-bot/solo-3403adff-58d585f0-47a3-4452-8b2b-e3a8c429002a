"""字幕质检：重叠、闪现、跨镜头、阅读速度超限、不自然断行等。

每项问题都给出原因（message）与修正候选（fix_candidates）。
"""
from __future__ import annotations

import math

from .parsing import Cue, cue_visible_len, format_timestamp, visible_len, visible_text
from .schemas import FixCandidate, Issue, Rules

# 闭合类标点：不应出现在行首
CLOSING_PUNCT = set("，。、！？；：…”’」』）】》,.!?;:%)]}»")
# 开启类标点：不应出现在行尾
OPENING_PUNCT = set("“‘「『（【《([{«")
# 中文虚词：不宜作为行尾
CJK_FUNC_WORDS = set("的了和与及而被把在于以就都还又或且吗呢吧")


def _fc(action: str, description: str, **params) -> FixCandidate:
    return FixCandidate(action=action, description=description, params=params)


def _ts(ms: int) -> str:
    return format_timestamp(ms, "vtt")


# ---------------------------------------------------------------- 时间轴类检查

def check_cue_timing(
    cue: Cue,
    next_cue: Cue | None,
    rules: Rules,
    shot_cuts: list[int],
) -> list[Issue]:
    issues: list[Issue] = []
    dur = cue.duration_ms

    if dur <= 0:
        issues.append(Issue(
            cue_index=cue.index, issue_type="invalid_timing", severity="error",
            message="结束时间早于或等于开始时间",
            details={"start_ms": cue.start_ms, "end_ms": cue.end_ms},
        ))
        return issues

    if dur < rules.min_duration_ms:
        target = cue.start_ms + rules.min_duration_ms
        issues.append(Issue(
            cue_index=cue.index, issue_type="flash", severity="error",
            message=f"显示时长 {dur}ms 低于最短显示时间 {rules.min_duration_ms}ms（闪现）",
            details={"duration_ms": dur, "min_duration_ms": rules.min_duration_ms},
            fix_candidates=[_fc("extend_end", f"结束时间延长到 {_ts(target)}", end_ms=target)],
        ))

    if dur > rules.max_duration_ms:
        target = cue.start_ms + rules.max_duration_ms
        issues.append(Issue(
            cue_index=cue.index, issue_type="duration_too_long", severity="warning",
            message=f"显示时长 {dur}ms 超过最长显示时间 {rules.max_duration_ms}ms",
            details={"duration_ms": dur, "max_duration_ms": rules.max_duration_ms},
            fix_candidates=[
                _fc("split_cue", "按句子拆分为多条字幕"),
                _fc("trim_end", f"结束时间提前到 {_ts(target)}", end_ms=target),
            ],
        ))

    chars = cue_visible_len(cue)
    if chars > 0:
        cps = chars / (dur / 1000.0)
        if cps > rules.max_cps + 1e-6:
            need = math.ceil(chars / rules.max_cps * 1000)
            issues.append(Issue(
                cue_index=cue.index, issue_type="cps_exceeded", severity="error",
                message=f"阅读速度 {cps:.1f} 字/秒，超过上限 {rules.max_cps} 字/秒",
                details={"cps": round(cps, 2), "chars": chars, "duration_ms": dur},
                fix_candidates=[
                    _fc("extend_end", f"结束时间延长到 {_ts(cue.start_ms + need)}",
                        end_ms=cue.start_ms + need),
                    _fc("split_cue", "按句子拆分为多条字幕"),
                ],
            ))

    if next_cue is not None:
        gap = next_cue.start_ms - cue.end_ms
        if gap < 0:
            target = next_cue.start_ms - rules.min_gap_ms
            issues.append(Issue(
                cue_index=cue.index, issue_type="overlap", severity="error",
                message=f"与第 {next_cue.index} 条字幕重叠 {-gap}ms",
                details={"overlap_ms": -gap, "next_cue_index": next_cue.index},
                fix_candidates=[
                    _fc("trim_end", f"结束时间提前到 {_ts(target)}", end_ms=target),
                    _fc("shift_next", f"将第 {next_cue.index} 条整体后移"),
                ],
            ))
        elif gap < rules.min_gap_ms:
            target = next_cue.start_ms - rules.min_gap_ms
            issues.append(Issue(
                cue_index=cue.index, issue_type="gap_too_small", severity="warning",
                message=f"与第 {next_cue.index} 条间隔 {gap}ms，小于最小间隔 {rules.min_gap_ms}ms",
                details={"gap_ms": gap, "min_gap_ms": rules.min_gap_ms,
                         "next_cue_index": next_cue.index},
                fix_candidates=[_fc("trim_end", f"结束时间提前到 {_ts(target)}", end_ms=target)],
            ))

    tol = rules.shot_tolerance_ms
    for cut in shot_cuts:
        if cue.start_ms + tol < cut < cue.end_ms - tol:
            issues.append(Issue(
                cue_index=cue.index, issue_type="shot_cross", severity="error",
                message=f"字幕跨越镜头切点 {_ts(cut)}",
                details={"cut_ms": cut, "start_ms": cue.start_ms, "end_ms": cue.end_ms},
                fix_candidates=[
                    _fc("trim_end", f"结束时间提前到切点 {_ts(cut)}", end_ms=cut),
                    _fc("shift_after_cut", f"整条移到切点之后（开始于 {_ts(cut)}）", start_ms=cut),
                ],
            ))
    return issues


# ---------------------------------------------------------------- 文本/断行类检查

def check_cue_text(cue: Cue, rules: Rules) -> list[Issue]:
    issues: list[Issue] = []
    for no, line in enumerate(cue.lines, 1):
        w = visible_len(line)
        if w > rules.max_chars_per_line:
            issues.append(Issue(
                cue_index=cue.index, issue_type="line_too_long", severity="error",
                message=f"第 {no} 行 {w} 字，超过单行上限 {rules.max_chars_per_line} 字",
                details={"line": no, "width": w, "max_chars_per_line": rules.max_chars_per_line},
                fix_candidates=[_fc("rewrap", "按单行字数上限重新分行")],
            ))
    if len(cue.lines) > rules.max_lines:
        issues.append(Issue(
            cue_index=cue.index, issue_type="too_many_lines", severity="error",
            message=f"共 {len(cue.lines)} 行，超过最大行数 {rules.max_lines}",
            details={"lines": len(cue.lines), "max_lines": rules.max_lines},
            fix_candidates=[
                _fc("rewrap", "重新分行压缩行数"),
                _fc("split_cue", "按句子拆分为多条字幕"),
            ],
        ))
    issues.extend(_break_issues(cue, rules))
    return issues


def _break_issues(cue: Cue, rules: Rules) -> list[Issue]:
    """不自然断行启发式检查（均为 warning）。"""
    out: list[Issue] = []
    lines = cue.lines
    if len(lines) < 2:
        return out

    def warn(issue_type: str, message: str, **details) -> None:
        out.append(Issue(
            cue_index=cue.index, issue_type=issue_type, severity="warning",
            message=message, details=details,
            fix_candidates=[_fc("rewrap", "按标点与行长重新分行")],
        ))

    vis = [visible_text(ln) for ln in lines]
    for i in range(1, len(lines)):
        cur = vis[i].lstrip()
        prev = vis[i - 1].rstrip()
        if not cur or not prev:
            continue
        if cur[0] in CLOSING_PUNCT:
            warn("break_punct_start", f"第 {i + 1} 行以标点「{cur[0]}」开头", line=i + 1)
        if prev[-1] in OPENING_PUNCT:
            warn("break_open_punct_end", f"第 {i} 行以开引号/开括号「{prev[-1]}」结尾", line=i)
        if prev[-1] in CJK_FUNC_WORDS:
            warn("break_func_word", f"第 {i} 行以虚词「{prev[-1]}」结尾", line=i)
        if (prev[-1].isascii() and prev[-1].isalpha()
                and cur[0].isascii() and cur[0].isalpha() and not prev.endswith("-")):
            warn("break_word_split", "英文单词在换行处被截断", line=i)

    widths = [visible_len(ln) for ln in lines]
    if len(lines) == 2 and max(widths) > 0:
        if (min(widths) <= max(2, int(max(widths) * 0.35))
                and max(widths) >= rules.max_chars_per_line * 0.6):
            warn("break_unbalanced", "两行长度悬殊",
                 widths=widths, max_chars_per_line=rules.max_chars_per_line)
    if widths[-1] and widths[-1] <= 2 and max(widths[:-1]) >= rules.max_chars_per_line * 0.5:
        warn("break_orphan", "尾行过短，孤字成行", widths=widths)
    return out


# ---------------------------------------------------------------- 全量质检

def run_qc(
    cues: list[Cue],
    rules: Rules,
    shot_cuts: list[int] | None = None,
    frame_rate: float | None = None,
) -> list[Issue]:
    cuts = sorted(shot_cuts or [])
    issues: list[Issue] = []
    for i, cue in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        issues.extend(check_cue_timing(cue, nxt, rules, cuts))
        issues.extend(check_cue_text(cue, rules))
    issues.sort(key=lambda x: (x.cue_index, x.issue_type))
    return issues


def summarize(issues: list[Issue]) -> dict:
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for it in issues:
        by_type[it.issue_type] = by_type.get(it.issue_type, 0) + 1
        by_severity[it.severity] = by_severity.get(it.severity, 0) + 1
    return {"issue_count": len(issues), "by_type": by_type, "by_severity": by_severity}
