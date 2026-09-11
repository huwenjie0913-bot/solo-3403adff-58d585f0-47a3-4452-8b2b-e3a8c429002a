"""SQLAlchemy 引擎与会话。数据库地址可用环境变量 SUBTITLE_QC_DB 覆盖。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("SUBTITLE_QC_DB", "sqlite:///./subtitle_qc.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 旧表 -> (新列, DDL)
_ADDED_COLUMNS = {
    "projects": [
        ("rate_num", "INTEGER NOT NULL DEFAULT 25"),
        ("rate_den", "INTEGER NOT NULL DEFAULT 1"),
        ("drop_frame", "BOOLEAN NOT NULL DEFAULT 0"),
        ("start_timecode", "VARCHAR(13) NOT NULL DEFAULT '00:00:00:00'"),
    ],
    "versions": [
        ("origin_version_id", "INTEGER REFERENCES versions(id)"),
        ("provenance", "JSON"),
    ],
}


def _migrate_ddl(conn) -> set[int]:
    """补列；返回升级前就存在的项目 id（其 shot_cuts 仍为毫秒）。"""
    inspector = inspect(conn)
    legacy_ids: set[int] = set()
    tables = set(inspector.get_table_names())
    if "projects" in tables:
        existing = {c["name"] for c in inspector.get_columns("projects")}
        if "rate_num" not in existing:
            rows = conn.execute(text("SELECT id FROM projects")).fetchall()
            legacy_ids = {r[0] for r in rows}
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns:
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return legacy_ids


def _backfill(legacy_ids: set[int] | None = None) -> None:
    """把旧版毫秒数据（frame_rate 浮点 / shot_cuts 毫秒 / cues.start_ms）迁移到帧。"""
    from fractions import Fraction

    from . import models
    from .timecode import build_timebase

    legacy_ids = legacy_ids or set()
    with SessionLocal() as db:
        changed = False
        projects = db.query(models.Project).all()
        for p in projects:
            if p.id in legacy_ids or not getattr(p, "rate_num", None):
                # 旧项目：浮点帧率按十进制精确值处理（仅 NDF）
                tb = build_timebase(float(p.frame_rate or 25.0))
                p.rate_num, p.rate_den = tb.rate.numerator, tb.rate.denominator
                p.drop_frame = False
                p.start_timecode = tb.start_timecode
                if p.id in legacy_ids:
                    # 升级前的行：shot_cuts 存的是毫秒，需换算到帧
                    p.shot_cuts = sorted({tb.ms_to_frames(int(c))
                                          for c in (p.shot_cuts or [])})
                changed = True
            else:
                tb = build_timebase(Fraction(p.rate_num, p.rate_den),
                                    drop_frame=bool(p.drop_frame),
                                    start_timecode=p.start_timecode)
            for v in p.versions:
                new_cues = []
                cue_changed = False
                for d in v.cues or []:
                    if "start_frame" not in d:
                        d = {**d,
                             "start_frame": tb.ms_to_frames(int(d["start_ms"])),
                             "end_frame": tb.ms_to_frames(int(d["end_ms"]))}
                        cue_changed = True
                    new_cues.append(d)
                if cue_changed:
                    v.cues = new_cues
                    changed = True
        if changed:
            db.commit()


def init_db() -> None:
    # models 必须在 create_all 前导入以注册元数据
    from . import models  # noqa: F401

    with engine.begin() as conn:
        legacy_ids = _migrate_ddl(conn)
    Base.metadata.create_all(engine)
    _backfill(legacy_ids)
