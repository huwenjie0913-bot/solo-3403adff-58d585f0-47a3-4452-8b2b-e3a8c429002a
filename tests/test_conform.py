"""剪辑改版字幕重套（re-conform）端到端测试。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _project(fps=25.0):
    r = client.post("/projects", json={"name": "重套测试", "frame_rate": fps})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(pid, srt, label="v1"):
    r = client.post(f"/projects/{pid}/versions",
                    json={"label": label, "content": srt})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seg(s0, s1, t0, t1):
    """帧号位置（'NNNf'）构造映射段；t0/t1 为 None 表示删除段。"""
    return {"source_start": f"{s0}f", "source_end": f"{s1}f",
            "target_start": None if t0 is None else f"{t0}f",
            "target_end": None if t1 is None else f"{t1}f"}


SRT = """1
00:00:01,000 --> 00:00:03,000
段内第一条

2
00:00:08,000 --> 00:00:10,000
段内第二条

3
00:00:18,000 --> 00:00:22,000
跨切点的一句台词，需要人工确认处理方式。
"""


def _setup(segments, **kw):
    pid = _project()
    vid = _upload(pid, SRT)
    body = {"version_id": vid, "segments": segments, **kw}
    r = client.post(f"/projects/{pid}/conform", json=body)
    return pid, vid, r


# ---------------------------------------------------------------- 映射校验

def test_mapping_validation_errors():
    pid = _project()
    vid = _upload(pid, SRT)

    # 源侧重叠
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid,
        "segments": [_seg(0, 500, 0, 500), _seg(400, 800, 600, 900)]})
    assert r.status_code == 400, r.text
    types = {i["issue_type"] for i in r.json()["detail"]["mapping_issues"]}
    assert "source_overlap" in types

    # 目标段冲突（源不重叠，目标重叠）
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid,
        "segments": [_seg(0, 400, 0, 400), _seg(500, 900, 300, 700)]})
    assert r.status_code == 400
    types = {i["issue_type"] for i in r.json()["detail"]["mapping_issues"]}
    assert "target_conflict" in types

    # 输入倒序
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid,
        "segments": [_seg(500, 900, 500, 900), _seg(0, 400, 0, 400)]})
    assert r.status_code == 400
    types = {i["issue_type"] for i in r.json()["detail"]["mapping_issues"]}
    assert "order_reversed" in types


def test_unmapped_source_is_warning():
    pid = _project()
    vid = _upload(pid, SRT)
    # 两段之间留 100 帧空隙（未映射区间）→ warning 但仍可重套
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid,
        "segments": [_seg(0, 400, 0, 400), _seg(500, 1000, 400, 900)]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping_valid"] is True
    types = {i["issue_type"] for i in body["mapping_issues"]}
    assert "unmapped_source" in types


def test_invalid_segment_ranges():
    pid = _project()
    vid = _upload(pid, SRT)
    # 源区间终点 <= 起点
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid, "segments": [_seg(500, 400, 0, 100)]})
    assert r.status_code == 400
    # 目标终点早于起点
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid, "segments": [_seg(0, 400, 300, 100)]})
    assert r.status_code == 400


# ---------------------------------------------------------------- 直接重套 / 变速

def test_direct_retime_and_retimed_segment():
    # 段1 原速 0-375f；段2 源 375-1000f（625f）-> 目标 375-675f（300f，
    # 比例 12/25 变速压缩）。cue1 在段1，cue2(200-250f) 完全在段1内原速，
    # cue3(450-550f) 在段2内变速。
    pid, vid, r = _setup([_seg(0, 375, 0, 375), _seg(375, 1000, 375, 675)])
    assert r.status_code == 200, r.text
    body = r.json()
    segs = {s["id"]: s for s in body["segments"]}
    assert segs[2]["retimed"] is True
    assert (segs[2]["ratio_num"], segs[2]["ratio_den"]) == (12, 25)

    cues = {c["cue"]["index"]: c for c in body["cues"]}
    # cue1: 25f-75f 段内原速，直接重套
    c1 = cues[1]
    assert c1["status"] == "direct"
    m = c1["mapped_cues"][0]
    assert (m["start_frame"], m["end_frame"], m["segment_id"]) == (25, 75, 1)
    assert (m["ratio_num"], m["ratio_den"]) == (1, 1)

    # cue2: 200f-250f 同样在段1 原速
    c2 = cues[2]
    assert c2["status"] == "direct"
    m2 = c2["mapped_cues"][0]
    assert (m2["start_frame"], m2["end_frame"], m2["segment_id"]) == (200, 250, 1)

    # cue3: 450f-550f 在变速段2：偏移 75f、175f → 36f、84f → 411f、459f
    c3 = cues[3]
    assert c3["status"] == "direct"
    m3 = c3["mapped_cues"][0]
    assert m3["segment_id"] == 2
    assert (m3["start_frame"], m3["end_frame"]) == (411, 459)
    assert (m3["ratio_num"], m3["ratio_den"]) == (12, 25)


def _cross_cut_fixture():
    # cue3 450f-550f 完全在段2，另构造跨切点场景
    srt = """1
