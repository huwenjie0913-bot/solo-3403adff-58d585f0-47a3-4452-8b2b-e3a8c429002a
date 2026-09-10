"""SMPTE 时间码 / 有理数帧运算 / 结构化 cue / 帧导出 测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.timecode import (
    Timebase,
    TimecodeError,
    abs_to_smpte,
    build_timebase,
    format_smpte,
    half_up,
    resolve_position,
)

client = TestClient(app)


# ---------------------------------------------------------------- 时间基单元

@pytest.mark.parametrize("fps,df", [
    ("24", False), ("25", False), ("30", False),
    ("24000/1001", False), ("30000/1001", False), ("30000/1001", True),
    ("60000/1001", False), ("60000/1001", True),
])
def test_smpte_roundtrip_all_presets(fps, df):
    tb = build_timebase(fps, drop_frame=df)
    total = tb.nominal * 3600 * (2 if df else 1)
    for af in range(0, total, 97):
        h, m, s, f = abs_to_smpte(tb, af)
        assert tb.smpte_to_abs(h, m, s, f) == af, (fps, af, (h, m, s, f))
    # 小时末帧
    h, m, s, f = abs_to_smpte(tb, total - 1)
    assert tb.smpte_to_abs(h, m, s, f) == total - 1


def test_dropframe_known_values():
    tb = build_timebase("30000/1001", drop_frame=True)
    # SMPTE 经典值
    assert resolve_position("00:01:00;02", tb, "x") == 1800
    assert format_smpte(tb, 1798) == "00:00:59;28"
    assert format_smpte(tb, 1800) == "00:01:00;02"
    assert format_smpte(tb, 17982) == "00:10:00;00"
    assert format_smpte(tb, 107892) == "01:00:00;00"
    tb60 = build_timebase("60000/1001", drop_frame=True)
    assert resolve_position("00:01:00;04", tb60, "x") == 3600
    assert format_smpte(tb60, 3600) == "00:01:00;04"


def test_dropframe_skipped_numbers_rejected():
    tb = build_timebase("30000/1001", drop_frame=True)
    for tc in ["00:01:00;00", "00:01:00;01", "00:09:00;01"]:
        with pytest.raises(TimecodeError) as ei:
            resolve_position(tc, tb, "f")
        assert "跳号" in ei.value.reason
        assert ei.value.field == "f"
    # 整 10 分钟不跳
    assert resolve_position("00:10:00;00", tb, "f") == 17982


def test_separator_timebase_mismatch():
    ndf = build_timebase("30000/1001")
    df = build_timebase("30000/1001", drop_frame=True)
    with pytest.raises(TimecodeError, match="drop-frame 分隔符"):
        resolve_position("00:00:01;00", ndf, "s")
    with pytest.raises(TimecodeError, match="drop-frame"):
        resolve_position("00:00:01:00", df, "s")


def test_out_of_range_fields():
    tb = build_timebase("25")
    for tc in ["00:00:01:25", "00:00:60:00", "00:60:00:00"]:
        with pytest.raises(TimecodeError) as ei:
            resolve_position(tc, tb, "cut")
        assert "越界" in ei.value.reason
        assert ei.value.field == "cut"
        assert ei.value.value == tc


def test_df_only_for_ntsc_rates():
    for fps in ["24", "25", "30", "24000/1001"]:
        with pytest.raises(TimecodeError, match="不支持 drop-frame"):
            build_timebase(fps, drop_frame=True)


def test_bad_preset_and_float():
    with pytest.raises(TimecodeError):
        build_timebase("23.98")
    # 浮点走十进制精确值
    tb = build_timebase(25.0)
    assert (tb.rate.numerator, tb.rate.denominator) == (25, 1)
    assert not tb.drop_frame
    tb = build_timebase(29.97)  # 2997/100，与 30000/1001 不同
    assert (tb.rate.numerator, tb.rate.denominator) == (2997, 100)


def test_start_timecode_offset():
    tb = build_timebase("30000/1001", drop_frame=True, start_timecode="01:00:00;00")
    assert resolve_position("01:00:00;00", tb, "x") == 0
    assert resolve_position("01:00:01;00", tb, "x") == 30
    with pytest.raises(TimecodeError, match="早于项目起始时间码"):
        resolve_position("00:59:59;28", tb, "x")
    assert format_smpte(tb, 0) == "01:00:00;00"


def test_ms_frame_roundtrip_no_drift():
    # ms -> frame -> tc -> frame 必须恒等
    for fps in ["24000/1001", "30000/1001", "60000/1001", "25"]:
        tb = build_timebase(fps)
        for ms in range(0, 120000, 333):
            fr = tb.ms_to_frames(ms)
            tc = format_smpte(tb, fr)
            assert resolve_position(tc, tb, "x") == fr
    # 半向上取整
    assert half_up(__import__("fractions").Fraction(1, 2)) == 1
    assert half_up(__import__("fractions").Fraction(3, 2)) == 2


# ---------------------------------------------------------------- 项目时间基

def _make_project(timebase=None, frame_rate=None, shot_cuts=None, name="P"):
    payload = {"name": name}
    if timebase is not None:
        payload["timebase"] = timebase
    if frame_rate is not None:
        payload["frame_rate"] = frame_rate
    if shot_cuts is not None:
        payload["shot_cuts"] = shot_cuts
    r = client.post("/projects", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_project_create_timebase_and_cuts():
    p = client.post("/projects", json={
        "name": "df",
        "timebase": {"fps": "30000/1001", "drop_frame": True},
        "shot_cuts": ["00:00:10;00", "300f", 6000, {"timecode": "00:01:00;02"}],
    }).json()
    assert p["timebase"]["fps_label"] == "30000/1001"
    assert p["timebase"]["drop_frame"] is True
    assert p["timebase"]["start_timecode"] == "00:00:00;00"
    # 10s 标签=300 帧（与 300f 同帧去重）；6000ms 半向上=180 帧；00:01:00;02=1800 帧
    assert p["shot_cuts"] == [180, 300, 1800]
    assert p["shot_cuts_ms"] == [6006, 10010, 60060]


def test_project_error_response_has_field_value_reason():
    cases = [
        ({"fps": "30000/1001", "drop_frame": True}, ["00:00:10:00"], "分隔符"),
        ({"fps": "30000/1001", "drop_frame": True}, ["00:01:00;00"], "跳号"),
        ({"fps": "25"}, ["00:00:01:25"], "越界"),
        ({"fps": "24000/1001", "drop_frame": True}, None, "不支持 drop-frame"),
    ]
    for tb_spec, cuts, reason_part in cases:
        body = {"name": "x", "timebase": tb_spec}
        if cuts is not None:
            body["shot_cuts"] = cuts
        r = client.post("/projects", json=body)
        assert r.status_code == 400, r.text
        err = r.json()["detail"]["errors"][0]
        assert err["field"] and "reason" in err and "value" in err
        assert reason_part in err["reason"]


def test_frame_rate_and_timebase_mutually_exclusive():
    r = client.post("/projects", json={
        "name": "x", "frame_rate": 25.0, "timebase": {"fps": "25"}})
    assert r.status_code == 422


# ---------------------------------------------------------------- 结构化 cue

def test_structured_cues_all_position_forms():
    pid = _make_project({"fps": "30000/1001", "drop_frame": True})
    r = client.post(f"/projects/{pid}/versions", json={"label": "s", "cues": [
        {"start": "00:00:01;00", "end": {"timecode": "00:00:02;00"}, "lines": "a"},
        {"start": "30f", "end": {"frame": 90}, "lines": ["b1", "b2"]},
        {"start": 1000, "end": {"ms": 2000}, "lines": "c"},
    ]})
    assert r.status_code == 201, r.text
    assert r.json()["format"] == "json"
    d = client.get(f"/projects/{pid}/versions/{r.json()['id']}").json()
    assert [c["start_frame"] for c in d["cues"]] == [30, 30, 30]
    assert [c["end_frame"] for c in d["cues"]] == [60, 90, 60]
    assert d["cues"][0]["start_tc"] == "00:00:01;00"
    assert d["cues"][1]["lines"] == ["b1", "b2"]
    assert d["cues"][2]["start_ms"] == 1001


def test_structured_cues_collect_all_errors():
    pid = _make_project({"fps": "30000/1001", "drop_frame": True})
    r = client.post(f"/projects/{pid}/versions", json={"cues": [
        {"start": "00:00:05;00", "end": "00:00:01;00", "lines": "x"},   # end<start
        {"start": "00:01:00;00", "end": "00:01:02;00", "lines": "y"},   # 跳号
        {"start": "00:00:01:00", "end": "00:00:02:00", "lines": "z"},   # 分隔符
    ]})
    assert r.status_code == 400
    errors = r.json()["detail"]["errors"]
    fields = {e["field"] for e in errors}
    assert "cues[0].end" in fields
    assert any("跳号" in e["reason"] for e in errors)
    assert any("分隔符" in e["reason"] for e in errors)
    assert all({"field", "value", "reason"} <= set(e) for e in errors)


def test_content_and_cues_exactly_one():
    pid = _make_project({"fps": "25"})
    assert client.post(f"/projects/{pid}/versions",
                       json={"content": "x", "cues": []}).status_code == 422
    assert client.post(f"/projects/{pid}/versions",
                       json={"label": "x"}).status_code == 422


# ---------------------------------------------------------------- 换算接口

def test_convert_in_project():
    pid = _make_project({"fps": "30000/1001", "drop_frame": True})
    r = client.post(f"/projects/{pid}/convert",
                    json={"items": ["00:00:01;00", "30f", 1000, {"frame": 1800}]})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["frame"] for x in rows[:3]] == [30, 30, 30]
    assert rows[0]["timecode"] == "00:00:01;00"
    assert rows[0]["ms"] == 1001
    assert rows[0]["ms_exact"] == "1001/1"
    assert rows[3]["timecode"] == "00:01:00;02"
    # 错误项携带字段名
    r = client.post(f"/projects/{pid}/convert", json={"items": ["00:01:00;00"]})
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["field"] == "items[0]"


def test_convert_standalone():
    r = client.post("/timecode/convert", json={
        "fps": "24000/1001", "drop_frame": False,
        "start_timecode": "00:00:00:00",
        "items": ["00:00:01:00", 1000, "24f"],
    })
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["frame"] for x in rows] == [24, 24, 24]
    assert rows[0]["ms_exact"] == "1001/1"  # 24 帧精确毫秒
    assert rows[0]["timecode"] == "00:00:01:00"


# ---------------------------------------------------------------- 帧导出与往返

def test_frames_export_and_roundtrip():
    pid = _make_project({"fps": "30000/1001", "drop_frame": True})
    srt = ("1\n00:00:01,000 --> 00:00:04,000\n第一句字幕内容\n\n"
           "2\n00:00:05,000 --> 00:00:07,500\n第二句字幕内容也足够长了吧\n")
    vid = client.post(f"/projects/{pid}/versions", json={"content": srt}).json()["id"]
    fx = client.get(f"/projects/{pid}/versions/{vid}/export",
                    params={"format": "frames"}).json()
    assert fx["timebase"]["drop_frame"] is True
    assert fx["cue_count"] == 2
    c0 = fx["cues"][0]
    assert c0["start_frame"] == 30 and c0["end_frame"] == 120
    assert c0["start_tc"] == "00:00:01;00" and c0["end_tc"] == "00:00:04;00"
    assert c0["start_ms_exact"] == "1001/1"
    assert c0["duration_frames"] == 90

    # SRT 导出后重新上传，帧位置必须逐 cue 相同（无漂移）
    srt_out = client.get(f"/projects/{pid}/versions/{vid}/export",
                         params={"format": "srt"}).text
    vid2 = client.post(f"/projects/{pid}/versions", json={"content": srt_out}).json()["id"]
    fx2 = client.get(f"/projects/{pid}/versions/{vid2}/export",
                     params={"format": "frames"}).json()
    for a, b in zip(fx["cues"], fx2["cues"]):
        assert a["start_frame"] == b["start_frame"]
        assert a["end_frame"] == b["end_frame"]
        assert a["start_tc"] == b["start_tc"]


# ---------------------------------------------------------------- 修复落在合法帧

def test_autofix_results_on_legal_frames_2997df():
    # 毫秒/帧号相对项目起点；SMPTE 时间码为绝对标签。
    # 起点 01:00:00;00 时，相对 1s 的 SRT 时间戳显示为 01:00:01;00。
    pid = _make_project({"fps": "30000/1001", "drop_frame": True,
                         "start_timecode": "01:00:00;00"}, shot_cuts=["01:00:06;00"])
    srt = ("1\n00:00:01,000 --> 00:00:01,300\n短\n\n"
           "2\n00:00:02,000 --> 00:00:05,000\n"
           "这是一句非常长的台词，包含了太多的信息，观众根本来不及读完，必须拆成两句才行。\n\n"
           "3\n00:00:07,000 --> 00:00:09,000\n后续台词\n")
    vid = client.post(f"/projects/{pid}/versions", json={"content": srt}).json()["id"]
    # 相对 1s 映射到绝对 01:00:01;00
    detail = client.get(f"/projects/{pid}/versions/{vid}").json()
    assert detail["cues"][0]["start_tc"] == "01:00:01;00"
    a = client.post(f"/projects/{pid}/versions/{vid}/autofix").json()
    nid = a["new_version_id"]
    fixed = client.get(f"/projects/{pid}/versions/{nid}").json()
    for cue in fixed["cues"]:
        assert cue["end_frame"] > cue["start_frame"]
        assert isinstance(cue["start_frame"], int)
        assert isinstance(cue["end_frame"], int)
        # 时间码可解析回相同帧（DF 合法性：不会落在跳过号上）
        r2 = client.post(f"/projects/{pid}/convert",
                         json={"items": [cue["start_tc"], cue["end_tc"]]}).json()
        assert [x["frame"] for x in r2] == [cue["start_frame"], cue["end_frame"]]
    report = client.post(f"/projects/{pid}/versions/{nid}/qc").json()
    assert [i for i in report["issues"] if i["severity"] == "error"] == []
    # fix candidate 的帧/时间码字段存在且自洽
    q = client.post(f"/projects/{pid}/versions/{vid}/qc").json()
    for issue in q["issues"]:
        for fc in issue["fix_candidates"]:
            if "frame" in fc["params"]:
                conv = client.post(f"/projects/{pid}/convert",
                                   json={"items": [fc["params"]["timecode"]]}).json()
                assert conv[0]["frame"] == fc["params"]["frame"]


def test_diff_uses_frames():
    pid = _make_project({"fps": "25"})
    srt = "1\n00:00:01,000 --> 00:00:01,300\n太短\n"
    vid = client.post(f"/projects/{pid}/versions", json={"content": srt}).json()["id"]
    nid = client.post(f"/projects/{pid}/versions/{vid}/autofix").json()["new_version_id"]
    d = client.get(f"/projects/{pid}/diff",
                   params={"from_version": vid, "to_version": nid}).json()
    changed = [c for c in d["changes"] if c["type"] == "changed"]
    assert changed
    ch = changed[0]["changes"]
    assert "start" in ch or "end" in ch
    side = ch.get("end") or ch.get("start")
    assert "from_frame" in side and "to_frame" in side
    assert "from_timecode" in side and "to_timecode" in side
    assert "shift_frames" in side


# ---------------------------------------------------------------- 旧毫秒请求兼容

def test_legacy_frame_rate_requests_still_work():
    pid = client.post("/projects", json={"name": "legacy", "frame_rate": 25.0,
                                         "shot_cuts": [6000, "00:00:12.500"]}).json()["id"]
    srt = "1\n00:00:01,000 --> 00:00:04,000\n普通字幕一行\n"
    vid = client.post(f"/projects/{pid}/versions", json={"content": srt}).json()["id"]
    d = client.get(f"/projects/{pid}/versions/{vid}").json()
    assert d["cues"][0]["start_frame"] == 25
    assert d["cues"][0]["end_frame"] == 100
    q = client.post(f"/projects/{pid}/versions/{vid}/qc").json()
    assert q["timebase"]["fps_label"] == "25/1"
    r = client.get(f"/projects/{pid}/versions/{vid}/export",
                   params={"format": "report"})
    assert r.status_code == 200
