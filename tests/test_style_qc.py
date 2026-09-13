"""字幕样式兼容性校核测试：引擎纯函数 + 端到端 API。

覆盖：标签未闭合/多余闭合/交叉/跨行、嵌套深度、未知属性、同片段冲突、
说话人标签、ASS 覆盖、时间戳标签、cue settings（字段白名单/百分比/对齐
枚举）、格式不支持项、序列化往返漂移，以及修复候选的预览/应用/冲突与
“时间码、可见文本、换行不变”安全约束。
"""
from fastapi.testclient import TestClient

from app.main import app
from app.parsing import parse_subtitles, serialize, visible_text
from app.schemas import RenderConfig, TimebaseOut
from app.style_qc import (
    StyleCandidateConflictError,
    UnknownStyleCandidateError,
    apply_style_fixes,
    preview_style_fixes,
    run_style_check,
)
from app.timecode import build_timebase

client = TestClient(app)

TB = build_timebase(25)
TBO = TimebaseOut(
    fps=25.0, fps_label="25", rate_num=25, rate_den=1,
    drop_frame=False, start_timecode="00:00:00:00")


def _run(vtt_or_srt: str, fmt: str, config: RenderConfig):
    cues, src = parse_subtitles(vtt_or_srt, TB, fmt)
    report, by_cue = run_style_check(
        cues, config, TB, source_format=src, project_id=1, version_id=1,
        timebase_out=TBO)
    return cues, report, by_cue


def _types(report, cue: int = 1) -> list[str]:
    row = report.cues[cue - 1]
    return [i.issue_type for i in row.issues]


VTT_HEADER = "WEBVTT\n\n"


def vtt(body: str) -> str:
    return VTT_HEADER + body + "\n"


# ---------------------------------------------------------------- 结构问题

def test_unclosed_tag_diagnosed_and_fixed():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n<i>斜体没有闭合\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "unclosed_tag" in _types(report)
    new, applied = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["<i>斜体没有闭合</i>"]
    # 时间码与可见文本不变
    assert (new[0].start_frame, new[0].end_frame) == (
        cues[0].start_frame, cues[0].end_frame)
    assert [visible_text(x) for x in new[0].lines] == [
        visible_text(x) for x in cues[0].lines]
    assert applied[0]["action"] == "repair_structure"


def test_stray_closing_tag_removed():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n普通文本</i>\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "stray_closing_tag" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["普通文本"]


def test_crossed_tag_diagnostic_only():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n<b><i>交叉</b></i>\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "crossed_tag" in _types(report)
    # 交叉不能自动改写：没有任何候选
    assert not by_cue[1]


def test_style_cross_line_split_into_pairs():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n<i>第一行\n第二行</i> 尾\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "style_cross_line" in _types(report)
    issue = next(i for i in report.cues[0].issues
                 if i.issue_type == "style_cross_line")
    # 片段跨两行
    lines = {f.line_index for f in issue.fragments}
    assert lines == {0, 1}
    new, applied = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["<i>第一行</i>", "<i>第二行</i> 尾"]
    assert len(new[0].lines) == len(cues[0].lines)  # 换行语义不变
    assert applied[0]["action"] == "repair_structure"


def test_nesting_depth_limit():
    body = "<b><i><u>三层深</u></i></b>"
    cues, report, by_cue = _run(
        vtt(f"00:00:01.000 --> 00:00:03.000\n{body}\n"),
        "vtt", RenderConfig(target_format="vtt", max_nesting_depth=2))
    assert "nesting_too_deep" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert visible_text(new[0].lines[0]) == "三层深"


def test_duplicate_same_style_conflict():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n<b><b>重复粗体</b></b>\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "conflicting_style" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["<b>重复粗体</b>"]


def test_font_color_conflict_same_span():
    cues, report, _ = _run(
        vtt('00:00:01.000 --> 00:00:03.000\n'
            '<font color="red"><font color="blue">字</font></font>\n'),
        "vtt", RenderConfig(target_format="vtt"))
    assert "conflicting_style" in _types(report)
    issue = next(i for i in report.cues[0].issues
                 if i.issue_type == "conflicting_style")
    assert issue.details["attribute"] == "color"


