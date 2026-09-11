"""字幕断行与时间轴校正 REST API（SMPTE 时间码版）。

全部处理在本地完成，不依赖任何外部模型或服务。时间轴以精确帧号为真值，
毫秒/SMPTE 时间码/帧号三种表示经项目时间基（有理数帧率 + NDF/DF +
起始时间码）互转，往返无浮点舍入漂移。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from .align import (
    AlignOptions,
    CandidateConflictError,
    UnknownCandidateError,
    apply_alignment,
    mapping_out,
    run_alignment,
    summarize_alignment,
    unmatched_out,
)
from .autofix import auto_fix
from .database import get_db, init_db
from .diffing import diff_cues
from .models import Project, RuleTemplate, TerminologyRule, Version
from .parsing import Cue, parse_shot_cuts, parse_subtitles, serialize
from .qc import run_qc, summarize
from .schemas import (
    AlignApplyRequest,
    AlignApplyResponse,
    AlignReport,
    AlignRequest,
    AlignThresholds,
    AutoFixRequest,
    AutoFixResponse,
    ConvertedPosition,
    ConvertRequest,
    CueOut,
    DiffResponse,
    FrameCueOut,
    FrameExport,
    ProjectCreate,
    ProjectOut,
    QCReport,
    RuleTemplateCreate,
    RuleTemplateOut,
    RuleTemplateUpdate,
    Rules,
    StandaloneConvertRequest,
    TerminologyApplyRequest,
    TerminologyApplyResponse,
    TerminologyCheckRequest,
    TerminologyPreviewRequest,
    TerminologyPreviewResponse,
    TerminologyReport,
    TerminologyRuleCreate,
    TerminologyRuleOut,
    TerminologyRuleUpdate,
    TimebaseOut,
    TimebaseSpec,
    VersionCreate,
    VersionDetail,
    VersionOut,
)
from .timecode import (
    Timebase,
    TimecodeError,
    build_timebase,
    format_smpte,
    resolve_frame_field,
)
from .terminology import (
    TermCandidateConflictError,
    TermCandidateStaleError,
    TermCandidateUnsafeError,
    UnknownTermCandidateError,
    apply_terminology,
    preview_terminology,
    run_terminology,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


init_db()  # 导入即建表（幂等），保证直接运行时数据库可用


app = FastAPI(
    title="字幕断行与时间轴校正 API（SMPTE 时间码）",
    description="供字幕制作团队校正影视字幕断行和时间轴的本地服务，支持 SMPTE 时间码与有理数帧运算",
    version="2.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 错误处理

@app.exception_handler(TimecodeError)
async def timecode_error_handler(request: Request, exc: TimecodeError):
    """时间码/帧位置错误统一 400：返回字段、原值和原因。"""
    return JSONResponse(
        status_code=400,
        content={"detail": {"errors": [
            {"field": exc.field, "value": exc.value, "reason": exc.reason}]}},
    )


def _position_errors_400(errors: list[dict]) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": {"errors": errors}})


class _PositionErrors(Exception):
    """多个位置字段错误聚合，一次性返回。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors


@app.exception_handler(_PositionErrors)
async def position_errors_handler(request: Request, exc: _PositionErrors):
    return _position_errors_400(exc.errors)


# ---------------------------------------------------------------- 工具

def _get_project(db: Session, project_id: int) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    return p


def _get_version(db: Session, project_id: int, version_id: int) -> Version:
    v = db.get(Version, version_id)
    if v is None or v.project_id != project_id:
        raise HTTPException(404, "字幕版本不存在")
    return v


def _project_tb(p: Project) -> Timebase:
    return build_timebase(
        Fraction(p.rate_num, p.rate_den),
        drop_frame=bool(p.drop_frame),
        start_timecode=p.start_timecode,
    )


def _tb_out(p: Project) -> TimebaseOut:
    tb = _project_tb(p)
    return TimebaseOut(
        fps=float(tb.rate), fps_label=tb.fps_label,
        rate_num=tb.rate.numerator, rate_den=tb.rate.denominator,
        drop_frame=tb.drop_frame, start_timecode=tb.start_timecode)


def _build_timebase(spec: TimebaseSpec | None, frame_rate: float | None) -> Timebase:
    if spec is not None:
        return build_timebase(spec.fps, drop_frame=spec.drop_frame,
                              start_timecode=spec.start_timecode)
    # 旧字段：浮点帧率按十进制精确值处理（NDF）
    return build_timebase(25.0 if frame_rate is None else frame_rate)


