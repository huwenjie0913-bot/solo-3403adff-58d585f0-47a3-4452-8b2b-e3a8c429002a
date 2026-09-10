"""字幕版本差异比较：按文本对齐，报告新增/删除/修改。

时间差异以帧号给出（同时附 SMPTE 时间码与毫秒位移），全部为精确整数运算。
"""
from __future__ import annotations

import difflib

from .parsing import Cue, join_lines, strip_tags
from .timecode import Timebase


def _key(cue: Cue) -> str:
    return strip_tags(join_lines(cue.lines)).strip()


def _cue_changes(old: Cue, new: Cue, tb: Timebase) -> dict:
    ch: dict = {}
    if old.start_frame != new.start_frame:
        shift = new.start_frame - old.start_frame
        ch["start"] = {
            "from_frame": old.start_frame, "to_frame": new.start_frame,
            "shift_frames": shift,
            "from_timecode": _tc(old.start_frame, tb),
            "to_timecode": _tc(new.start_frame, tb),
            "shift_ms": tb.frames_to_ms(new.start_frame) - tb.frames_to_ms(old.start_frame),
        }
    if old.end_frame != new.end_frame:
        shift = new.end_frame - old.end_frame
        ch["end"] = {
            "from_frame": old.end_frame, "to_frame": new.end_frame,
            "shift_frames": shift,
            "from_timecode": _tc(old.end_frame, tb),
            "to_timecode": _tc(new.end_frame, tb),
            "shift_ms": tb.frames_to_ms(new.end_frame) - tb.frames_to_ms(old.end_frame),
        }
    if old.lines != new.lines:
        ch["text"] = {"from": old.lines, "to": new.lines}
    return ch


def _tc(frame: int, tb: Timebase) -> str:
    from .timecode import format_smpte
    return format_smpte(tb, frame)


def diff_cues(old: list[Cue], new: list[Cue], tb: Timebase) -> dict:
    a = [_key(c) for c in old]
    b = [_key(c) for c in new]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    changes: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                o, n = old[i1 + k], new[j1 + k]
                ch = _cue_changes(o, n, tb)
                if ch:
                    changes.append({"type": "changed", "old_index": o.index,
                                    "new_index": n.index, "changes": ch})
        elif tag == "delete":
            for o in old[i1:i2]:
                changes.append({"type": "removed", "old_index": o.index, "text": _key(o)})
        elif tag == "insert":
            for n in new[j1:j2]:
                changes.append({"type": "added", "new_index": n.index, "text": _key(n)})
        else:  # replace：按位置配对为修改，多余的计为增删
            o_seg, n_seg = old[i1:i2], new[j1:j2]
            for k in range(min(len(o_seg), len(n_seg))):
                ch = _cue_changes(o_seg[k], n_seg[k], tb)
                ch.setdefault("text", {"from": o_seg[k].lines, "to": n_seg[k].lines})
                changes.append({"type": "changed", "old_index": o_seg[k].index,
                                "new_index": n_seg[k].index, "changes": ch})
            for o in o_seg[len(n_seg):]:
                changes.append({"type": "removed", "old_index": o.index, "text": _key(o)})
            for n in n_seg[len(o_seg):]:
                changes.append({"type": "added", "new_index": n.index, "text": _key(n)})

    summary = {
        "added": sum(1 for c in changes if c["type"] == "added"),
        "removed": sum(1 for c in changes if c["type"] == "removed"),
        "changed": sum(1 for c in changes if c["type"] == "changed"),
        "cue_count_from": len(old),
        "cue_count_to": len(new),
    }
    return {"summary": summary, "changes": changes}
