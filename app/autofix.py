"""自动修复：结合标点、语句长度与可用时间窗重新断句、分行并调整起止时间。

设计原则：
- 每条字幕在其“时间窗”内修复：窗口由允许偏移上限、相邻字幕位置
  （保留最小间隔）和镜头切点共同限定，窗口边界全部在**帧域**给出；
- 阅读速度 / 最长显示时间无法满足时，按句末标点 → 从句标点 → 原子硬拆
  的顺序拆分字幕，并按可见字数比例分配帧；
- 所有结果天然落在合法帧上（整数帧号），无需再做浮点“帧对齐”；
- 规则冲突无法消解的字幕保留原稿，并作为冲突项返回（不覆盖原稿）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from .parsing import Cue, join_lines, strip_tags, visible_len
from .qc import CLOSING_PUNCT, OPENING_PUNCT
from .schemas import Rules
from .timecode import Timebase, ceil_pos, format_smpte

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
            i += 1
            continue
        if ch in ">}":
            in_tag = False
            i += 1
            continue
        if not in_tag and ch in enders:
            if ch in _LATIN_NEED_SPACE and i + 1 < n and not text[i + 1].isspace():
                i += 1
                continue
            j = i + 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            while j < n and text[j] == " ":
                j += 1
            # 剩余内容仅为标签（如尾部 </i></v>）时不拆，标签归前一段
            if j < n and strip_tags(text[j:]).strip():
                pos.append(j)
            i = j
        else:
            i += 1
    return pos


def _split_at(text: str, positions: list[int]) -> list[str]:
    out, last = [], 0
    for p in positions:
        seg = text[last:p].strip()
        if seg and strip_tags(seg).strip():  # 丢弃无可见内容的纯标签段
            out.append(seg)
        last = p
    tail = text[last:].strip()
    if tail and strip_tags(tail).strip():
        out.append(tail)
    return out


_TAG_NAME_RE = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9]*)")


def _tag_name(tag: str) -> str | None:
    m = _TAG_NAME_RE.match(tag)
    return m.group(1).lower() if m else None


def _balance_tags(pieces: list[str]) -> list[str]:
    """拆分后保留说话人/样式标签并使各段标签闭合。

    每段末尾补闭合未关闭的标签，下一段开头重新打开，
    使各段都是格式良好的片段（如 ``<v 张三><i>…</i></v>``）。
    """
    result: list[str] = []
    open_tags: list[str] = []
    for idx, piece in enumerate(pieces):
        if idx > 0 and open_tags:
            piece = "".join(open_tags) + piece
        # 从空栈扫描：段首补开的标签就在文本中，会被正常入栈
        stack: list[str] = []
        for m in re.finditer(r"<[^>]+>", piece):
            tag = m.group(0)
            if tag.startswith("</"):
                name = _tag_name(tag)
                for k in range(len(stack) - 1, -1, -1):
                    if _tag_name(stack[k]) == name:
                        del stack[k]
                        break
            elif not tag.endswith("/>"):
                stack.append(tag)
        if stack:
            piece += "".join(f"</{_tag_name(t)}>" for t in reversed(stack))
        result.append(piece)
        open_tags = stack
    return result


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


def split_text(text: str, rules: Rules, depth: int = 0) -> list[str]:
    """把一段文本拆成多条：句末标点 → 从句标点 → 原子硬拆，超长段递归。"""
    pieces = _split_at(text, _split_positions(text, _SENT_ENDERS))
    if len(pieces) < 2:
        pieces = _split_at(text, _split_positions(text, _CLAUSE_ENDERS))
    if len(pieces) < 2:
        pieces = _hard_split(text)
    pieces = _balance_tags(pieces)
    pieces = _merge_label(pieces)
    if depth >= 4:
        return pieces
    out: list[str] = []
    for p in pieces:
        # 以毫秒规则粗判是否需要继续递归拆分（精确时间运算在帧域进行）
        need_ms = max(rules.min_duration_ms,
                      -((-visible_len(p) * 1000) // int(rules.max_cps)) if visible_len(p) else 0)
        if need_ms > rules.max_duration_ms and visible_len(p) > 1:
            sub = split_text(p, rules, depth + 1)
            if len(sub) > 1:
                out.extend(sub)
                continue
        out.append(p)
    return out


# ---------------------------------------------------------------- 帧域换算

def _need_frames(chars: int, rules: Rules, tb: Timebase) -> int:
    """满足最短显示时间与阅读速度所需的最少帧数（向上取整）。"""
    dur = ceil_pos(Fraction(rules.min_duration_ms) * tb.rate / 1000)
    if chars > 0:
        cps = Fraction(str(rules.max_cps))
        dur = max(dur, ceil_pos(Fraction(chars) / cps * tb.rate))
    return dur


def _max_frames(rules: Rules, tb: Timebase) -> int:
    """最长显示时间对应的帧数：不超过该时长的最大整数帧（向下取整）。"""
    return Fraction(rules.max_duration_ms) * tb.rate // 1000


def _gap_frames(rules: Rules, tb: Timebase) -> int:
    return ceil_pos(Fraction(rules.min_gap_ms) * tb.rate / 1000)


def _offset_frames(rules: Rules, tb: Timebase) -> int:
    """允许偏移上限换算为帧：半向上取整（最近合法帧）。"""
    from .timecode import half_up
    return half_up(Fraction(rules.max_offset_ms) * tb.rate / 1000)


def _tc(f: int, tb: Timebase) -> str:
    return format_smpte(tb, f)


# ---------------------------------------------------------------- 时间窗放置

def _place(dur_need: int, slo: int, shi: int, elo: int, ehi: int,
           orig_s: int, orig_e: int) -> tuple[int, int] | None:
    """在 [slo,shi]×[elo,ehi] 帧窗口内放置时长至少 dur_need 的字幕。

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