def _project_out(p: Project) -> ProjectOut:
    tb = _project_tb(p)
    return ProjectOut(
        id=p.id, name=p.name, frame_rate=float(tb.rate), timebase=_tb_out(p),
        shot_cuts=list(p.shot_cuts or []),
        shot_cuts_ms=[tb.frames_to_ms(c) for c in (p.shot_cuts or [])],
        rules=Rules(**p.rules), rule_template_id=p.rule_template_id,
        version_count=len(p.versions), created_at=p.created_at,
    )


def _version_out(v: Version) -> VersionOut:
    return VersionOut(
        id=v.id, project_id=v.project_id, label=v.label, format=v.format,
        cue_count=len(v.cues), origin_version_id=v.origin_version_id,
        provenance=v.provenance, created_at=v.created_at,
    )


def _cues_of(v: Version, tb: Timebase) -> list[Cue]:
    return [Cue.from_dict(d, tb) for d in v.cues]


def _cue_out(c: Cue, tb: Timebase) -> CueOut:
    return CueOut(
        index=c.index, start_frame=c.start_frame, end_frame=c.end_frame,
        start_ms=tb.frames_to_ms(c.start_frame), end_ms=tb.frames_to_ms(c.end_frame),
        start_tc=format_smpte(tb, c.start_frame), end_tc=format_smpte(tb, c.end_frame),
        lines=c.lines, identifier=c.identifier, settings=c.settings,
    )


def _build_report(project: Project, version: Version) -> QCReport:
    tb = _project_tb(project)
    cues = _cues_of(version, tb)
    rules = Rules(**project.rules)
    issues = run_qc(cues, rules, list(project.shot_cuts or []), tb)
    return QCReport(
        project_id=project.id, version_id=version.id,
        generated_at=datetime.now(timezone.utc),
        rules=rules, timebase=_tb_out(project),
        shot_cuts=list(project.shot_cuts or []),
        summary={"cue_count": len(cues), **summarize(issues)}, issues=issues,
    )


def _position_value(pos: Any) -> Any:
    return pos.model_dump(exclude_none=True) if hasattr(pos, "model_dump") else pos


def _structured_cues(cue_inputs: list, tb: Timebase) -> list[Cue]:
    """把结构化 cue 请求换算为帧；收集全部字段错误后一次性 400。"""
    errors: list[dict] = []
    cues: list[Cue] = []
    for i, ci in enumerate(cue_inputs):
        try:
            sf = resolve_frame_field(_position_value(ci.start), tb, f"cues[{i}].start")
        except TimecodeError as e:
            errors.append({"field": e.field, "value": e.value, "reason": e.reason})
            sf = None
        try:
            ef = resolve_frame_field(_position_value(ci.end), tb, f"cues[{i}].end")
        except TimecodeError as e:
            errors.append({"field": e.field, "value": e.value, "reason": e.reason})
            ef = None
        if sf is not None and ef is not None:
            if ef <= sf:
                errors.append({
                    "field": f"cues[{i}].end", "value": _position_value(ci.end),
                    "reason": (f"结束帧必须晚于开始帧：end_frame={ef} <= "
                               f"start_frame={sf}"),
                })
            else:
                lines = ci.lines if isinstance(ci.lines, list) else [ci.lines]
                cues.append(Cue(i + 1, sf, ef, lines, ci.identifier, ci.settings))
    if errors:
        raise _PositionErrors(errors)
    return cues


# ---------------------------------------------------------------- 基本信息

@app.get("/")
def root():
    return {
        "service": "字幕断行与时间轴校正 API（SMPTE 时间码）",
        "version": "2.1.0",
        "docs": "/docs",
        "endpoints": [
            "/rule-templates", "/projects", "/projects/{id}/versions",
            "/projects/{id}/versions/{vid}/qc",
            "/projects/{id}/versions/{vid}/autofix",
            "/projects/{id}/versions/{vid}/export", "/projects/{id}/diff",
            "/projects/{id}/align", "/projects/{id}/align/apply",
            "/projects/{id}/terminology",
            "/projects/{id}/terminology/check",
            "/projects/{id}/terminology/preview",
            "/projects/{id}/terminology/apply",
            "/projects/{id}/convert", "/timecode/convert",
        ],
    }


