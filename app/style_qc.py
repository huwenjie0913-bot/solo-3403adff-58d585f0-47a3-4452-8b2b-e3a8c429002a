"""字幕样式兼容性校核与安全修复。

用户为目标播放器声明 :class:`~app.schemas.RenderConfig`（目标格式 SRT/WebVTT、
允许的内联标签与属性、标签嵌套深度、是否允许说话人标签、WebVTT cue settings
可用字段），本模块逐条 cue 解析原始行与 settings，检查：

- ``unclosed_tag``         标签未闭合（栈尾残留）
- ``stray_closing_tag``    多余的闭合标签
- ``crossed_tag``          标签交叉嵌套（只诊断，不自动改写）
- ``style_cross_line``     样式标签跨行（在断行处仍处于打开状态）
- ``nesting_too_deep``     嵌套深度超过配置
- ``unknown_attribute``    标签带未知/不被播放器支持的属性
- ``conflicting_style``    同一片段上重复或冲突的样式（如同名标签重复嵌套、
                            两个 font color 覆盖同一文本）
- ``unsupported_tag``      目标格式/配置不支持的内联标签
- ``speaker_not_allowed``  配置不允许说话人标签时出现 ``<v …>``
- ``unsupported_override`` ASS 覆盖标记（``{\\an8}``）
- ``unsupported_timestamp_tag`` 目标格式不支持的卡拉 OK 时间戳标签
- ``unsupported_setting`` / ``invalid_setting_value`` cue settings 字段不被
                            允许，或百分比/对齐枚举值非法
- ``settings_unsupported`` SRT 目标不支持任何 cue settings
- ``identifier_unsupported`` SRT 目标无法表达 cue 标识符
- ``malformed_tag``        残缺的标签（只有 ``<`` 等，只诊断）
- ``roundtrip_drift``      序列化到目标格式再解析后，标识符/可见文本/settings 发生变化

可安全处理的问题生成**保持时间码、可见文本与换行语义**的修复候选（补齐闭合
标签、在断行处成对拆分标签、移除不支持标签/属性/设置项等）；无法保留原意
（交叉标签、非法枚举值、说话人标签、标识符丢失等）只给诊断。

候选 id 形如 ``s{cue 序号}.c{序号}``，在同一版本与渲染配置下重算稳定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .parsing import Cue, parse_subtitles, serialize, strip_tags, visible_text
from .schemas import (
    RenderConfig,
    StyleCueRef,
    StyleFixCandidate,
    StyleIssue,
    StyleReport,
    TimebaseOut,
)
from .timecode import Timebase, format_smpte

# ---------------------------------------------------------------- 标签词法

_TAG_OR_OVERRIDE_RE = re.compile(r"\{[^}]*\}|<!--.*?-->|<[^>]*>")
_BARE_ANGLE_RE = re.compile(r"<(?![^>]*>)")
_TIMESTAMP_INNER_RE = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3}")
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_ATTR_RE = re.compile(
    r"""([A-Za-z_][\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")

# VTT cue settings 枚举字段
_ALIGN_VALUES = {"start", "middle", "end", "left", "right"}
_VERTICAL_VALUES = {"rl", "lr"}
_SETTING_KEYS = {"vertical", "line", "position", "size", "align", "region"}


@dataclass
class Attr:
    name: str
    value: str
    start: int           # 在行内的原始偏移（含整个 name=value）
    end: int


@dataclass
class Token:
    """行内一个词法记号（标签 / ASS 覆盖标记），位置为该行原始字符偏移。"""

    kind: str            # open / close / timestamp / override
    name: str            # 标签名（timestamp/override 为对应字面量）
    raw: str             # 原始文本（含 <> 或 {}）
    line_index: int
    start: int
    end: int
    attrs: list[Attr] = field(default_factory=list)
    # open 配对到的 close（open.close）及 close 配对到的 open（close.open）
    close: "Token | None" = None
    open: "Token | None" = None
    crossed: bool = False  # 该 open/close 对是否构成交叉嵌套


def _parse_tag(line_index: int, m: re.Match) -> Token:
    raw = m.group(0)
    inner = raw[1:-1]
    if _TIMESTAMP_INNER_RE.fullmatch(inner.strip()):
        return Token("timestamp", "timestamp", raw, line_index,
                     m.start(), m.end())
    close = inner.strip().startswith("/")
    body = inner.strip()[1:] if close else inner.strip()
    nm = _NAME_RE.match(body)
    name = nm.group(0).lower() if nm else ""
    tok = Token("close" if close else "open", name, raw, line_index,
                m.start(), m.end())
    if not close and nm:
        # <c.foo.bar> / <v.john John>：点号段为 class
        dot = nm.end()
        head_classes: list[str] = []
        while dot < len(body) and body[dot] == ".":
            j = dot + 1
            while j < len(body) and not body[j].isspace():
                j += 1
            head_classes.append(body[dot + 1:j])
            dot = j
        if head_classes:
            tok.attrs.append(Attr("class", " ".join(head_classes),
                                  nm.end(), dot))
        rest = body[dot:]
        # name=value 形式属性（font color="red" 等）
        # name=value 形式属性（font color="red" 等）；偏移按原始行绝对位置
        used_spans: list[tuple[int, int]] = []
        for am in _ATTR_RE.finditer(rest):
            val = next(g for g in am.groups()[1:] if g is not None)
            # rest 起点在原串中的位置 = 标签起点 + "<" + 标签名/点号类段长度
            abs_start = m.start() + 1 + dot + am.start()
            abs_end = abs_start + len(am.group(0))
            tok.attrs.append(Attr(am.group(1).lower(), val, abs_start, abs_end))
            used_spans.append((am.start(), am.end()))
        # 自由注释：说话人标签 <v 张三> / <lang en>
        annotation = rest
        for s, e in sorted(used_spans, reverse=True):
            annotation = annotation[:s] + annotation[e:]
        annotation = annotation.strip()
        if annotation and name in ("v", "lang"):
            tok.attrs.insert(0, Attr(
                "voice" if name == "v" else "lang", annotation,
                m.start() + 1 + nm.end(), m.end() - 1))
    return tok


def tokenize_line(line_index: int,
                  line: str) -> tuple[list[Token], list[tuple[int, int]]]:
    """切分一行，返回 (标签/覆盖记号列表, 残缺尖括号位置列表)。"""
    tokens: list[Token] = []
    for m in _TAG_OR_OVERRIDE_RE.finditer(line):
        raw = m.group(0)
        if raw.startswith("{"):
            tokens.append(Token("override", "override", raw, line_index,
                                m.start(), m.end()))
        else:
            tokens.append(_parse_tag(line_index, m))
    malformed = [(mm.start(), mm.end())
                 for mm in _BARE_ANGLE_RE.finditer(line)]
    return tokens, malformed


# ---------------------------------------------------------------- 修复编辑

@dataclass
class Edit:
    """针对某行（target=("line", i)）或 settings 字符串的一处替换。"""

    target: tuple[str, int]
    start: int
    end: int
    replacement: str
    found: str = ""
    tokens: list[tuple[tuple[str, int], int]] = field(default_factory=list)


@dataclass
class Candidate:
    id: str
    action: str
    description: str
    cue_index: int
    edits: list[Edit]

    def overlaps(self, other: "Candidate") -> bool:
        """两处修复是否触及相同记号或重叠区间（不能同时应用）。"""
        touched = {k for e in self.edits for k in e.tokens}
        other_touched = {k for e in other.edits for k in e.tokens}
        if touched & other_touched:
            return True
        for a in self.edits:
            for b in other.edits:
                if a.target != b.target:
                    continue
                if a.start < b.end and b.start < a.end:
                    return True
                # 同一位置的纯插入（如补齐闭合标签）顺序敏感，也视为冲突
                if a.start == a.end == b.start == b.end:
                    return True
        return False


def _line_key(line_index: int) -> tuple[str, int]:
    return ("line", line_index)


# ---------------------------------------------------------------- cue 结构解析

def _scan_structure(cue: Cue) -> tuple[list[Token], list[Token], bool]:
    """重放全部行的标签，返回 (全部记号, 未闭合 open 列表, 是否存在交叉)。"""
    all_tokens: list[Token] = []
    stack: list[Token] = []
    # 交叉处被提前结束的标签名：之后匹配的闭合标签视为交叉的一部分而非多余
    ghost_closes: list[str] = []
    crossed = False
    for li, line in enumerate(cue.lines):
        toks, _ = tokenize_line(li, line)
        for t in toks:
            if t.kind in ("override", "timestamp"):
                all_tokens.append(t)
                continue
            if t.kind == "open":
                stack.append(t)
                all_tokens.append(t)
            else:  # close
                names = [x.name for x in stack]
                if t.name not in names:
                    if t.name in ghost_closes:
                        # 交叉恢复：该闭合标签对应被提前结束的标签，消费掉
                        ghost_closes.remove(t.name)
                        t.crossed = True
                    else:
                        all_tokens.append(t)  # 多余闭合
                    continue
                k = len(names) - 1 - names[::-1].index(t.name)  # 最近的同名开标签
                if k != len(stack) - 1:
                    crossed = True
                    for x in stack[k:]:
                        x.crossed = True
                        if x is not stack[k]:
                            ghost_closes.append(x.name)
                    t.crossed = True
                opener = stack[k]
                opener.close = t
                t.open = opener
                del stack[k:]
                all_tokens.append(t)
    return all_tokens, stack, crossed


def _visible_fragment(cue: Cue, open_tok: Token,
                      close_tok: Token | None) -> list[dict]:
    """取标签覆盖区间的可见文本片段（逐行，位置按行内去标签可见文本计）。"""
    out: list[dict] = []
    last_line = (close_tok.line_index if close_tok is not None
                 else len(cue.lines) - 1)
    for li in range(open_tok.line_index, last_line + 1):
        line = cue.lines[li]
        a = open_tok.end if li == open_tok.line_index else 0
        b = (close_tok.start if (close_tok is not None
                                 and li == close_tok.line_index)
             else len(line))
        seg = strip_tags(line[a:b]).strip()
        if seg:
            offset = len(strip_tags(line[:a]))
            vis = visible_text(line)
            out.append({"line_index": li, "text": seg,
                        "start": offset,
                        "end": min(offset + len(seg), len(vis))})
    return out


def _positions(tok: Token, end_tok: Token | None = None) -> list[dict]:
    pos = [{"line_index": tok.line_index, "start": tok.start,
            "end": tok.end}]
    if end_tok is not None and (end_tok.line_index, end_tok.start) != (
            tok.line_index, tok.start):
        pos.append({"line_index": end_tok.line_index,
                    "start": end_tok.start, "end": end_tok.end})
    return pos


# ---------------------------------------------------------------- 主检查

def _allowed_attrs(config: RenderConfig, tag_name: str) -> set[str]:
    return {a.lower() for a in config.allowed_attributes.get(tag_name, [])}


def _check_cue(cue: Cue, config: RenderConfig,
               idseq: dict[int, int]) -> tuple[list[StyleIssue], list[Candidate]]:
    issues: list[StyleIssue] = []
    candidates: list[Candidate] = []

    def next_id() -> str:
        idseq[cue.index] = idseq.get(cue.index, 0) + 1
        return f"s{cue.index}.c{idseq[cue.index]}"

    all_tokens, unclosed_stack, crossed = _scan_structure(cue)

    # ---- 残缺尖括号（只诊断）----
    for li, line in enumerate(cue.lines):
        for mm in _BARE_ANGLE_RE.finditer(line):
            s, e = mm.span()
            issues.append(StyleIssue(
                cue_index=cue.index, issue_type="malformed_tag",
                severity="error",
                message=f"第 {li + 1} 行第 {s + 1} 列存在未闭合的尖括号（标签残缺）",
                positions=[{"line_index": li, "start": s, "end": e}],
                fragments=[], details={"raw": "<"}))

    opens = [t for t in all_tokens if t.kind == "open"]
    closes = [t for t in all_tokens if t.kind == "close"]
    stray = [t for t in closes if t.open is None]
    unclosed = list(unclosed_stack)
    cross_pairs = [t for t in opens
                   if t.close is not None and not t.crossed
                   and t.close.line_index > t.line_index]

    # ---- 跨行 ----
    for t in cross_pairs:
        issues.append(StyleIssue(
            cue_index=cue.index, issue_type="style_cross_line",
            severity="error",
            message=(f"标签 <{t.name}> 在第 {t.line_index + 1} 行打开，"
                     f"直到第 {t.close.line_index + 1} 行才闭合，样式跨行可能"
                     f"被播放器按行重置"),
            positions=_positions(t, t.close),
            fragments=_visible_fragment(cue, t, t.close),
            details={"tag": t.name, "open_line": t.line_index,
                     "close_line": t.close.line_index}))

    # ---- 未闭合 ----
    for t in unclosed:
        issues.append(StyleIssue(
            cue_index=cue.index, issue_type="unclosed_tag",
            severity="error",
            message=f"标签 <{t.name}> 在第 {t.line_index + 1} 行打开后未闭合",
            positions=_positions(t),
            fragments=_visible_fragment(cue, t, None),
            details={"tag": t.name, "line_index": t.line_index}))

    # ---- 多余闭合 ----
    for t in stray:
        issues.append(StyleIssue(
            cue_index=cue.index, issue_type="stray_closing_tag",
            severity="error",
            message=(f"第 {t.line_index + 1} 行出现没有对应开始标签的"
                     f"</{t.name or '?'}>"),
            positions=_positions(t), fragments=[],
            details={"tag": t.name, "line_index": t.line_index}))

    # ---- 交叉（只诊断）----
    seen_ids: set[int] = set()
    cross_openers: list[Token] = []
    for t in opens:
        if t.crossed and id(t) not in seen_ids:
            seen_ids.add(id(t))
            cross_openers.append(t)
    cross_openers.sort(key=lambda x: (x.line_index, x.start))
    for t in cross_openers:
        issues.append(StyleIssue(
            cue_index=cue.index, issue_type="crossed_tag",
            severity="error",
            message=(f"标签 <{t.name}>（第 {t.line_index + 1} 行）与外层标签"
                     f"交叉嵌套，无法在不改变样式含义的情况下自动修复"),
            positions=_positions(t, t.close),
            fragments=_visible_fragment(cue, t, t.close),
            details={"tag": t.name, "open_line": t.line_index,
                     "close_line": (t.close.line_index if t.close else None)}))

    # 结构修复候选延后到逐标签检查之后生成（剥离候选会使补闭合/删闭合冗余）

    # ---- 逐开标签：深度 / 冲突 / 属性 / 支持性（复用结构扫描的记号）----
    depth_stack: list[Token] = []
    for t in all_tokens:
        if t.kind in ("override", "timestamp"):
            continue
        if t.kind == "open":
            depth_stack.append(t)
            _check_open_tag(cue, t, depth_stack, config, issues,
                            candidates, next_id)
        else:
            opener = t.open
            if opener is not None and opener in depth_stack:
                k = depth_stack.index(opener)
                del depth_stack[k:]

    # ---- 结构修复候选：断行成对拆分 + 补齐闭合 + 删除多余闭合 ----
    # 已生成剥离候选的开标签不再补闭合（两个候选互斥，且剥离已覆盖该问题）
    cross_left = [t for t in cross_pairs if not getattr(t, "_unwrapped", False)]
    unclosed_left = [t for t in unclosed
                     if not getattr(t, "_unwrapped", False)]
    stray_left = [t for t in stray if not getattr(t, "_unwrapped", False)]
    if not crossed and (cross_left or unclosed_left or stray_left):
        struct = _structural_candidate(cue, next_id(), cross_left,
                                       unclosed_left, stray_left)
        if struct is not None:
            out = _candidate_out(struct, cue)
            candidates.append(struct)
            for it in issues:
                if it.issue_type in ("style_cross_line", "unclosed_tag",
                                    "stray_closing_tag"):
                    it.fix_candidates.append(out)

    # ---- ASS 覆盖标记 ----
    for t in [x for x in all_tokens if x.kind == "override"]:
        cand = Candidate(
            next_id(), "remove_override",
            f"移除不被 SRT/WebVTT 支持的 ASS 覆盖标记 {t.raw}",
            cue.index,
            [Edit(("line", t.line_index), t.start, t.end, "", t.raw,
                  [(_line_key(t.line_index), t.start)])])
        _emit_with_candidate(cue, issues, candidates, StyleIssue(
            cue_index=cue.index, issue_type="unsupported_override",
            severity="error",
            message=(f"第 {t.line_index + 1} 行含 ASS 覆盖标记 {t.raw}，"
                     f"SRT/WebVTT 播放器不识别"),
            positions=_positions(t), fragments=[],
            details={"raw": t.raw}), cand)

    # ---- 卡拉 OK 时间戳标签（WebVTT 专有，SRT 目标不支持）----
    if config.target_format == "srt":
        for t in [x for x in all_tokens if x.kind == "timestamp"]:
            issue = StyleIssue(
                cue_index=cue.index, issue_type="unsupported_timestamp_tag",
                severity="error",
                message=(f"第 {t.line_index + 1} 行含内联时间戳标签 {t.raw}，"
                         f"为 WebVTT 专有，SRT 播放器不识别"),
                positions=_positions(t), fragments=[],
                details={"raw": t.raw})
            cand = Candidate(
                next_id(), "remove_timestamp_tag",
                f"移除 SRT 不支持的内联时间戳标签 {t.raw}（可见文本不变）",
                cue.index,
                [Edit(("line", t.line_index), t.start, t.end, "", t.raw,
                      [(_line_key(t.line_index), t.start)])])
            _emit_with_candidate(cue, issues, candidates, issue, cand)

    # ---- settings ----
    _check_settings(cue, config, issues, candidates, next_id)

    # ---- 标识符（SRT 无法表达，只诊断）----
    if cue.identifier and config.target_format == "srt":
        issues.append(StyleIssue(
            cue_index=cue.index, issue_type="identifier_unsupported",
            severity="warning",
            message=(f"cue 标识符 {cue.identifier!r} 无法在 SRT 中表达"
                     f"（WebVTT 专有），转换后会丢失"),
            positions=[], fragments=[],
            details={"identifier": cue.identifier}))

    return issues, candidates


def _structural_candidate(cue: Cue, cid: str, cross_pairs: list[Token],
                          unclosed: list[Token], stray: list[Token]
                          ) -> Candidate | None:
    edits: list[Edit] = []
    boundary_count = 0

    # 断行处成对拆分：收集每个行边界上仍打开的标签
    if cross_pairs:
        boundaries: dict[int, list[Token]] = {}
        for t in cross_pairs:
            for b in range(t.line_index, t.close.line_index):
                boundaries.setdefault(b, []).append(t)
        boundary_count = len(boundaries)
        for b in sorted(boundaries):
            active = sorted(boundaries[b], key=lambda x: (x.line_index, x.start))
            for t in reversed(active):
                edits.append(Edit(
                    ("line", b), len(cue.lines[b]), len(cue.lines[b]),
                    f"</{t.name}>", "",
                    [(_line_key(b), t.start)]))
            for t in active:
                edits.append(Edit(
                    ("line", b + 1), 0, 0, t.raw, "",
                    [(_line_key(b), t.start)]))

    # cue 末尾仍未闭合：逆序补齐
    for t in reversed(unclosed):
        li = len(cue.lines) - 1
        edits.append(Edit(
            ("line", li), len(cue.lines[li]), len(cue.lines[li]),
            f"</{t.name}>", "",
            [(_line_key(t.line_index), t.start)]))

    # 多余闭合：删除
    for t in stray:
        edits.append(Edit(
            ("line", t.line_index), t.start, t.end, "", t.raw,
            [(_line_key(t.line_index), t.start)]))

    if not edits:
        return None
    parts = []
    if cross_pairs:
        parts.append(f"在 {boundary_count} 处断行成对拆分跨行标签")
    if unclosed:
        parts.append(f"补齐 {len(unclosed)} 个未闭合标签")
    if stray:
        parts.append(f"移除 {len(stray)} 个多余闭合标签")
    return Candidate(cid, "repair_structure", "；".join(parts), cue.index,
                     edits)


def _check_open_tag(cue: Cue, t: Token, stack: list[Token],
                    config: RenderConfig, issues: list[StyleIssue],
                    candidates: list[Candidate], next_id) -> None:
    name = t.name
    close_tok = t.close
    fragments = _visible_fragment(cue, t, close_tok)

    # ---- 嵌套深度 ----
    if len(stack) > config.max_nesting_depth:
        issue = StyleIssue(
            cue_index=cue.index, issue_type="nesting_too_deep",
            severity="error",
            message=(f"标签 <{name}> 使嵌套深度达到 {len(stack)}，"
                     f"超过播放器配置上限 {config.max_nesting_depth}"),
            positions=_positions(t, close_tok), fragments=fragments,
            details={"tag": name, "depth": len(stack),
                     "max_nesting_depth": config.max_nesting_depth})
        if _can_unwrap(t):
            cand = _unwrap_candidate(cue, t, next_id(),
                                     f"剥离超出嵌套上限的 <{name}> 标签对")
            _emit_with_candidate(cue, issues, candidates, issue, cand)
        else:
            issues.append(issue)

    # ---- 同名样式标签重复嵌套 ----
    same = [x for x in stack[:-1]
            if x.name == name and name in ("i", "b", "u", "c")]
    if same:
        issue = StyleIssue(
            cue_index=cue.index, issue_type="conflicting_style",
            severity="warning",
            message=(f"同一片段被 <{name}> 重复嵌套（第 "
                     f"{same[0].line_index + 1} 行与第 {t.line_index + 1} 行），"
                     f"样式相互冲突"),
            positions=_positions(t, close_tok), fragments=fragments,
            details={"tag": name, "first_line": same[0].line_index})
        if _can_unwrap(t):
            cand = _unwrap_candidate(cue, t, next_id(),
                                     f"剥离内层重复的 <{name}> 标签对")
            _emit_with_candidate(cue, issues, candidates, issue, cand)
        else:
            issues.append(issue)

    # ---- font 同属性不同值覆盖同一片段 ----
    if name == "font":
        for outer in stack[:-1]:
            if outer.name != "font":
                continue
            for a in t.attrs:
                ov = next((x for x in outer.attrs if x.name == a.name), None)
                if ov is not None and ov.value != a.value:
                    issues.append(StyleIssue(
                        cue_index=cue.index, issue_type="conflicting_style",
                        severity="warning",
                        message=(f"同一片段的 {a.name} 属性冲突："
                                 f"{ov.value!r} 与 {a.value!r} 同时生效"),
                        positions=_positions(t, close_tok), fragments=fragments,
                        details={"attribute": a.name,
                                 "outer_value": ov.value,
                                 "inner_value": a.value}))

    # ---- 说话人标签 ----
    if name == "v":
        if not config.allow_speaker_tags:
            issues.append(StyleIssue(
                cue_index=cue.index, issue_type="speaker_not_allowed",
                severity="error",
                message=(f"第 {t.line_index + 1} 行含说话人标签 {t.raw}，"
                         f"目标播放器渲染配置不允许说话人标签；移除会丢失"
                         f"说话人归属信息，需人工处理"),
                positions=_positions(t, close_tok), fragments=fragments,
                details={"raw": t.raw}))
        return

    # ---- 标签支持性 ----
    if name and name not in config.allowed_tags:
        issue = StyleIssue(
            cue_index=cue.index, issue_type="unsupported_tag",
            severity="error",
            message=(f"标签 <{name}> 不在目标播放器允许的内联标签列表 "
                     f"{sorted(config.allowed_tags)} 中"),
            positions=_positions(t, close_tok), fragments=fragments,
            details={"tag": name,
                     "allowed_tags": sorted(config.allowed_tags)})
        if _can_unwrap(t):
            cand = _unwrap_candidate(
                cue, t, next_id(),
                f"剥离不支持的 <{name}> 标签对（保留可见文本）")
            _emit_with_candidate(cue, issues, candidates, issue, cand)
        else:
            issues.append(issue)
        return

    # ---- 属性核对（同一标签的不支持属性聚合为一个候选）----
    allowed = _allowed_attrs(config, name)
    bad_attrs = [a for a in t.attrs if a.name not in allowed]
    if bad_attrs:
        drop = {a.name for a in bad_attrs}
        issue = StyleIssue(
            cue_index=cue.index, issue_type="unknown_attribute",
            severity="error",
            message=(f"标签 <{name}> 存在不被目标播放器支持的属性 "
                     f"{sorted(drop)}（允许：{sorted(allowed) or '无'}）"),
            positions=[{"line_index": t.line_index,
                        "start": a.start, "end": a.end} for a in bad_attrs],
            fragments=fragments,
            details={"tag": name,
                     "attributes": [
                         {"name": a.name, "value": a.value}
                         for a in bad_attrs],
                     "allowed_attributes": sorted(allowed)})
        new_tag = _rebuild_open_tag(t, drop)
        cand = Candidate(
            next_id(), "remove_attribute",
            f"移除 <{name}> 不支持的属性 {sorted(drop)}", cue.index,
            [Edit(("line", t.line_index), t.start, t.end, new_tag, t.raw,
                  [(_line_key(t.line_index), t.start)])])
        _emit_with_candidate(cue, issues, candidates, issue, cand)


def _can_unwrap(t: Token) -> bool:
    """非交叉、非说话人标签即可安全剥离开标签（可见文本/行数不变）。"""
    return not t.crossed and t.name != "v"


def _unwrap_candidate(cue: Cue, t: Token, cid: str,
                      description: str) -> Candidate:
    setattr(t, "_unwrapped", True)
    edits = [Edit(("line", t.line_index), t.start, t.end, "", t.raw,
                  [(_line_key(t.line_index), t.start)])]
    close = _matching_close(cue, t)
    if close is not None:
        setattr(close, "_unwrapped", True)
        edits.append(Edit(
            ("line", close.line_index), close.start, close.end, "",
            close.raw,
            [(_line_key(close.line_index), close.start)]))
    return Candidate(cid, "unwrap_tag", description, cue.index, edits)


def _matching_close(cue: Cue, opener: Token) -> Token | None:
    """找 opener 之后第一个匹配的闭合标签（结构扫描未配对时的兜底）。"""
    for li in range(opener.line_index, len(cue.lines)):
        toks, _ = tokenize_line(li, cue.lines[li])
        for t in toks:
            past = (li > opener.line_index
                    or (li == opener.line_index and t.start >= opener.end))
            if past and t.kind == "close" and t.name == opener.name:
                return t
    return None


def _rebuild_open_tag(tok: Token, drop: set[str]) -> str:
    """去掉 drop 中的属性后重建开始标签。"""
    inner = tok.raw[1:-1]
    for a in sorted([a for a in tok.attrs if a.name in drop],
                    key=lambda a: a.start, reverse=True):
        s = a.start - tok.start - 1
        e = a.end - tok.start - 1
        inner = inner[:s].rstrip() + inner[e:].lstrip()
        inner = re.sub(r"\s{2,}", " ", inner)
    return "<" + inner.strip() + ">"


def _emit_with_candidate(cue: Cue, issues: list[StyleIssue],
                         candidates: list[Candidate], issue: StyleIssue,
                         cand: Candidate) -> None:
    issue.fix_candidates.append(_candidate_out(cand, cue))
    issues.append(issue)
    candidates.append(cand)


# ---------------------------------------------------------------- settings

_PERCENT_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")
_INT_RE = re.compile(r"^-?\d+$")


def _parse_settings(raw: str) -> list[tuple[str, str, int, int]]:
    """拆 cue settings 为 (key, value, start, end)（偏移相对 settings 串）。"""
    out = []
    for m in re.finditer(r"\S+", raw):
        tok = m.group(0)
        if ":" in tok:
            k, v = tok.split(":", 1)
            out.append((k, v, m.start(), m.end()))
        else:
            out.append((tok, "", m.start(), m.end()))
    return out


def _check_settings(cue: Cue, config: RenderConfig, issues: list[StyleIssue],
                    candidates: list[Candidate], next_id) -> None:
    if not cue.settings:
        return
    raw = cue.settings
    if config.target_format == "srt":
        issue = StyleIssue(
            cue_index=cue.index, issue_type="settings_unsupported",
            severity="error",
            message=(f"cue settings {raw!r} 无法在 SRT 中表达（WebVTT 专有），"
                     f"转换后会丢失"),
            positions=[], fragments=[], details={"settings": raw})
        cand = Candidate(next_id(), "drop_settings",
                         "移除 SRT 无法表达的 cue settings（时间码与文本不变）",
                         cue.index,
                         [Edit(("settings", 0), 0, len(raw), "", raw,
                               [(("settings", 0), 0)])])
        _emit_with_candidate(cue, issues, candidates, issue, cand)
        return

    disallowed: list[tuple[str, str, int, int]] = []
    for key, value, s, e in _parse_settings(raw):
        if key not in _SETTING_KEYS:
            issues.append(StyleIssue(
                cue_index=cue.index, issue_type="unsupported_setting",
                severity="error",
                message=f"未知的 cue setting 字段 {key!r}",
                positions=[], fragments=[],
                details={"field": key, "value": value}))
            disallowed.append((key, value, s, e))
            continue
        if key not in config.allowed_cue_settings:
            issue = StyleIssue(
                cue_index=cue.index, issue_type="unsupported_setting",
                severity="error",
                message=(f"cue setting 字段 {key!r} 不在目标播放器允许列表 "
                         f"{config.allowed_cue_settings} 中"),
                positions=[], fragments=[],
                details={"field": key, "value": value,
                         "allowed_settings": config.allowed_cue_settings})
            issues.append(issue)
            disallowed.append((key, value, s, e))
            continue
        invalid = _invalid_setting_value(key, value)
        if invalid:
            issues.append(StyleIssue(
                cue_index=cue.index, issue_type="invalid_setting_value",
                severity="error",
                message=(f"cue setting {key}:{value} 非法：{invalid}"
                         f"（需人工修正）"),
                positions=[], fragments=[],
                details={"field": key, "value": value, "reason": invalid}))

    # 所有“不在白名单”的已知字段聚合为一个整串重写候选（未知字段不给候选）
    known = [d for d in disallowed if d[0] in _SETTING_KEYS]
    if known:
        keep = [(k, v) for k, v, *_ in _parse_settings(raw)
                if k in _SETTING_KEYS and k in config.allowed_cue_settings]
        repl = " ".join(f"{k}:{v}" for k, v in keep)
        cand = Candidate(
            next_id(), "remove_setting",
            f"移除不支持的 cue setting 字段 {sorted({d[0] for d in known})}",
            cue.index,
            [Edit(("settings", 0), 0, len(raw), repl, raw,
                  [(("settings", 0), d[2]) for d in known])])
        for d in known:
            issue = next(
                i for i in reversed(issues)
                if i.cue_index == cue.index
                and i.issue_type == "unsupported_setting"
                and i.details.get("field") == d[0])
            issue.fix_candidates.append(_candidate_out(cand, cue))
        candidates.append(cand)


def _invalid_setting_value(key: str, value: str) -> str | None:
    if key == "align" and value not in _ALIGN_VALUES:
        return f"align 只允许 {sorted(_ALIGN_VALUES)}"
    if key == "vertical" and value not in _VERTICAL_VALUES:
        return f"vertical 只允许 {sorted(_VERTICAL_VALUES)}"
    if key in ("position", "size"):
        if not _PERCENT_RE.fullmatch(value):
            return f"{key} 必须是 0%–100% 的百分比"
        pct = float(value[:-1])
        if not 0 <= pct <= 100:
            return f"{key} 百分比必须在 0–100 之间"
    if key == "line":
        if _PERCENT_RE.fullmatch(value):
            pct = float(value[:-1])
            if not -100 <= pct <= 100:
                return "line 百分比必须在 -100–100 之间"
        elif not _INT_RE.fullmatch(value):
            return "line 必须是整数行号或百分比"
    if key == "region" and not value:
        return "region 引用不能为空"
    return None


# ---------------------------------------------------------------- 编辑应用 / 候选输出

def _apply_edits(cue: Cue, edits: list[Edit]
                 ) -> tuple[list[str], str | None, str | None]:
    lines = list(cue.lines)
    settings = cue.settings
    by_line: dict[int, list[Edit]] = {}
    set_edits: list[Edit] = []
    for e in edits:
        kind, idx = e.target
        if kind == "line":
            by_line.setdefault(idx, []).append(e)
        else:
            set_edits.append(e)
    for idx, es in by_line.items():
        line = lines[idx]
        for e in sorted(es, key=lambda x: x.start, reverse=True):
            assert line[e.start:e.end] == e.found, (
                f"修复区间与原文不一致: {line[e.start:e.end]!r} != {e.found!r}")
            line = line[:e.start] + e.replacement + line[e.end:]
        lines[idx] = line
    for e in sorted(set_edits, key=lambda x: x.start, reverse=True):
        assert (settings or "")[e.start:e.end] == e.found
        settings = ((settings or "")[:e.start] + e.replacement
                    + (settings or "")[e.end:])
    return lines, (settings or None), cue.identifier


def _candidate_out(cand: Candidate, cue: Cue) -> StyleFixCandidate:
    lines, settings, identifier = _apply_edits(cue, cand.edits)
    return StyleFixCandidate(
        id=cand.id, action=cand.action, description=cand.description,
        cue_index=cand.cue_index,
        preview_lines=lines, preview_settings=settings,
        preview_identifier=identifier,
        preserves=["timecode", "visible_text", "line_breaks"])


# ---------------------------------------------------------------- 序列化往返

def _canon_settings(raw: str | None) -> list[tuple[str, str]]:
    if not raw:
        return []
    return sorted((k, v) for k, v, *_ in _parse_settings(raw))


def _roundtrip_drifts(cues: list[Cue], config: RenderConfig,
                      tb: Timebase) -> list[StyleIssue]:
    """序列化到目标格式再解析，比对标识符/可见文本/settings。"""
    text = serialize(cues, tb, config.target_format)
    reparsed, _ = parse_subtitles(text, tb, config.target_format)
    drifts: list[StyleIssue] = []
    for orig, new in zip(cues, reparsed):
        diffs: list[dict] = []
        if (orig.identifier or None) != (new.identifier or None):
            diffs.append({"field": "identifier",
                          "before": orig.identifier, "after": new.identifier})
        if _canon_settings(orig.settings) != _canon_settings(new.settings):
            diffs.append({"field": "settings",
                          "before": orig.settings, "after": new.settings})
        before_vis = [visible_text(x) for x in orig.lines]
        after_vis = [visible_text(x) for x in new.lines]
        if before_vis != after_vis:
            diffs.append({"field": "visible_text",
                          "before": before_vis, "after": after_vis})
        if diffs:
            drifts.append(StyleIssue(
                cue_index=orig.index, issue_type="roundtrip_drift",
                severity="warning",
                message=(f"序列化为 {config.target_format.upper()} 再解析后，"
                         f"{'、'.join(d['field'] for d in diffs)} 发生变化"),
                positions=[], fragments=[], details={"diffs": diffs}))
    return drifts


# ---------------------------------------------------------------- 报告

def _cue_ref(c: Cue, tb: Timebase) -> StyleCueRef:
    return StyleCueRef(
        index=c.index, start_frame=c.start_frame, end_frame=c.end_frame,
        start_ms=tb.frames_to_ms(c.start_frame),
        end_ms=tb.frames_to_ms(c.end_frame),
        start_tc=format_smpte(tb, c.start_frame),
        end_tc=format_smpte(tb, c.end_frame),
        lines=list(c.lines),
        visible_lines=[visible_text(x) for x in c.lines],
        identifier=c.identifier, settings=c.settings)


def run_style_check(cues: list[Cue], config: RenderConfig, tb: Timebase,
                    *, source_format: str, project_id: int,
                    version_id: int, timebase_out: TimebaseOut
                    ) -> tuple[StyleReport, dict[int, list[Candidate]]]:
    """纯函数：同版本 + 同渲染配置重算结果稳定。返回 (报告, {cue序号: 候选})。"""
    idseq: dict[int, int] = {}
    cue_rows: list[dict] = []
    all_candidates: dict[int, list[Candidate]] = {}
    issues: list[StyleIssue] = []
    for cue in cues:
        ci, cands = _check_cue(cue, config, idseq)
        issues.extend(ci)
        all_candidates[cue.index] = cands
        cue_rows.append({"cue": _cue_ref(cue, tb), "issues": list(ci)})

    drifts = _roundtrip_drifts(cues, config, tb)
    issues.extend(drifts)
    row_by_idx = {r["cue"].index: r for r in cue_rows}
    for d in drifts:
        row_by_idx[d.cue_index]["issues"].append(d)

    issues.sort(key=lambda i: (i.cue_index, i.issue_type))
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    cand_ids: set[str] = set()
    for i in issues:
        by_type[i.issue_type] = by_type.get(i.issue_type, 0) + 1
        by_severity[i.severity] = by_severity.get(i.severity, 0) + 1
        for c in i.fix_candidates:
            cand_ids.add(c.id)
    report = StyleReport(
        project_id=project_id, version_id=version_id,
        generated_at=datetime.now(timezone.utc),
        source_format=source_format, target_format=config.target_format,
        timebase=timebase_out, config=config,
        summary={
            "cue_count": len(cues),
            "issue_count": len(issues),
            "by_type": by_type,
            "by_severity": by_severity,
            "candidate_count": len(cand_ids),
            "cues_with_roundtrip_drift": sorted(d.cue_index for d in drifts),
        },
        cues=cue_rows)
    return report, all_candidates


# ---------------------------------------------------------------- 预览 / 应用

class UnknownStyleCandidateError(ValueError):
    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"未知候选 id: {', '.join(ids)}")


class StyleCandidateConflictError(ValueError):
    def __init__(self, cue_index: int, first: str, second: str):
        self.cue_index = cue_index
        super().__init__(
            f"候选 {first} 与 {second} 在第 {cue_index} 条字幕上触及同一标签/"
            f"区间，不能同时应用")


class StyleCandidateUnsafeError(ValueError):
    """候选应用后会改变可见文本或换行，拒绝。"""

    def __init__(self, cid: str, reason: str):
        self.cid = cid
        super().__init__(f"候选 {cid} 不安全：{reason}")


def index_candidates(by_cue: dict[int, list[Candidate]]
                     ) -> dict[str, Candidate]:
    return {c.id: c for cands in by_cue.values() for c in cands}


def _select(by_cue: dict[int, list[Candidate]],
            candidate_ids: list[str] | None) -> list[Candidate]:
    by_id = index_candidates(by_cue)
    if candidate_ids is None:
        return list(by_id.values())
    unknown = [cid for cid in dict.fromkeys(candidate_ids) if cid not in by_id]
    if unknown:
        raise UnknownStyleCandidateError(unknown)
    return [by_id[cid] for cid in dict.fromkeys(candidate_ids)]


def preview_style_fixes(cues: list[Cue],
                        by_cue: dict[int, list[Candidate]],
                        candidate_ids: list[str]) -> list[dict]:
    """返回选定候选的逐条预览（不落库、不改原稿）。"""
    cue_by_idx = {c.index: c for c in cues}
    items: list[dict] = []
    for cand in _select(by_cue, candidate_ids):
        cue = cue_by_idx[cand.cue_index]
        out = _candidate_out(cand, cue)
        items.append({
            "candidate_id": cand.id, "cue_index": cand.cue_index,
            "action": cand.action, "description": cand.description,
            "before_lines": list(cue.lines),
            "before_settings": cue.settings,
            "before_identifier": cue.identifier,
            "after_lines": out.preview_lines,
            "after_settings": out.preview_settings,
            "after_identifier": out.preview_identifier,
            "preserves": out.preserves,
        })
    return items


def apply_style_fixes(cues: list[Cue], by_cue: dict[int, list[Candidate]],
                      candidate_ids: list[str] | None
                      ) -> tuple[list[Cue], list[dict]]:
    """把选定候选应用到 cue，返回 (新 cue 列表, 应用记录)。

    时间码、可见文本与行数/行序必须保持不变；同 cue 候选触及同一标签或
    重叠区间时报冲突。按行内偏移自后向前替换。
    """
    selected = _select(by_cue, candidate_ids)

    # 冲突检测（按 cue）
    groups: dict[int, list[Candidate]] = {}
    for cand in selected:
        groups.setdefault(cand.cue_index, []).append(cand)
    for idx, cands in groups.items():
        for a_i in range(len(cands)):
            for b_i in range(a_i + 1, len(cands)):
                if cands[a_i].overlaps(cands[b_i]):
                    raise StyleCandidateConflictError(
                        idx, cands[a_i].id, cands[b_i].id)

    new_cues = [Cue(c.index, c.start_frame, c.end_frame, list(c.lines),
                    c.identifier, c.settings) for c in cues]
    by_index = {c.index: c for c in new_cues}
    applied: list[dict] = []
    for idx, cands in groups.items():
        cue = by_index[idx]
        before_vis = [visible_text(x) for x in cue.lines]
        edits = [e for cand in cands for e in cand.edits]
        lines, settings, identifier = _apply_edits(cue, edits)
        after_vis = [visible_text(x) for x in lines]
        if after_vis != before_vis:
            raise StyleCandidateUnsafeError(
                cands[0].id, "修复会改变可见文本")
        if len(lines) != len(cue.lines):
            raise StyleCandidateUnsafeError(
                cands[0].id, "修复会改变换行行数")
        cue.lines, cue.settings, cue.identifier = lines, settings, identifier
        for cand in cands:
            applied.append({"candidate_id": cand.id, "cue_index": idx,
                            "action": cand.action})
    applied.sort(key=lambda a: (a["cue_index"], a["candidate_id"]))
    return new_cues, applied
