"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_teams (
    team_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_banks (
    bank_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    lineage_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE(lineage_id, version)
);
CREATE TABLE IF NOT EXISTS quiz_questions (
    bank_id TEXT NOT NULL REFERENCES quiz_banks(bank_id),
    question_id TEXT NOT NULL,
    level INTEGER NOT NULL CHECK(level >= 1),
    position INTEGER NOT NULL CHECK(position >= 1),
    prompt TEXT NOT NULL,
    answer_key TEXT NOT NULL,
    points INTEGER NOT NULL CHECK(points >= 0),
    min_age INTEGER NOT NULL CHECK(min_age >= 0),
    knowledge_source TEXT NOT NULL,
    hints_json TEXT NOT NULL,
    PRIMARY KEY(bank_id, question_id),
    UNIQUE(bank_id, level, position)
);
CREATE TABLE IF NOT EXISTS quiz_participants (
    participant_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    alias TEXT NOT NULL,
    age INTEGER NOT NULL CHECK(age >= 0),
    consent_public INTEGER NOT NULL CHECK(consent_public IN (0, 1)),
    team_id TEXT REFERENCES quiz_teams(team_id),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, alias)
);
CREATE TABLE IF NOT EXISTS quiz_sessions (
    session_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES quiz_participants(participant_id),
    bank_id TEXT NOT NULL REFERENCES quiz_banks(bank_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'settled')),
    created_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE TABLE IF NOT EXISTS quiz_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES quiz_sessions(session_id),
    client_seq INTEGER NOT NULL CHECK(client_seq >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('answer', 'hint', 'skip')),
    question_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective', 'superseded')),
    received_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS quiz_events_effective_seq
    ON quiz_events(session_id, client_seq) WHERE status='effective';
CREATE TABLE IF NOT EXISTS quiz_conflicts (
    conflict_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES quiz_sessions(session_id),
    client_seq INTEGER NOT NULL,
    contenders_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, client_seq)
);
CREATE TABLE IF NOT EXISTS quiz_settlements (
    session_id TEXT PRIMARY KEY REFERENCES quiz_sessions(session_id),
    score INTEGER NOT NULL,
    adopted_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    settled_by TEXT NOT NULL REFERENCES actors(actor_id),
    settled_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._write_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；写事务按服务实例串行化。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