# ---------------------------------------------------------------- 规则模板

@app.post("/rule-templates", response_model=RuleTemplateOut, status_code=201)
def create_rule_template(body: RuleTemplateCreate, db: Session = Depends(get_db)):
    if db.query(RuleTemplate).filter_by(name=body.name).first():
        raise HTTPException(409, "同名规则模板已存在")
    t = RuleTemplate(name=body.name, rules=body.rules.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@app.get("/rule-templates", response_model=list[RuleTemplateOut])
def list_rule_templates(db: Session = Depends(get_db)):
    return db.query(RuleTemplate).order_by(RuleTemplate.id).all()


@app.get("/rule-templates/{template_id}", response_model=RuleTemplateOut)
def get_rule_template(template_id: int, db: Session = Depends(get_db)):
    t = db.get(RuleTemplate, template_id)
    if t is None:
        raise HTTPException(404, "规则模板不存在")
    return t


@app.put("/rule-templates/{template_id}", response_model=RuleTemplateOut)
def update_rule_template(template_id: int, body: RuleTemplateUpdate,
                         db: Session = Depends(get_db)):
    t = db.get(RuleTemplate, template_id)
    if t is None:
        raise HTTPException(404, "规则模板不存在")
    if body.name is not None and body.name != t.name:
        if db.query(RuleTemplate).filter_by(name=body.name).first():
            raise HTTPException(409, "同名规则模板已存在")
        t.name = body.name
    if body.rules is not None:
        t.rules = body.rules.model_dump()
    db.commit()
    db.refresh(t)
    return t


@app.delete("/rule-templates/{template_id}", status_code=204)
def delete_rule_template(template_id: int, db: Session = Depends(get_db)):
    t = db.get(RuleTemplate, template_id)
    if t is None:
        raise HTTPException(404, "规则模板不存在")
    # 项目保存的是规则快照，仅解除引用
    db.query(Project).filter_by(rule_template_id=template_id).update(
        {"rule_template_id": None})
    db.delete(t)
    db.commit()


# ---------------------------------------------------------------- 项目

@app.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    if body.rules is not None:
        rules = body.rules
    elif body.rule_template_id is not None:
        t = db.get(RuleTemplate, body.rule_template_id)
        if t is None:
            raise HTTPException(404, "规则模板不存在")
        rules = Rules(**t.rules)
    else:
        rules = Rules()
    tb = _build_timebase(body.timebase, body.frame_rate)  # TimecodeError -> 400
    cuts = parse_shot_cuts(body.shot_cuts, tb)
    p = Project(
        name=body.name, frame_rate=float(tb.rate),
        rate_num=tb.rate.numerator, rate_den=tb.rate.denominator,
        drop_frame=tb.drop_frame, start_timecode=tb.start_timecode,
        shot_cuts=cuts, rules=rules.model_dump(),
        rule_template_id=body.rule_template_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _project_out(p)


@app.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return [_project_out(p) for p in db.query(Project).order_by(Project.id).all()]


@app.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    return _project_out(_get_project(db, project_id))


@app.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)):
    p = _get_project(db, project_id)
    db.delete(p)
    db.commit()


# ---------------------------------------------------------------- 字幕版本

@app.post("/projects/{project_id}/versions", response_model=VersionOut, status_code=201)
def upload_version(project_id: int, body: VersionCreate, db: Session = Depends(get_db)):
    p = _get_project(db, project_id)
    tb = _project_tb(p)
    if body.content is not None:
        try:
            cues, fmt = parse_subtitles(body.content, tb, body.format)
        except TimecodeError:
            raise
        except ValueError as e:
            raise HTTPException(400, f"字幕解析失败: {e}")
        content = body.content
    else:
        cues = _structured_cues(body.cues, tb)
        fmt = "json"
        content = serialize(cues, tb, "srt")  # 规范化 SRT 表示（真值以 cue 帧号为准）
    v = Version(project_id=project_id, label=body.label, format=fmt,
                content=content, cues=[c.to_dict() for c in cues])
    db.add(v)
    db.commit()
    db.refresh(v)
    return _version_out(v)


@app.get("/projects/{project_id}/versions", response_model=list[VersionOut])
def list_versions(project_id: int, db: Session = Depends(get_db)):
    _get_project(db, project_id)
    return [_version_out(v) for v in
            db.query(Version).filter_by(project_id=project_id).order_by(Version.id)]


