"""药材知识闯关：冻结题库、独立会话、乱序补传、隐私视图与结算。

模块在基础层（权限、幂等、事务、审计链）之上实现文化展示区的闯关规则：

- 题库按版本管理，活动开始前冻结题目、知识来源与适用年龄并生成内容哈希；
  冻结版本不可改写，撤回题目只能通过新版本生效，且只影响之后开启的会话。
- 每个参与家庭使用化名开启一次独立会话，会话钉住当时的最新冻结版本。
- 现场终端断网补传的事件按客户端序号归并：同序号同内容直接沿用第一次处理
  结果，同序号不同内容全部保留为待解释冲突，绝不自动挑选其中一份。
- 关卡解锁只取决于会话钉住的题库版本和已经生效的前置完成事件；有效事件
  集合可以逐条列出并带有内容哈希，结算落库后不再改写。
- 儿童只能看到适龄提示；儿童明细与未同意公开的成员进度不会进入公开排行，
  家庭视图只共享成员明确同意公开的进度。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService
from .storage import Database

EVENT_KINDS = frozenset({"answer", "hint", "skip"})
VIEWS = frozenset({"operator", "family", "public"})


@dataclass(frozen=True)
class IngestResult:
    """描述一批补传事件的处理回执。"""

    batch_id: str
    session_id: str
    replayed: bool
    accepted: int
    conflicts: int
    rejected: int
    results: list[dict[str, Any]]


@dataclass(frozen=True)
class Settlement:
    """描述一次会话的最终结算结果。"""

    session_id: str
    score: int
    correct_questions: list[str]
    completed_levels: list[str]
    effective_event_count: int
    effective_hash: str
    settled_at: str


class QuizService(DomainService):
    """在基础服务之上提供闯关题库、会话、事件归并与结算能力。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)

    # ------------------------------------------------------------------
    # 题库与版本
    # ------------------------------------------------------------------

    def create_bank(self, *, request_id: str, actor_id: str, bank_id: str,
                    site_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "bank_id": bank_id, "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所下建题库")
            bank_id = self._identifier(bank_id, "bank_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO quiz_banks(bank_id,site_id,name,created_by,created_at) VALUES(?,?,?,?,?)",
                        (bank_id, site_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("题库编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="quiz.bank_created",
                             resource_type="quiz_bank", resource_id=bank_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "quiz_bank", bank_id, {"bank_id": bank_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_create_bank", payload=payload, create=create)

    def create_bank_version(self, *, request_id: str, actor_id: str, bank_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "bank_id": bank_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            bank = self._bank(connection, bank_id)
            self._check_site_scope(connection, actor, bank["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT MAX(version_no) AS max_no FROM quiz_bank_versions WHERE bank_id=?", (bank_id,)
                ).fetchone()
                version_no = (row["max_no"] or 0) + 1
                version_id = f"{bank_id}-v{version_no}"
                connection.execute(
                    "INSERT INTO quiz_bank_versions(version_id,bank_id,version_no,status,created_by,created_at) "
                    "VALUES(?,?,?,'draft',?,?)",
                    (version_id, bank_id, version_no, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="quiz.bank_version_created",
                             resource_type="quiz_bank_version", resource_id=version_id,
                             detail={"bank_id": bank_id, "version_no": version_no}, occurred_at=self._now())
                return "quiz_bank_version", version_id, {"version_id": version_id, "version_no": version_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_create_bank_version", payload=payload, create=create)

    def replace_version_content(self, *, request_id: str, actor_id: str, version_id: str,
                                levels: list[dict[str, Any]],
                                questions: list[dict[str, Any]]) -> WriteReceipt:
        """整体替换草稿版本的关卡与题目；冻结版本拒绝任何改写。"""

        payload = {"actor_id": actor_id, "version_id": version_id, "levels": levels, "questions": questions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = self._version(connection, version_id)
            bank = self._bank(connection, version["bank_id"])
            self._check_site_scope(connection, actor, bank["site_id"])
            if version["status"] != "draft":
                raise ConflictError("题库版本已冻结，不能改写题目、知识来源或适用年龄")
            normalized_levels = self._normalize_levels(levels)
            normalized_questions = self._normalize_questions(questions, normalized_levels)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("DELETE FROM quiz_levels WHERE version_id=?", (version_id,))
                connection.execute("DELETE FROM quiz_questions WHERE version_id=?", (version_id,))
                for level in normalized_levels:
                    connection.execute(
                        "INSERT INTO quiz_levels(version_id,code,title,position,prerequisites_json) VALUES(?,?,?,?,?)",
                        (version_id, level["code"], level["title"], level["position"],
                         canonical_json(level["prerequisites"])),
                    )
                for question in normalized_questions:
                    connection.execute(
                        "INSERT INTO quiz_questions(version_id,code,level_code,position,prompt,options_json,"
                        "answer_key_json,knowledge_source,min_age) VALUES(?,?,?,?,?,?,?,?,?)",
                        (version_id, question["code"], question["level_code"], question["position"],
                         question["prompt"], canonical_json(question["options"]),
                         canonical_json(question["answer_key"]), question["knowledge_source"], question["min_age"]),
                    )
                append_event(connection, actor_id=actor_id, action="quiz.bank_version_content_set",
                             resource_type="quiz_bank_version", resource_id=version_id,
                             detail={"levels": len(normalized_levels), "questions": len(normalized_questions)},
                             occurred_at=self._now())
                return "quiz_bank_version", version_id, {
                    "version_id": version_id,
                    "levels": len(normalized_levels),
                    "questions": len(normalized_questions),
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_replace_version_content", payload=payload, create=create)

    def freeze_bank_version(self, *, request_id: str, actor_id: str, version_id: str) -> WriteReceipt:
        """冻结题库版本：题目、知识来源与适用年龄自此不可改写。"""

        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = self._version(connection, version_id)
            bank = self._bank(connection, version["bank_id"])
            self._check_site_scope(connection, actor, bank["site_id"])
            if version["status"] == "frozen":
                raise ConflictError("题库版本已经冻结")
            snapshot = self._version_snapshot(connection, version_id)
            if not snapshot["levels"] or not snapshot["questions"]:
                raise ValidationError("冻结前必须至少配置一个关卡和一道题目")
            content_hash = digest(snapshot)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE quiz_bank_versions SET status='frozen', content_hash=?, frozen_by=?, frozen_at=? "
                    "WHERE version_id=?",
                    (content_hash, actor_id, self._now(), version_id),
                )
                append_event(connection, actor_id=actor_id, action="quiz.bank_version_frozen",
                             resource_type="quiz_bank_version", resource_id=version_id,
                             detail={"content_hash": content_hash,
                                     "levels": len(snapshot["levels"]),
                                     "questions": len(snapshot["questions"])},
                             occurred_at=self._now())
                return "quiz_bank_version", version_id, {"version_id": version_id, "content_hash": content_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_freeze_bank_version", payload=payload, create=create)

    def withdraw_question(self, *, request_id: str, actor_id: str, bank_id: str,
                          question_code: str) -> WriteReceipt:
        """撤回题目：生成一个不含该题的新冻结版本，只影响之后开启的会话。"""

        payload = {"actor_id": actor_id, "bank_id": bank_id, "question_code": question_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            bank = self._bank(connection, bank_id)
            self._check_site_scope(connection, actor, bank["site_id"])
            question_code = self._identifier(question_code, "question_code")
            source = self._latest_frozen_version(connection, bank_id)
            if source is None:
                raise NotFoundError("题库还没有冻结版本，无法撤回题目")
            snapshot = self._version_snapshot(connection, source["version_id"])
            remaining = [q for q in snapshot["questions"] if q["code"] != question_code]
            if len(remaining) == len(snapshot["questions"]):
                raise NotFoundError("当前冻结版本中不存在该题目")
            if not remaining:
                raise ValidationError("不能撤回题库中的最后一道题目")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT MAX(version_no) AS max_no FROM quiz_bank_versions WHERE bank_id=?", (bank_id,)
                ).fetchone()
                version_no = (row["max_no"] or 0) + 1
                version_id = f"{bank_id}-v{version_no}"
                connection.execute(
                    "INSERT INTO quiz_bank_versions(version_id,bank_id,version_no,status,created_by,created_at) "
                    "VALUES(?,?,?,'draft',?,?)",
                    (version_id, bank_id, version_no, actor_id, self._now()),
                )
                for level in snapshot["levels"]:
                    connection.execute(
                        "INSERT INTO quiz_levels(version_id,code,title,position,prerequisites_json) VALUES(?,?,?,?,?)",
                        (version_id, level["code"], level["title"], level["position"],
                         canonical_json(level["prerequisites"])),
                    )
                for question in remaining:
                    connection.execute(
                        "INSERT INTO quiz_questions(version_id,code,level_code,position,prompt,options_json,"
                        "answer_key_json,knowledge_source,min_age) VALUES(?,?,?,?,?,?,?,?,?)",
                        (version_id, question["code"], question["level_code"], question["position"],
                         question["prompt"], canonical_json(question["options"]),
                         canonical_json(question["answer_key"]), question["knowledge_source"], question["min_age"]),
                    )
                new_snapshot = self._version_snapshot(connection, version_id)
                content_hash = digest(new_snapshot)
                connection.execute(
                    "UPDATE quiz_bank_versions SET status='frozen', content_hash=?, frozen_by=?, frozen_at=? "
                    "WHERE version_id=?",
                    (content_hash, actor_id, self._now(), version_id),
                )
                append_event(connection, actor_id=actor_id, action="quiz.question_withdrawn",
                             resource_type="quiz_bank_version", resource_id=version_id,
                             detail={"bank_id": bank_id, "question_code": question_code,
                                     "source_version_id": source["version_id"],
                                     "content_hash": content_hash},
                             occurred_at=self._now())
                return "quiz_bank_version", version_id, {
                    "version_id": version_id,
                    "content_hash": content_hash,
                    "withdrawn_question": question_code,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_withdraw_question", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 会话与成员
    # ------------------------------------------------------------------

    def open_session(self, *, request_id: str, actor_id: str, session_id: str, site_id: str,
                     bank_id: str, family_alias: str,
                     members: list[dict[str, Any]]) -> WriteReceipt:
        """为一个家庭开启独立会话，并钉住当时的最新冻结题库版本。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "site_id": site_id,
                   "bank_id": bank_id, "family_alias": family_alias, "members": members}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能为其他组织的场所开启会话")
            session_id = self._identifier(session_id, "session_id")
            family_alias = self._text(family_alias, "family_alias", 80)
            normalized_members = self._normalize_members(members)
            bank = self._bank(connection, bank_id)
            if bank["site_id"] != site_id:
                raise ValidationError("题库不属于该场所")
            version = self._latest_frozen_version(connection, bank_id)
            if version is None:
                raise ConflictError("题库尚未冻结，活动开始前不能开启会话")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO quiz_sessions(session_id,site_id,bank_id,bank_version_id,family_alias,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (session_id, site_id, bank_id, version["version_id"], family_alias,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("会话编号已经存在") from exc
                for member in normalized_members:
                    connection.execute(
                        "INSERT INTO quiz_session_members(session_id,member_alias,is_child,age,share_consent) "
                        "VALUES(?,?,?,?,?)",
                        (session_id, member["member_alias"], int(member["is_child"]),
                         member["age"], int(member["share_consent"])),
                    )
                append_event(connection, actor_id=actor_id, action="quiz.session_opened",
                             resource_type="quiz_session", resource_id=session_id,
                             detail={"site_id": site_id, "bank_id": bank_id,
                                     "bank_version_id": version["version_id"],
                                     "family_alias": family_alias,
                                     "members": normalized_members},
                             occurred_at=self._now())
                return "quiz_session", session_id, {
                    "session_id": session_id,
                    "bank_version_id": version["version_id"],
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_open_session", payload=payload, create=create)

    def set_share_consent(self, *, request_id: str, actor_id: str, session_id: str,
                          member_alias: str, consent: bool) -> WriteReceipt:
        """记录成员是否明确同意公开进度；未同意的成员不进入公开排行。"""

        payload = {"actor_id": actor_id, "session_id": session_id,
                   "member_alias": member_alias, "consent": bool(consent)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session = self._session(connection, session_id)
            self._check_site_scope(connection, actor, session["site_id"])
            member = connection.execute(
                "SELECT * FROM quiz_session_members WHERE session_id=? AND member_alias=?",
                (session_id, member_alias),
            ).fetchone()
            if member is None:
                raise NotFoundError("会话成员不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE quiz_session_members SET share_consent=? WHERE session_id=? AND member_alias=?",
                    (int(bool(consent)), session_id, member_alias),
                )
                append_event(connection, actor_id=actor_id, action="quiz.share_consent_set",
                             resource_type="quiz_session", resource_id=session_id,
                             detail={"member_alias": member_alias, "consent": bool(consent)},
                             occurred_at=self._now())
                return "quiz_session", session_id, {"session_id": session_id,
                                                    "member_alias": member_alias,
                                                    "consent": bool(consent)}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_set_share_consent", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 事件归并（断网补传）
    # ------------------------------------------------------------------

    def ingest_events(self, *, request_id: str, actor_id: str, session_id: str,
                      batch_id: str, events: list[dict[str, Any]]) -> IngestResult:
        """按客户端序号归并一批补传事件。

        同序号同内容的重复补传沿用第一次处理结果；同序号不同内容的事件全部
        保留为待解释冲突，等待运营人员裁决，绝不自动选取其中一份。
        """

        payload = {"actor_id": actor_id, "session_id": session_id, "batch_id": batch_id, "events": events}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session = self._session(connection, session_id)
            self._check_site_scope(connection, actor, session["site_id"])
            batch_id = self._identifier(batch_id, "batch_id")
            if not isinstance(events, list) or not events:
                raise ValidationError("events 必须是非空数组")

            def create() -> tuple[str, str, dict[str, Any]]:
                results = self._ingest_batch(connection, session, events, actor_id)
                summary = {
                    "batch_id": batch_id,
                    "session_id": session_id,
                    "accepted": sum(1 for item in results if item["status"] == "accepted"),
                    "conflicts": sum(1 for item in results if item["status"] == "conflicted"),
                    "rejected": sum(1 for item in results if item["status"] == "rejected"),
                    "results": results,
                }
                append_event(connection, actor_id=actor_id, action="quiz.events_ingested",
                             resource_type="quiz_session", resource_id=session_id,
                             detail={"batch_id": batch_id, "accepted": summary["accepted"],
                                     "conflicts": summary["conflicts"], "rejected": summary["rejected"]},
                             occurred_at=self._now())
                return "quiz_batch", batch_id, summary

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="quiz_ingest_events", payload=payload, create=create)
            row = connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            summary = json.loads(row["response_json"])
            return IngestResult(batch_id=batch_id, session_id=session_id, replayed=receipt.replayed,
                                accepted=summary["accepted"], conflicts=summary["conflicts"],
                                rejected=summary["rejected"], results=summary["results"])

    def _ingest_batch(self, connection, session, events: list[dict[str, Any]],
                      actor_id: str) -> list[dict[str, Any]]:
        if session["status"] != "active":
            raise ConflictError("会话已经结算，不能再补传事件")
        snapshot = self._version_snapshot(connection, session["bank_version_id"])
        questions = {q["code"]: q for q in snapshot["questions"]}
        members = self._members(connection, session["session_id"])
        results: list[dict[str, Any]] = []
        seen_in_batch: set[int] = set()
        for raw in events:
            result = self._ingest_one(connection, session, raw, questions, members,
                                      seen_in_batch, actor_id)
            results.append(result)
        return results

    def _ingest_one(self, connection, session, raw: Any, questions: dict[str, dict[str, Any]],
                    members: dict[str, dict[str, Any]], seen_in_batch: set[int],
                    actor_id: str) -> dict[str, Any]:
        session_id = session["session_id"]
        try:
            if not isinstance(raw, dict):
                raise ValidationError("事件必须是对象")
            client_seq = raw.get("client_seq")
            if not isinstance(client_seq, int) or isinstance(client_seq, bool) or client_seq < 1:
                raise ValidationError("client_seq 必须是正整数")
            if client_seq in seen_in_batch:
                raise ValidationError("同一批次内 client_seq 重复")
            seen_in_batch.add(client_seq)
            event_kind = str(raw.get("event_kind", ""))
            if event_kind not in EVENT_KINDS:
                raise ValidationError("event_kind 不在允许范围内")
            question_code = str(raw.get("question_code", ""))
            question = questions.get(question_code)
            if question is None:
                raise ValidationError("题目不在会话钉住的题库版本中")
            member_alias = raw.get("member_alias")
            member = None
            if member_alias is not None:
                member = members.get(str(member_alias))
                if member is None:
                    raise ValidationError("成员不属于该会话")
                member_alias = str(member_alias)
            answer = raw.get("answer")
            if event_kind == "answer" and answer is None:
                raise ValidationError("answer 事件必须携带答案")
            if event_kind == "hint" and member is not None and member["is_child"] \
                    and member["age"] < question["min_age"]:
                return {"client_seq": client_seq, "status": "rejected",
                        "reason": "age_restricted",
                        "message": "儿童只能看到适龄提示，该提示暂不适用"}
            client_occurred_at = raw.get("client_occurred_at")
            if client_occurred_at is not None:
                client_occurred_at = str(client_occurred_at)
        except ValidationError as exc:
            seq = raw.get("client_seq") if isinstance(raw, dict) else None
            return {"client_seq": seq, "status": "rejected", "reason": "invalid", "message": str(exc)}

        event_payload = {"event_kind": event_kind, "question_code": question_code,
                         "member_alias": member_alias, "answer": answer,
                         "client_occurred_at": client_occurred_at}
        payload_hash = digest(event_payload)
        existing = connection.execute(
            "SELECT * FROM quiz_events WHERE session_id=? AND client_seq=? ORDER BY received_at, event_id",
            (session_id, client_seq),
        ).fetchall()
        conflict_row = connection.execute(
            "SELECT * FROM quiz_event_conflicts WHERE session_id=? AND client_seq=?",
            (session_id, client_seq),
        ).fetchone()

        if conflict_row is not None:
            # 已存在冲突：同内容变体沿用首次处理结果，新内容继续作为变体保留。
            for row in existing:
                if row["payload_hash"] == payload_hash:
                    return {"client_seq": client_seq, "status": "replayed",
                            "event_id": row["event_id"], "conflict_id": conflict_row["conflict_id"]}
            event_id = self._store_event(connection, session_id, client_seq, event_payload,
                                         payload_hash, "conflicted", None)
            return {"client_seq": client_seq, "status": "conflicted", "event_id": event_id,
                    "conflict_id": conflict_row["conflict_id"],
                    "message": "同一序号出现新的分叉内容，已保留待运营解释"}

        if existing:
            first = existing[0]
            if first["payload_hash"] == payload_hash:
                return {"client_seq": client_seq, "status": "replayed", "event_id": first["event_id"]}
            # 同序号不同内容：保留全部内容并登记冲突，不自动选取任何一份。
            connection.execute("UPDATE quiz_events SET state='conflicted' WHERE event_id=?",
                               (first["event_id"],))
            event_id = self._store_event(connection, session_id, client_seq, event_payload,
                                         payload_hash, "conflicted", None)
            conflict_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO quiz_event_conflicts(conflict_id,session_id,client_seq,status,created_at) "
                "VALUES(?,?,?,'open',?)",
                (conflict_id, session_id, client_seq, self._now()),
            )
            append_event(connection, actor_id=actor_id, action="quiz.event_conflict_recorded",
                         resource_type="quiz_session", resource_id=session_id,
                         detail={"conflict_id": conflict_id, "client_seq": client_seq,
                                 "event_ids": [row["event_id"] for row in existing] + [event_id]},
                         occurred_at=self._now())
            return {"client_seq": client_seq, "status": "conflicted", "event_id": event_id,
                    "conflict_id": conflict_id,
                    "message": "同一客户端序号出现分叉内容，已保留全部内容待运营解释"}

        correctness = None
        if event_kind == "answer":
            correctness = 1 if answer == questions[question_code]["answer_key"] else 0
        event_id = self._store_event(connection, session_id, client_seq, event_payload,
                                     payload_hash, "accepted", correctness)
        return {"client_seq": client_seq, "status": "accepted", "event_id": event_id,
                "correct": None if correctness is None else bool(correctness)}

    def _store_event(self, connection, session_id: str, client_seq: int,
                     event_payload: dict[str, Any], payload_hash: str,
                     state: str, correctness: int | None) -> str:
        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO quiz_events(event_id,session_id,client_seq,event_kind,question_code,member_alias,"
            "payload_json,payload_hash,state,chosen_by_resolution,correctness,client_occurred_at,received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (event_id, session_id, client_seq, event_payload["event_kind"],
             event_payload["question_code"], event_payload["member_alias"],
             canonical_json(event_payload), payload_hash, state, correctness,
             event_payload["client_occurred_at"], self._now()),
        )
        return event_id

    # ------------------------------------------------------------------
    # 冲突裁决
    # ------------------------------------------------------------------

    def resolve_conflict(self, *, request_id: str, actor_id: str, conflict_id: str,
                         chosen_event_id: str, note: str) -> WriteReceipt:
        """由运营人员解释同序号分叉，明确采用哪一份，另一份标记为弃用变体。"""

        payload = {"actor_id": actor_id, "conflict_id": conflict_id,
                   "chosen_event_id": chosen_event_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            conflict = connection.execute(
                "SELECT * FROM quiz_event_conflicts WHERE conflict_id=?", (conflict_id,)
            ).fetchone()
            if conflict is None:
                raise NotFoundError("冲突记录不存在")
            session = self._session(connection, conflict["session_id"])
            self._check_site_scope(connection, actor, session["site_id"])
            note = self._text(note, "note", 400)
            variants = connection.execute(
                "SELECT * FROM quiz_events WHERE session_id=? AND client_seq=? ORDER BY received_at, event_id",
                (conflict["session_id"], conflict["client_seq"]),
            ).fetchall()
            variant_ids = {row["event_id"] for row in variants}
            if chosen_event_id not in variant_ids:
                raise ValidationError("chosen_event_id 必须是该序号下已保留的其中一份")
            if conflict["status"] == "resolved":
                if conflict["chosen_event_id"] != chosen_event_id:
                    raise ConflictError("冲突已按另一份内容裁决，不能改判")
                raise ConflictError("冲突已经裁决，不能重复处理")
            if session["status"] != "active":
                raise ConflictError("会话已经结算，不能再裁决冲突")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE quiz_event_conflicts SET status='resolved', chosen_event_id=?, resolution_note=?,"
                    " resolved_by=?, resolved_at=? WHERE conflict_id=?",
                    (chosen_event_id, note, actor_id, self._now(), conflict_id),
                )
                for row in variants:
                    if row["event_id"] == chosen_event_id:
                        correctness = None
                        if row["event_kind"] == "answer":
                            answer = json.loads(row["payload_json"])["answer"]
                            question = connection.execute(
                                "SELECT answer_key_json FROM quiz_questions WHERE version_id=? AND code=?",
                                (session["bank_version_id"], row["question_code"]),
                            ).fetchone()
                            answer_key = json.loads(question["answer_key_json"]) if question is not None else None
                            correctness = 1 if question is not None and answer == answer_key else 0
                        connection.execute(
                            "UPDATE quiz_events SET state='accepted', chosen_by_resolution=1, correctness=? "
                            "WHERE event_id=?",
                            (correctness, row["event_id"]),
                        )
                    else:
                        connection.execute(
                            "UPDATE quiz_events SET state='variant_discarded' WHERE event_id=?",
                            (row["event_id"],),
                        )
                append_event(connection, actor_id=actor_id, action="quiz.event_conflict_resolved",
                             resource_type="quiz_session", resource_id=conflict["session_id"],
                             detail={"conflict_id": conflict_id, "client_seq": conflict["client_seq"],
                                     "chosen_event_id": chosen_event_id, "note": note},
                             occurred_at=self._now())
                return "quiz_conflict", conflict_id, {"conflict_id": conflict_id,
                                                      "chosen_event_id": chosen_event_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz_resolve_conflict", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 有效事件、解锁与结算
    # ------------------------------------------------------------------

    def _effective_events(self, connection, session) -> list[dict[str, Any]]:
        """按客户端序号归并出成绩实际采用的有效事件。

        有效事件必须满足：状态为 accepted、所在关卡在会话钉住的版本中可达
        （前置关卡已由有效事件完成）。冲突与弃用变体一律不计入。
        """

        snapshot = self._version_snapshot(connection, session["bank_version_id"])
        levels = {level["code"]: level for level in snapshot["levels"]}
        questions = {q["code"]: q for q in snapshot["questions"]}
        rows = connection.execute(
            "SELECT * FROM quiz_events WHERE session_id=? AND state='accepted' "
            "ORDER BY client_seq, received_at, event_id",
            (session["session_id"],),
        ).fetchall()
        accepted: list[dict[str, Any]] = []
        for row in rows:
            accepted.append({
                "event_id": row["event_id"],
                "client_seq": row["client_seq"],
                "event_kind": row["event_kind"],
                "question_code": row["question_code"],
                "member_alias": row["member_alias"],
                "correctness": row["correctness"],
                "chosen_by_resolution": bool(row["chosen_by_resolution"]),
                "received_at": row["received_at"],
            })
        completed: set[str] = set()
        effective: list[dict[str, Any]] = []
        remaining = list(accepted)
        # 不动点迭代：由已完成关卡推出解锁关卡，再把解锁关卡上的事件并入
        # 有效集，直到没有新事件可以并入为止。有效集始终保持客户端序号顺序。
        while True:
            unlocked = {level["code"] for level in snapshot["levels"]
                        if all(pre in completed for pre in level["prerequisites"])}
            moved = [event for event in remaining
                     if questions[event["question_code"]]["level_code"] in unlocked]
            if not moved:
                break
            effective.extend(moved)
            moved_ids = {id(event) for event in moved}
            remaining = [event for event in remaining if id(event) not in moved_ids]
            terminal_codes = {event["question_code"] for event in effective
                              if event["event_kind"] in ("answer", "skip")}
            for level in snapshot["levels"]:
                if level["code"] in completed:
                    continue
                level_questions = {q["code"] for q in snapshot["questions"]
                                   if q["level_code"] == level["code"]}
                if level_questions and level_questions <= terminal_codes:
                    completed.add(level["code"])
        # 输出统一按客户端序号排序，与不动点累积的先后顺序无关。
        effective.sort(key=lambda event: event["client_seq"])
        return effective

    def _session_progress(self, connection, session) -> dict[str, Any]:
        snapshot = self._version_snapshot(connection, session["bank_version_id"])
        effective = self._effective_events(connection, session)
        terminal_kinds = ("answer", "skip")
        terminal_by_question: dict[str, dict[str, Any]] = {}
        for event in effective:
            if event["event_kind"] in terminal_kinds and event["question_code"] not in terminal_by_question:
                terminal_by_question[event["question_code"]] = event
        completed_levels: list[str] = []
        for level in snapshot["levels"]:
            level_questions = [q["code"] for q in snapshot["questions"] if q["level_code"] == level["code"]]
            if level_questions and all(code in terminal_by_question for code in level_questions):
                completed_levels.append(level["code"])
        unlocked_levels: list[str] = []
        for level in snapshot["levels"]:
            if level["code"] in completed_levels or \
                    all(pre in completed_levels for pre in level["prerequisites"]):
                unlocked_levels.append(level["code"])
        correct_questions = sorted({code for code, event in terminal_by_question.items()
                                    if event["event_kind"] == "answer" and event["correctness"] == 1})
        return {
            "effective_events": effective,
            "completed_levels": completed_levels,
            "unlocked_levels": unlocked_levels,
            "correct_questions": correct_questions,
            "score": len(correct_questions),
            "answered_questions": sorted(terminal_by_question),
        }

    def finalize_session(self, *, request_id: str, actor_id: str, session_id: str) -> Settlement:
        """结算会话：要求冲突全部解释完毕，落库后成绩不再改写。"""

        payload = {"actor_id": actor_id, "session_id": session_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session = self._session(connection, session_id)
            self._check_site_scope(connection, actor, session["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                existing_settlement = connection.execute(
                    "SELECT * FROM quiz_settlements WHERE session_id=?", (session_id,)
                ).fetchone()
                if existing_settlement is not None:
                    # 已结束的成绩不再改写：换 request_id 重放仍返回首次结算结果。
                    return "quiz_settlement", session_id, {
                        "session_id": session_id,
                        "score": existing_settlement["score"],
                        "correct_questions": json.loads(existing_settlement["correct_questions_json"]),
                        "completed_levels": json.loads(existing_settlement["completed_levels_json"]),
                        "effective_event_count": existing_settlement["effective_event_count"],
                        "effective_hash": existing_settlement["effective_hash"],
                        "settled_at": existing_settlement["settled_at"],
                    }
                open_conflicts = connection.execute(
                    "SELECT COUNT(*) AS count FROM quiz_event_conflicts WHERE session_id=? AND status='open'",
                    (session_id,),
                ).fetchone()["count"]
                if open_conflicts:
                    raise ConflictError("仍存在未解释的同序号冲突，运营人员裁决后才能结算")
                progress = self._session_progress(connection, session)
                effective_hash = digest([
                    {"event_id": event["event_id"], "client_seq": event["client_seq"],
                     "event_kind": event["event_kind"], "question_code": event["question_code"],
                     "correctness": event["correctness"]}
                    for event in progress["effective_events"]
                ])
                settled_at = self._now()
                connection.execute(
                    "INSERT INTO quiz_settlements(session_id,score,correct_questions_json,"
                    "completed_levels_json,effective_event_count,effective_hash,settled_by,settled_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, progress["score"], canonical_json(progress["correct_questions"]),
                     canonical_json(progress["completed_levels"]), len(progress["effective_events"]),
                     effective_hash, actor_id, settled_at),
                )
                connection.execute(
                    "UPDATE quiz_sessions SET status='finalized', finalized_at=? WHERE session_id=?",
                    (settled_at, session_id),
                )
                append_event(connection, actor_id=actor_id, action="quiz.session_finalized",
                             resource_type="quiz_session", resource_id=session_id,
                             detail={"score": progress["score"],
                                     "effective_event_count": len(progress["effective_events"]),
                                     "effective_hash": effective_hash},
                             occurred_at=settled_at)
                return "quiz_settlement", session_id, {
                    "session_id": session_id,
                    "score": progress["score"],
                    "correct_questions": progress["correct_questions"],
                    "completed_levels": progress["completed_levels"],
                    "effective_event_count": len(progress["effective_events"]),
                    "effective_hash": effective_hash,
                    "settled_at": settled_at,
                }

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="quiz_finalize_session", payload=payload, create=create)
            row = connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            summary = json.loads(row["response_json"])
            return Settlement(session_id=session_id, score=summary["score"],
                              correct_questions=summary["correct_questions"],
                              completed_levels=summary["completed_levels"],
                              effective_event_count=summary["effective_event_count"],
                              effective_hash=summary["effective_hash"],
                              settled_at=summary["settled_at"])

    # ------------------------------------------------------------------
    # 后台查询与隐私视图
    # ------------------------------------------------------------------

    def get_session_backend(self, *, actor_id: str, session_id: str) -> dict[str, Any]:
        """后台完整视图：会话状态、钉住版本、成员、进度与待解释冲突。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            session = self._session(connection, session_id)
            progress = self._session_progress(connection, session)
            settlement = connection.execute(
                "SELECT * FROM quiz_settlements WHERE session_id=?", (session_id,)
            ).fetchone()
            conflicts = connection.execute(
                "SELECT * FROM quiz_event_conflicts WHERE session_id=? ORDER BY client_seq",
                (session_id,),
            ).fetchall()
            return {
                "session_id": session_id,
                "site_id": session["site_id"],
                "family_alias": session["family_alias"],
                "status": session["status"],
                "bank_id": session["bank_id"],
                "bank_version_id": session["bank_version_id"],
                "members": list(self._members(connection, session_id).values()),
                "score": progress["score"],
                "correct_questions": progress["correct_questions"],
                "completed_levels": progress["completed_levels"],
                "unlocked_levels": progress["unlocked_levels"],
                "effective_event_count": len(progress["effective_events"]),
                "open_conflicts": [row["conflict_id"] for row in conflicts if row["status"] == "open"],
                "settlement": None if settlement is None else {
                    "score": settlement["score"],
                    "effective_hash": settlement["effective_hash"],
                    "settled_at": settlement["settled_at"],
                },
            }

    def list_effective_events(self, *, actor_id: str, session_id: str,
                              view: str = "operator") -> list[dict[str, Any]]:
        """列出成绩实际采用的有效事件，按客户端序号排序。

        operator 视图返回全部字段；family 视图隐藏成员化名以外的归属细节；
        public 视图只返回题目与结果，不包含任何成员信息。
        """

        if view not in VIEWS:
            raise ValidationError("view 不在允许范围内")
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            session = self._session(connection, session_id)
            progress = self._session_progress(connection, session)
            items: list[dict[str, Any]] = []
            for event in progress["effective_events"]:
                item = {
                    "client_seq": event["client_seq"],
                    "event_kind": event["event_kind"],
                    "question_code": event["question_code"],
                    "correctness": event["correctness"],
                }
                if view == "operator":
                    item.update({
                        "event_id": event["event_id"],
                        "member_alias": event["member_alias"],
                        "chosen_by_resolution": event["chosen_by_resolution"],
                        "received_at": event["received_at"],
                    })
                elif view == "family":
                    item["member_alias"] = event["member_alias"]
                items.append(item)
            return items

    def family_view(self, *, session_id: str, member_alias: str) -> dict[str, Any]:
        """家庭视图：只共享成员明确同意公开的进度，儿童明细绝不外泄。"""

        with self.database.transaction() as connection:
            session = self._session(connection, session_id)
            members = self._members(connection, session_id)
            requester = members.get(member_alias)
            if requester is None:
                raise NotFoundError("会话成员不存在")
            progress = self._session_progress(connection, session)
            shared: list[dict[str, Any]] = []
            for event in progress["effective_events"]:
                alias = event["member_alias"]
                if alias is None:
                    continue
                member = members.get(alias)
                if member is None or member["is_child"] or not member["share_consent"]:
                    continue
                shared.append({
                    "member_alias": alias,
                    "question_code": event["question_code"],
                    "event_kind": event["event_kind"],
                    "correctness": event["correctness"],
                })
            return {
                "session_id": session_id,
                "family_alias": session["family_alias"],
                "status": session["status"],
                "score": progress["score"],
                "completed_levels": progress["completed_levels"],
                "unlocked_levels": progress["unlocked_levels"],
                "shared_progress": shared,
            }

    def child_view(self, *, session_id: str, member_alias: str) -> dict[str, Any]:
        """儿童视图：只返回适龄的题目与提示，超龄内容一律不出现。"""

        with self.database.transaction() as connection:
            session = self._session(connection, session_id)
            members = self._members(connection, session_id)
            member = members.get(member_alias)
            if member is None:
                raise NotFoundError("会话成员不存在")
            if not member["is_child"]:
                raise PermissionDenied("该成员不是儿童，请使用家庭视图")
            snapshot = self._version_snapshot(connection, session["bank_version_id"])
            progress = self._session_progress(connection, session)
            unlocked = set(progress["unlocked_levels"])
            questions = []
            for question in snapshot["questions"]:
                if question["level_code"] not in unlocked:
                    continue
                if question["min_age"] > member["age"]:
                    continue
                questions.append({
                    "code": question["code"],
                    "level_code": question["level_code"],
                    "prompt": question["prompt"],
                    "options": question["options"],
                    "knowledge_source": question["knowledge_source"],
                })
            return {
                "session_id": session_id,
                "member_alias": member_alias,
                "unlocked_levels": progress["unlocked_levels"],
                "questions": questions,
            }

    def public_leaderboard(self, *, site_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """公开排行：只展示化名与总分，儿童明细和未同意公开的成员一律排除。"""

        if limit < 1 or limit > 100:
            raise ValidationError("limit 必须在 1 到 100 之间")
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT s.session_id, s.family_alias, st.score, st.settled_at "
                "FROM quiz_sessions s JOIN quiz_settlements st ON st.session_id=s.session_id "
                "WHERE s.site_id=? AND s.status='finalized' ORDER BY st.score DESC, st.settled_at ASC, s.session_id",
                (site_id,),
            ).fetchall()
            board: list[dict[str, Any]] = []
            for row in rows:
                members = self._members(connection, row["session_id"])
                adults = [m for m in members.values() if not m["is_child"]]
                # 儿童明细不能进入公开排行：纯儿童会话直接排除；
                # 含成人的会话要求所有成人明确同意公开，才展示家庭化名。
                if not adults or not all(m["share_consent"] for m in adults):
                    continue
                board.append({"family_alias": row["family_alias"], "score": row["score"]})
                if len(board) >= limit:
                    break
            return board

    def list_conflicts(self, *, actor_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        """列出待解释或已裁决的同序号冲突及其保留的全部内容。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            if session_id is not None:
                self._session(connection, session_id)
                rows = connection.execute(
                    "SELECT * FROM quiz_event_conflicts WHERE session_id=? ORDER BY client_seq",
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM quiz_event_conflicts ORDER BY created_at, conflict_id"
                ).fetchall()
            conflicts: list[dict[str, Any]] = []
            for row in rows:
                variants = connection.execute(
                    "SELECT * FROM quiz_events WHERE session_id=? AND client_seq=? "
                    "ORDER BY received_at, event_id",
                    (row["session_id"], row["client_seq"]),
                ).fetchall()
                conflicts.append({
                    "conflict_id": row["conflict_id"],
                    "session_id": row["session_id"],
                    "client_seq": row["client_seq"],
                    "status": row["status"],
                    "chosen_event_id": row["chosen_event_id"],
                    "resolution_note": row["resolution_note"],
                    "variants": [{
                        "event_id": variant["event_id"],
                        "event_kind": variant["event_kind"],
                        "question_code": variant["question_code"],
                        "member_alias": variant["member_alias"],
                        "payload": json.loads(variant["payload_json"]),
                        "state": variant["state"],
                        "received_at": variant["received_at"],
                    } for variant in variants],
                })
            return conflicts

    def get_bank_version(self, *, actor_id: str, version_id: str) -> dict[str, Any]:
        """后台查看题库版本的冻结内容与内容哈希。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            version = self._version(connection, version_id)
            snapshot = self._version_snapshot(connection, version_id)
            return {
                "version_id": version_id,
                "bank_id": version["bank_id"],
                "version_no": version["version_no"],
                "status": version["status"],
                "content_hash": version["content_hash"],
                "frozen_at": version["frozen_at"],
                "levels": snapshot["levels"],
                "questions": snapshot["questions"],
            }

    def list_bank_versions(self, *, actor_id: str, bank_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            self._bank(connection, bank_id)
            rows = connection.execute(
                "SELECT * FROM quiz_bank_versions WHERE bank_id=? ORDER BY version_no", (bank_id,)
            ).fetchall()
            return [{
                "version_id": row["version_id"],
                "version_no": row["version_no"],
                "status": row["status"],
                "content_hash": row["content_hash"],
                "frozen_at": row["frozen_at"],
            } for row in rows]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _bank(self, connection, bank_id: str):
        row = connection.execute("SELECT * FROM quiz_banks WHERE bank_id=?", (bank_id,)).fetchone()
        if row is None:
            raise NotFoundError("题库不存在")
        return row

    def _version(self, connection, version_id: str):
        row = connection.execute(
            "SELECT * FROM quiz_bank_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("题库版本不存在")
        return row

    def _session(self, connection, session_id: str):
        row = connection.execute("SELECT * FROM quiz_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("会话不存在")
        return row

    def _check_site_scope(self, connection, actor: Actor, site_id: str) -> None:
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所数据")

    def _latest_frozen_version(self, connection, bank_id: str):
        return connection.execute(
            "SELECT * FROM quiz_bank_versions WHERE bank_id=? AND status='frozen' "
            "ORDER BY version_no DESC LIMIT 1",
            (bank_id,),
        ).fetchone()

    def _members(self, connection, session_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM quiz_session_members WHERE session_id=? ORDER BY member_alias",
            (session_id,),
        ).fetchall()
        return {
            row["member_alias"]: {
                "member_alias": row["member_alias"],
                "is_child": bool(row["is_child"]),
                "age": row["age"],
                "share_consent": bool(row["share_consent"]),
            }
            for row in rows
        }

    def _version_snapshot(self, connection, version_id: str) -> dict[str, Any]:
        levels = []
        for row in connection.execute(
                "SELECT * FROM quiz_levels WHERE version_id=? ORDER BY position, code", (version_id,)):
            levels.append({
                "code": row["code"],
                "title": row["title"],
                "position": row["position"],
                "prerequisites": json.loads(row["prerequisites_json"]),
            })
        questions = []
        for row in connection.execute(
                "SELECT * FROM quiz_questions WHERE version_id=? ORDER BY position, code", (version_id,)):
            questions.append({
                "code": row["code"],
                "level_code": row["level_code"],
                "position": row["position"],
                "prompt": row["prompt"],
                "options": json.loads(row["options_json"]),
                "answer_key": json.loads(row["answer_key_json"]),
                "knowledge_source": row["knowledge_source"],
                "min_age": row["min_age"],
            })
        return {"version_id": version_id, "levels": levels, "questions": questions}

    def _normalize_levels(self, levels: Any) -> list[dict[str, Any]]:
        if not isinstance(levels, list) or not levels:
            raise ValidationError("levels 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(levels):
            if not isinstance(raw, dict):
                raise ValidationError("关卡必须是对象")
            code = self._identifier(str(raw.get("code", "")), "level.code")
            if code in seen:
                raise ValidationError("关卡编号重复")
            seen.add(code)
            title = self._text(str(raw.get("title", "")), "level.title", 120)
            prerequisites = raw.get("prerequisites", [])
            if not isinstance(prerequisites, list):
                raise ValidationError("关卡前置条件必须是数组")
            normalized.append({
                "code": code,
                "title": title,
                "position": index + 1,
                "prerequisites": [self._identifier(str(pre), "level.prerequisites") for pre in prerequisites],
            })
        for level in normalized:
            for pre in level["prerequisites"]:
                if pre not in seen:
                    raise ValidationError("关卡前置条件引用了不存在的关卡")
                if pre == level["code"]:
                    raise ValidationError("关卡不能以前自己为前置条件")
        graph = {level["code"]: list(level["prerequisites"]) for level in normalized}
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(code: str) -> None:
            if code in visiting:
                raise ValidationError("关卡前置关系不能成环")
            if code in visited:
                return
            visiting.add(code)
            for pre in graph[code]:
                visit(pre)
            visiting.discard(code)
            visited.add(code)

        for code in graph:
            visit(code)
        return normalized

    def _normalize_questions(self, questions: Any,
                             levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(questions, list) or not questions:
            raise ValidationError("questions 必须是非空数组")
        level_codes = {level["code"] for level in levels}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(questions):
            if not isinstance(raw, dict):
                raise ValidationError("题目必须是对象")
            code = self._identifier(str(raw.get("code", "")), "question.code")
            if code in seen:
                raise ValidationError("题目编号重复")
            seen.add(code)
            level_code = str(raw.get("level_code", ""))
            if level_code not in level_codes:
                raise ValidationError("题目引用了不存在的关卡")
            prompt = self._text(str(raw.get("prompt", "")), "question.prompt", 400)
            options = raw.get("options", [])
            if not isinstance(options, list) or not options:
                raise ValidationError("题目选项必须是非空数组")
            options = [self._text(str(option), "question.options", 200) for option in options]
            if "answer_key" not in raw:
                raise ValidationError("题目必须提供 answer_key")
            answer_key = raw["answer_key"]
            knowledge_source = self._text(str(raw.get("knowledge_source", "")),
                                          "question.knowledge_source", 200)
            min_age = raw.get("min_age", 0)
            if not isinstance(min_age, int) or isinstance(min_age, bool) or min_age < 0 or min_age > 120:
                raise ValidationError("min_age 必须是 0 到 120 的整数")
            normalized.append({
                "code": code,
                "level_code": level_code,
                "position": index + 1,
                "prompt": prompt,
                "options": options,
                "answer_key": answer_key,
                "knowledge_source": knowledge_source,
                "min_age": min_age,
            })
        return normalized

    def _normalize_members(self, members: Any) -> list[dict[str, Any]]:
        if not isinstance(members, list) or not members:
            raise ValidationError("members 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in members:
            if not isinstance(raw, dict):
                raise ValidationError("成员必须是对象")
            alias = self._text(str(raw.get("member_alias", "")), "member_alias", 80)
            if alias in seen:
                raise ValidationError("成员化名重复")
            seen.add(alias)
            is_child = raw.get("is_child")
            if not isinstance(is_child, bool):
                raise ValidationError("is_child 必须是布尔值")
            age = raw.get("age")
            if not isinstance(age, int) or isinstance(age, bool) or age < 0 or age > 120:
                raise ValidationError("age 必须是 0 到 120 的整数")
            if is_child and age >= 18:
                raise ValidationError("儿童成员年龄必须小于 18 岁")
            share_consent = raw.get("share_consent", False)
            if not isinstance(share_consent, bool):
                raise ValidationError("share_consent 必须是布尔值")
            normalized.append({
                "member_alias": alias,
                "is_child": is_child,
                "age": age,
                "share_consent": share_consent,
            })
        return normalized