00:00:01,000 --> 00:00:03,000
段内第一条

2
00:00:17,000 --> 00:00:19,000
跨切点的一句台词，需要人工确认处理方式。
"""
    pid = _project()
    vid = _upload(pid, srt)
    # cue2 425f-475f 跨越 450 切点
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid,
        "segments": [_seg(0, 450, 0, 450), _seg(450, 1000, 450, 1000)]})
    assert r.status_code == 200, r.text
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    c2 = cues[2]
    assert c2["status"] == "cross_cut"
    assert {i["issue_type"] for i in c2["issues"]} == {"cross_cut"}
    actions = {c["action"] for c in c2["fix_candidates"]}
    assert {"move", "trim", "split_at_punctuation", "manual"} <= actions
    # 默认策略 move；两侧各 25 帧，主保留段取 id 较小的段1
    assert c2["proposed_candidate_id"] == "q2.c1"
    assert c2["mapped_cues"][0]["segment_id"] == 1
    return pid, vid, [_seg(0, 450, 0, 450), _seg(450, 1000, 450, 1000)]


def test_cross_cut_candidate_set():
    """跨切点 cue 返回移动/裁切/标点拆分/人工四类候选，默认 move。"""
    _, _, _ = _cross_cut_fixture()


def test_deleted_segment_default_manual():
    # cue2 200-250f 落在删除段 175-300f 内
    pid, vid, r = _setup([
        _seg(0, 175, 0, 175), _seg(175, 300, None, None),
        _seg(300, 1000, 175, 875)])
    assert r.status_code == 200, r.text
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    c2 = cues[2]
    assert c2["status"] == "in_deleted"
    assert c2["issues"][0]["issue_type"] == "in_deleted_segment"
    assert c2["proposed_candidate_id"].startswith("q2.c")
    assert c2["mapped_cues"][0]["needs_manual"] is True
    assert any(s["deleted"] for s in r.json()["segments"])


# ---------------------------------------------------------------- 容差吸附 / 过短诊断

def test_cut_tolerance_snaps_boundary():
    # 用结构化 cue：起点 445f，切点 450f，容差 2 帧 → 吸附到切点不跨切点
    pid = _project()
    r = client.post(f"/projects/{pid}/versions", json={
        "label": "v1",
        "cues": [
            {"start": "25f", "end": "75f", "lines": "段内第一条"},
            {"start": "448f", "end": "550f",
             "lines": "跨切点附近的一句台词，容差内吸附。"},
        ]})
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid, "cut_tolerance_ms": 80,
        "segments": [_seg(0, 450, 0, 450), _seg(450, 1000, 450, 1000)]})
    assert r.status_code == 200, r.text
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    c2 = cues[2]
    assert c2["snapped_start"] is True
    assert c2["status"] == "direct"

    # 不容差时同一 cue 跨切点
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid, "cut_tolerance_ms": 0,
        "segments": [_seg(0, 450, 0, 450), _seg(450, 1000, 450, 1000)]})
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    assert cues[2]["status"] == "cross_cut"


def test_duration_too_short_warning():
    # 段2 源 625f -> 目标 125f（5:1 压缩）；cue3 450-550f → 偏移
    # 75/625*125=15、175/625*125=35 → 20 帧（800ms < 1000ms）
    pid, vid, r = _setup([_seg(0, 375, 0, 375), _seg(375, 1000, 375, 500)],
                         min_duration_ms=1000)
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    c3 = cues[3]
    assert c3["status"] == "direct"
    assert any(i["issue_type"] == "duration_too_short" for i in c3["issues"])
    # 默认 move 策略下提出延长候选
    assert c3["proposed_candidate_id"] is not None
    move = next(c for c in c3["fix_candidates"] if c["action"] == "move")
    assert move["params"]["to"]["end_frame"] - move["params"]["to"]["start_frame"] >= 25


# ---------------------------------------------------------------- 预览与应用

def _applied_split_fixture():
    """跨切点 cue 应用“按标点拆分”候选，返回 (pid, vid, segs, split_id, new_vid)。"""
    pid, vid, segs = _cross_cut_fixture()
    r = client.post(f"/projects/{pid}/conform",
                    json={"version_id": vid, "segments": segs})
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    split_id = next(c["id"] for c in cues[2]["fix_candidates"]
                    if c["action"] == "split_at_punctuation")

    r = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "candidate_ids": [split_id],
        "label": "改版v2", "segments": segs})
    assert r.status_code == 201, r.text
    new_vid = r.json()["new_version_id"]
    return pid, vid, segs, split_id, new_vid


def test_preview_before_after_and_origin_unchanged():
    pid, vid, segs, split_id, _ = _applied_split_fixture()
    # 预览：展示改版前后时间码与映射来源，不改原版本
    r = client.post(f"/projects/{pid}/conform/preview", json={
        "version_id": vid, "candidate_ids": [split_id], "segments": segs})
    assert r.status_code == 200, r.text
    items = {it["cue_index"]: it for it in r.json()["items"]}
    assert items[1]["before"]["start_frame"] == 25
    assert items[1]["outcomes"][0]["action"] == "direct_retime"
    assert items[1]["outcomes"][0]["segment_id"] == 1
    i2 = items[2]
    assert len(i2["outcomes"]) == 2  # 拆分两条
    assert {o["segment_id"] for o in i2["outcomes"]} == {1, 2}
    assert {o["lines"][0] for o in i2["outcomes"]} == {
        "跨切点的一句台词，", "需要人工确认处理方式。"}
    # 原稿未变
    orig = client.get(f"/projects/{pid}/versions/{vid}").json()
    assert orig["cues"][1]["start_frame"] == 425


def test_apply_split_saves_snapshot_version():
    pid, vid, segs, split_id, new_vid = _applied_split_fixture()
    assert new_vid != vid
    nv = client.get(f"/projects/{pid}/versions/{new_vid}").json()
    assert nv["origin_version_id"] == vid
    prov = nv["provenance"]
    assert prov["kind"] == "conform"
    assert len(prov["mapping_snapshot"]) == 2
    assert prov["mapping_snapshot"][1]["ratio_num"] == 1
    assert nv["cue_count"] == 3  # 2 -> 拆分后 3
    texts = {tuple(c["lines"]) for c in nv["cues"]}
    assert ("跨切点的一句台词，",) in texts
    assert ("需要人工确认处理方式。",) in texts


def test_new_version_supports_qc_diff_export():
    pid, vid, _, _, new_vid = _applied_split_fixture()
    # 新版本可继续质检
    r = client.post(f"/projects/{pid}/versions/{new_vid}/qc")
    assert r.status_code == 200
    # 版本比较
    r = client.get(f"/projects/{pid}/diff",
                   params={"from_version": vid, "to_version": new_vid})
    assert r.status_code == 200
    assert r.json()["summary"]["cue_count_to"] == 3
    # SRT / VTT / 帧级导出
    for fmt in ("srt", "vtt", "frames"):
        r = client.get(f"/projects/{pid}/versions/{new_vid}/export",
                       params={"format": fmt})
        assert r.status_code == 200, fmt


def test_trim_and_manual_candidates():
    pid, vid, segs = _cross_cut_fixture()
    r = client.post(f"/projects/{pid}/conform",
                    json={"version_id": vid, "segments": segs})
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    trim_id = next(c["id"] for c in cues[2]["fix_candidates"]
                   if c["action"] == "trim")
    r = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "candidate_ids": [trim_id], "segments": segs})
    assert r.status_code == 201, r.text
    nv = client.get(
        f"/projects/{pid}/versions/{r.json()['new_version_id']}").json()
    # cue2 被裁到主保留段（段1：425-450f 映射到同区间）
    c2 = nv["cues"][1]
    assert (c2["start_frame"], c2["end_frame"]) == (425, 450)
    assert c2["lines"] == ["跨切点的一句台词，需要人工确认处理方式。"]

    # 人工候选：保留原时间码
    manual_id = next(c["id"] for c in cues[2]["fix_candidates"]
                     if c["action"] == "manual")
    r = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "candidate_ids": [manual_id], "segments": segs})
    nv = client.get(
        f"/projects/{pid}/versions/{r.json()['new_version_id']}").json()
    c2 = nv["cues"][1]
    assert (c2["start_frame"], c2["end_frame"]) == (425, 475)


def test_merge_adjacent_candidate():
    srt = """1