@app.get("/projects/{project_id}/versions/{version_id}", response_model=VersionDetail)
def get_version(project_id: int, version_id: int, db: Session = Depends(get_db)):
    p = _get_project(db, project_id)
    v = _get_version(db, project_id, version_id)
    tb = _project_tb(p)
    cues = [_cue_out(c, tb) for c in _cues_of(v, tb)]
    return VersionDetail(**_version_out(v).model_dump(), cues=cues)


# ---------------------------------------------------------------- 时间码换算

def _convert_items(tb: Timebase, items: list) -> list[ConvertedPosition]:
    errors: list[dict] = []
    out: list[ConvertedPosition | None] = []
    for i, item in enumerate(items):
        raw = _position_value(item)
        try:
            frame = resolve_frame_field(raw, tb, f"items[{i}]")
        except TimecodeError as e:
            errors.append({"field": e.field, "value": e.value, "reason": e.reason})
            out.append(None)
        else:
            exact = tb.frames_to_ms_exact(frame)
            out.append(ConvertedPosition(
                input=raw, frame=frame, ms=tb.frames_to_ms(frame),
                ms_exact=f"{exact.numerator}/{exact.denominator}",
                timecode=format_smpte(tb, frame)))
    if errors:
        raise _PositionErrors(errors)
    return out  # type: ignore[return-value]


@app.post("/projects/{project_id}/convert", response_model=list[ConvertedPosition])
def convert_in_project(project_id: int, body: ConvertRequest,
                       db: Session = Depends(get_db)):
    """按项目时间基换算：毫秒 / 帧号 / SMPTE 时间码 → 帧 + 毫秒 + 时间码。"""
    p = _get_project(db, project_id)
    return _convert_items(_project_tb(p), body.items)


@app.post("/timecode/convert", response_model=list[ConvertedPosition])
def convert_standalone(body: StandaloneConvertRequest):
    """无状态换算：请求体给出时间基（fps/drop_frame/start_timecode）与 items。

    用法：``{"fps": "30000/1001", "drop_frame": true,
    "start_timecode": "00:00:00;00", "items": ["00:00:01;00", 1000, "30f"]}``
    """
    tb = build_timebase(body.fps, drop_frame=body.drop_frame,
                        start_timecode=body.start_timecode)
    return _convert_items(tb, body.items)


# ---------------------------------------------------------------- 质检

@app.post("/projects/{project_id}/versions/{version_id}/qc", response_model=QCReport)
def qc(project_id: int, version_id: int, db: Session = Depends(get_db)):
    """对指定版本运行质检，返回每项问题的原因与修正候选。"""
    p = _get_project(db, project_id)
    v = _get_version(db, project_id, version_id)
    return _build_report(p, v)


# ---------------------------------------------------------------- 自动修复

@app.post("/projects/{project_id}/versions/{version_id}/autofix",
          response_model=AutoFixResponse, status_code=201)
def autofix(project_id: int, version_id: int, body: AutoFixRequest | None = None,
            db: Session = Depends(get_db)):
    """自动修复并保存为新版本；冲突项保留原稿并在响应中返回。"""
    p = _get_project(db, project_id)
    v = _get_version(db, project_id, version_id)
    tb = _project_tb(p)
    cues = _cues_of(v, tb)
    rules = Rules(**p.rules)
    new_cues, applied, conflicts = auto_fix(cues, rules, list(p.shot_cuts or []), tb)
    label = (body.label if body and body.label else f"{v.label}-autofix")
    src_fmt = v.format if v.format in ("srt", "vtt") else "srt"
    nv = Version(project_id=project_id, label=label, format=v.format,
                 content=serialize(new_cues, tb, src_fmt),
                 cues=[c.to_dict() for c in new_cues],
                 origin_version_id=v.id,
                 provenance={"kind": "autofix"})
    db.add(nv)
    db.commit()
    db.refresh(nv)
    return AutoFixResponse(
        new_version_id=nv.id, label=label, applied=applied, conflicts=conflicts,
        summary={
            "cue_count_before": len(cues),
            "cue_count_after": len(new_cues),
            "fixed_cues": len(applied),
            "conflict_cues": len(conflicts),
        },
    )


# ---------------------------------------------------------------- 双语对齐

def _align_versions(db: Session, project_id: int,
                    source_id: int, target_id: int) -> tuple[Version, Version]:
    if source_id == target_id:
        raise HTTPException(400, "源语言版本与译文版本不能相同")
    return (_get_version(db, project_id, source_id),
            _get_version(db, project_id, target_id))


