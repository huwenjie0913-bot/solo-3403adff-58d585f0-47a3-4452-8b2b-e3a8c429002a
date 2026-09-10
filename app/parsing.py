"""SRT / WebVTT 字幕解析与序列化。

解析时保留说话人标签（如 ``<v 张三>``、``- 张三：``）与基础样式标签
（``<i>``、``<b>``、``<u>``、``<font>`` 等），所有检测均基于去除标签后的
可见文本。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 时间戳

_TS_FULL = re.compile(r"^(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")
_TS_SHORT = re.compile(r"^(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")


def parse_timestamp(text: str) -> int:
    """把 ``HH:MM:SS,mmm`` / ``HH:MM:SS.mmm`` / ``MM:SS.mmm`` 解析为毫秒。"""
    text = text.strip()
    m = _TS_FULL.match(text)
    if m:
        h, mi, s, ms = m.groups()
        return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))
    m = _TS_SHORT.match(text)
    if m:
        mi, s, ms = m.groups()
        return (int(mi) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))
    raise ValueError(f"无法解析时间戳: {text!r}")


def format_timestamp(ms: int | float, fmt: str = "srt") -> str:
    """把毫秒格式化为 SRT（逗号）或 VTT（句点）时间戳。"""
    sep = "," if fmt == "srt" else "."
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    mi, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1_000)
    return f"{h:02d}:{mi:02d}:{s:02d}{sep}{milli:03d}"


# ---------------------------------------------------------------- 数据结构

@dataclass
class Cue:
    """一条字幕。lines 保留原始文本（含说话人标签与样式标签）。"""

    index: int
    start_ms: int
    end_ms: int
    lines: list[str]
    identifier: str | None = None  # WebVTT cue 标识符
    settings: str | None = None    # WebVTT cue 设置（位置/对齐等）

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "lines": list(self.lines),
            "identifier": self.identifier,
            "settings": self.settings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(
            index=int(d["index"]),
            start_ms=int(d["start_ms"]),
            end_ms=int(d["end_ms"]),
            lines=list(d["lines"]),
            identifier=d.get("identifier"),
            settings=d.get("settings"),
        )


# ---------------------------------------------------------------- 可见文本

_TAG_RE = re.compile(r"<[^>]+>")
_ASS_RE = re.compile(r"\{[^}]*\}")
_WS_RE = re.compile(r"\s+")


def strip_tags(text: str) -> str:
    """去除样式标签（``<i>`` 等）与 ASS 覆盖标签（``{\\an8}`` 等）。"""
    return _ASS_RE.sub("", _TAG_RE.sub("", text))


def visible_text(line: str) -> str:
    return strip_tags(line).strip()


def visible_len(text: str) -> int:
    """可见字符数：去除标签与空白后的字符数（CPS/行宽均按此计算）。"""
    return len(_WS_RE.sub("", strip_tags(text)))


def cue_visible_len(cue: Cue) -> int:
    return sum(visible_len(ln) for ln in cue.lines)


def is_cjk(ch: str) -> bool:
    return (
        "⺀" <= ch <= "鿿"      # CJK 部首补充 / 统一表意文字
        or "　" <= ch <= "〿"   # CJK 符号与标点
        or "＀" <= ch <= "￯"   # 全角字符
        or "豈" <= ch <= "﫿"   # CJK 兼容表意文字
    )


def join_lines(lines: list[str]) -> str:
    """把多行合并为单段文本。CJK 之间直接相连，其他情况以空格相连。"""
    text = ""
    for ln in lines:
        if not text:
            text = ln
            continue
        prev_vis = strip_tags(text).rstrip()
        next_vis = strip_tags(ln).lstrip()
        if prev_vis and next_vis and is_cjk(prev_vis[-1]) and is_cjk(next_vis[0]):
            text += ln
        else:
            text += " " + ln
    return text


# ---------------------------------------------------------------- 解析

_TIMING_LINE = re.compile(r"^(?P<a>\S+)\s*-->\s*(?P<b>\S+)(?P<rest>.*)$")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def _split_blocks(text: str) -> list[list[str]]:
    blocks = []
    for block in re.split(r"\n[ \t]*\n", text.strip("\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines:
            blocks.append(lines)
    return blocks


def _parse_timing(line: str) -> tuple[int, int, str | None]:
    m = _TIMING_LINE.match(line.strip())
    if not m:
        raise ValueError(f"时间轴行无法解析: {line!r}")
    start, end = parse_timestamp(m["a"]), parse_timestamp(m["b"])
    if end <= start:
        raise ValueError(f"字幕结束时间必须大于开始时间: {line!r}")
    return start, end, (m["rest"].strip() or None)


def parse_srt(text: str) -> list[Cue]:
    text = _normalize(text)
    cues: list[Cue] = []
    for lines in _split_blocks(text):
        # 可选的序号行
        if re.fullmatch(r"\d{1,6}", lines[0].strip()):
            lines = lines[1:]
        if not lines:
            continue
        start, end, settings = _parse_timing(lines[0])
        cues.append(Cue(len(cues) + 1, start, end, lines[1:], settings=settings))
    if not cues:
        raise ValueError("未解析到任何字幕块")
    return cues


def parse_vtt(text: str) -> list[Cue]:
    text = _normalize(text)
    if not text.startswith("WEBVTT"):
        raise ValueError("不是有效的 WebVTT 文件（缺少 WEBVTT 头）")
    lines = text.split("\n")
    # 跳过文件头（到第一个空行为止）
    i = 1
    while i < len(lines) and lines[i].strip():
        i += 1
    cues: list[Cue] = []
    for block in _split_blocks("\n".join(lines[i:])):
        head = block[0].strip()
        # 跳过 NOTE / STYLE / REGION 块
        if head.startswith(("NOTE", "STYLE", "REGION")):
            continue
        identifier = None
        if "-->" not in head:
            identifier = head
            block = block[1:]
            if not block:
                continue
            head = block[0].strip()
        start, end, settings = _parse_timing(head)
        cues.append(Cue(len(cues) + 1, start, end, block[1:],
                        identifier=identifier, settings=settings))
    if not cues:
        raise ValueError("未解析到任何字幕块")
    return cues


def detect_format(text: str) -> str:
    t = _normalize(text).lstrip()
    if t.startswith("WEBVTT"):
        return "vtt"
    if "-->" in t:
        return "srt"
    raise ValueError("无法识别字幕格式（既非 SRT 也非 WebVTT）")


def parse_subtitles(text: str, fmt: str = "auto") -> tuple[list[Cue], str]:
    """解析字幕文本，返回 (cue 列表, 实际格式 "srt"/"vtt")。"""
    if fmt == "auto":
        fmt = detect_format(text)
    if fmt == "srt":
        return parse_srt(text), "srt"
    if fmt == "vtt":
        return parse_vtt(text), "vtt"
    raise ValueError(f"不支持的字幕格式: {fmt!r}")


# ---------------------------------------------------------------- 序列化

def serialize_srt(cues: list[Cue]) -> str:
    parts = []
    for i, c in enumerate(cues, 1):
        timing = f"{format_timestamp(c.start_ms, 'srt')} --> {format_timestamp(c.end_ms, 'srt')}"
        if c.settings:
            timing += f" {c.settings}"
        parts.append("\n".join([str(i), timing, *c.lines]))
    return "\n\n".join(parts) + "\n"


def serialize_vtt(cues: list[Cue]) -> str:
    parts = ["WEBVTT"]
    for c in cues:
        block: list[str] = []
        if c.identifier:
            block.append(c.identifier)
        timing = f"{format_timestamp(c.start_ms, 'vtt')} --> {format_timestamp(c.end_ms, 'vtt')}"
        if c.settings:
            timing += f" {c.settings}"
        block.append(timing)
        block.extend(c.lines)
        parts.append("\n".join(block))
    return "\n\n".join(parts) + "\n"


def serialize(cues: list[Cue], fmt: str) -> str:
    return serialize_srt(cues) if fmt == "srt" else serialize_vtt(cues)


# ---------------------------------------------------------------- 镜头切点

def parse_shot_cuts(items: list) -> list[int]:
    """把镜头切点列表规范化为升序毫秒列表。

    数字按毫秒处理；字符串支持 ``HH:MM:SS.mmm`` 等时间戳或纯数字（毫秒）。
    """
    cuts: list[int] = []
    for it in items or []:
        if isinstance(it, bool):
            raise ValueError(f"无法解析镜头切点: {it!r}")
        if isinstance(it, (int, float)):
            if it < 0:
                raise ValueError("镜头切点不能为负数")
            cuts.append(int(round(it)))
        elif isinstance(it, str):
            s = it.strip()
            if re.fullmatch(r"\d+(\.\d+)?", s):
                cuts.append(int(round(float(s))))
            else:
                cuts.append(parse_timestamp(s))
        else:
            raise ValueError(f"无法解析镜头切点: {it!r}")
    return sorted(set(cuts))
