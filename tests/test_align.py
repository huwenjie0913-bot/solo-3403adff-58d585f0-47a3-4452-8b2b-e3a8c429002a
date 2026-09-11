"""双语字幕轨道对齐与同步检查测试。

25fps 项目：1 秒 = 25 帧。译文版本 TGT 故意包含：说话人标签不一致、
多对一、一对多、顺序倒置（文件顺序与时间顺序不一致）、未匹配译文、
译文漏条、重叠不足。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 源语言版本（帧号：25-75 / 90-125 / 140-175 / 200-250 / 300-325 / 350-400）
SRC = """1
00:00:01,000 --> 00:00:03,000
<v 张三>你好，世界</v>

2
00:00:03,600 --> 00:00:05,000
今天天气不错。

3
00:00:05,600 --> 00:00:07,000
我们出发吧。

4
00:00:08,000 --> 00:00:10,000
好的，没问题。

5
00:00:12,000 --> 00:00:13,000
等等我。

6
00:00:14,000 --> 00:00:16,000
最后一句。
"""

# 译文版本：cue 2/3（Sure./No problem.）在文件中排在 cue 4 之前 → 顺序倒置
TGT = """1
00:00:01,200 --> 00:00:03,200
<v 李四>Hello, world.</v>

2
00:00:08,200 --> 00:00:09,000
Sure.

3
00:00:09,200 --> 00:00:10,200
No problem.

4
00:00:03,800 --> 00:00:06,800
Nice weather today. Let's go.

5
00:00:11,000 --> 00:00:12,000
A line with no source.

6
00:00:15,800 --> 00:00:17,000
The last one.
"""

# 无倒置、说话人一致的干净版本（用于 apply 后质检全通过）
SRC_OK = """1
00:00:01,000 --> 00:00:03,000
<v 张三>你好，世界</v>

2
00:00:03,600 --> 00:00:05,000
今天天气不错。

3
00:00:05,600 --> 00:00:07,000
我们出发吧。

4
00:00:08,000 --> 00:00:10,000
好的，没问题。

5
00:00:14,000 --> 00:00:16,000
最后一句。
"""

TGT_OK = """1
00:00:01,200 --> 00:00:03,200
<v 张三>Hello, world.</v>

2
00:00:03,800 --> 00:00:06,800
Nice weather today. Let's go.

3
00:00:08,200 --> 00:00:09,000
Sure.

4
00:00:09,200 --> 00:00:10,200
No problem.