def _align_options(body, tb: Timebase) -> AlignOptions:
    return AlignOptions.from_ms(body.min_overlap_ms, body.min_overlap_ratio,
                                body.speaker_check, body.candidate_strategy, tb)


def _thresholds_out(body, opts: AlignOptions) -> AlignThresholds:
    return AlignThresholds(
        min_overlap_ms=body.min_overlap_ms,
        min_overlap_frames=opts.min_overlap_frames,
        min_overlap_ratio=body.min_overlap_ratio,
        speaker_check=body.speaker_check,
        candidate_strategy=body.candidate_strategy)


@app.post("/projects/{project_id}/align", response_model=AlignReport)
def align(project_id: int, body: AlignRequest, db: Session = Depends(get_db)):
    """把两个版本指定为源语言与译文，生成 cue 映射、同步诊断与修复候选。

    按时间区间重叠及相邻顺序生成一对一/一对多/多对一映射；对未匹配 cue、
    重叠不足、译文漏条、顺序倒置和说话人标签不一致返回字段化诊断；修复
    候选以源 cue 边界为依据（拆分/合并/时间调整），不改写文本。
    """
    p = _get_project(db, project_id)
    sv, tv = _align_versions(db, project_id,
                             body.source_version_id, body.target_version_id)
    tb = _project_tb(p)
    opts = _align_options(body, tb)
    result = run_alignment(_cues_of(sv, tb), _cues_of(tv, tb), opts, tb)
    return AlignReport(
        project_id=project_id, source_version_id=sv.id, target_version_id=tv.id,
        generated_at=datetime.now(timezone.utc), timebase=_tb_out(p),
        thresholds=_thresholds_out(body, opts),
        summary=summarize_alignment(result),
        mappings=[mapping_out(m, tb) for m in result.mappings],
        unmatched_source=[unmatched_out(c, "source", tb)
                          for c in result.unmatched_source],
        unmatched_target=[unmatched_out(c, "target", tb)
                          for c in result.unmatched_target],
    )


@app.post("/projects/{project_id}/align/apply",
          response_model=AlignApplyResponse, status_code=201)
def align_apply(project_id: int, body: AlignApplyRequest,
                db: Session = Depends(get_db)):
    """把选定候选应用到译文版本，保存为关联原版本的新字幕版本（原稿保留）。

    阈值参数需与生成候选时的 /align 请求一致（候选 id 按同一阈值重算）。
    新版本可继续使用质检、差异比较与 SRT/WebVTT/帧级导出。
    """
    p = _get_project(db, project_id)
    sv, tv = _align_versions(db, project_id,
                             body.source_version_id, body.target_version_id)
    tb = _project_tb(p)
    opts = _align_options(body, tb)
    src_cues = _cues_of(sv, tb)
    tgt_cues = _cues_of(tv, tb)
    result = run_alignment(src_cues, tgt_cues, opts, tb)
    selected = body.candidate_ids
    if selected is None:  # 缺省应用全部候选
        selected = [c.id for m in result.mappings for c in m.candidates]
    try:
        new_cues, applied = apply_alignment(result.mappings, selected,
                                            tgt_cues, tb)
    except UnknownCandidateError as e:
        raise HTTPException(400, f"未知候选 id: {', '.join(e.ids)}")
    except CandidateConflictError as e:
        raise HTTPException(400, str(e))
    label = body.label or f"{tv.label}-aligned"
    src_fmt = tv.format if tv.format in ("srt", "vtt") else "srt"
    nv = Version(
        project_id=project_id, label=label, format=tv.format,
        content=serialize(new_cues, tb, src_fmt),
        cues=[c.to_dict() for c in new_cues],
        origin_version_id=tv.id,
        provenance={
            "kind": "align",
            "source_version_id": sv.id,
            "target_version_id": tv.id,
            "options": {
                "min_overlap_ms": body.min_overlap_ms,
                "min_overlap_ratio": body.min_overlap_ratio,
                "speaker_check": body.speaker_check,
                "candidate_strategy": body.candidate_strategy,
            },
            "applied_candidate_ids": [a["candidate_id"] for a in applied],
        })
    db.add(nv)
    db.commit()
    db.refresh(nv)
    return AlignApplyResponse(
        new_version_id=nv.id, label=label, origin_version_id=tv.id,
        applied=applied,
        summary={
            "cue_count_before": len(tgt_cues),
            "cue_count_after": len(new_cues),
            "applied_count": len(applied),
        })


