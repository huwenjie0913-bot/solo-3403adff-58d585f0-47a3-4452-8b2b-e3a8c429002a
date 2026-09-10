"""自动修复：结合标点、语句长度与可用时间窗重新断句、分行并调整起止时间。

设计原则：
- 每条字幕在其“时间窗”内修复：窗口由允许偏移上限、相邻字幕位置
  （保留最小间隔）和镜头切点共同限定；
- 阅读速度 / 最长显示时间无法满足时，按句末标点 → 从句标点 → 原子硬拆
  的顺序拆分字幕，并按可见字数比例分配时间；
- 修复后的时间点按帧率对齐；
- 规则冲突无法消解的字幕保留原稿，并作为冲突项返回（不覆盖原稿）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .parsing import Cue, format_timestamp, join_lines, strip_tags, visible_len
from .qc import CLOSING_PUNCT, OPENING_PUNCT
from .schemas import Rules

# ---------------------------------------------------------------- 分词

_TOKEN_RE = re.compile(
    r"<[^>]+>"                       # 样式/说话人标签（零宽，附着于后随原子）
    r"|\{[^}]*\}"                    # ASS 覆盖标签
    r"|\s+"                          # 空白（可折叠的分隔符）
    r"|[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*"  # 拉丁单词
    r"|.",                           # 其他单字（CJK、标点）
    re.S,
)


@dataclass
class _Unit:
    text: str                 # 可见原子（字 / 词 / 标点）
    prefix: str = ""          # 前置标签
    suffix: str = ""          # 后置标签
    space_before: bool = False

    @property
    def width(self) -> int:
        return len(self.text)


def _tokenize(text: str) -> list[_Unit]:
    units: list[_Unit] = []
    pending_tags = ""
    space = False
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok.startswith("<") or (tok.startswith("{") and tok.endswith("}")):
            pending_tags += tok
        elif tok.isspace():
            space = True
        else:
            units.append(_Unit(text=tok, prefix=pending_tags,
                               space_before=space if units else False))
            pending_tags, space = "", False
    if pending_tags and units:  # 尾部标签附到最后一个原子
        units[-1].suffix += pending_tags
    return units


def _units_text(units: list[_Unit]) -> str:
    out = []
    for i, u in enumerate(units):
        seg = " " if (i and u.space_before) else ""
        seg += u.prefix + u.text + u.suffix
        out.append(seg)
    return "".join(out)


def _line_width(line: list[_Unit]) -> int:
    return sum(u.width for u in line) + sum(1 for u in line[1:] if u.space_before)


# ---------------------------------------------------------------- 重新分行

def _split_overlong(units: list[_Unit], limit: int) -> list[_Unit]:
    """把超过单行上限的长单词用连字符硬拆（CJK 为单字原子，不会触发）。"""
    out: list[_Unit] = []
    for u in units:
        if u.width <= limit or limit <= 2:
            out.append(u)
            continue
        pieces = []
        t = u.text
        while len(t) > limit - 1:
            pieces.append(t[: limit - 1] + "-")
            t = t[limit - 1:]
        pieces.append(t)
        for j, p in enumerate(pieces):
            out.append(_Unit(
                text=p,
                prefix=u.prefix if j == 0 else "",
                suffix=u.suffix if j == len(pieces) - 1 else "",
                space_before=u.space_before if j == 0 else False,
            ))
    return out


def rewrap(text: str, rules: Rules) -> list[str]:
    """按单行字数上限重新分行，兼顾避头尾与两行均衡。"""
    limit = rules.max_chars_per_line
    units = _split_overlong(_tokenize(text), limit)
    if not units:
        return [""]

    lines: list[list[_Unit]] = [[units[0]]]
    for u in units[1:]:
        cur = lines[-1]
        need = u.width + (1 if u.space_before else 0)
        if _line_width(cur) + need > limit and cur:
            # 避头尾：闭合标点不置行首，把上一行末尾原子一并下移
            if u.text in CLOSING_PUNCT and len(cur) >= 2 and cur[-1].text not in OPENING_PUNCT:
                moved = cur.pop()
                moved.space_before = False
                u.space_before = False
                lines.append([moved, u])
            else:
                u.space_before = False
                lines.append([u])
        else:
            cur.append(u)

    _fix_opening_at_eol(lines, limit)
    _balance(lines, limit)
    return [_units_text(ln) for ln in lines]


def _fix_opening_at_eol(lines: list[list[_Unit]], limit: int) -> None:
    """行尾的开引号/开括号尽量移到下一行行首。"""
    for i in range(len(lines) - 1):
        line = lines[i]
        while len(line) > 1 and line[-1].text in OPENING_PUNCT:
            u = line[-1]
            nxt = lines[i + 1]
            if _line_width(nxt) + u.width <= limit:
                line.pop()
                u.space_before = False
                nxt.insert(0, u)
            else:
                break


def _balance(lines: list[list[_Unit]], limit: int) -> None:
    """两行字幕长度悬殊时，从较长行向较短行挪动原子使其均衡。"""
    if len(lines) != 2:
        return
    for _ in range(8):
        w1, w2 = _line_width(lines[0]), _line_width(lines[1])
        if abs(w1 - w2) <= 3:
            return
        if w1 > w2:
            if len(lines[0]) < 2:
                return
            u = lines[0][-1]
            if u.text in OPENING_PUNCT or u.text in CLOSING_PUNCT:
                return
            lines[0].pop()
            u.space_before = False
            lines[1].insert(0, u)
            if abs(_line_width(lines[0]) - _line_width(lines[1])) >= abs(w1 - w2):
                lines[1].pop(0)
                lines[0].append(u)
                return
        else:
            if len(lines[1]) < 2:
                return
            u = lines[1][0]
            if u.text in CLOSING_PUNCT or u.text in OPENING_PUNCT:
                return
            if lines[0] and lines[0][-1].text in OPENING_PUNCT:
                return
            if _line_width(lines[0]) + u.width + (1 if u.space_before else 0) > limit:
                return
            lines[1].pop(0)
            lines[0].append(u)
            if abs(_line_width(lines[0]) - _line_width(lines[1])) >= abs(w1 - w2):
                lines[0].pop()
                lines[1].insert(0, u)
                return


# ---------------------------------------------------------------- 断句

_SENT_ENDERS = "。！？!?…."
_CLAUSE_ENDERS = "，、；：,;:—"
_CLOSERS = "”’\"')]}》】」』"      # 句末标点之后跟随的闭合符号
# 拉丁句读符号只有在后跟空白/结尾时才算断点
_LATIN_NEED_SPACE = ".,;:"


def _split_positions(text: str, enders: str) -> list[int]:
    pos: list[int] = []
    i, n = 0, len(text)
    in_tag = False
    while i < n:
        ch = text[i]
        if ch in "<{":
            in_tag = True
        elif ch in ">}":
            in_tag = False
        elif not in_tag and ch in enders:
            if ch in _LATIN_NEED_SPACE and i + 1 < n and not text[i + 1].isspace():
                i += 1
                continue
            j = i + 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            while j < n and text[j] == " ":
                j += 1
            if j < n:
                pos.append(j)
            i = j
        else:
            i += 1
    return pos


def _split_at(text: str, positions: list[int]) -> list[str]:
    out, last = [], 0
    for p in positions:
        seg = text[last:p].strip()
        if seg:
            out.append(seg)
        last = p
    tail = text[last:].strip()
    if tail:
        out.append(tail)
    return out


_LABEL_RE = re.compile(r"^[-–—]?\s*\S{1,12}[：:]$")


def _merge_label(pieces: list[str]) -> list[str]:
    """说话人标签（如 ``- 张三：``、``<v 张三>``）不单独成段，并入后句。"""
    while len(pieces) >= 2:
        first = pieces[0]
        plain = strip_tags(first).strip()
        if visible_len(first) == 0 or _LABEL_RE.match(plain):
            pieces = [join_lines([first, pieces[1]])] + pieces[2:]
        else:
            break
    return pieces


def _hard_split(text: str) -> list[str]:
    """无标点可断时，按原子在最接近中点处硬拆（优先空格/标点后）。"""
    units = _tokenize(text)
    if len(units) < 2:
        return [text]
    total = sum(u.width for u in units)
    best_k, best_score, acc = 1, float("inf"), 0
    for k in range(1, len(units)):
        acc += units[k - 1].width
        score = abs(total / 2 - acc)
        if units[k - 1].text[-1] in "，、；：,;:。！？!?…":
            score -= 2
        if units[k].space_before:
            score -= 1
        if score < best_score:
            best_score, best_k = score, k
    return [_units_text(units[:best_k]), _units_text(units[best_k:])]


def _need_ms(chars: int, rules: Rules) -> int:
    """满足阅读速度所需的最短显示时长。"""
    if chars <= 0:
        return rules.min_duration_ms
    return max(rules.min_duration_ms, math.ceil(chars / rules.max_cps * 1000))


def split_text(text: str, rules: Rules, depth: int = 0) -> list[str]:
    """把一段文本拆成多条：句末标点 → 从句标点 → 原子硬拆，超长段递归。"""
    pieces = _split_at(text, _split_positions(text, _SENT_ENDERS))
    if len(pieces) < 2:
        pieces = _split_at(text, _split_positions(text, _CLAUSE_ENDERS))
    if len(pieces) < 2:
        pieces = _hard_split(text)
    pieces = _merge_label(pieces)
    if depth >= 4:
        return pieces
    out: list[str] = []
    for p in pieces:
        if _need_ms(visible_len(p), rules) > rules.max_duration_ms and visible_len(p) > 1:
            sub = split_text(p, rules, depth + 1)
            if len(sub) > 1:
                out.extend(sub)
                continue
        out.append(p)
    return out


# ---------------------------------------------------------------- 时间窗放置

def _place(dur_need: int, slo: int, shi: int, elo: int, ehi: int,
           orig_s: int, orig_e: int) -> tuple[int, int] | None:
    """在 [slo,shi]×[elo,ehi] 窗口内放置时长至少 dur_need 的字幕。

    优先贴近原始起止时间；放不下时尝试向前挪。返回 (start, end) 或 None。
    """
    if slo > shi or elo > ehi:
        return None
    for s in (min(max(orig_s, slo), shi), slo, shi):
        e = min(max(orig_e, elo), ehi)
        if e - s >= dur_need:
            return s, e
        e = s + dur_need
        if elo <= e <= ehi:
            return s, e
    s = ehi - dur_need
    if slo <= s <= shi and ehi >= elo:
        return s, ehi
    return None


def _snap(s: int, e: int, frame: float | None, need: int, ehi: int) -> tuple[int, int]:
    """按帧对齐：起点向上、终点向下取整；时长不足时在窗口内补足。"""
    if not frame:
        return s, e
    s2 = int(math.ceil(s / frame - 1e-6) * frame)
    e2 = int(math.floor(e / frame + 1e-6) * frame)
    while e2 - s2 < need and e2 + frame <= ehi + 1e-6:
        e2 += int(round(frame))
    if e2 <= s2:
        e2 = s2 + int(round(frame))
    return s2, e2


# ---------------------------------------------------------------- 校验

def _validate(cue: Cue, prev_end: int | None, rules: Rules,
              cuts: list[int]) -> list[tuple[str, str]]:
    """修复结果的硬性校验，返回 (类型, 说明) 列表。"""
    problems: list[tuple[str, str]] = []
    dur = cue.duration_ms
    if dur < rules.min_duration_ms:
        problems.append(("flash", f"显示时长 {dur}ms 低于下限 {rules.min_duration_ms}ms"))
    if dur > rules.max_duration_ms:
        problems.append(("duration_too_long",
                         f"显示时长 {dur}ms 超过上限 {rules.max_duration_ms}ms"))
    chars = sum(visible_len(ln) for ln in cue.lines)
    if dur > 0 and chars / (dur / 1000.0) > rules.max_cps + 1e-6:
        problems.append(("cps_exceeded", "阅读速度仍超限"))
    if len(cue.lines) > rules.max_lines:
        problems.append(("too_many_lines", "行数仍超限"))
    for no, ln in enumerate(cue.lines, 1):
        if visible_len(ln) > rules.max_chars_per_line:
            problems.append(("line_too_long", f"第 {no} 行仍超宽"))
    if prev_end is not None and cue.start_ms < prev_end + rules.min_gap_ms:
        problems.append(("overlap", "与前一条字幕间隔不足"))
    tol = rules.shot_tolerance_ms
    for c in cuts:
        if cue.start_ms + tol < c < cue.end_ms - tol:
            problems.append(("shot_cross", f"仍跨越镜头切点 {c}ms"))
    return problems


def _conflict_reasons(cue: Cue, rules: Rules, need: int,
                      slo: int, shi: int, elo: int, ehi: int) -> list[str]:
    reasons: list[str] = []
    if shi < slo or ehi < elo:
        reasons.append("允许偏移范围与相邻字幕/镜头切点约束冲突，可用时间窗无效")
    window = ehi - slo
    if need > rules.max_duration_ms:
        reasons.append(
            f"按每秒 {rules.max_cps} 字需 {need}ms，超过最长显示时间 "
            f"{rules.max_duration_ms}ms，且文本无法进一步拆分")
    if 0 <= window < need:
        reasons.append(f"满足阅读速度需 {need}ms，可用时间窗仅 {window}ms")
    total = visible_len(join_lines(cue.lines))
    if total > rules.max_chars_per_line * rules.max_lines:
        reasons.append("文本超出最大行数可容纳字数，且时间窗内无法拆分")
    if not reasons:
        reasons.append("规则冲突无法消解")
    return reasons


# ---------------------------------------------------------------- 主流程

def auto_fix(
    cues: list[Cue],
    rules: Rules,
    shot_cuts: list[int] | None = None,
    frame_rate: float | None = None,
) -> tuple[list[Cue], list[dict], list[dict]]:
    """自动修复字幕。

    返回 (修复后的 cue 列表, 已应用修复列表, 冲突列表)。
    冲突字幕在新列表中保持原稿不变。
    """
    frame = 1000.0 / frame_rate if frame_rate and frame_rate > 0 else None
    cuts = sorted(shot_cuts or [])
    off, gap = rules.max_offset_ms, rules.min_gap_ms
    out: list[Cue] = []
    applied: list[dict] = []
    conflicts: list[dict] = []
    prev_end: int | None = None

    for i, cue in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        # ---- 可用时间窗 ----
        slo = max(0, cue.start_ms - off)
        shi = cue.start_ms + off
        if prev_end is not None:
            slo = max(slo, prev_end + gap)
        elo = cue.end_ms - off
        ehi = cue.end_ms + off
        if nxt is not None:
            ehi = min(ehi, nxt.start_ms - gap)
        tol = rules.shot_tolerance_ms
        cut_after = next((c for c in cuts if c > cue.start_ms + tol), None)
        if cut_after is not None:
            ehi = min(ehi, cut_after)

        text = join_lines(cue.lines)
        chars = visible_len(text)
        need = _need_ms(chars, rules)
        new_cues: list[Cue] | None = None
        actions: list[str] = []

        # ---- 方案一：整条放置 ----
        if need <= rules.max_duration_ms:
            placed = _place(need, slo, shi, elo, ehi, cue.start_ms, cue.end_ms)
            if placed:
                s, e = placed
                lines = rewrap(text, rules)
                if len(lines) <= rules.max_lines:
                    new_cues = [Cue(cue.index, s, e, lines, cue.identifier, cue.settings)]
                    if (s, e) != (cue.start_ms, cue.end_ms):
                        actions.append(
                            f"调整时间轴 {format_timestamp(cue.start_ms, 'vtt')}–"
                            f"{format_timestamp(cue.end_ms, 'vtt')} → "
                            f"{format_timestamp(s, 'vtt')}–{format_timestamp(e, 'vtt')}")
                    if lines != cue.lines:
                        actions.append("按标点与行长重新分行")

        # ---- 方案二：拆分 ----
        if new_cues is None:
            pieces = split_text(text, rules)
            if len(pieces) >= 2:
                needs = [_need_ms(visible_len(p), rules) for p in pieces]
                total = sum(needs) + gap * (len(pieces) - 1)
                s0 = min(max(cue.start_ms, slo), shi)
                if s0 + total > ehi:
                    s0 = ehi - total
                if slo <= s0 <= shi and s0 + total <= ehi and all(
                        nd <= rules.max_duration_ms for nd in needs):
                    trial: list[Cue] = []
                    s = s0
                    for p, nd in zip(pieces, needs):
                        lines = rewrap(p, rules)
                        if len(lines) > rules.max_lines:
                            break
                        trial.append(Cue(cue.index, s, s + nd, lines,
                                         cue.identifier, cue.settings))
                        s += nd + gap
                    else:
                        new_cues = trial
                        actions.append(f"按句子拆分为 {len(pieces)} 条并重新分配时间轴")

        # ---- 帧对齐 + 校验 ----
        if new_cues is not None:
            snapped: list[Cue] = []
            pe = prev_end
            for c in new_cues:
                need_c = _need_ms(sum(visible_len(l) for l in c.lines), rules)
                s, e = _snap(c.start_ms, c.end_ms, frame, need_c, ehi)
                if pe is not None and s < pe + gap:
                    # 帧对齐不得侵蚀与前一条的最小间隔
                    s = pe + gap
                    if frame:
                        s = int(math.ceil(s / frame - 1e-6) * frame)
                    e = max(e, s + need_c)
                    if frame:
                        e = int(math.floor(e / frame + 1e-6) * frame)
                        while e - s < need_c and e + frame <= ehi + 1e-6:
                            e += int(round(frame))
                snapped.append(Cue(cue.index, s, e, c.lines, c.identifier, c.settings))
                pe = e
            problems: list[tuple[str, str]] = []
            pe = prev_end
            for c in snapped:
                if c.end_ms > ehi:
                    problems.append(("window_exceeded", "帧对齐后超出可用时间窗"))
                problems.extend(_validate(c, pe, rules, cuts))
                pe = c.end_ms
            if problems:
                new_cues = None
            else:
                new_cues = snapped

        # ---- 落定或保留原稿 ----
        if new_cues is None:
            out.append(cue)
            conflicts.append({
                "cue_index": cue.index,
                "message": "无法在规则约束内修复，已保留原稿",
                "reasons": _conflict_reasons(cue, rules, need, slo, shi, elo, ehi),
            })
            prev_end = cue.end_ms
            continue
        out.extend(new_cues)
        prev_end = new_cues[-1].end_ms
        if actions:
            applied.append({"cue_index": cue.index, "actions": actions})

    for idx, c in enumerate(out, 1):
        c.index = idx
    return out, applied, conflicts