5
00:00:15,800 --> 00:00:17,000
The last one.
"""


def _project():
    r = client.post("/projects", json={"name": "双语剧集", "frame_rate": 25.0})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(pid, content, label):
    r = client.post(f"/projects/{pid}/versions",
                    json={"label": label, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _align(pid, sid, tid, **kw):
    r = client.post(f"/projects/{pid}/align",
                    json={"source_version_id": sid, "target_version_id": tid, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _setup(src=SRC, tgt=TGT):
    pid = _project()
    sid = _upload(pid, src, "源-中文")
    tid = _upload(pid, tgt, "译-英文")
    return pid, sid, tid


# ---------------------------------------------------------------- 映射与诊断

def test_align_mappings_types_and_timecodes():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid)

    assert report["source_version_id"] == sid
    assert report["target_version_id"] == tid
    assert report["thresholds"]["min_overlap_frames"] == 5  # 200ms @25fps
    summary = report["summary"]
    assert summary["mapping_count"] == 4
    assert summary["one_to_one"] == 2
    assert summary["many_to_one"] == 1
    assert summary["one_to_many"] == 1
    assert summary["unmatched_source_count"] == 1   # 源第 5 条（译文漏条）
    assert summary["unmatched_target_count"] == 1   # 译文第 5 条

    m1, m2, m3, m4 = report["mappings"]
    # m1：一对一，两侧时间码都给出
    assert m1["type"] == "one_to_one"
    assert m1["source"]["indices"] == [1]
    assert m1["target"]["indices"] == [1]
    assert m1["source"]["start_tc"] == "00:00:01:00"
    assert m1["source"]["end_tc"] == "00:00:03:00"
    assert m1["target"]["start_tc"] == "00:00:01:05"  # 30 帧 @25fps
    assert m1["overlap_frames"] == 45
    assert m1["overlap_ratio_source"] == 0.9
    # m2：多对一（源 2+3 -> 译文 4）
    assert m2["type"] == "many_to_one"
    assert m2["source"]["indices"] == [2, 3]
    assert m2["target"]["indices"] == [4]
    # m3：一对多（源 4 -> 译文 2+3）
    assert m3["type"] == "one_to_many"
    assert m3["source"]["indices"] == [4]
    assert m3["target"]["indices"] == [2, 3]
    # m4：一对一但重叠不足（仅 5 帧）
    assert m4["type"] == "one_to_one"
    assert m4["overlap_frames"] == 5
    assert m4["overlap_ratio_source"] == 0.1


def test_align_diagnostics_fielded():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid)
    by_type = report["summary"]["by_type"]
    assert by_type["speaker_mismatch"] == 1      # 张三 vs 李四
    assert by_type["order_inversion"] == 1       # 译文 2/3 排在 4 之前
    assert by_type["insufficient_overlap"] == 1  # m4 仅 5 帧重叠
    assert by_type["missing_translation"] == 1   # 源第 5 条漏译
    assert by_type["unmatched_target"] == 1      # 译文第 5 条无源

    m1 = report["mappings"][0]
    spk = [i for i in m1["issues"] if i["issue_type"] == "speaker_mismatch"][0]
    assert spk["severity"] == "warning"
    assert spk["details"]["source_speakers"] == ["张三"]
    assert spk["details"]["target_speakers"] == ["李四"]

    m3 = report["mappings"][2]
    inv = [i for i in m3["issues"] if i["issue_type"] == "order_inversion"][0]
    assert inv["severity"] == "error"
    assert inv["details"]["target_indices"] == [2, 3]

    m4 = report["mappings"][3]
    ins = [i for i in m4["issues"] if i["issue_type"] == "insufficient_overlap"][0]
    assert ins["details"]["overlap_frames"] == 5
    assert ins["details"]["min_overlap_ratio"] == 0.3

    # 未匹配 cue 字段化返回
    us = report["unmatched_source"][0]
    assert us["issue_type"] == "missing_translation"
    assert us["severity"] == "error"
    assert us["cue"]["index"] == 5
    assert us["cue"]["start_tc"] == "00:00:12:00"
    ut = report["unmatched_target"][0]
    assert ut["issue_type"] == "unmatched_target"
    assert ut["cue"]["index"] == 5


def test_align_candidates_source_boundaries():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid)
    m1, m2, m3, m4 = report["mappings"]

    # m1：时间调整候选（对齐到源边界）
    c1 = m1["fix_candidates"][0]
    assert c1["id"] == "m1.c1"
    assert c1["action"] == "retime_to_source"
    assert c1["params"]["to"]["start_frame"] == 25
    assert c1["params"]["to"]["end_frame"] == 75
    assert c1["params"]["to"]["start_tc"] == "00:00:01:00"

    # m2：多对一 → 按源边界拆分
    c2 = m2["fix_candidates"][0]
    assert c2["action"] == "split_at_source_boundaries"
    assert c2["params"]["target_index"] == 4
    assert c2["params"]["source_indices"] == [2, 3]
    segs = c2["params"]["segments"]
    assert [(s["start_frame"], s["end_frame"]) for s in segs] == [(90, 125), (140, 175)]
    pieces = c2["params"]["text_pieces"]
    assert len(pieces) == 2
    # 不改写文本：拆分后的可见文本拼接与原文一致
    original = "Nice weather today. Let's go."
    assert "".join(pieces).replace(" ", "") == original.replace(" ", "")

    # m3：一对多 → 合并到源跨度
    c3 = m3["fix_candidates"][0]
    assert c3["action"] == "merge_to_source"
    assert c3["params"]["target_indices"] == [2, 3]
    assert c3["params"]["to"]["start_frame"] == 200
    assert c3["params"]["to"]["end_frame"] == 250

    # m4：重叠不足 → 时间调整候选
    c4 = m4["fix_candidates"][0]
    assert c4["action"] == "retime_to_source"
    assert c4["params"]["to"]["start_frame"] == 350
    assert c4["params"]["to"]["end_frame"] == 400


# ---------------------------------------------------------------- 阈值与候选策略

def test_align_thresholds_change_matching():
    pid, sid, tid = _setup()
    # 最小重叠提高到 1000ms（25 帧）：m3 的 20 帧、m4 的 5 帧重叠不再算匹配
    report = _align(pid, sid, tid, min_overlap_ms=1000)
    summary = report["summary"]
    assert summary["mapping_count"] == 2
    assert summary["unmatched_source_count"] == 3   # 源 4、5、6
    assert summary["unmatched_target_count"] == 4   # 译文 2、3、5、6
    assert report["thresholds"]["min_overlap_frames"] == 25


def test_align_proportional_strategy():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid, candidate_strategy="proportional")
    m1, m2, m3, m4 = report["mappings"]
    # 一对一仍是边界对齐
    assert m1["fix_candidates"][0]["action"] == "retime_to_source"
    # 多对一 / 一对多 → 组内按比例映射
    c2 = m2["fix_candidates"][0]
    assert c2["action"] == "fit_group_to_source"
    assert c2["params"]["per_cue"][0]["to"]["start_frame"] == 90
    assert c2["params"]["per_cue"][0]["to"]["end_frame"] == 175
    c3 = m3["fix_candidates"][0]
    assert c3["action"] == "fit_group_to_source"
    per = {pc["target_index"]: pc["to"] for pc in c3["params"]["per_cue"]}
    assert (per[2]["start_frame"], per[2]["end_frame"]) == (200, 220)
    assert (per[3]["start_frame"], per[3]["end_frame"]) == (225, 250)


def test_align_speaker_check_disabled():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid, speaker_check=False)
    assert "speaker_mismatch" not in report["summary"]["by_type"]


def test_align_same_version_rejected():
    pid, sid, tid = _setup()
    r = client.post(f"/projects/{pid}/align",
                    json={"source_version_id": sid, "target_version_id": sid})
    assert r.status_code == 400
    r = client.post(f"/projects/{pid}/align",
                    json={"source_version_id": sid, "target_version_id": 9999})
    assert r.status_code == 404


# ---------------------------------------------------------------- 应用候选

def test_align_apply_all_candidates():
    pid = _project()
    sid = _upload(pid, SRC_OK, "源")
    tid = _upload(pid, TGT_OK, "译")
    r = client.post(f"/projects/{pid}/align/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "label": "译-对齐"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["origin_version_id"] == tid
    assert body["summary"]["applied_count"] == 4   # 1 对齐 + 1 拆分 + 1 合并 + 1 对齐
    assert body["summary"]["cue_count_before"] == 5
    assert body["summary"]["cue_count_after"] == 5  # 拆 1 为 2、合 2 为 1
    actions = [a["action"] for a in body["applied"]]
    assert actions == ["retime_to_source", "split_at_source_boundaries",
                       "merge_to_source", "retime_to_source"]

    new_vid = body["new_version_id"]
    detail = client.get(f"/projects/{pid}/versions/{new_vid}").json()
    cues = detail["cues"]
    # 时间全部对齐到源 cue 边界
    assert [(c["start_frame"], c["end_frame"]) for c in cues] == [
        (25, 75), (90, 125), (140, 175), (200, 250), (350, 400)]
    # 拆分：文本不改写
    assert cues[1]["lines"] == ["Nice weather today."]
    assert cues[2]["lines"] == ["Let's go."]
    # 合并：两条译文文本都保留
    assert cues[3]["lines"] == ["Sure.", "No problem."]
    # 说话人标签保留
    assert cues[0]["lines"] == ["<v 张三>Hello, world.</v>"]

    # 版本关联原版本，原稿保留
    assert detail["origin_version_id"] == tid
    assert detail["provenance"]["kind"] == "align"
    assert detail["provenance"]["source_version_id"] == sid
    orig = client.get(f"/projects/{pid}/versions/{tid}").json()
    assert orig["cues"][0]["start_frame"] == 30  # 原译文未改动
    assert orig["origin_version_id"] is None

    # 新版本可继续质检（无 error 级问题）
    report = client.post(f"/projects/{pid}/versions/{new_vid}/qc").json()
    assert [i for i in report["issues"] if i["severity"] == "error"] == []

    # 可继续差异比较（相对原译文版本）
    diff = client.get(f"/projects/{pid}/diff",
                      params={"from_version": tid, "to_version": new_vid}).json()
    assert diff["summary"]["changed"] > 0

    # 可继续导出 SRT / WebVTT / 帧级 JSON
    r = client.get(f"/projects/{pid}/versions/{new_vid}/export",
                   params={"format": "srt"})
    assert "00:00:01,000 --> 00:00:03,000" in r.text
    r = client.get(f"/projects/{pid}/versions/{new_vid}/export",
                   params={"format": "vtt"})
    assert r.text.startswith("WEBVTT")
    r = client.get(f"/projects/{pid}/versions/{new_vid}/export",
                   params={"format": "frames"})
    frames = r.json()
    assert frames["cues"][0]["start_frame"] == 25
    assert frames["cues"][0]["start_tc"] == "00:00:01:00"


def test_align_apply_selected_candidates():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid)
    # 只选一对多映射的合并候选
    merge_id = report["mappings"][2]["fix_candidates"][0]["id"]
    r = client.post(f"/projects/{pid}/align/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": [merge_id]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["summary"]["applied_count"] == 1
    assert body["summary"]["cue_count_after"] == 5  # 6 - 1（合并）

    detail = client.get(f"/projects/{pid}/versions/{body['new_version_id']}").json()
    merged = [c for c in detail["cues"] if c["lines"] == ["Sure.", "No problem."]]
    assert len(merged) == 1
    assert (merged[0]["start_frame"], merged[0]["end_frame"]) == (200, 250)
    # 未选中的 cue 保持原稿（译文第 1 条仍是 30-80 帧）
    assert detail["cues"][0]["start_frame"] == 30
    assert detail["cues"][0]["end_frame"] == 80


def test_align_apply_unknown_candidate_rejected():
    pid, sid, tid = _setup()
    r = client.post(f"/projects/{pid}/align/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": ["m99.c1"]})
    assert r.status_code == 400


def test_align_apply_proportional_fit():
    pid, sid, tid = _setup()
    report = _align(pid, sid, tid, candidate_strategy="proportional")
    fit_id = report["mappings"][2]["fix_candidates"][0]["id"]  # 一对多的 fit
    r = client.post(f"/projects/{pid}/align/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": [fit_id],
                          "candidate_strategy": "proportional"})
    assert r.status_code == 201, r.text
    detail = client.get(f"/projects/{pid}/versions/{r.json()['new_version_id']}").json()
    by_lines = {tuple(c["lines"]): (c["start_frame"], c["end_frame"])
                for c in detail["cues"]}
    # 两条译文按比例映射进源跨度 200-250，结构（条数）不变
    assert by_lines[("Sure.",)] == (200, 220)
    assert by_lines[("No problem.",)] == (225, 250)


# ---------------------------------------------------------------- 说话人提取（单元）

def test_speakers_of_variants():
    from app.align import speakers_of
    from app.parsing import Cue

    assert speakers_of(Cue(1, 0, 10, ["<v 张三>你好</v>"])) == ["张三"]
    assert speakers_of(Cue(1, 0, 10, ["- 李四：你好吗"])) == ["李四"]
    assert speakers_of(Cue(1, 0, 10, ["【王五】喂"])) == ["王五"]
    assert speakers_of(Cue(1, 0, 10, ["赵六：走吧"])) == ["赵六"]
    assert speakers_of(Cue(1, 0, 10, ["没有标签的一句"])) == []