# ---------------------------------------------------------------- 版本比较

def _get_term_rule(db: Session, project_id: int, rule_id: int) -> TerminologyRule:
    t = db.get(TerminologyRule, rule_id)
    if t is None or t.project_id != project_id:
        raise HTTPException(404, "术语条目不存在")
    return t


def _term_rule_out(t: TerminologyRule) -> TerminologyRuleOut:
    return TerminologyRuleOut.model_validate(t)


# ------------------------------------------------ 术语表维护

@app.post("/projects/{project_id}/terminology",
          response_model=TerminologyRuleOut, status_code=201)
def create_term_rule(project_id: int, body: TerminologyRuleCreate,
                     db: Session = Depends(get_db)):
    """在项目术语表中新增条目（source_term 在项目内唯一，重名 409）。"""
    _get_project(db, project_id)
    if db.query(TerminologyRule).filter_by(
            project_id=project_id, source_term=body.source_term).first():
        raise HTTPException(409, f"源语词条已存在：{body.source_term}")
    t = TerminologyRule(
        project_id=project_id, source_term=body.source_term,
        preferred_translation=body.preferred_translation,
        acceptable_variants=body.acceptable_variants,
        forbidden_variants=body.forbidden_variants,
        case_sensitive=body.case_sensitive, whole_word=body.whole_word,
        severity=body.severity, note=body.note)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _term_rule_out(t)


@app.get("/projects/{project_id}/terminology",
         response_model=list[TerminologyRuleOut])
def list_term_rules(project_id: int, db: Session = Depends(get_db)):
    _get_project(db, project_id)
    return [_term_rule_out(t) for t in
            db.query(TerminologyRule).filter_by(project_id=project_id)
            .order_by(TerminologyRule.id).all()]


@app.get("/projects/{project_id}/terminology/{rule_id}",
         response_model=TerminologyRuleOut)
def get_term_rule(project_id: int, rule_id: int, db: Session = Depends(get_db)):
    return _term_rule_out(_get_term_rule(db, project_id, rule_id))


@app.put("/projects/{project_id}/terminology/{rule_id}",
         response_model=TerminologyRuleOut)
def update_term_rule(project_id: int, rule_id: int,
                     body: TerminologyRuleUpdate, db: Session = Depends(get_db)):
    t = _get_term_rule(db, project_id, rule_id)
    merged = TerminologyRuleCreate(
        source_term=body.source_term or t.source_term,
        preferred_translation=(body.preferred_translation
                               or t.preferred_translation),
        acceptable_variants=(body.acceptable_variants
                             if body.acceptable_variants is not None
                             else list(t.acceptable_variants or [])),
        forbidden_variants=(body.forbidden_variants
                            if body.forbidden_variants is not None
                            else list(t.forbidden_variants or [])),
        case_sensitive=(body.case_sensitive
                        if body.case_sensitive is not None else t.case_sensitive),
        whole_word=(body.whole_word if body.whole_word is not None else t.whole_word),
        severity=body.severity or t.severity,
        note=body.note if body.note is not None else t.note)
    if merged.source_term != t.source_term and db.query(TerminologyRule).filter(
            TerminologyRule.project_id == project_id,
            TerminologyRule.id != rule_id,
            TerminologyRule.source_term == merged.source_term).first():
        raise HTTPException(409, f"源语词条已存在：{merged.source_term}")
    t.source_term = merged.source_term
    t.preferred_translation = merged.preferred_translation
    t.acceptable_variants = merged.acceptable_variants
    t.forbidden_variants = merged.forbidden_variants
    t.case_sensitive = merged.case_sensitive
    t.whole_word = merged.whole_word
    t.severity = merged.severity
    t.note = merged.note
    db.commit()
    db.refresh(t)
    return _term_rule_out(t)


@app.delete("/projects/{project_id}/terminology/{rule_id}", status_code=204)
def delete_term_rule(project_id: int, rule_id: int, db: Session = Depends(get_db)):
    """删除术语条目；已生成报告/新版本保存的是快照，不受影响。"""
    t = _get_term_rule(db, project_id, rule_id)
    db.delete(t)
    db.commit()


# ------------------------------------------------ 术语一致性检查

