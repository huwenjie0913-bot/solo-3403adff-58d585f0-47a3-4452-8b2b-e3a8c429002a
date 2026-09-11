"""双语术语一致性模块测试：术语表维护 → 检查 → 预览 → 应用 → 质检/比较/导出。

25fps 项目：源（中文）/译文（英文）各 5 条一对一 cue，另加一个译文漏条场景。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 源语言版本
SRC = """1
00:00:01,000 --> 00:00:03,000
我们马上出发吧

2
00:00:04,000 --> 00:00:06,000
请联系管理员

3
00:00:07,000 --> 00:00:09,000
联系管理员确认

4
00:00:10,000 --> 00:00:12,000
打开控制面板

5
00:00:13,000 --> 00:00:15,000
登录后台
"""

# 译文版本：
# cue1 禁用变体 leave；cue2 首选 administrator；cue3 可接受 admin（跨组不一致）；
# cue4 大小写错误 control panel；cue5 大小写错误 log in
TGT = """1
00:00:01,000 --> 00:00:03,000
We will leave now.

2
00:00:04,000 --> 00:00:06,000
Contact the administrator.

3
00:00:07,000 --> 00:00:09,000
Contact the admin.

4
00:00:10,000 --> 00:00:12,000
Open the control panel.

5
00:00:13,000 --> 00:00:15,000
log in here
"""

# 译文漏条：只有 4 条 cue（源第 5 条无对应译文）
TGT_MISSING = """1
00:00:01,000 --> 00:00:03,000
We depart now.

2
00:00:04,000 --> 00:00:06,000
Contact the administrator.

3
00:00:07,000 --> 00:00:09,000
Contact the admin.