# ---------------------------------------------------------------- 校验

def _validate(cue: Cue, prev_end: int | None, rules: Rules,
              cuts: list[int], tb: Timebase) -> list[tuple[str, str]]:
    """修复结果的硬性校验（帧域），返回 (类型, 说明) 列表。"""
    problems: list[tuple[str, str]] = []
    dur = cue.duration_frames
    if dur < _need_frames(0, rules, tb):
        problems.append(("flash", f"显示时长 {dur} 帧低于下限 {rules.min_duration_ms}ms"))
    if Fraction(dur) > Fraction(rules.max_duration_ms) * tb.rate / 1000:
        problems.append(("duration_too_long",
                         f"显示时长 {dur} 帧超过上限 {rules.max_duration_ms}ms"))
    chars = sum(visible_len(ln) for ln in cue.lines)
    if dur > 0 and Fraction(chars) * tb.rate > Fraction(str(rules.max_cps)) * dur:
        problems.append(("cps_exceeded", "阅读速度仍超限"))
    if len(cue.lines) > rules.max_lines:
        problems.append(("too_many_lines", "行数仍超限"))
    for no, ln in enumerate(cue.lines, 1):
        if visible_len(ln) > rules.max_chars_per_line:
            problems.append(("line_too_long", f"第 {no} 行仍超宽"))
    gap = _gap_frames(rules, tb)
    if prev_end is not None and cue.start_frame < prev_end + gap:
        problems.append(("overlap", "与前一条字幕间隔不足"))
    tol = Fraction(rules.shot_tolerance_ms) * tb.rate / 1000
    for c in cuts:
        if Fraction(cue.start_frame) + tol < c < Fraction(cue.end_frame) - tol:
            problems.append(("shot_cross", f"仍跨越镜头切点 {_tc(c, tb)}"))
    return problems