def _load_term_rules(db: Session, project_id: int,
                     rule_ids: list[int] | None) -> list[TerminologyRuleOut]:
    q = db.query(TerminologyRule).filter_by(project_id=project_id)
    if rule_ids is not None:
        q = q.filter(TerminologyRule.id.in_(rule_ids))
    rows = q.order_by(TerminologyRule.id).all()
    if rule_ids is not None:
        missing = sorted(set(rule_ids) - {r.id for r in rows})
        if missing:
            raise HTTPException(404, f"术语条目不存在：{', '.join(map(str, missing))}")
    return [_term_rule_out(r) for r in rows]


def _run_terminology_report(db: Session, p: Project, *, source_id: int,
                            target_id: int, min_overlap_ms: int,
                            min_overlap_ratio: float,
                            rule_ids: list[int] | None
                            ) -> tuple[TerminologyReport, Version, Version, list[Cue]]:
    if source_id == target_id:
        raise HTTPException(400, "源语言版本与译文版本不能相同")
    sv, tv = (_get_version(db, p.id, source_id),
              _get_version(db, p.id, target_id))
    tb = _project_tb(p)
    opts = AlignOptions.from_ms(min_overlap_ms, min_overlap_ratio,
                                True, "source_boundaries", tb)
    rules = _load_term_rules(db, p.id, rule_ids)
    src_cues, tgt_cues = _cues_of(sv, tb), _cues_of(tv, tb)
    report, _ = run_terminology(
        src_cues, tgt_cues, rules, opts, tb,
        project_id=p.id, source_version_id=sv.id, target_version_id=tv.id,
        timebase_out=_tb_out(p), min_overlap_ms=min_overlap_ms)
    return report, sv, tv, tgt_cues


@app.post("/projects/{project_id}/terminology/check",
          response_model=TerminologyReport)
def terminology_check(project_id: int, body: TerminologyCheckRequest,
                      db: Session = Depends(get_db)):
    """复用时间重叠映射逐组核对术语：未译/不一致/禁用变体/大小写错误。

    返回规则快照、两侧 cue、实际命中片段、上下文与字段化原因；可修复
    问题携带不改时间码/标签/换行的精确替换候选。
    """
    p = _get_project(db, project_id)
    report, _, _, _ = _run_terminology_report(
        db, p, source_id=body.source_version_id,
        target_id=body.target_version_id,
        min_overlap_ms=body.min_overlap_ms,
        min_overlap_ratio=body.min_overlap_ratio, rule_ids=body.rule_ids)
    return report


@app.post("/projects/{project_id}/terminology/preview",
          response_model=TerminologyPreviewResponse)
def terminology_preview(project_id: int, body: TerminologyPreviewRequest,
                        db: Session = Depends(get_db)):
    """按候选 id 预览精确替换后的行（不保存、不改动原稿）。"""
    p = _get_project(db, project_id)
    report, _, _, tgt_cues = _run_terminology_report(
        db, p, source_id=body.source_version_id,
        target_id=body.target_version_id,
        min_overlap_ms=body.min_overlap_ms,
        min_overlap_ratio=body.min_overlap_ratio, rule_ids=body.rule_ids)
    try:
        items = preview_terminology(report, tgt_cues, body.candidate_ids)
    except UnknownTermCandidateError as e:
        raise HTTPException(400, str(e))
    except TermCandidateStaleError as e:
        raise HTTPException(409, str(e))
    except TermCandidateUnsafeError as e:
        raise HTTPException(400, str(e))
    return TerminologyPreviewResponse(items=items)


@app.post("/projects/{project_id}/terminology/apply",
          response_model=TerminologyApplyResponse, status_code=201)
