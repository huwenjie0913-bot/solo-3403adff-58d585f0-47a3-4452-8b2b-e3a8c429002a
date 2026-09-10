"""端到端 API 测试：模板 → 项目 → 版本 → 质检 → 自动修复 → 比较 → 导出。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

RULES = {
    "max_cps": 20.0,
    "max_chars_per_line": 16,
    "max_lines": 2,
    "min_duration_ms": 800,
    "max_duration_ms": 7000,
    "min_gap_ms": 100,
    "max_offset_ms": 1200,
    "shot_tolerance_ms": 0,
}

SRT_BAD = """1
00:00:01,000 --> 00:00:01,300
太短

2
00:00:02,000 --> 00:00:04,000
这一行字幕实在是太长了，远远超过单行字数上限

3
00:00:03,900 --> 00:00:05,000
重叠了

4
00:00:05,200 --> 00:00:07,000
这句字幕跨越了镜头切点

5
00:00:07,200 --> 00:00:09,000
断行不自然的
，示例

6
00:00:09,200 --> 00:00:10,000
阅读速度超限的一句话字数很多时间短
"""

VTT = """WEBVTT

intro
00:00:01.000 --> 00:00:03.000
<v 张三>你好，世界</v>

00:00:04.000 --> 00:00:06.500
<i>斜体</i>与<b>粗体</b>样式保留
"""


def _make_project(rules=None, shot_cuts=None):
    payload = {"name": "测试剧集", "frame_rate": 25.0}
    if rules is not None:
        payload["rules"] = rules
    if shot_cuts is not None:
        payload["shot_cuts"] = shot_cuts
    r = client.post("/projects", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(pid, content, label="v1"):
    r = client.post(f"/projects/{pid}/versions", json={"label": label, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- 规则模板

def test_rule_template_crud():
    r = client.post("/rule-templates", json={"name": "网剧规范", "rules": RULES})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["rules"]["max_cps"] == 20.0

    # 重名冲突
    r = client.post("/rule-templates", json={"name": "网剧规范", "rules": RULES})
    assert r.status_code == 409

    r = client.get("/rule-templates")
    assert any(t["id"] == tid for t in r.json())

    r = client.get(f"/rule-templates/{tid}")
    assert r.json()["name"] == "网剧规范"

    r = client.put(f"/rule-templates/{tid}", json={"rules": {**RULES, "max_cps": 17.0}})
    assert r.status_code == 200
    assert r.json()["rules"]["max_cps"] == 17.0

    r = client.delete(f"/rule-templates/{tid}")
    assert r.status_code == 204
    assert client.get(f"/rule-templates/{tid}").status_code == 404


def test_project_with_template():
    r = client.post("/rule-templates", json={"name": "电影规范", "rules": RULES})
    tid = r.json()["id"]
    r = client.post("/projects", json={
        "name": "电影A", "frame_rate": 24.0,
        "shot_cuts": [6000, "00:00:12.500"], "rule_template_id": tid,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["shot_cuts"] == [6000, 12500]
    assert body["rules"]["max_chars_per_line"] == 16

    # 不存在的模板
    r = client.post("/projects", json={"name": "x", "rule_template_id": 9999})
    assert r.status_code == 404


# ---------------------------------------------------------------- 上传与解析

def test_upload_srt_and_vtt():
    pid = _make_project()
    vid = _upload(pid, SRT_BAD)
    r = client.get(f"/projects/{pid}/versions/{vid}")
    assert r.status_code == 200
    assert len(r.json()["cues"]) == 6

    vid2 = _upload(pid, VTT, "vtt版")
    r = client.get(f"/projects/{pid}/versions/{vid2}")
    body = r.json()
    assert body["format"] == "vtt"
    assert body["cue_count"] == 2
    # 说话人标签与样式标签保留
    assert body["cues"][0]["lines"] == ["<v 张三>你好，世界</v>"]
    assert "<i>" in body["cues"][1]["lines"][0]
    # VTT cue 标识符保留
    assert body["cues"][0]["identifier"] == "intro"

    r = client.get(f"/projects/{pid}/versions")
    assert len(r.json()) == 2


def test_upload_invalid():
    pid = _make_project()
    r = client.post(f"/projects/{pid}/versions",
                    json={"label": "bad", "content": "这不是字幕"})
    assert r.status_code == 400


# ---------------------------------------------------------------- 质检

def test_qc_detects_issues():
    pid = _make_project(rules=RULES, shot_cuts=[6000])
    vid = _upload(pid, SRT_BAD)
    r = client.post(f"/projects/{pid}/versions/{vid}/qc")
    assert r.status_code == 200, r.text
    report = r.json()
    types = {i["issue_type"] for i in report["issues"]}
    assert "flash" in types            # 第1条 300ms 闪现
    assert "line_too_long" in types    # 第2条超宽
    assert "overlap" in types          # 第2/3条重叠
    assert "shot_cross" in types       # 第4条跨 6000ms 切点
    assert "break_punct_start" in types  # 第5条行首标点
    assert "cps_exceeded" in types     # 第6条阅读速度超限
    # 每项问题都有原因和修正候选
    for issue in report["issues"]:
        assert issue["message"]
        assert issue["fix_candidates"], issue
    assert report["summary"]["by_severity"]["error"] > 0


# ---------------------------------------------------------------- 自动修复

def test_autofix_creates_fixed_version():
    pid = _make_project(rules=RULES, shot_cuts=[6000])
    vid = _upload(pid, SRT_BAD)
    r = client.post(f"/projects/{pid}/versions/{vid}/autofix", json={"label": "fixed"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["summary"]["fixed_cues"] > 0
    new_vid = body["new_version_id"]
    assert new_vid != vid

    # 原稿未被覆盖
    orig = client.get(f"/projects/{pid}/versions/{vid}").json()
    assert orig["cues"][0]["end_ms"] == 1300

    # 新版本全部通过质检（无 error 级问题）
    report = client.post(f"/projects/{pid}/versions/{new_vid}/qc").json()
    errors = [i for i in report["issues"] if i["severity"] == "error"]
    assert errors == [], errors
    assert body["conflicts"] == []

    # 帧对齐：25fps → 所有时间点为 40ms 的整数倍
    fixed = client.get(f"/projects/{pid}/versions/{new_vid}").json()
    for cue in fixed["cues"]:
        assert cue["start_ms"] % 40 == 0
        assert cue["end_ms"] % 40 == 0


def test_autofix_conflict_keeps_original():
    # 偏移上限极小 + 间隔不足 → 闪现无法修复 → 冲突且保留原稿
    rules = {**RULES, "max_offset_ms": 50}
    srt = """1
