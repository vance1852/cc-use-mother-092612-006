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
CREATE TABLE IF NOT EXISTS quiz_banks (
    bank_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_bank_versions (
    version_id TEXT PRIMARY KEY,
    bank_id TEXT NOT NULL REFERENCES quiz_banks(bank_id),
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
    content_hash TEXT,
    frozen_by TEXT REFERENCES actors(actor_id),
    frozen_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(bank_id, version_no)
);
CREATE TABLE IF NOT EXISTS quiz_levels (
    version_id TEXT NOT NULL REFERENCES quiz_bank_versions(version_id),
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL,
    prerequisites_json TEXT NOT NULL,
    PRIMARY KEY(version_id, code)
);
CREATE TABLE IF NOT EXISTS quiz_questions (
    version_id TEXT NOT NULL REFERENCES quiz_bank_versions(version_id),
    code TEXT NOT NULL,
    level_code TEXT NOT NULL,
    position INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    options_json TEXT NOT NULL,
    answer_key_json TEXT NOT NULL,
    knowledge_source TEXT NOT NULL,
    min_age INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(version_id, code)
);
CREATE TABLE IF NOT EXISTS quiz_sessions (
    session_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    bank_id TEXT NOT NULL REFERENCES quiz_banks(bank_id),
    bank_version_id TEXT NOT NULL REFERENCES quiz_bank_versions(version_id),
    family_alias TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'finalized')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS quiz_session_members (
    session_id TEXT NOT NULL REFERENCES quiz_sessions(session_id),
    member_alias TEXT NOT NULL,
    is_child INTEGER NOT NULL CHECK(is_child IN (0, 1)),
    age INTEGER NOT NULL CHECK(age >= 0),
    share_consent INTEGER NOT NULL CHECK(share_consent IN (0, 1)),
    PRIMARY KEY(session_id, member_alias)
);
CREATE TABLE IF NOT EXISTS quiz_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES quiz_sessions(session_id),
    client_seq INTEGER NOT NULL CHECK(client_seq >= 1),
    event_kind TEXT NOT NULL CHECK(event_kind IN ('answer', 'hint', 'skip')),
    question_code TEXT NOT NULL,
    member_alias TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('accepted', 'conflicted', 'variant_discarded')),
    chosen_by_resolution INTEGER NOT NULL DEFAULT 0 CHECK(chosen_by_resolution IN (0, 1)),
    correctness INTEGER CHECK(correctness IS NULL OR correctness IN (0, 1)),
    client_occurred_at TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiz_events_session_seq ON quiz_events(session_id, client_seq);
CREATE TABLE IF NOT EXISTS quiz_event_conflicts (
    conflict_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES quiz_sessions(session_id),
    client_seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    chosen_event_id TEXT REFERENCES quiz_events(event_id),
    resolution_note TEXT,
    resolved_by TEXT REFERENCES actors(actor_id),
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, client_seq)
);
CREATE TABLE IF NOT EXISTS quiz_settlements (
    session_id TEXT PRIMARY KEY REFERENCES quiz_sessions(session_id),
    score INTEGER NOT NULL,
    correct_questions_json TEXT NOT NULL,
    completed_levels_json TEXT NOT NULL,
    effective_event_count INTEGER NOT NULL,
    effective_hash TEXT NOT NULL,
    settled_by TEXT NOT NULL REFERENCES actors(actor_id),
    settled_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。

    文件数据库为每个线程提供独立连接，使并发写入在 ``BEGIN IMMEDIATE``
    下由 SQLite 串行化；内存数据库只在单线程场景使用。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._connection = self._connect()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None,
                                     check_same_thread=(self.path == ":memory:"))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        """返回当前线程的连接，必要时惰性创建。"""

        if self.path == ":memory:":
            return self._connection
        connection = getattr(self._local, "connection", None)
        if connection is None:
            with self._init_lock:
                connection = self._connect()
            self._local.connection = connection
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        connection = self.connection
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def close(self) -> None:
        """关闭当前线程的底层连接。"""

        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None
        self._connection.close()