def _conflict_reasons(cue: Cue, rules: Rules, need: int,
                      slo: int, shi: int, elo: int, ehi: int,
                      tb: Timebase) -> list[str]:
    reasons: list[str] = []
    if shi < slo or ehi < elo:
        reasons.append("允许偏移范围与相邻字幕/镜头切点约束冲突，可用时间窗无效")
    max_dur = _max_frames(rules, tb)
    if need > max_dur:
        reasons.append(
            f"按每秒 {rules.max_cps} 字需 {need} 帧，超过最长显示时间 "
            f"{rules.max_duration_ms}ms（{max_dur} 帧），且文本无法进一步拆分")
    window = ehi - slo
    if 0 <= window < need:
        reasons.append(f"满足阅读速度需 {need} 帧，可用时间窗仅 {window} 帧")
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
    tb: Timebase | None = None,
) -> tuple[list[Cue], list[dict], list[dict]]:
    """自动修复字幕（全部在帧域进行）。

    返回 (修复后的 cue 列表, 已应用修复列表, 冲突列表)。
    冲突字幕在新列表中保持原稿不变。
    """
    if tb is None:
        from .timecode import build_timebase
        tb = build_timebase(25)
    cuts = sorted(shot_cuts or [])
    off = _offset_frames(rules, tb)
    gap = _gap_frames(rules, tb)
    max_dur = _max_frames(rules, tb)
    tol = Fraction(rules.shot_tolerance_ms) * tb.rate / 1000
    out: list[Cue] = []
    applied: list[dict] = []
    conflicts: list[dict] = []
    prev_end: int | None = None

    for i, cue in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        # ---- 可用时间窗（帧） ----
        slo = max(0, cue.start_frame - off)
        shi = cue.start_frame + off
        if prev_end is not None:
            slo = max(slo, prev_end + gap)
        elo = cue.end_frame - off
        ehi = cue.end_frame + off
        if nxt is not None:
            ehi = min(ehi, nxt.start_frame - gap)
        cut_after = next((c for c in cuts if Fraction(c) > Fraction(cue.start_frame) + tol), None)
        if cut_after is not None:
            ehi = min(ehi, cut_after)

        text = join_lines(cue.lines)
        chars = visible_len(text)
        need = _need_frames(chars, rules, tb)
        new_cues: list[Cue] | None = None
        actions: list[str] = []

        # ---- 方案一：整条放置 ----
        if need <= max_dur:
            placed = _place(need, slo, shi, elo, ehi, cue.start_frame, cue.end_frame)
            if placed:
                s, e = placed
                lines = rewrap(text, rules)
                if len(lines) <= rules.max_lines:
                    new_cues = [Cue(cue.index, s, e, lines, cue.identifier, cue.settings)]
                    if (s, e) != (cue.start_frame, cue.end_frame):
                        actions.append(
                            f"调整时间轴 {_tc(cue.start_frame, tb)}–"
                            f"{_tc(cue.end_frame, tb)} → {_tc(s, tb)}–{_tc(e, tb)}")
                    if lines != cue.lines:
                        actions.append("按标点与行长重新分行")

        # ---- 方案二：拆分 ----
        if new_cues is None:
            pieces = split_text(text, rules)
            if len(pieces) >= 2:
                needs = [_need_frames(visible_len(p), rules, tb) for p in pieces]
                total = sum(needs) + gap * (len(pieces) - 1)
                s0 = min(max(cue.start_frame, slo), shi)
                if s0 + total > ehi:
                    s0 = ehi - total
                if slo <= s0 <= shi and s0 + total <= ehi and all(nd <= max_dur for nd in needs):
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
                        actions.append(f"按句子拆分为 {len(pieces)} 条并重新分配帧位置")

        # ---- 帧合法 + 校验（整数帧天然对齐，仅需检查窗口/规则） ----
        if new_cues is not None:
            problems: list[tuple[str, str]] = []
            pe = prev_end
            for c in new_cues:
                if c.end_frame > ehi:
                    problems.append(("window_exceeded", "修复后超出可用时间窗"))
                problems.extend(_validate(c, pe, rules, cuts, tb))
                pe = c.end_frame
            if problems:
                new_cues = None

        # ---- 落定或保留原稿 ----
        if new_cues is None:
            out.append(cue)
            conflicts.append({
                "cue_index": cue.index,
                "message": "无法在规则约束内修复，已保留原稿",
                "reasons": _conflict_reasons(cue, rules, need, slo, shi, elo, ehi, tb),
            })
            prev_end = cue.end_frame
            continue
        out.extend(new_cues)
        prev_end = new_cues[-1].end_frame
        if actions:
            applied.append({"cue_index": cue.index, "actions": actions})

    for idx, c in enumerate(out, 1):
        c.index = idx
    return out, applied, conflicts