00:00:01,000 --> 00:00:01,300
太短

2
00:00:01,350 --> 00:00:03,000
正常的第二条字幕
"""
    pid = _make_project(rules=rules)
    vid = _upload(pid, srt)
    r = client.post(f"/projects/{pid}/versions/{vid}/autofix")
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["cue_index"] == 1
    assert body["conflicts"][0]["reasons"]
    # 冲突字幕在新版本中保持原稿
    fixed = client.get(f"/projects/{pid}/versions/{body['new_version_id']}").json()
    assert fixed["cues"][0]["start_ms"] == 1000
    assert fixed["cues"][0]["end_ms"] == 1300


def test_autofix_splits_long_cue():
    rules = {**RULES, "max_cps": 15.0, "max_duration_ms": 3000, "max_offset_ms": 4000}
    srt = """1
00:00:01,000 --> 00:00:05,000
这是一句非常长的台词，包含了太多的信息。观众根本来不及读完，必须拆成两句才行。

2
00:00:12,000 --> 00:00:14,000
后续台词
"""
    pid = _make_project(rules=rules)
    vid = _upload(pid, srt)
    r = client.post(f"/projects/{pid}/versions/{vid}/autofix")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["summary"]["cue_count_after"] > 2  # 被拆分
    report = client.post(
        f"/projects/{pid}/versions/{body['new_version_id']}/qc").json()
    assert [i for i in report["issues"] if i["severity"] == "error"] == []


# ---------------------------------------------------------------- 版本比较

def test_diff_versions():
    pid = _make_project(rules=RULES, shot_cuts=[6000])
    vid = _upload(pid, SRT_BAD)
    new_vid = client.post(f"/projects/{pid}/versions/{vid}/autofix").json()["new_version_id"]
    r = client.get(f"/projects/{pid}/diff",
                   params={"from_version": vid, "to_version": new_vid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["changed"] > 0
    kinds = set()
    for ch in body["changes"]:
        if ch["type"] == "changed":
            kinds.update(ch["changes"].keys())
    assert "start" in kinds or "end" in kinds  # 时间轴改动
    assert "text" in kinds                     # 断行改动


# ---------------------------------------------------------------- 导出

def test_export_formats():
    pid = _make_project(rules=RULES, shot_cuts=[6000])
    vid = _upload(pid, SRT_BAD)
    new_vid = client.post(f"/projects/{pid}/versions/{vid}/autofix").json()["new_version_id"]

    r = client.get(f"/projects/{pid}/versions/{new_vid}/export", params={"format": "srt"})
    assert r.status_code == 200
    assert "-->" in r.text and "00:00" in r.text
    assert "attachment" in r.headers["content-disposition"]

    r = client.get(f"/projects/{pid}/versions/{new_vid}/export", params={"format": "vtt"})
    assert r.text.startswith("WEBVTT")
    assert "-->" in r.text

    r = client.get(f"/projects/{pid}/versions/{new_vid}/export", params={"format": "report"})
    assert r.status_code == 200
    report = r.json()
    assert report["summary"]["cue_count"] > 0
    assert "issues" in report and "rules" in report


def test_export_vtt_roundtrip_keeps_tags():
    pid = _make_project()
    vid = _upload(pid, VTT)
    r = client.get(f"/projects/{pid}/versions/{vid}/export", params={"format": "vtt"})
    assert "<v 张三>" in r.text
    assert "<i>" in r.text
    # 也可以导出为 SRT（格式转换）
    r = client.get(f"/projects/{pid}/versions/{vid}/export", params={"format": "srt"})
    assert "-->" in r.text
