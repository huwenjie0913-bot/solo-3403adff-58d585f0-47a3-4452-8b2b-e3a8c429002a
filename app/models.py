"""ORM 模型：规则模板、项目、字幕版本。

项目时间基以精确帧率（``rate_num``/``rate_den``）、drop-frame 标志与
起始时间码保存；``frame_rate`` 列保留浮点近似值供旧接口展示。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuleTemplate(Base):
    __tablename__ = "rule_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    rules: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    frame_rate: Mapped[float] = mapped_column(default=25.0)  # 浮点近似（旧字段）
    rate_num: Mapped[int] = mapped_column(Integer, default=25)
    rate_den: Mapped[int] = mapped_column(Integer, default=1)
    drop_frame: Mapped[bool] = mapped_column(Boolean, default=False)
    start_timecode: Mapped[str] = mapped_column(String(13), default="00:00:00:00")
    shot_cuts: Mapped[list] = mapped_column(JSON, default=list)  # 时间线帧号列表
    rules: Mapped[dict] = mapped_column(JSON)  # 创建时快照（内联规则 > 模板 > 默认）
    rule_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("rule_templates.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    versions: Mapped[list["Version"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="Version.id")

    terminology_rules: Mapped[list["TerminologyRule"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="TerminologyRule.id")


class TerminologyRule(Base):
    """双语术语表条目（项目级）：源语词条 + 首选译法/可接受变体/禁用变体。

    应用时保存完整快照到质检结果与新版本 provenance，之后修改/删除条目
    不影响已生成的报告与新版本。
    """

    __tablename__ = "terminology_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), index=True)
    source_term: Mapped[str] = mapped_column(String(200))   # 源语词条
    preferred_translation: Mapped[str] = mapped_column(String(200))  # 首选译法
    acceptable_variants: Mapped[list] = mapped_column(JSON, default=list)   # 可接受变体
    forbidden_variants: Mapped[list] = mapped_column(JSON, default=list)    # 禁用变体
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)  # 大小写敏感
    whole_word: Mapped[bool] = mapped_column(Boolean, default=True)       # 整词匹配
    severity: Mapped[str] = mapped_column(String(10), default="error")    # error / warning
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=_utcnow, onupdate=_utcnow)

    project: Mapped[Project] = relationship(back_populates="terminology_rules")


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    format: Mapped[str] = mapped_column(String(20))  # srt / vtt / json
    content: Mapped[str] = mapped_column(Text)       # 字幕全文（原稿或修复稿）
    cues: Mapped[list] = mapped_column(JSON)         # 解析后的 cue 快照（帧号）
    origin_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id"), nullable=True)    # 派生来源版本（自动修复/对齐）
    provenance: Mapped[dict | None] = mapped_column(
        JSON, nullable=True)                         # 派生信息（来源类型、参数等）
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    project: Mapped[Project] = relationship(back_populates="versions")