00:00:01,000 --> 00:00:02,000
第一句。

2
00:00:02,000 --> 00:00:03,000
第二句紧挨着。
"""
    pid = _project()
    vid = _upload(pid, srt)
    segs = [_seg(0, 1000, 0, 1000)]
    r = client.post(f"/projects/{pid}/conform", json={
        "version_id": vid, "merge_gap_ms": 0, "segments": segs})
    assert r.status_code == 200, r.text
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    merge_id = next(c["id"] for c in cues[1]["fix_candidates"]
                    if c["action"] == "merge_adjacent")
    r = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "candidate_ids": [merge_id], "segments": segs})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["summary"]["cue_count_after"] == 1
    nv = client.get(f"/projects/{pid}/versions/{body['new_version_id']}").json()
    assert nv["cues"][0]["lines"] == ["第一句。", "第二句紧挨着。"]
    assert (nv["cues"][0]["start_frame"], nv["cues"][0]["end_frame"]) == (25, 75)


def test_unknown_and_conflicting_candidates():
    pid, vid, segs = _cross_cut_fixture()
    r = client.post(f"/projects/{pid}/conform",
                    json={"version_id": vid, "segments": segs})
    # 未知候选
    r2 = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "candidate_ids": ["q9.c9"], "segments": segs})
    assert r2.status_code == 400
    # 同一 cue 选两个冲突候选
    cues = {c["cue"]["index"]: c for c in r.json()["cues"]}
    ids = [c["id"] for c in cues[2]["fix_candidates"]
           if c["action"] in ("move", "trim")]
    r2 = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "candidate_ids": ids, "segments": segs})
    assert r2.status_code == 400


def test_vtt_identifier_settings_preserved():
    vtt = """WEBVTT

cue-1
00:00:01.000 --> 00:00:03.000 align:start
<v 张三>你好，世界</v>
"""
    pid = _project()
    r = client.post(f"/projects/{pid}/versions",
                    json={"label": "vtt", "content": vtt})
    vid = r.json()["id"]
    segs = [_seg(0, 1000, 100, 1100)]  # 整体后移 100 帧、原速
    r = client.post(f"/projects/{pid}/conform/apply", json={
        "version_id": vid, "segments": segs})
    assert r.status_code == 201, r.text
    new_vid = r.json()["new_version_id"]
    nv = client.get(f"/projects/{pid}/versions/{new_vid}").json()
    c = nv["cues"][0]
    assert c["identifier"] == "cue-1"
    assert c["settings"] == "align:start"
    assert c["lines"] == ["<v 张三>你好，世界</v>"]
    assert c["start_frame"] == 125
    # VTT 导出仍带 identifier/settings
    text = client.get(f"/projects/{pid}/versions/{new_vid}/export",
                      params={"format": "vtt"}).text
    assert "cue-1" in text and "align:start" in text and "<v 张三>" in text
