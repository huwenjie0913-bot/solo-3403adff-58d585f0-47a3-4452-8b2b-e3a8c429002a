"""SMPTE 时间码与有理数帧运算。

时间基（:class:`Timebase`）以 ``Fraction`` 保存精确帧率，支持：

- 预设帧率 24、25、30、24000/1001（23.976）、30000/1001（29.97）、
  60000/1001（59.94），以及任意浮点帧率（按十进制精确值处理，仅 NDF）；
- non-drop-frame（``HH:MM:SS:FF``）与 drop-frame（``HH:MM:SS;FF``）；
- 项目起始时间码（时间线 0 帧对应的绝对时间码）。

所有换算只用整数 / ``Fraction``，往返转换不产生浮点舍入漂移。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

# ---------------------------------------------------------------- 帧率预设

# 标签 -> (精确帧率, 时间码标称每秒帧数)
PRESETS: dict[str, tuple[Fraction, int]] = {
    "24": (Fraction(24, 1), 24),
    "25": (Fraction(25, 1), 25),
    "30": (Fraction(30, 1), 30),
    "24000/1001": (Fraction(24000, 1001), 24),
    "30000/1001": (Fraction(30000, 1001), 30),
    "60000/1001": (Fraction(60000, 1001), 60),
}
# 常见小数别名
_ALIASES: dict[str, str] = {
    "23.976": "24000/1001",
    "29.97": "30000/1001",
    "59.94": "60000/1001",
}
# 允许 drop-frame 的精确帧率
_DF_RATES = {Fraction(30000, 1001), Fraction(60000, 1001)}


class TimecodeError(ValueError):
    """时间码/帧位置错误，携带出错字段、原值与原因。"""

    def __init__(self, field: str, value: Any, reason: str):
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"{field}={value!r}: {reason}")


def _to_fraction(value: float | int | str | Fraction) -> Fraction:
    """float 走十进制字符串，避免二进制尾数污染。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


def _resolve_fps(fps: float | int | str | Fraction, field: str = "fps") -> tuple[Fraction, int, str]:
    """把帧率输入解析为 (精确帧率, 标称帧率, 预设标签)。

    字符串必须是六个预设之一（或 23.976/29.97/59.94 别名）；
    数字命中预设时取预设精确值，否则按十进制精确值处理（仅 NDF）。
    """
    if isinstance(fps, str):
        key = fps.strip()
        if key in _ALIASES:
            key = _ALIASES[key]
        if key not in PRESETS:
            allowed = "24、25、30、24000/1001、30000/1001、60000/1001"
            raise TimecodeError(field, fps, f"不支持的帧率预设 {key!r}，可选：{allowed}")
        rate, nominal = PRESETS[key]
        return rate, nominal, key

    if isinstance(fps, bool):
        raise TimecodeError(field, fps, "帧率不能为布尔值")
    if isinstance(fps, Fraction):
        rate = fps
    elif isinstance(fps, (int, float)) and fps > 0:
        rate = _to_fraction(fps)
    else:
        raise TimecodeError(field, fps, "帧率必须为正数或预设字符串")

    # 数值命中预设（24/25/30 或 23.976/29.97/59.94）
        for label, (prate, nominal) in PRESETS.items():
            if "/" in label and abs(rate - prate) <= Fraction(1, 100000):
                return prate, nominal, label
            if "/" not in label and rate == prate:
                return prate, nominal, label
    nominal = int(round(float(rate)))
    if nominal <= 0:
        raise TimecodeError(field, fps, "标称帧率必须为正整数")
    return rate, nominal, f"{rate.numerator}/{rate.denominator}"


# ---------------------------------------------------------------- 取整（非负数）

