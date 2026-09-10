"""SRT / WebVTT 字幕解析与序列化。

解析时保留说话人标签（如 ``<v 张三>``、``- 张三：``）与基础样式标签
（``<i>``、``<b>``、``<u>``、``<font>`` 等），所有检测均基于去除标签后的
可见文本。

时间轴以**精确帧号**为唯一真值（``start_frame`` / ``end_frame``）：
毫秒时间戳通过项目时间基半向上取整换算到帧；导出时帧再换算回毫秒，
全过程使用有理数运算，不产生浮点舍入漂移。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .timecode import Timebase, TimecodeError

# ---------------------------------------------------------------- 毫秒时间戳（旧格式，仅解析阶段使用）

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


def _timestamp_to_frames(text: str, tb: Timebase, field: str) -> int:
    return tb.ms_to_frames(parse_timestamp(text))


def format_timestamp(ms: int | float, fmt: str = "srt") -> str:
    """把毫秒格式化为 SRT（逗号）或 VTT（句点）时间戳（仅旧调用/测试使用）。"""
    sep = "," if fmt == "srt" else "."
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    mi, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1_000)
    return f"{h:02d}:{mi:02d}:{s:02d}{sep}{milli:03d}"


def format_frame_timestamp(frame: int, tb: Timebase, fmt: str = "srt") -> str:
    """把时间线帧号格式化为 SRT（逗号）或 VTT（句点）毫秒时间戳。"""
    ms = tb.frames_to_ms(frame)
    return format_timestamp(ms, fmt)


# ---------------------------------------------------------------- 数据结构

@dataclass
class Cue:
    """一条字幕。lines 保留原始文本（含说话人标签与样式标签）。

    时间以帧号表示；毫秒为时间基的派生量。
    """

    index: int
    start_frame: int
    end_frame: int
    lines: list[str]
    identifier: str | None = None  # WebVTT cue 标识符
    settings: str | None = None    # WebVTT cue 设置（位置/对齐等）

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame

    def start_ms(self, tb: Timebase) -> int:
        return tb.frames_to_ms(self.start_frame)

    def end_ms(self, tb: Timebase) -> int:
        return tb.frames_to_ms(self.end_frame)

    def duration_ms(self, tb: Timebase) -> int:
        return self.end_ms(tb) - self.start_ms(tb)

    def start_tc(self, tb: Timebase) -> str:
        from .timecode import format_smpte
        return format_smpte(tb, self.start_frame)

    def end_tc(self, tb: Timebase) -> str:
        from .timecode import format_smpte
        return format_smpte(tb, self.end_frame)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "lines": list(self.lines),
            "identifier": self.identifier,
            "settings": self.settings,
        }

    @classmethod
    def from_dict(cls, d: dict, tb: Timebase | None = None) -> "Cue":
        if "start_frame" in d and "end_frame" in d:
            sf, ef = int(d["start_frame"]), int(d["end_frame"])
        elif tb is not None and "start_ms" in d and "end_ms" in d:
            # 旧版数据迁移：毫秒按项目时间基换算到帧
            sf = tb.ms_to_frames(int(d["start_ms"]))
            ef = tb.ms_to_frames(int(d["end_ms"]))
        else:
            raise ValueError("cue 快照缺少帧位置字段，且无时间基可用于毫秒迁移")
        return cls(
            index=int(d["index"]),
            start_frame=sf,
            end_frame=ef,
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
        or "豈" <= ch <= "﫿"   # CJK 兼容表意文字
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
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")


def _split_blocks(text: str) -> list[list[str]]:
    blocks = []
    for block in re.split(r"\n[ \t]*\n", text.strip("\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines:
            blocks.append(lines)
    return blocks


def _parse_timing(line: str, tb: Timebase, cue_no: int) -> tuple[int, int, str | None]:
    m = _TIMING_LINE.match(line.strip())
    if not m:
        raise TimecodeError(f"cue[{cue_no}].timing", line, "时间轴行无法解析")
    try:
        start = _timestamp_to_frames(m["a"], tb, f"cue[{cue_no}].start")
    except ValueError:
        raise TimecodeError(f"cue[{cue_no}].start", m["a"], "无法解析的开始时间戳")
    try:
        end = _timestamp_to_frames(m["b"], tb, f"cue[{cue_no}].end")
    except ValueError:
        raise TimecodeError(f"cue[{cue_no}].end", m["b"], "无法解析的结束时间戳")
    if end <= start:
        raise TimecodeError(
            f"cue[{cue_no}].end", m["b"],
            f"结束时间必须晚于开始时间（{m['a']}），换算到帧后 end_frame={end} "
            f"<= start_frame={start}")
    return start, end, (m["rest"].strip() or None)


def parse_srt(text: str, tb: Timebase) -> list[Cue]:
    text = _normalize(text)
    cues: list[Cue] = []
    for lines in _split_blocks(text):
        # 可选的序号行
        if re.fullmatch(r"\d{1,6}", lines[0].strip()):
            lines = lines[1:]
        if not lines:
            continue
        start, end, settings = _parse_timing(lines[0], tb, len(cues) + 1)
        cues.append(Cue(len(cues) + 1, start, end, lines[1:], settings=settings))
    if not cues:
        raise TimecodeError("content", text[:80], "未解析到任何字幕块")
    return cues


def parse_vtt(text: str, tb: Timebase) -> list[Cue]:
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
        start, end, settings = _parse_timing(head, tb, len(cues) + 1)
        cues.append(Cue(len(cues) + 1, start, end, block[1:],
                        identifier=identifier, settings=settings))
    if not cues:
        raise TimecodeError("content", text[:80], "未解析到任何字幕块")
    return cues


def detect_format(text: str) -> str:
    t = _normalize(text).lstrip()
    if t.startswith("WEBVTT"):
        return "vtt"
    if "-->" in t:
        return "srt"
    raise ValueError("无法识别字幕格式（既非 SRT 也非 WebVTT）")


def parse_subtitles(text: str, tb: Timebase, fmt: str = "auto") -> tuple[list[Cue], str]:
    """解析字幕文本，返回 (cue 列表, 实际格式 "srt"/"vtt")。"""
    if fmt == "auto":
        fmt = detect_format(text)
    if fmt == "srt":
        return parse_srt(text, tb), "srt"
    if fmt == "vtt":
        return parse_vtt(text, tb), "vtt"
    raise ValueError(f"不支持的字幕格式: {fmt!r}")


# ---------------------------------------------------------------- 序列化

def serialize_srt(cues: list[Cue], tb: Timebase) -> str:
    parts = []
    for i, c in enumerate(cues, 1):
        timing = (f"{format_frame_timestamp(c.start_frame, tb, 'srt')} --> "
                  f"{format_frame_timestamp(c.end_frame, tb, 'srt')}")
        if c.settings:
            timing += f" {c.settings}"
        parts.append("\n".join([str(i), timing, *c.lines]))
    return "\n\n".join(parts) + "\n"


def serialize_vtt(cues: list[Cue], tb: Timebase) -> str:
    parts = ["WEBVTT"]
    for c in cues:
        block: list[str] = []
        if c.identifier:
            block.append(c.identifier)
        timing = (f"{format_frame_timestamp(c.start_frame, tb, 'vtt')} --> "
                  f"{format_frame_timestamp(c.end_frame, tb, 'vtt')}")
        if c.settings:
            timing += f" {c.settings}"
        block.append(timing)
        block.extend(c.lines)
        parts.append("\n".join(block))
    return "\n\n".join(parts) + "\n"


def serialize(cues: list[Cue], tb: Timebase, fmt: str) -> str:
    return serialize_srt(cues, tb) if fmt == "srt" else serialize_vtt(cues, tb)


# ---------------------------------------------------------------- 镜头切点

def parse_shot_cuts(items: list, tb: Timebase) -> list[int]:
    """把镜头切点列表统一换算为升序去重的**时间线帧号**列表。

    数字与纯数字字符串按毫秒（半向上取整到帧）；``"NNNf"`` 为绝对帧号；
    ``HH:MM:SS:FF`` / ``HH:MM:SS;FF`` 为 SMPTE 时间码；
    旧格式 ``HH:MM:SS.mmm`` 按毫秒。
    """
    from .timecode import resolve_frame_field

    cuts: list[int] = []
    for no, it in enumerate(items or []):
        field = f"shot_cuts[{no}]"
        if isinstance(it, bool):
            raise TimecodeError(field, it, "切点不能为布尔值")
        cuts.append(resolve_frame_field(it, tb, field))
    return sorted(set(cuts))