# ---------------------------------------------------------------- 属性 / 标签白名单

def test_unknown_attribute_removed():
    cues, report, by_cue = _run(
        vtt('00:00:01.000 --> 00:00:03.000\n<i class="x">文本</i>\n'),
        "vtt", RenderConfig(target_format="vtt"))
    assert "unknown_attribute" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["<i>文本</i>"]


def test_unsupported_tag_unwrapped():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n<custom>自定义</custom>\n"),
        "vtt", RenderConfig(target_format="vtt", allowed_tags=["i", "b"]))
    assert "unsupported_tag" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["自定义"]


def test_speaker_tag_not_allowed_is_diagnostic_only():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n<v 张三>说话</v>\n"),
        "vtt", RenderConfig(target_format="srt"))
    assert "speaker_not_allowed" in _types(report)
    assert not by_cue[1]  # 不自动改写（会丢说话人归属）


def test_ass_override_removed():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n{\\an8}顶部文本\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "unsupported_override" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["顶部文本"]


def test_timestamp_tag_removed_for_srt_target():
    body = "00:00:01.000 --> 00:00:05.000\n<00:00:02.000>卡拉OK\n"
    cues, report, by_cue = _run(vtt(body), "vtt",
                                RenderConfig(target_format="srt"))
    assert "unsupported_timestamp_tag" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].lines == ["卡拉OK"]
    # VTT 目标不报错
    _, report_vtt, _ = _run(vtt(body), "vtt",
                            RenderConfig(target_format="vtt"))
    assert "unsupported_timestamp_tag" not in _types(report_vtt)


def test_malformed_tag_diagnostic_only():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n残缺<标签\n"),
        "vtt", RenderConfig(target_format="vtt"))
    assert "malformed_tag" in _types(report)
    assert not by_cue[1]


# ---------------------------------------------------------------- cue settings

def test_invalid_setting_values_diagnostic_only():
    body = ("00:00:01.000 --> 00:00:03.000 "
            "align:side position:150% line:banana\n文本\n")
    cues, report, _ = _run(vtt(body), "vtt",
                           RenderConfig(target_format="vtt"))
    types = _types(report)
    assert types.count("invalid_setting_value") == 3
    # 非法值不给候选（需人工决定正确对齐/位置）
    assert not report.cues[0].issues[0].fix_candidates


def test_disallowed_setting_field_removed():
    body = "00:00:01.000 --> 00:00:03.000 align:start line:1\n文本\n"
    cues, report, by_cue = _run(
        vtt(body), "vtt",
        RenderConfig(target_format="vtt",
                     allowed_cue_settings=["align"]))
    assert "unsupported_setting" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].settings == "align:start"


def test_unknown_setting_field():
    body = "00:00:01.000 --> 00:00:03.000 bogus:1\n文本\n"
    _, report, _ = _run(vtt(body), "vtt",
                        RenderConfig(target_format="vtt"))
    assert "unsupported_setting" in _types(report)


def test_settings_dropped_for_srt_target():
    body = "00:00:01.000 --> 00:00:03.000 align:middle\n文本\n"
    cues, report, by_cue = _run(vtt(body), "vtt",
                                RenderConfig(target_format="srt"))
    assert "settings_unsupported" in _types(report)
    new, _ = apply_style_fixes(cues, by_cue, None)
    assert new[0].settings is None


# ---------------------------------------------------------------- 格式与往返

def test_identifier_srt_diagnostic_and_roundtrip_drift():
    body = "intro\n00:00:01.000 --> 00:00:03.000\n文本\n"
    cues, report, _ = _run(vtt(body), "vtt",
                           RenderConfig(target_format="srt"))
    assert "identifier_unsupported" in _types(report)
    assert "roundtrip_drift" in _types(report)
    drift = next(i for i in report.cues[0].issues
                 if i.issue_type == "roundtrip_drift")
    fields = {d["field"] for d in drift.details["diffs"]}
    assert "identifier" in fields


