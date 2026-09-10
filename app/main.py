"""字幕断行与时间轴校正 REST API。

全部处理在本地完成，不依赖外部模型或服务。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .autofix import auto_fix
from .database import get_db, init_db
from .diffing import diff_cues
from .models import Project, RuleTemplate, Version
from .parsing import Cue, parse_shot_cuts, parse_subtitles, serialize
from .qc import run_qc, summarize
from .schemas import (
    AutoFixRequest,
    AutoFixResponse,
    DiffResponse,
    ProjectCreate,
    ProjectOut,
    QCReport,
    RuleTemplateCreate,
    RuleTemplateOut,
    RuleTemplateUpdate,
    Rules,
    VersionCreate,
    VersionDetail,
    VersionOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


init_db()  # 导入即建表（幂等），保证直接运行时数据库可用


app = FastAPI(
    title="字幕断行与时间轴校正 API",
    description="供字幕制作团队校正影视字幕断行和时间轴的本地服务",
    version="1.0.0",
    lifespan=lifespan,
)


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


def _project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id, name=p.name, frame_rate=p.frame_rate, shot_cuts=p.shot_cuts,
        rules=Rules(**p.rules), rule_template_id=p.rule_template_id,
        version_count=len(p.versions), created_at=p.created_at,
    )


def _version_out(v: Version) -> VersionOut:
    return VersionOut(
        id=v.id, project_id=v.project_id, label=v.label, format=v.format,
        cue_count=len(v.cues), created_at=v.created_at,
    )


def _cues_of(v: Version) -> list[Cue]:
    return [Cue.from_dict(d) for d in v.cues]


def _build_report(project: Project, version: Version) -> QCReport:
    cues = _cues_of(version)
    rules = Rules(**project.rules)
    issues = run_qc(cues, rules, project.shot_cuts, project.frame_rate)
    return QCReport(
        project_id=project.id, version_id=version.id,
        generated_at=datetime.now(timezone.utc),
        rules=rules, frame_rate=project.frame_rate, shot_cuts=project.shot_cuts,
        summary={"cue_count": len(cues), **summarize(issues)}, issues=issues,
    )


# ---------------------------------------------------------------- 基本信息

@app.get("/")
def root():
    return {
        "service": "字幕断行与时间轴校正 API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": [
            "/rule-templates", "/projects", "/projects/{id}/versions",
            "/projects/{id}/versions/{vid}/qc",
            "/projects/{id}/versions/{vid}/autofix",
            "/projects/{id}/versions/{vid}/export", "/projects/{id}/diff",
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
    try:
        cuts = parse_shot_cuts(body.shot_cuts)
    except ValueError as e:
        raise HTTPException(400, f"镜头切点解析失败: {e}")
    p = Project(name=body.name, frame_rate=body.frame_rate, shot_cuts=cuts,
                rules=rules.model_dump(), rule_template_id=body.rule_template_id)
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
    _get_project(db, project_id)
    try:
        cues, fmt = parse_subtitles(body.content, body.format)
    except ValueError as e:
        raise HTTPException(400, f"字幕解析失败: {e}")
    v = Version(project_id=project_id, label=body.label, format=fmt,
                content=body.content, cues=[c.to_dict() for c in cues])
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
    v = _get_version(db, project_id, version_id)
    return VersionDetail(**_version_out(v).model_dump(), cues=v.cues)


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
    cues = _cues_of(v)
    rules = Rules(**p.rules)
    new_cues, applied, conflicts = auto_fix(cues, rules, p.shot_cuts, p.frame_rate)
    label = (body.label if body and body.label else f"{v.label}-autofix")
    nv = Version(project_id=project_id, label=label, format=v.format,
                 content=serialize(new_cues, v.format),
                 cues=[c.to_dict() for c in new_cues])
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


# ---------------------------------------------------------------- 版本比较

@app.get("/projects/{project_id}/diff", response_model=DiffResponse)
def diff(project_id: int, from_version: int = Query(...), to_version: int = Query(...),
         db: Session = Depends(get_db)):
    _get_project(db, project_id)
    v1 = _get_version(db, project_id, from_version)
    v2 = _get_version(db, project_id, to_version)
    result = diff_cues(_cues_of(v1), _cues_of(v2))
    return DiffResponse(project_id=project_id, from_version=from_version,
                        to_version=to_version, **result)


# ---------------------------------------------------------------- 导出

@app.get("/projects/{project_id}/versions/{version_id}/export")
def export(project_id: int, version_id: int,
           format: str = Query(..., pattern="^(srt|vtt|report)$"),
           db: Session = Depends(get_db)):
    """导出修正后的 SRT / WebVTT 字幕或 JSON 质检报告。"""
    p = _get_project(db, project_id)
    v = _get_version(db, project_id, version_id)
    if format == "report":
        return _build_report(p, v)
    text = serialize(_cues_of(v), format)
    media_type = "application/x-subrip" if format == "srt" else "text/vtt"
    filename = f"project{project_id}-v{version_id}.{format}"
    return PlainTextResponse(
        text, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