4
00:00:10,000 --> 00:00:12,000
Open the Control Panel.
"""


def _project():
    r = client.post("/projects", json={"name": "术语剧集", "frame_rate": 25.0})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(pid, content, label):
    r = client.post(f"/projects/{pid}/versions",
                    json={"label": label, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _setup(tgt=TGT):
    pid = _project()
    sid = _upload(pid, SRC, "源-中文")
    tid = _upload(pid, tgt, "译-英文")
    return pid, sid, tid


def _add_rule(pid, **kw):
    r = client.post(f"/projects/{pid}/terminology", json=kw)
    assert r.status_code == 201, r.text
    return r.json()


def _default_rules(pid):
    r1 = _add_rule(pid, source_term="出发", preferred_translation="depart",
                   forbidden_variants=["leave"], severity="error")
    r2 = _add_rule(pid, source_term="管理员", preferred_translation="administrator",
                   acceptable_variants=["admin"], severity="warning")
    r3 = _add_rule(pid, source_term="控制面板", preferred_translation="Control Panel",
                   case_sensitive=True, whole_word=True, severity="error")
    r4 = _add_rule(pid, source_term="登录", preferred_translation="Log in",
                   case_sensitive=True, severity="error")
    return r1["id"], r2["id"], r3["id"], r4["id"]


def _check(pid, sid, tid, **kw):
    r = client.post(f"/projects/{pid}/terminology/check",
                    json={"source_version_id": sid, "target_version_id": tid, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 术语表维护

def test_term_rule_crud():
    pid = _project()
    t = _add_rule(pid, source_term="出发", preferred_translation="depart",
                  acceptable_variants=["set off", " set off ", ""],
                  forbidden_variants=["leave"], note="动作戏统一")
    rid = t["id"]
    # 变体去空白、去重、去空串
    assert t["acceptable_variants"] == ["set off"]
    assert t["case_sensitive"] is False
    assert t["whole_word"] is True
    assert t["severity"] == "error"

    # 列表 / 详情
    assert [x["id"] for x in client.get(f"/projects/{pid}/terminology").json()] == [rid]
    assert client.get(f"/projects/{pid}/terminology/{rid}").json()["source_term"] == "出发"

    # 项目内源语词条唯一
    r = client.post(f"/projects/{pid}/terminology",
                    json={"source_term": "出发", "preferred_translation": "go"})
    assert r.status_code == 409

    # 禁用变体不能与首选/可接受重合
    r = client.post(f"/projects/{pid}/terminology",
                    json={"source_term": "x", "preferred_translation": "y",
                          "forbidden_variants": ["y"]})
    assert r.status_code == 422
    r = client.post(f"/projects/{pid}/terminology",
                    json={"source_term": "x", "preferred_translation": "y",
                          "acceptable_variants": ["z"], "forbidden_variants": ["z"]})
    assert r.status_code == 422

    # 局部更新
    r = client.put(f"/projects/{pid}/terminology/{rid}",
                   json={"severity": "warning", "note": "改了"})
    assert r.status_code == 200
    assert r.json()["severity"] == "warning"
    assert r.json()["note"] == "改了"
    assert r.json()["preferred_translation"] == "depart"  # 未传字段保留

    # 改成重名 409
    other = _add_rule(pid, source_term="抵达", preferred_translation="arrive")
    r = client.put(f"/projects/{pid}/terminology/{other['id']}",
                   json={"source_term": "出发"})
    assert r.status_code == 409

    # 删除
    assert client.delete(f"/projects/{pid}/terminology/{rid}").status_code == 204
    assert client.get(f"/projects/{pid}/terminology/{rid}").status_code == 404


def test_term_rule_project_isolation():
    p1, p2 = _project(), _project()
    _add_rule(p1, source_term="出发", preferred_translation="depart")
    assert client.get(f"/projects/{p2}/terminology").json() == []
    # 跨项目访问 404
    rid = client.get(f"/projects/{p1}/terminology").json()[0]["id"]
    assert client.get(f"/projects/{p2}/terminology/{rid}").status_code == 404


def test_term_rule_deleted_with_project():
    pid = _project()
    _add_rule(pid, source_term="出发", preferred_translation="depart")
    assert client.delete(f"/projects/{pid}").status_code == 204
    # 项目级联删除术语条目（不报错即可；条目随项目消失）
    other = _project()
    assert client.get(f"/projects/{other}/terminology").json() == []


# ---------------------------------------------------------------- 检查：四类问题

def test_check_detects_all_issue_types():
    pid, sid, tid = _setup()
    _default_rules(pid)
    rep = _check(pid, sid, tid)

    assert rep["thresholds"]["min_overlap_frames"] == 5  # 200ms @25fps
    assert rep["thresholds"]["rule_count"] == 4
    assert rep["mapping_count"] == 5
    by_type = rep["summary"]["by_type"]
    assert by_type["term_forbidden_variant"] == 1   # cue1 leave
    assert by_type["term_inconsistent"] == 1        # cue3 admin vs administrator
    assert by_type["term_case_error"] == 2          # cue4 control panel / cue5 log in
    assert rep["summary"]["fixable_candidate_count"] == 4
    # 严重级别取条目设置
    sevs = {(i["issue_type"], i["severity"]) for i in rep["issues"]}
    assert ("term_forbidden_variant", "error") in sevs
    assert ("term_inconsistent", "warning") in sevs

    # 字段化原因 + 两侧 cue + 实际片段 + 上下文 + 规则快照
    forbidden = next(i for i in rep["issues"]
                     if i["issue_type"] == "term_forbidden_variant")
    assert forbidden["mapping_id"] == 1
    assert forbidden["rule"]["source_term"] == "出发"
    assert [c["index"] for c in forbidden["source_cues"]] == [1]
    assert [c["index"] for c in forbidden["target_cues"]] == [1]
    assert forbidden["source_fragments"][0]["text"] == "出发"
    assert forbidden["target_fragments"][0]["text"] == "leave"
    assert forbidden["target_fragments"][0]["cue_index"] == 1
    assert "leave" in forbidden["target_fragments"][0]["context"]
    assert forbidden["reasons"]["forbidden_variants"] == ["leave"]
    assert forbidden["reasons"]["preferred_translation"] == "depart"
    # 时间码字段
    assert forbidden["source_cues"][0]["start_tc"] == "00:00:01:00"

    # 不一致：标准用法为首选 administrator，cue3 的 admin 出候选
    inc = next(i for i in rep["issues"] if i["issue_type"] == "term_inconsistent")
    assert inc["mapping_id"] == 3
    assert inc["reasons"]["canonical"] == "administrator"
    assert inc["target_fragments"][0]["text"] == "admin"

    # 大小写错误：期望写法字段化，候选替换为规范拼写（保留原词）
    ce = [i for i in rep["issues"] if i["issue_type"] == "term_case_error"]
    panel = next(i for i in ce if i["rule"]["source_term"] == "控制面板")
    assert panel["reasons"]["expected"] == ["Control Panel"]
    assert panel["reasons"]["found"] == ["control panel"]


def test_check_untranslated_and_missing_translation():
    pid, sid, tid = _setup(tgt=TGT_MISSING)
    _default_rules(pid)
    rep = _check(pid, sid, tid)
    by_type = rep["summary"]["by_type"]
    assert rep["summary"]["unmatched_source_count"] == 1
    assert by_type["term_untranslated"] == 1  # 源第 5 条（登录）译文漏条
    # TGT_MISSING 中管理员译法也不一致（administrator vs admin），另报一项
    assert by_type["term_inconsistent"] == 1

    miss = next(i for i in rep["issues"]
                if i["issue_type"] == "term_untranslated")
    assert miss["mapping_id"] is None
    assert miss["source_cues"][0]["index"] == 5
    assert miss["target_cues"] == []
    assert miss["reasons"]["missing_translation"] is True
    assert miss["fix_candidates"] == []  # 无译文可替换


def test_check_untranslated_when_target_has_no_variant():
    """源词出现但译文既无首选也无可接受变体 → 源词未译（无候选）。"""
    pid = _project()
    src = "1\n00:00:01,000 --> 00:00:03,000\n出发吧\n"
    tgt = "1\n00:00:01,000 --> 00:00:03,000\nWe go now.\n"
    sid, tid = _upload(pid, src, "s"), _upload(pid, tgt, "t")
    _add_rule(pid, source_term="出发", preferred_translation="depart",
              forbidden_variants=["leave"])
    rep = _check(pid, sid, tid)
    types = [i["issue_type"] for i in rep["issues"]]
    assert types == ["term_untranslated"]
    issue = rep["issues"][0]
    assert issue["reasons"]["expected_any"] == ["depart"]
    assert issue["target_fragments"] == []


def test_check_clean_when_preferred_everywhere():
    pid = _project()
    src = "1\n00:00:01,000 --> 00:00:03,000\n出发\n\n2\n00:00:04,000 --> 00:00:06,000\n出发\n"
    tgt = "1\n00:00:01,000 --> 00:00:03,000\ndepart\n\n2\n00:00:04,000 --> 00:00:06,000\nDepart\n"
    sid, tid = _upload(pid, src, "s"), _upload(pid, tgt, "t")
    _add_rule(pid, source_term="出发", preferred_translation="depart")
    rep = _check(pid, sid, tid)
    assert rep["issues"] == []
    assert rep["summary"]["issue_count"] == 0


def test_check_whole_word_boundary():
    pid = _project()
    # whole_word=True 时不得匹配子串：departed 不算 depart
    src = "1\n00:00:01,000 --> 00:00:03,000\n出发\n"
    tgt = "1\n00:00:01,000 --> 00:00:03,000\nThey departed.\n"
    sid, tid = _upload(pid, src, "s"), _upload(pid, tgt, "t")
    r = _add_rule(pid, source_term="出发", preferred_translation="depart")
    rep = _check(pid, sid, tid)
    assert [i["issue_type"] for i in rep["issues"]] == ["term_untranslated"]

    # 关闭整词匹配后 departed 命中
    client.put(f"/projects/{pid}/terminology/{r['id']}", json={"whole_word": False})
    rep = _check(pid, sid, tid)
    assert rep["issues"] == []


def test_check_rule_ids_filter_and_thresholds():
    pid, sid, tid = _setup()
    ids = _default_rules(pid)
    rep = _check(pid, sid, tid, rule_ids=[ids[0]], min_overlap_ms=1000)
    assert [r["id"] for r in rep["rules"]] == [ids[0]]
    assert rep["thresholds"]["min_overlap_frames"] == 25
    assert all(i["rule"]["id"] == ids[0] for i in rep["issues"])
    # 未知 rule id → 404
    r = client.post(f"/projects/{pid}/terminology/check",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "rule_ids": [9999]})
    assert r.status_code == 404
    # 同版本 400
    r = client.post(f"/projects/{pid}/terminology/check",
                    json={"source_version_id": sid, "target_version_id": sid})
    assert r.status_code == 400


def test_check_candidate_ids_stable():
    pid, sid, tid = _setup()
    _default_rules(pid)
    r1 = _check(pid, sid, tid)
    r2 = _check(pid, sid, tid)
    ids1 = sorted(c["id"] for i in r1["issues"] for c in i["fix_candidates"])
    ids2 = sorted(c["id"] for i in r2["issues"] for c in i["fix_candidates"])
    assert ids1 == ids2
    assert all(x.startswith("t") and ".r" in x and x.endswith(("c1",)) for x in ids1)


# ---------------------------------------------------------------- 预览

def test_preview_selected_candidates():
    pid, sid, tid = _setup()
    _default_rules(pid)
    rep = _check(pid, sid, tid)
    forbidden = next(i for i in rep["issues"]
                     if i["issue_type"] == "term_forbidden_variant")
    cid = forbidden["fix_candidates"][0]["id"]
    r = client.post(f"/projects/{pid}/terminology/preview",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": [cid]})
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["candidate_id"] == cid
    assert item["before_line"] == "We will leave now."
    assert item["after_line"] == "We will depart now."
    assert item["found"] == "leave"
    assert item["replacement"] == "depart"
    # 预览不落库、原稿不变
    orig = client.get(f"/projects/{pid}/versions/{tid}").json()
    assert orig["cues"][0]["lines"] == ["We will leave now."]

    r = client.post(f"/projects/{pid}/terminology/preview",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": ["t1.r1.c9"]})
    assert r.status_code == 400


# ---------------------------------------------------------------- 应用

def test_apply_all_candidates_preserves_timing_tags_breaks():
    pid, sid, tid = _setup()
    _default_rules(pid)
    r = client.post(f"/projects/{pid}/terminology/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "label": "英文-术语统一"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["label"] == "英文-术语统一"
    assert body["origin_version_id"] == tid
    assert body["summary"]["applied_count"] == 4
    assert body["summary"]["cue_count_after"] == 5

    nid = body["new_version_id"]
    cues = client.get(f"/projects/{pid}/versions/{nid}").json()["cues"]
    assert cues[0]["lines"] == ["We will depart now."]
    assert cues[1]["lines"] == ["Contact the administrator."]
    assert cues[2]["lines"] == ["Contact the administrator."]
    assert cues[3]["lines"] == ["Open the Control Panel."]
    assert cues[4]["lines"] == ["Log in here"]
    # 时间码完全不变
    assert [(c["start_frame"], c["end_frame"]) for c in cues] == [
        (25, 75), (100, 150), (175, 225), (250, 300), (325, 375)]

    detail = client.get(f"/projects/{pid}/versions/{nid}").json()
    prov = detail["provenance"]
    assert prov["kind"] == "terminology"
    assert prov["source_version_id"] == sid
    assert prov["target_version_id"] == tid
    assert len(prov["rules_snapshot"]) == 4
    assert prov["rules_snapshot"][0]["source_term"] == "出发"
    assert detail["origin_version_id"] == tid

    # 原稿保留
    orig = client.get(f"/projects/{pid}/versions/{tid}").json()
    assert orig["cues"][0]["lines"] == ["We will leave now."]
    assert orig["origin_version_id"] is None


def test_apply_selected_and_empty():
    pid, sid, tid = _setup()
    _default_rules(pid)
    rep = _check(pid, sid, tid)
    forbidden = next(i for i in rep["issues"]
                     if i["issue_type"] == "term_forbidden_variant")
    cid = forbidden["fix_candidates"][0]["id"]
    r = client.post(f"/projects/{pid}/terminology/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": [cid]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["summary"]["applied_count"] == 1
    cues = client.get(
        f"/projects/{pid}/versions/{body['new_version_id']}").json()["cues"]
    assert cues[0]["lines"] == ["We will depart now."]
    assert cues[4]["lines"] == ["log in here"]  # 未选中，原样

    # 空列表：不应用任何替换
    r = client.post(f"/projects/{pid}/terminology/apply",
                    json={"source_version_id": sid, "target_version_id": tid,
                          "candidate_ids": []})
    body = r.json()
    assert body["summary"]["applied_count"] == 0


def test_apply_preserves_speaker_and_style_tags():
    """替换片段位于标签内部时保留标签；片段跨标签时不出候选。"""
    pid = _project()
    src = ("1\n00:00:01,000 --> 00:00:03,000\n联系管理员\n\n"
           "2\n00:00:04,000 --> 00:00:06,000\n联系管理员\n\n"
           "3\n00:00:07,000 --> 00:00:09,000\n管理员在吗\n")
    tgt = ("WEBVTT\n\n"
           "00:00:01.000 --> 00:00:03.000\n"
           "Contact the administrator.\n\n"
           "00:00:04.000 --> 00:00:06.000\n"
           "<v 张三>Contact the <b>admin</b>.</v>\n\n"
           "00:00:07.000 --> 00:00:09.000\n"
           "adm<i>i</i>n here?\n")
    sid = _upload(pid, src, "s")
    tid = _upload(pid, tgt, "t")
    _add_rule(pid, source_term="管理员", preferred_translation="administrator",
              acceptable_variants=["admin"], severity="warning")
    rep = _check(pid, sid, tid)
    inc = [i for i in rep["issues"] if i["issue_type"] == "term_inconsistent"]
    # 组2 的 admin 整段位于 <b> 内 → 有候选；组3 的匹配跨越 <i> 标签 → 无候选
    by_group = {i["mapping_id"]: i for i in inc}
    assert len(by_group[2]["fix_candidates"]) == 1
    assert by_group[3]["fix_candidates"] == []

    r = client.post(f"/projects/{pid}/terminology/apply",
                    json={"source_version_id": sid, "target_version_id": tid})
    nid = r.json()["new_version_id"]
    cues = client.get(f"/projects/{pid}/versions/{nid}").json()["cues"]
    assert cues[1]["lines"] == ["<v 张三>Contact the <b>administrator</b>.</v>"]
    # 组3 不可自动替换（匹配跨越 <i> 标签），原样保留
    assert cues[2]["lines"] == ["adm<i>i</i>n here?"]
    # VTT 导出标签保留
    vtt = client.get(f"/projects/{pid}/versions/{nid}/export",
                     params={"format": "vtt"}).text
    assert "<v 张三>" in vtt and "<b>administrator</b>" in vtt
    assert "adm<i>i</i>n here?" in vtt


def test_apply_then_qc_diff_export():
    pid, sid, tid = _setup()
    _default_rules(pid)
    nid = client.post(f"/projects/{pid}/terminology/apply",
                      json={"source_version_id": sid, "target_version_id": tid}
                      ).json()["new_version_id"]
    # 继续质检
    qc = client.post(f"/projects/{pid}/versions/{nid}/qc").json()
    assert [i for i in qc["issues"] if i["severity"] == "error"] == []
    # 差异比较
    diff = client.get(f"/projects/{pid}/diff",
                      params={"from_version": tid, "to_version": nid}).json()
    assert diff["summary"]["changed"] >= 4
    texts = {ch["old_index"]: ch["changes"]["text"]
             for ch in diff["changes"]
             if ch["type"] == "changed" and "text" in ch["changes"]}
    assert 1 in texts
    # 导出 SRT / WebVTT / 帧级
    srt = client.get(f"/projects/{pid}/versions/{nid}/export",
                     params={"format": "srt"}).text
    assert "We will depart now." in srt
    vtt = client.get(f"/projects/{pid}/versions/{nid}/export",
                     params={"format": "vtt"}).text
    assert vtt.startswith("WEBVTT")
    frames = client.get(f"/projects/{pid}/versions/{nid}/export",
                        params={"format": "frames"}).json()
    assert frames["cue_count"] == 5
    assert frames["cues"][0]["start_tc"] == "00:00:01:00"
    # 术语复检：禁用/大小写问题已消失
    rep = _check(pid, sid, nid)
    remaining = {i["issue_type"] for i in rep["issues"]}
    assert "term_forbidden_variant" not in remaining
    assert "term_case_error" not in remaining


# ---------------------------------------------------------------- 结构安全回归（换行/标签注入）

def test_create_rejects_newline_or_tag_in_translation_fields():
    pid = _project()
    base = {"source_term": "校对", "preferred_translation": "proofread"}

    # preferred_translation 含换行（\n 与 \r\n）
    r = client.post(f"/projects/{pid}/terminology",
                    json={**base, "preferred_translation": "fix\nnow"})
    assert r.status_code == 422
    assert "换行" in str(r.json())

    r = client.post(f"/projects/{pid}/terminology",
                    json={**base, "preferred_translation": "fix\r\nnow"})
    assert r.status_code == 422 and "换行" in str(r.json())

    # preferred_translation 含标签
    r = client.post(f"/projects/{pid}/terminology",
                    json={**base, "preferred_translation": "<i>proofread</i>"})
    assert r.status_code == 422
    assert "标签" in str(r.json())

    # 可接受/禁用变体含换行或标签同样拒绝
    r = client.post(f"/projects/{pid}/terminology",
                    json={**base, "acceptable_variants": ["fix\nnow"]})
    assert r.status_code == 422 and "换行" in str(r.json())
    r = client.post(f"/projects/{pid}/terminology",
                    json={**base, "forbidden_variants": ["<b>x</b>"]})
    assert r.status_code == 422 and "标签" in str(r.json())
    r = client.post(f"/projects/{pid}/terminology",
                    json={**base, "forbidden_variants": ["{\\an8}"]})
    assert r.status_code == 422 and "标签" in str(r.json())

    # source_term 含换行/标签也拒绝（源侧匹配同样基于纯文本）
    r = client.post(f"/projects/{pid}/terminology",
                    json={"source_term": "校\n对", "preferred_translation": "x"})
    assert r.status_code == 422


def test_update_rejects_newline_or_tag():
    pid = _project()
    rid = _add_rule(pid, source_term="校对",
                    preferred_translation="proofread")["id"]
    r = client.put(f"/projects/{pid}/terminology/{rid}",
                   json={"preferred_translation": "fix\nnow"})
    assert r.status_code == 422 and "换行" in str(r.json())
    r = client.put(f"/projects/{pid}/terminology/{rid}",
                   json={"acceptable_variants": ["<i>x</i>"]})
    assert r.status_code == 422 and "标签" in str(r.json())
    # 拒绝后原值保持不变
    cur = client.get(f"/projects/{pid}/terminology/{rid}").json()
    assert cur["preferred_translation"] == "proofread"
    assert cur["acceptable_variants"] == []


def test_candidate_generation_guard_drops_structural_replacement():
    """引擎层防护：替换文本含换行/标签时不生成候选（防御绕过校验层的旧数据）。"""
    from app.schemas import TerminologyRuleOut
    from app.terminology import run_terminology
    from app.align import AlignOptions
    from app.timecode import build_timebase
    from app.parsing import Cue, parse_subtitles

    tb = build_timebase(25.0)
    src_cues, _ = parse_subtitles(
        "1\n00:00:01,000 --> 00:00:03,000\n校对完成\n", tb, "srt")
    tgt_cues, _ = parse_subtitles(
        "1\n00:00:01,000 --> 00:00:03,000\ncheck done\n", tb, "srt")

    # 直接构造规则快照（绕过 API 校验）：首选译法含换行
    rule = TerminologyRuleOut(
        id=1, project_id=1, source_term="校对",
        preferred_translation="fix\nnow", acceptable_variants=[],
        forbidden_variants=["check"], case_sensitive=False, whole_word=True,
        severity="error", note=None,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")
    opts = AlignOptions.from_ms(200, 0.3, True, "source_boundaries", tb)
    from app.schemas import TimebaseOut
    tbo = TimebaseOut(fps=25.0, fps_label="25", rate_num=25, rate_den=1,
                      drop_frame=False, start_timecode="00:00:00:00")
    report, _ = run_terminology(
        src_cues, tgt_cues, [rule], opts, tb, project_id=1,
        source_version_id=1, target_version_id=2,
        timebase_out=tbo, min_overlap_ms=200)
    issue = report.issues[0]
    assert issue.issue_type == "term_forbidden_variant"
    # 首选译法 "fix\nnow" 含换行 → 不出候选，避免结构被改写
    assert issue.fix_candidates == []

    # 首选译法含标签同理
    rule2 = rule.model_copy(update={"id": 2,
                                    "preferred_translation": "<i>proofread</i>"})
    report2, _ = run_terminology(
        src_cues, tgt_cues, [rule2], opts, tb, project_id=1,
        source_version_id=1, target_version_id=2,
        timebase_out=tbo, min_overlap_ms=200)
    assert report2.issues[0].fix_candidates == []


def test_apply_rejects_structural_candidate_via_engine_guard():
    """API 端到端：校验层保证正常术语可用；含结构的首选译法无法创建，
    且即便存在也不会产生候选，应用不改变 cue 结构。"""
    pid, sid, tid = _setup()
    ids = _default_rules(pid)
    # 正常普通替换仍可用（对照）
    rep = _check(pid, sid, tid)
    assert rep["summary"]["fixable_candidate_count"] >= 1
    # 尝试通过 API 创建含结构的术语必须失败
    r = client.post(f"/projects/{pid}/terminology",
                    json={"source_term": "校对", "preferred_translation": "fix\nnow"})
    assert r.status_code == 422
    r = client.post(f"/projects/{pid}/terminology",
                    json={"source_term": "校对",
                          "preferred_translation": "<i>proofread</i>"})
    assert r.status_code == 422


def test_normal_replacement_preserves_structure_and_downstream():
    """普通单行纯文本替换：时间码/标签/换行不变，质检/比较/导出链路正常。"""
    pid, sid, tid = _setup()
    _default_rules(pid)

    before = client.get(f"/projects/{pid}/versions/{tid}").json()["cues"]
    before_struct = [(c["start_frame"], c["end_frame"], len(c["lines"]),
                      tuple(c["lines"])) for c in before]

    nid = client.post(f"/projects/{pid}/terminology/apply",
                      json={"source_version_id": sid,
                            "target_version_id": tid}).json()["new_version_id"]
    after = client.get(f"/projects/{pid}/versions/{nid}").json()["cues"]

    # 时间码与行数完全一致；只有文本变化，无新增换行/标签
    assert [(c["start_frame"], c["end_frame"], len(c["lines"]))
            for c in after] == [(s, e, n) for s, e, n, _ in before_struct]
    for c in after:
        for ln in c["lines"]:
            assert "\n" not in ln and "\r" not in ln

    # 原稿未改动
    orig = client.get(f"/projects/{pid}/versions/{tid}").json()["cues"]
    assert [tuple(c["lines"]) for c in orig] == [lines for *_, lines in before_struct]

    # 质检 / 比较 / SRT / VTT / 帧级导出链路正常
    assert client.post(f"/projects/{pid}/versions/{nid}/qc").status_code == 200
    assert client.get(f"/projects/{pid}/diff",
                      params={"from_version": tid, "to_version": nid}
                      ).json()["summary"]["changed"] >= 1
    srt = client.get(f"/projects/{pid}/versions/{nid}/export",
                     params={"format": "srt"}).text
    vtt = client.get(f"/projects/{pid}/versions/{nid}/export",
                     params={"format": "vtt"}).text
    frames = client.get(f"/projects/{pid}/versions/{nid}/export",
                        params={"format": "frames"}).json()
    # 替换后 cue 块时间码与原 cue 一一对应（以 cue1 为例）
    assert "00:00:01,000 --> 00:00:03,000" in srt
    assert vtt.startswith("WEBVTT")
    assert frames["cues"][0]["start_frame"] == before[0]["start_frame"]


def test_preview_and_apply_guard_reject_structural_candidate():
    """直接构造含换行/标签的候选（绕过生成环节），预览/应用必须拒绝。"""
    import pytest
    from app.schemas import TermFixCandidate
    from app.terminology import (
        TermCandidateUnsafeError, apply_terminology, preview_terminology)

    pid = _project()
    sid = _upload(pid, "1\n00:00:01,000 --> 00:00:03,000\n出发\n", "s")
    tid = _upload(pid, "1\n00:00:01,000 --> 00:00:03,000\nwe leave\n", "t")
    rep_json = _check(pid, sid, tid)
    assert rep_json["issues"] == []  # 无术语，报告为空

    # 手工伪造报告与含结构的候选
    from app.schemas import TermIssue, TerminologyReport, TerminologyRuleOut
    rule = TerminologyRuleOut(
        id=9, project_id=pid, source_term="出发", preferred_translation="depart",
        acceptable_variants=[], forbidden_variants=[], case_sensitive=False,
        whole_word=True, severity="error", note=None,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")
    from app.parsing import Cue
    tgt_cues = [Cue(1, 25, 75, ["we leave"])]

    def forged(replacement):
        return TermFixCandidate(
            id="t1.r9.c1", action="replace_term", description="x",
            cue_index=1, line_index=0, start=3, end=8, found="leave",
            replacement=replacement,
            preview_line="we " + replacement)

    def report_with(cand):
        issue = TermIssue(
            issue_type="term_forbidden_variant", severity="error",
            message="x", rule=rule, mapping_id=1,
            source_cues=[], target_cues=[], fix_candidates=[cand])
        return TerminologyReport.model_validate(
            {**rep_json, "issues": [issue.model_dump(mode="json")]})

    # 引擎层：换行替换
    rpt = report_with(forged("go\nnow"))
    with pytest.raises(TermCandidateUnsafeError):
        preview_terminology(rpt, tgt_cues, ["t1.r9.c1"])
    with pytest.raises(TermCandidateUnsafeError):
        apply_terminology(rpt, tgt_cues, ["t1.r9.c1"])
    # 引擎层：标签替换
    rpt = report_with(forged("<i>go</i>"))
    with pytest.raises(TermCandidateUnsafeError):
        preview_terminology(rpt, tgt_cues, ["t1.r9.c1"])
    with pytest.raises(TermCandidateUnsafeError):
        apply_terminology(rpt, tgt_cues, ["t1.r9.c1"])
    # 安全的普通替换正常应用，且不改变结构
    rpt = report_with(forged("depart"))
    new_cues, applied = apply_terminology(rpt, tgt_cues, ["t1.r9.c1"])
    assert new_cues[0].lines == ["we depart"]
    assert (new_cues[0].start_frame, new_cues[0].end_frame) == (25, 75)
    assert len(applied) == 1