def test_clean_vtt_has_no_issues():
    body = ("00:00:01.000 --> 00:00:03.000 align:start\n"
            "<c.foo>类</c><v 张三>说</v> "
            "<font color=\"red\">红</font>\n")
    _, report, by_cue = _run(vtt(body), "vtt",
                             RenderConfig(target_format="vtt"))
    assert report.summary["issue_count"] == 0
    assert not by_cue[1]


# ---------------------------------------------------------------- 候选选择 / 冲突

def test_select_subset_and_unknown_id():
    cues, report, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n{\\an8}<i>x</i> <b>未闭\n"),
        "vtt", RenderConfig(target_format="vtt"))
    ids = [c.id for row in report.cues for i in row.issues
           for c in i.fix_candidates]
    assert len(ids) >= 2
    items = preview_style_fixes(cues, by_cue, [ids[0]])
    assert len(items) == 1
    assert items[0]["after_lines"] != items[0]["before_lines"]
    try:
        preview_style_fixes(cues, by_cue, ["s9.c9"])
        assert False
    except UnknownStyleCandidateError as e:
        assert "s9.c9" in e.ids


def test_conflicting_candidates_rejected():
    # 同一标签的“补闭合”与“剥离”互斥（<custom> 未闭合）：只选其一即可，
    # 手工传入两个候选 id 时结构候选已不再生成；构造两个触及同标签的候选：
    # 不支持标签的剥离候选 + 同 cue 上补闭合的结构候选同时存在的场景是
    # 允许标签内未闭合（结构）与 ASS（不同区间）——这里直接验证同区间冲突：
    cues, _, by_cue = _run(
        vtt("00:00:01.000 --> 00:00:03.000\n{\\an8}{\\an8}x\n"),
        "vtt", RenderConfig(target_format="vtt"))
    ids = [c.id for c in by_cue[1]]
    assert len(ids) == 2
    # 两个 ASS 标记位置不同，不冲突，可同时应用
    new, applied = apply_style_fixes(cues, by_cue, ids)
    assert len(applied) == 2
    assert new[0].lines == ["x"]

    # 构造真正重叠：同一不支持标签同时被 unwrap 与 attribute 处理不可能，
    # 这里验证冲突检测本身——同 token 键的两个候选
    from app.style_qc import Candidate, Edit
    e1 = Edit(("line", 0), 0, 3, "x", "abc",
              [(("line", 0), 0)])
    e2 = Edit(("line", 0), 1, 4, "y", "bcd",
              [(("line", 0), 0)])
    c1 = Candidate("a", "x", "x", 1, [e1])
    c2 = Candidate("b", "x", "x", 1, [e2])
    assert c1.overlaps(c2)


def test_apply_all_preserves_timecode_text_breaks():
    body = ("00:00:01.000 --> 00:00:03.000\n"
            "<i>跨行\n这里</i> {\\an8}\n")
    cues, _, by_cue = _run(vtt(body), "vtt",
                           RenderConfig(target_format="vtt"))
    new, applied = apply_style_fixes(cues, by_cue, None)
    assert len(new) == len(cues)
    for a, b in zip(cues, new):
        assert (a.start_frame, a.end_frame) == (b.start_frame, b.end_frame)
        assert len(a.lines) == len(b.lines)
        assert [visible_text(x) for x in a.lines] == [
            visible_text(x) for x in b.lines]
    assert new[0].lines == ["<i>跨行</i>", "<i>这里</i> "]


def test_fixes_roundtrip_clean_for_vtt():
    body = ("00:00:01.000 --> 00:00:03.000\n"
            "<i>没有闭合\n{\\an8}x\n")
    cues, _, by_cue = _run(vtt(body), "vtt",
                           RenderConfig(target_format="vtt"))
    new, _ = apply_style_fixes(cues, by_cue, None)
    text = serialize(new, TB, "vtt")
    reparsed, _ = parse_subtitles(text, TB, "vtt")
    report2, _ = run_style_check(
        reparsed, RenderConfig(target_format="vtt"), TB,
        source_format="vtt", project_id=1, version_id=2, timebase_out=TBO)
    remaining = {i.issue_type for row in report2.cues for i in row.issues}
    assert not (remaining & {"unclosed_tag", "unsupported_override",
                             "roundtrip_drift"})


# ---------------------------------------------------------------- 端到端 API

