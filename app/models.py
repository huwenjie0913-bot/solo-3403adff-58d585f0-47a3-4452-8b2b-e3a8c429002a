"""ORM 模型：规则模板、项目、字幕版本。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, String, Text
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
    frame_rate: Mapped[float] = mapped_column(default=25.0)
    shot_cuts: Mapped[list] = mapped_column(JSON, default=list)
    rules: Mapped[dict] = mapped_column(JSON)  # 创建时快照（内联规则 > 模板 > 默认）
    rule_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("rule_templates.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    versions: Mapped[list["Version"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="Version.id")


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    format: Mapped[str] = mapped_column(String(10))  # srt / vtt
    content: Mapped[str] = mapped_column(Text)       # 字幕全文（原稿或修复稿）
    cues: Mapped[list] = mapped_column(JSON)         # 解析后的 cue 快照
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    project: Mapped[Project] = relationship(back_populates="versions")