def terminology_apply(project_id: int, body: TerminologyApplyRequest,
                      db: Session = Depends(get_db)):
    """把精确替换候选应用到译文版本，保存为带来源与术语规则快照的新版本。

    时间码、标签与换行原样保留；原稿不改动。新版本可继续质检、差异比较
    及 SRT/WebVTT/帧级导出。
    """
    p = _get_project(db, project_id)
    report, sv, tv, tgt_cues = _run_terminology_report(
        db, p, source_id=body.source_version_id,
        target_id=body.target_version_id,
        min_overlap_ms=body.min_overlap_ms,
        min_overlap_ratio=body.min_overlap_ratio, rule_ids=body.rule_ids)
    try:
        new_cues, applied = apply_terminology(report, tgt_cues, body.candidate_ids)
    except UnknownTermCandidateError as e:
        raise HTTPException(400, str(e))
    except TermCandidateConflictError as e:
        raise HTTPException(400, str(e))
    except TermCandidateUnsafeError as e:
        raise HTTPException(400, str(e))
    except TermCandidateStaleError as e:
        raise HTTPException(409, str(e))
    tb = _project_tb(p)
    label = body.label or f"{tv.label}-terms"
    src_fmt = tv.format if tv.format in ("srt", "vtt") else "srt"
    nv = Version(
        project_id=project_id, label=label, format=tv.format,
        content=serialize(new_cues, tb, src_fmt),
        cues=[c.to_dict() for c in new_cues],
        origin_version_id=tv.id,
        provenance={
            "kind": "terminology",
            "source_version_id": sv.id,
            "target_version_id": tv.id,
            "options": {
                "min_overlap_ms": body.min_overlap_ms,
                "min_overlap_ratio": body.min_overlap_ratio,
                "rule_ids": body.rule_ids,
            },
            "applied_candidate_ids": [a["candidate_id"] for a in applied],
            # 术语规则快照：之后修改/删除条目不影响本版本
            "rules_snapshot": [r.model_dump(mode="json") for r in report.rules],
        })
    db.add(nv)
    db.commit()
    db.refresh(nv)
    return TerminologyApplyResponse(
        new_version_id=nv.id, label=label, origin_version_id=tv.id,
        applied=applied,
        summary={
            "cue_count_before": len(tgt_cues),
            "cue_count_after": len(new_cues),
            "applied_count": len(applied),
        })


# ---------------------------------------------------------------- 版本比较

@app.get("/projects/{project_id}/diff", response_model=DiffResponse)
def diff(project_id: int, from_version: int = Query(...), to_version: int = Query(...),
         db: Session = Depends(get_db)):
    p = _get_project(db, project_id)
    tb = _project_tb(p)
    v1 = _get_version(db, project_id, from_version)
    v2 = _get_version(db, project_id, to_version)
    result = diff_cues(_cues_of(v1, tb), _cues_of(v2, tb), tb)
    return DiffResponse(project_id=project_id, from_version=from_version,
                        to_version=to_version, **result)


# ---------------------------------------------------------------- 导出

def _frame_export(p: Project, v: Version) -> FrameExport:
    tb = _project_tb(p)
    cues = _cues_of(v, tb)
    out_cues: list[FrameCueOut] = []
    for c in cues:
        s_exact, e_exact = tb.frames_to_ms_exact(c.start_frame), tb.frames_to_ms_exact(c.end_frame)
        out_cues.append(FrameCueOut(
            index=c.index,
            start_frame=c.start_frame, end_frame=c.end_frame,
            duration_frames=c.duration_frames,
            start_ms=tb.frames_to_ms(c.start_frame), end_ms=tb.frames_to_ms(c.end_frame),
            duration_ms=c.duration_ms(tb),
            start_ms_exact=f"{s_exact.numerator}/{s_exact.denominator}",
            end_ms_exact=f"{e_exact.numerator}/{e_exact.denominator}",
            start_tc=c.start_tc(tb), end_tc=c.end_tc(tb),
            duration_tc_frames=c.duration_frames,
            lines=c.lines, identifier=c.identifier, settings=c.settings))
    return FrameExport(
        project_id=p.id, version_id=v.id, label=v.label, format=v.format,
        timebase=_tb_out(p), cue_count=len(cues),
        shot_cuts=list(p.shot_cuts or []), cues=out_cues)


@app.get("/projects/{project_id}/versions/{version_id}/export")
def export(project_id: int, version_id: int,
           format: str = Query(..., pattern="^(srt|vtt|report|frames)$"),
           db: Session = Depends(get_db)):
    """导出 SRT / WebVTT / JSON 质检报告 / 帧级 JSON。"""
    p = _get_project(db, project_id)
    v = _get_version(db, project_id, version_id)
    tb = _project_tb(p)
    if format == "report":
        return _build_report(p, v)
    if format == "frames":
        return _frame_export(p, v)
    text = serialize(_cues_of(v, tb), tb, format)
    media_type = "application/x-subrip" if format == "srt" else "text/vtt"
    filename = f"project{project_id}-v{version_id}.{format}"
    return PlainTextResponse(
        text, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
