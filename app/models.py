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


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    format: Mapped[str] = mapped_column(String(20))  # srt / vtt / json
    content: Mapped[str] = mapped_column(Text)       # 字幕全文（原稿或修复稿）
    cues: Mapped[list] = mapped_column(JSON)         # 解析后的 cue 快照（帧号）
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    project: Mapped[Project] = relationship(back_populates="versions")
