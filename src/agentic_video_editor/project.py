from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .db import connect_db, migrate


CONFIG_NAME = "ave_project.json"
DB_NAME = "library.sqlite"

PROJECT_DIRS = (
    "media/proxies",
    "media/chunks",
    "media/thumbnails",
    "media/frame_strips",
    "analysis/runs",
    "analysis/gemini_cache",
    "analysis/transcripts",
    "vectors",
    "timelines",
    "renders",
    "directives",
    "logs",
)


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def db_path(self) -> Path:
        return self.root / DB_NAME


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def init_project(path: Path, *, name: str | None = None) -> Project:
    root = path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    for rel_path in PROJECT_DIRS:
        (root / rel_path).mkdir(parents=True, exist_ok=True)

    project = Project(root=root)
    created_at = utc_now()
    config = {
        "schema": "ave-project-v1",
        "schema_version": 1,
        "project_id": "default",
        "name": name or root.name,
        "root": str(root),
        "database": DB_NAME,
        "created_at": created_at,
        "updated_at": created_at,
        "ave_version": __version__,
    }

    if project.config_path.exists():
        existing = json.loads(project.config_path.read_text(encoding="utf-8"))
        config["created_at"] = existing.get("created_at", created_at)
        config["name"] = existing.get("name", config["name"])

    project.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    with connect_db(project.db_path) as conn:
        migrate(conn)
        _upsert_project(conn, project, config)

    return project


def load_project(path: Path) -> Project:
    root = path.expanduser().resolve()
    project = Project(root=root)
    if not project.config_path.exists():
        raise FileNotFoundError(f"{root} is not an AVE project; run `ave init {root}` first")
    if not project.db_path.exists():
        with connect_db(project.db_path) as conn:
            migrate(conn)
    return project


def _upsert_project(conn: sqlite3.Connection, project: Project, config: dict[str, str]) -> None:
    now = utc_now()
    conn.execute(
        """
        insert into projects
            (id, name, root_path, schema_version, config_json, created_at, updated_at)
        values (?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            name = excluded.name,
            root_path = excluded.root_path,
            schema_version = excluded.schema_version,
            config_json = excluded.config_json,
            updated_at = excluded.updated_at
        """,
        (
            "default",
            config["name"],
            str(project.root),
            config["schema_version"],
            json.dumps(config, sort_keys=True),
            config.get("created_at", now),
            now,
        ),
    )