def ceil_pos(x: Fraction) -> int:
    """向正无穷取整（输入非负）。"""
    return -((-x.numerator) // x.denominator)


def half_up(x: Fraction) -> int:
    """半向上取整（输入非负）：0.5 → 1。"""
    return (x.numerator * 2 + x.denominator) // (2 * x.denominator)


# ---------------------------------------------------------------- 时间基

@dataclass(frozen=True)
class Timebase:
    """一个项目的时间基：精确帧率 + DF/NDF + 起始时间码。"""

    rate: Fraction           # 精确帧率（帧/秒）
    nominal: int             # 时间码标称每秒帧数（24/25/30/60…）
    drop_frame: bool
    start_frame: int         # 时间线 0 帧对应的绝对帧号
    start_timecode: str      # 起始时间码原文（规范化形式）
    fps_label: str           # 帧率预设标签或 "num/den"

    # ---- 毫秒 <-> 帧（毫秒是派生量，半向上取整）----

    def ms_to_frames(self, ms: float | int) -> int:
        n = half_up(_to_fraction(ms) * self.rate / 1000)
        return n

    def frames_to_ms(self, frame: int) -> int:
        return half_up(Fraction(frame * 1000) / self.rate)

    def frames_to_ms_exact(self, frame: int) -> Fraction:
        """帧对应的精确毫秒（有理数，无舍入）。"""
        return Fraction(frame * 1000) / self.rate

    # ---- 时间线帧 <-> 绝对帧 ----

    def to_abs(self, timeline_frame: int) -> int:
        return timeline_frame + self.start_frame

    def to_timeline(self, abs_frame: int) -> int:
        return abs_frame - self.start_frame

    # ---- SMPTE 分量 <-> 绝对帧号 ----

    def smpte_to_abs(self, h: int, m: int, s: int, f: int) -> int:
        total_minutes = 60 * h + m
        total_seconds = 60 * total_minutes + s
        if not self.drop_frame:
            return total_seconds * self.nominal + f
        d = self.nominal // 15
        # 到该标签为止被跳过的帧数（SMPTE 标准公式，仅整数运算）
        dropped = d * (total_minutes - total_minutes // 10)
        return total_seconds * self.nominal + f - dropped


def _abs_to_smpte_ndf(abs_frame: int, nominal: int) -> tuple[int, int, int, int]:
    h, rem = divmod(abs_frame, nominal * 3600)
    m, rem = divmod(rem, nominal * 60)
    s, f = divmod(rem, nominal)
    return h, m, s, f


def _abs_to_smpte_df(tb: Timebase, abs_frame: int) -> tuple[int, int, int, int]:
    """drop-frame 反算（SMPTE 标准公式，仅整数运算）。

    每小时 6 个 10 分钟组：组内首分钟完整（nominal*60 帧），
    其余 9 分钟块为 nominal*60-d 帧，块内偏移 0 对应标签 ;0d（;00–;d-1 被跳过）。
    """
    nominal, d = tb.nominal, tb.nominal // 15
    frames_per_hour = nominal * 3600 - d * 54
    group = nominal * 600 - d * 9
    first = nominal * 60
    short = nominal * 60 - d

    h, rem = divmod(abs_frame, frames_per_hour)
    ten, rem = divmod(rem, group)
    if rem < first:
        m0, off = 0, rem
    else:
        r2 = rem - first
        m0 = 1 + r2 // short
        off = r2 % short + d
    s, f = divmod(off, nominal)
    return h, ten * 10 + m0, s, f


def abs_to_smpte(tb: Timebase, abs_frame: int) -> tuple[int, int, int, int]:
    if abs_frame < 0:
        raise TimecodeError("frame", abs_frame, "帧号不能为负数")
    if not tb.drop_frame:
        return _abs_to_smpte_ndf(abs_frame, tb.nominal)
    return _abs_to_smpte_df(tb, abs_frame)


def format_smpte(tb: Timebase, timeline_frame: int, *, sep: str | None = None) -> str:
    """把时间线帧号格式化为 SMPTE 时间码字符串。"""
    h, m, s, f = abs_to_smpte(tb, tb.to_abs(timeline_frame))
    df_sep = ";" if tb.drop_frame else ":"
    last = sep if sep in (":", ";") else df_sep
    return f"{h:02d}:{m:02d}:{s:02d}{last}{f:02d}"


# ---------------------------------------------------------------- 时间码解析

_SMPTE_RE = re.compile(
    r"^(\d{1,3}):(\d{1,2}):(\d{1,2})([:;])(\d{1,2})$"
)
# 也允许全分号写法 00;00;00;00
_SMPTE_ALL_SEMI_RE = re.compile(
    r"^(\d{1,3});(\d{1,2});(\d{1,2});(\d{1,2})$"
)
_FRAME_SUFFIX_RE = re.compile(r"^([+-]?\d+)\s*f$", re.I)


def parse_smpte(text: str, tb: Timebase, field: str = "timecode") -> tuple[int, int, int, int, bool]:
    """解析 SMPTE 时间码，返回 (h,m,s,f,is_drop)，并做全部合法性校验。

    - 分隔符与时间基 NDF/DF 必须匹配（不匹配时明确报错）；
    - FF/SS/MM/HH 越界报错；
    - drop-frame 跳号标签（如 29.97 下 ``00:01:00;00``）报错。
    """
    raw = text.strip()
    is_drop: bool | None = None
    m = _SMPTE_ALL_SEMI_RE.match(raw)
    if m:
        h, mi, s, f = (int(x) for x in m.groups())
        is_drop = True
    else:
        m = _SMPTE_RE.match(raw)
        if not m:
            raise TimecodeError(field, text,
                                "时间码格式应为 HH:MM:SS:FF 或 HH:MM:SS;FF")
        h, mi, s = int(m[1]), int(m[2]), int(m[3])
        is_drop = m[4] == ";"
        f = int(m[5])

    if is_drop and not tb.drop_frame:
        raise TimecodeError(
            field, text,
            "时间码使用 drop-frame 分隔符 ';'，但项目时间基为 non-drop-frame")
    if not is_drop and tb.drop_frame:
        raise TimecodeError(
            field, text,
            "项目时间基为 drop-frame，时间码必须使用 ';' 作为帧分隔符（HH:MM:SS;FF）")
    if h > 99:
        raise TimecodeError(field, text, "小时数越界（最大 99）")
    if mi >= 60:
        raise TimecodeError(field, text, f"分钟数越界（{mi} >= 60）")
    if s >= 60:
        raise TimecodeError(field, text, f"秒数越界（{s} >= 60）")
    if f >= tb.nominal:
        raise TimecodeError(
            field, text,
            f"帧号越界：{f} >= {tb.nominal}（{tb.fps_label} 每秒帧数）")
    if tb.drop_frame:
        d = tb.nominal // 15
        if s == 0 and mi % 10 != 0 and f < d:
            raise TimecodeError(
                field, text,
                f"drop-frame 跳号：{_fmt_hmsf(h, mi, s, 0, ';')} 之后 "
                f"{d} 帧不存在（;00–;{d - 1:02d} 被跳过）")
    return h, mi, s, f, is_drop


def _fmt_hmsf(h: int, m: int, s: int, f: int, sep: str) -> str:
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


def timecode_to_frame(text: str, tb: Timebase, field: str = "timecode") -> int:
    """SMPTE 时间码 -> 时间线帧号（相对项目起始时间码）。"""
    h, m, s, f, _ = parse_smpte(text, tb, field)
    abs_frame = tb.smpte_to_abs(h, m, s, f)
    frame = tb.to_timeline(abs_frame)
    if frame < 0:
        raise TimecodeError(
            field, text,
            f"时间码早于项目起始时间码 {tb.start_timecode}（越界）")
    return frame


# ---------------------------------------------------------------- 构建时间基

def build_timebase(
    fps: float | int | str | Fraction | None = None,
    *,
    drop_frame: bool = False,
    start_timecode: str | None = None,
    field_prefix: str = "timecode",
) -> Timebase:
    """根据帧率、DF 标志与起始时间码构建 :class:`Timebase`。"""
    rate, nominal, label = _resolve_fps(25 if fps is None else fps,
                                        field=f"{field_prefix}.fps")
    if drop_frame and rate not in _DF_RATES:
        raise TimecodeError(
            f"{field_prefix}.drop_frame", drop_frame,
            f"帧率 {label} 不支持 drop-frame（仅 30000/1001 与 60000/1001 可用）")

    default_tc = "00:00:00;00" if drop_frame else "00:00:00:00"
    tc_text = start_timecode or default_tc
    provisional = Timebase(
        rate=rate, nominal=nominal, drop_frame=drop_frame,
        start_frame=0, start_timecode=default_tc, fps_label=label)
    h, m, s, f, _ = parse_smpte(
        tc_text, provisional, field=f"{field_prefix}.start_timecode")
    start_abs = provisional.smpte_to_abs(h, m, s, f)
    return Timebase(
        rate=rate, nominal=nominal, drop_frame=drop_frame,
        start_frame=start_abs,
        start_timecode=_fmt_hmsf(h, m, s, f, ";" if drop_frame else ":"),
        fps_label=label)


# ---------------------------------------------------------------- 统一位置解析

def resolve_position(value: Any, tb: Timebase, field: str) -> int:
    """把毫秒 / 绝对帧号 / SMPTE 时间码统一解析为时间线帧号。

    接受：

    - ``int`` / ``float``：毫秒（半向上取整到帧）；
    - ``"NNNf"``：时间线绝对帧号（相对起始时间码）；
    - ``"HH:MM:SS:FF"`` / ``"HH:MM:SS;FF"``：SMPTE 时间码；
    - 旧格式 ``"HH:MM:SS.mmm"`` 或纯数字字符串：毫秒。
    """
    if isinstance(value, bool):
        raise TimecodeError(field, value, "位置不能为布尔值")

    if isinstance(value, (int, float)):
        if value < 0:
            raise TimecodeError(field, value, "毫秒位置不能为负数（越界）")
        frame = tb.ms_to_frames(value)
        return frame

    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise TimecodeError(field, value, "位置不能为空")
        if _SMPTE_RE.match(s) or _SMPTE_ALL_SEMI_RE.match(s):
            return timecode_to_frame(s, tb, field)
        mf = _FRAME_SUFFIX_RE.match(s)
        if mf:
            frame = int(mf[1])
            if frame < 0:
                raise TimecodeError(field, value, "帧号不能为负数（越界）")
            return frame
        # 旧格式毫秒时间戳 / 纯数字毫秒
        from .parsing import parse_timestamp  # 避免循环导入
        try:
            if re.fullmatch(r"\d+(\.\d+)?", s):
                ms = float(s)
            else:
                ms = parse_timestamp(s)
        except ValueError:
            raise TimecodeError(
                field, value,
                "无法识别的位置：应为毫秒数字、帧号（如 150f）或 "
                "HH:MM:SS:FF / HH:MM:SS;FF 时间码")
        if ms < 0:
            raise TimecodeError(field, value, "毫秒位置不能为负数（越界）")
        return tb.ms_to_frames(ms)

    raise TimecodeError(field, value, "位置类型应为数字或字符串")


def resolve_frame_field(obj: Any, tb: Timebase, field: str) -> int:
    """解析 ``{"ms": ..., "frame": ..., "timecode": "..."}`` 形式的位置对象。"""
    if not isinstance(obj, dict):
        return resolve_position(obj, tb, field)
    present = [k for k in ("ms", "frame", "timecode") if obj.get(k) is not None]
    if len(present) != 1:
        raise TimecodeError(
            field, obj,
            "位置必须且只能提供 ms、frame、timecode 三者之一")
    key = present[0]
    raw = obj[key]
    if key == "ms":
        return resolve_position(raw, tb, f"{field}.ms")
    if key == "frame":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TimecodeError(f"{field}.frame", raw, "帧号必须为整数")
        if raw < 0:
            raise TimecodeError(f"{field}.frame", raw, "帧号不能为负数（越界）")
        return raw
    return timecode_to_frame(str(raw), tb, f"{field}.timecode")