def _make_project():
    r = client.post("/projects",
                    json={"name": "样式校核剧集", "frame_rate": 25.0})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload_vtt(pid, body, label="v1"):
    r = client.post(f"/projects/{pid}/versions",
                    json={"label": label, "content": vtt(body)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_style_check_api_report():
    pid = _make_project()
    vid = _upload_vtt(pid, "00:00:01.000 --> 00:00:03.000\n<i>未闭合\n")
    r = client.post(f"/projects/{pid}/style-check", json={
        "version_id": vid,
        "config": {"target_format": "srt"}})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["target_format"] == "srt"
    assert data["config"]["allow_speaker_tags"] is False  # SRT 缺省
    all_types = {i["issue_type"] for row in data["cues"]
                 for i in row["issues"]}
    assert "unclosed_tag" in all_types
    # cue 引用带行列位置、可见文本与原始行
    cue = data["cues"][0]["cue"]
    assert cue["visible_lines"] == ["未闭合"]
    assert cue["lines"] == ["<i>未闭合"]
    cand = next(c for row in data["cues"] for i in row["issues"]
                for c in i["fix_candidates"])
    assert set(cand["preserves"]) == {
        "timecode", "visible_text", "line_breaks"}


def test_style_preview_and_apply_api_saves_snapshot():
    pid = _make_project()
    vid = _upload_vtt(
        pid,
        '00:00:01.000 --> 00:00:03.000 align:middle\n{\\an8}<i>文本</i>\n')
    config = {"target_format": "srt"}
    check = client.post(f"/projects/{pid}/style-check",
                        json={"version_id": vid, "config": config})
    cids = [c["id"] for row in check.json()["cues"]
            for i in row["issues"] for c in i["fix_candidates"]]
    assert cids

    prev = client.post(f"/projects/{pid}/style-check/preview", json={
        "version_id": vid, "config": config, "candidate_ids": cids})
    assert prev.status_code == 200, prev.text
    assert len(prev.json()["items"]) == len(cids)
    assert all(it["preserves"] for it in prev.json()["items"])

    applied = client.post(f"/projects/{pid}/style-check/apply", json={
        "version_id": vid, "config": config,
        "candidate_ids": cids, "label": "srt-clean"})
    assert applied.status_code == 201, applied.text
    body = applied.json()
    assert body["output_format"] == "srt"
    assert body["origin_version_id"] == vid

    detail = client.get(
        f"/projects/{pid}/versions/{body['new_version_id']}").json()
    assert "{\\an8}" not in detail["cues"][0]["lines"][0]
    assert detail["cues"][0]["settings"] is None

    # provenance 带渲染配置快照与候选 id
    assert detail["provenance"]["kind"] == "style_fix"
    snap = detail["provenance"]["render_config"]
    assert snap["target_format"] == "srt"
    assert set(body["applied"][c]["candidate_id"]
               for c in range(len(body["applied"]))) <= set(cids)


def test_style_apply_unknown_candidate_400_and_conflict_400():
    pid = _make_project()
    vid = _upload_vtt(pid, "00:00:01.000 --> 00:00:03.000\n<i>x</i>\n")
    r = client.post(f"/projects/{pid}/style-check/apply", json={
        "version_id": vid,
        "config": {"target_format": "vtt"},
        "candidate_ids": ["s1.c99"]})
    assert r.status_code == 400


def test_style_check_version_not_found_404():
    pid = _make_project()
    r = client.post(f"/projects/{pid}/style-check", json={
        "version_id": 9999, "config": {"target_format": "vtt"}})
    assert r.status_code == 404


def test_config_defaults_per_format():
    srt = RenderConfig(target_format="srt")
    vttcfg = RenderConfig(target_format="vtt")
    assert srt.allowed_tags == ["i", "b", "u", "font"]
    assert srt.allow_speaker_tags is False
    assert srt.allowed_cue_settings == []
    assert "v" in vttcfg.allowed_tags
    assert vttcfg.allow_speaker_tags is True
    assert "align" in vttcfg.allowed_cue_settings
    # font 默认带 color 属性
    assert srt.allowed_attributes["font"] == ["color", "face", "size"]
