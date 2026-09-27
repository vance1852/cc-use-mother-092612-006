"""药材知识闯关：冻结题库、独立会话、事件归并、冲突保留与隐私视图。

规则概述：
- 题库先冻结后使用，冻结内容包含题目、知识来源与适用年龄；撤回题目会
  生成同谱系的新冻结版本，只影响之后建立的会话，不改写既有会话与已
  结算成绩。
- 每个参与者建立一次独立会话，会话绑定创建时谱系内最新的冻结版本。
- 终端断网补传的事件按客户端序号归并：同序号同内容沿用第一次处理结
  果；同序号不同内容保留为待解释冲突，系统不擅自取舍，冲突未解决前
  会话不能结算。
- 成绩只采用每道题目客户端序号最小的有效答题事件；关卡解锁由会话绑
  定的题库版本与已完成前置条件推导。
- 儿童只能看到适龄提示，且明细不进入公开排行；家庭队伍只共享成员明
  确同意公开的进度。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService


EVENT_KINDS = frozenset({"answer", "hint", "skip"})
ADULT_AGE = 18
MAX_BATCH = 200


class QuizService(DomainService):
    """在基础服务边界上实现药材知识闯关的题库、会话与结算规则。"""

    # ---------- 基础校验与上下文 ----------

    def _integer(self, value: Any, field: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if value < minimum or value > maximum:
            raise ValidationError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
        return value

    def _site_for_write(self, connection, actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能写入其他组织的场所")
        return site

    def _bank(self, connection, bank_id: str):
        row = connection.execute("SELECT * FROM quiz_banks WHERE bank_id=?", (bank_id,)).fetchone()
        if row is None:
            raise NotFoundError("题库不存在")
        return row

    def _latest_frozen(self, connection, lineage_id: str):
        return connection.execute(
            "SELECT * FROM quiz_banks WHERE lineage_id=? AND status='frozen' "
            "ORDER BY version DESC LIMIT 1",
            (lineage_id,),
        ).fetchone()

    def _participant(self, connection, participant_id: str):
        row = connection.execute(
            "SELECT * FROM quiz_participants WHERE participant_id=?", (participant_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("参与者不存在")
        return row

    def _questions_of(self, connection, bank_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM quiz_questions WHERE bank_id=? ORDER BY level, position", (bank_id,)
        ).fetchall()
        return [
            {
                "question_id": row["question_id"],
                "level": row["level"],
                "position": row["position"],
                "prompt": row["prompt"],
                "answer_key": row["answer_key"],
                "points": row["points"],
                "min_age": row["min_age"],
                "knowledge_source": row["knowledge_source"],
                "hints": json.loads(row["hints_json"]),
            }
            for row in rows
        ]

    def _session_context(self, connection, session_id: str):
        session = connection.execute(
            "SELECT * FROM quiz_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if session is None:
            raise NotFoundError("会话不存在")
        participant = self._participant(connection, session["participant_id"])
        bank = self._bank(connection, session["bank_id"])
        questions = self._questions_of(connection, session["bank_id"])
        return session, participant, bank, questions

    def _effective_events(self, connection, session_id: str) -> list[Any]:
        return connection.execute(
            "SELECT * FROM quiz_events WHERE session_id=? AND status='effective' "
            "ORDER BY client_seq",
            (session_id,),
        ).fetchall()

    def _pending_conflicts(self, connection, session_id: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS count FROM quiz_conflicts WHERE session_id=? AND status='pending'",
            (session_id,),
        ).fetchone()["count"]

    @staticmethod
    def _adopted_answers(events: list[Any]) -> dict[str, Any]:
        """每道题目采用客户端序号最小的有效答题事件。"""

        adopted: dict[str, Any] = {}
        for row in events:
            if row["kind"] == "answer" and row["question_id"] not in adopted:
                adopted[row["question_id"]] = row
        return adopted

    def _score_snapshot(self, questions: list[dict[str, Any]], events: list[Any],
                        age: int) -> tuple[int, list[dict[str, Any]]]:
        """按有效事件推导成绩与实际采用的事件清单。"""

        by_id = {question["question_id"]: question for question in questions}
        score = 0
        adopted_list: list[dict[str, Any]] = []
        adopted = self._adopted_answers(events)
        for question_id, row in sorted(adopted.items(), key=lambda item: item[1]["client_seq"]):
            question = by_id.get(question_id)
            if question is None or question["min_age"] > age:
                continue  # 防御：事件写入时已按会话版本与年龄校验
            answer = json.loads(row["payload_json"])["answer"]
            correct = answer == question["answer_key"]
            awarded = question["points"] if correct else 0
            score += awarded
            adopted_list.append({
                "event_id": row["event_id"],
                "client_seq": row["client_seq"],
                "question_id": question_id,
                "level": question["level"],
                "answer": answer,
                "correct": correct,
                "points_awarded": awarded,
            })
        return score, adopted_list

    def _progress_levels(self, questions: list[dict[str, Any]], events: list[Any],
                         age: int) -> list[dict[str, Any]]:
        """按会话题库版本与已完成前置条件推导各关卡状态。"""

        applicable = [q for q in questions if q["min_age"] <= age]
        adopted = self._adopted_answers(events)
        levels: list[dict[str, Any]] = []
        unlocked = True
        for level in sorted({q["level"] for q in applicable}):
            level_questions = [q for q in applicable if q["level"] == level]
            answered = [q for q in level_questions if q["question_id"] in adopted]
            correct = 0
            for question in answered:
                payload = json.loads(adopted[question["question_id"]]["payload_json"])
                if payload["answer"] == question["answer_key"]:
                    correct += 1
            completed = len(answered) == len(level_questions)
            levels.append({
                "level": level,
                "unlocked": unlocked,
                "completed": completed,
                "questions": len(level_questions),
                "answered": len(answered),
                "correct": correct,
            })
            unlocked = unlocked and completed
        return levels

    def _validate_event_content(self, participant, by_id: dict[str, dict[str, Any]],
                                kind: str, question_id: str, payload: dict[str, Any]) -> None:
        question = by_id.get(question_id)
        if question is None:
            raise ValidationError("题目不属于该会话采用的题库版本")
        if question["min_age"] > participant["age"]:
            raise ValidationError("题目超出参与者适龄范围")
        if kind == "hint":
            hint = next((h for h in question["hints"] if h["hint_id"] == payload["hint_id"]), None)
            if hint is None:
                raise ValidationError("提示不存在于该题目")
            if hint["min_age"] > participant["age"]:
                raise ValidationError("提示超出参与者适龄范围")

    def _normalize_hints(self, hints: Any) -> list[dict[str, Any]]:
        if hints is None:
            return []
        if not isinstance(hints, list) or len(hints) > 10:
            raise ValidationError("hints 必须是不超过 10 条的列表")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(hints, start=1):
            if not isinstance(raw, dict):
                raise ValidationError("hints 元素必须是对象")
            hint_id = self._identifier(str(raw.get("hint_id") or f"h{index}"), "hint_id")
            if hint_id in seen:
                raise ValidationError("hint_id 重复")
            seen.add(hint_id)
            text = self._text(str(raw.get("text", "")), "hint.text")
            min_age = self._integer(raw.get("min_age", 0), "hint.min_age", 0, 120)
            normalized.append({"hint_id": hint_id, "text": text, "min_age": min_age})
        return normalized

    # ---------- 题库版本管理 ----------

    def create_bank(self, *, request_id: str, actor_id: str, site_id: str,
                    bank_id: str, title: str, lineage_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "bank_id": bank_id,
                   "title": title, "lineage_id": lineage_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            bank_id = self._identifier(bank_id, "bank_id")
            title = self._text(title, "title")
            lineage_id = self._identifier(lineage_id, "lineage_id") if lineage_id else bank_id

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO quiz_banks(bank_id,site_id,lineage_id,version,title,status,"
                        "created_by,created_at) VALUES(?,?,?,1,?,'draft',?,?)",
                        (bank_id, site_id, lineage_id, title, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("题库编号已经存在或谱系版本冲突") from exc
                append_event(connection, actor_id=actor_id, action="quiz.bank_created",
                             resource_type="quiz_bank", resource_id=bank_id,
                             detail={"site_id": site_id, "lineage_id": lineage_id, "title": title},
                             occurred_at=self._now())
                return "quiz_bank", bank_id, {"bank_id": bank_id, "lineage_id": lineage_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.create_bank", payload=payload, create=create)

    def add_question(self, *, request_id: str, actor_id: str, bank_id: str, question_id: str,
                     level: int, position: int, prompt: str, answer_key: str, points: int,
                     min_age: int, knowledge_source: str,
                     hints: list[dict[str, Any]] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "bank_id": bank_id, "question_id": question_id,
                   "level": level, "position": position, "prompt": prompt,
                   "answer_key": answer_key, "points": points, "min_age": min_age,
                   "knowledge_source": knowledge_source, "hints": hints}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            bank = self._bank(connection, bank_id)
            question_id = self._identifier(question_id, "question_id")
            level = self._integer(level, "level", 1, 50)
            position = self._integer(position, "position", 1, 200)
            prompt = self._text(prompt, "prompt", 500)
            answer_key = self._text(answer_key, "answer_key")
            points = self._integer(points, "points", 0, 1000)
            min_age = self._integer(min_age, "min_age", 0, 120)
            knowledge_source = self._text(knowledge_source, "knowledge_source", 200)
            normalized_hints = self._normalize_hints(hints)

            def create() -> tuple[str, str, dict[str, Any]]:
                if bank["status"] != "draft":
                    raise ConflictError("题库已经冻结，不能再修改题目")
                try:
                    connection.execute(
                        "INSERT INTO quiz_questions(bank_id,question_id,level,position,prompt,"
                        "answer_key,points,min_age,knowledge_source,hints_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (bank_id, question_id, level, position, prompt, answer_key, points,
                         min_age, knowledge_source, canonical_json(normalized_hints)),
                    )
                except Exception as exc:
                    raise ConflictError("题目编号或关卡位置已经被占用") from exc
                append_event(connection, actor_id=actor_id, action="quiz.question_added",
                             resource_type="quiz_bank", resource_id=bank_id,
                             detail={"question_id": question_id, "level": level,
                                     "min_age": min_age, "knowledge_source": knowledge_source},
                             occurred_at=self._now())
                return "quiz_question", f"{bank_id}/{question_id}", {"bank_id": bank_id,
                                                                     "question_id": question_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.add_question", payload=payload, create=create)

    def freeze_bank(self, *, request_id: str, actor_id: str, bank_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "bank_id": bank_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            bank = self._bank(connection, bank_id)
            questions = self._questions_of(connection, bank_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if bank["status"] != "draft":
                    raise ConflictError("题库已经冻结")
                if not questions:
                    raise ValidationError("冻结前至少需要登记一道题目")
                connection.execute(
                    "UPDATE quiz_banks SET status='frozen', frozen_at=? WHERE bank_id=?",
                    (self._now(), bank_id),
                )
                append_event(connection, actor_id=actor_id, action="quiz.bank_frozen",
                             resource_type="quiz_bank", resource_id=bank_id,
                             detail={"lineage_id": bank["lineage_id"], "version": bank["version"],
                                     "question_count": len(questions)},
                             occurred_at=self._now())
                return "quiz_bank", bank_id, {"bank_id": bank_id, "status": "frozen",
                                              "version": bank["version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.freeze_bank", payload=payload, create=create)

    def withdraw_question(self, *, request_id: str, actor_id: str, bank_id: str,
                          question_id: str, new_bank_id: str) -> WriteReceipt:
        """从最新冻结版本撤回题目，生成同谱系的新冻结版本，只影响新会话。"""

        payload = {"actor_id": actor_id, "bank_id": bank_id, "question_id": question_id,
                   "new_bank_id": new_bank_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            bank = self._bank(connection, bank_id)
            questions = self._questions_of(connection, bank_id)
            new_bank_id = self._identifier(new_bank_id, "new_bank_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                if bank["status"] != "frozen":
                    raise ValidationError("只有已冻结的题库才能撤回题目")
                latest = self._latest_frozen(connection, bank["lineage_id"])
                if latest is None or latest["bank_id"] != bank_id:
                    raise ConflictError("只能基于谱系中的最新冻结版本撤回题目")
                remaining = [q for q in questions if q["question_id"] != question_id]
                if len(remaining) == len(questions):
                    raise NotFoundError("题目不存在于该题库版本")
                try:
                    connection.execute(
                        "INSERT INTO quiz_banks(bank_id,site_id,lineage_id,version,title,status,"
                        "created_by,created_at,frozen_at) VALUES(?,?,?,?,?,'frozen',?,?,?)",
                        (new_bank_id, bank["site_id"], bank["lineage_id"], bank["version"] + 1,
                         bank["title"], actor_id, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("新题库编号已经存在") from exc
                for question in remaining:
                    connection.execute(
                        "INSERT INTO quiz_questions(bank_id,question_id,level,position,prompt,"
                        "answer_key,points,min_age,knowledge_source,hints_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (new_bank_id, question["question_id"], question["level"],
                         question["position"], question["prompt"], question["answer_key"],
                         question["points"], question["min_age"], question["knowledge_source"],
                         canonical_json(question["hints"])),
                    )
                append_event(connection, actor_id=actor_id, action="quiz.question_withdrawn",
                             resource_type="quiz_bank", resource_id=new_bank_id,
                             detail={"source_bank_id": bank_id, "question_id": question_id,
                                     "lineage_id": bank["lineage_id"],
                                     "new_version": bank["version"] + 1},
                             occurred_at=self._now())
                return "quiz_bank", new_bank_id, {"bank_id": new_bank_id,
                                                  "lineage_id": bank["lineage_id"],
                                                  "version": bank["version"] + 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.withdraw_question", payload=payload, create=create)

    # ---------- 队伍与参与者 ----------

    def create_team(self, *, request_id: str, actor_id: str, site_id: str,
                    team_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "team_id": team_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            team_id = self._identifier(team_id, "team_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO quiz_teams(team_id,site_id,name,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (team_id, site_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("队伍编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="quiz.team_created",
                             resource_type="quiz_team", resource_id=team_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "quiz_team", team_id, {"team_id": team_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.create_team", payload=payload, create=create)

    def register_participant(self, *, request_id: str, actor_id: str, site_id: str,
                             alias: str, age: int, consent_public: bool,
                             team_id: str | None = None,
                             participant_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "alias": alias, "age": age,
                   "consent_public": consent_public, "team_id": team_id,
                   "participant_id": participant_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            alias = self._text(alias, "alias", 80)
            age = self._integer(age, "age", 0, 120)
            if not isinstance(consent_public, bool):
                raise ValidationError("consent_public 必须是布尔值")
            if team_id is not None:
                team = connection.execute(
                    "SELECT * FROM quiz_teams WHERE team_id=?", (team_id,)
                ).fetchone()
                if team is None:
                    raise NotFoundError("队伍不存在")
                if team["site_id"] != site_id:
                    raise ValidationError("队伍与参与者不属于同一站点")
            new_id = self._identifier(participant_id, "participant_id") if participant_id \
                else uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO quiz_participants(participant_id,site_id,alias,age,"
                        "consent_public,team_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (new_id, site_id, alias, age, 1 if consent_public else 0, team_id,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("参与者编号已经存在或化名已被使用") from exc
                append_event(connection, actor_id=actor_id, action="quiz.participant_registered",
                             resource_type="quiz_participant", resource_id=new_id,
                             detail={"site_id": site_id, "alias": alias, "age": age,
                                     "consent_public": consent_public, "team_id": team_id},
                             occurred_at=self._now())
                return "quiz_participant", new_id, {"participant_id": new_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.register_participant", payload=payload,
                                    create=create)

    def update_consent(self, *, request_id: str, actor_id: str, participant_id: str,
                       consent_public: bool) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "consent_public": consent_public}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant = self._participant(connection, participant_id)
            self._site_for_write(connection, actor, participant["site_id"])
            if not isinstance(consent_public, bool):
                raise ValidationError("consent_public 必须是布尔值")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE quiz_participants SET consent_public=? WHERE participant_id=?",
                    (1 if consent_public else 0, participant_id),
                )
                append_event(connection, actor_id=actor_id, action="quiz.consent_updated",
                             resource_type="quiz_participant", resource_id=participant_id,
                             detail={"consent_public": consent_public}, occurred_at=self._now())
                return "quiz_participant", participant_id, {
                    "participant_id": participant_id, "consent_public": consent_public}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.update_consent", payload=payload, create=create)

    # ---------- 会话与事件归并 ----------

    def start_session(self, *, request_id: str, actor_id: str, participant_id: str,
                      bank_id: str, session_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "bank_id": bank_id, "session_id": session_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant = self._participant(connection, participant_id)
            self._site_for_write(connection, actor, participant["site_id"])
            bank = self._bank(connection, bank_id)
            new_id = self._identifier(session_id, "session_id") if session_id \
                else uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                if bank["status"] != "frozen":
                    raise ValidationError("题库尚未冻结，不能开始会话")
                latest = self._latest_frozen(connection, bank["lineage_id"])
                if latest is None or latest["bank_id"] != bank_id:
                    raise ConflictError("题库已有更新的冻结版本，新会话必须使用最新版本")
                if bank["site_id"] != participant["site_id"]:
                    raise PermissionDenied("参与者与题库不属于同一站点")
                try:
                    connection.execute(
                        "INSERT INTO quiz_sessions(session_id,participant_id,bank_id,status,"
                        "created_at) VALUES(?,?,?,'active',?)",
                        (new_id, participant_id, bank_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("会话编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="quiz.session_started",
                             resource_type="quiz_session", resource_id=new_id,
                             detail={"participant_id": participant_id, "bank_id": bank_id,
                                     "bank_version": bank["version"],
                                     "lineage_id": bank["lineage_id"]},
                             occurred_at=self._now())
                return "quiz_session", new_id, {"session_id": new_id, "bank_id": bank_id,
                                                "bank_version": bank["version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.start_session", payload=payload, create=create)

    def submit_events(self, *, actor_id: str, session_id: str,
                      events: list[dict[str, Any]]) -> dict[str, Any]:
        """按客户端序号归并一批终端事件，重复补传沿用第一次处理结果。"""

        if not isinstance(events, list) or not events:
            raise ValidationError("events 必须是非空列表")
        if len(events) > MAX_BATCH:
            raise ValidationError(f"单次补传不能超过 {MAX_BATCH} 条事件")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session, participant, _bank, questions = self._session_context(connection, session_id)
            self._site_for_write(connection, actor, participant["site_id"])
            if session["status"] != "active":
                raise ConflictError("会话已经结算，不能再写入事件")
            by_id = {q["question_id"]: q for q in questions}
            results = [
                self._ingest_event(connection, actor_id, session, participant, by_id, raw)
                for raw in events
            ]
            summary = {"recorded": 0, "replayed": 0, "conflict": 0, "conflict_replayed": 0}
            for item in results:
                summary[item["status"]] += 1
            append_event(connection, actor_id=actor_id, action="quiz.events_submitted",
                         resource_type="quiz_session", resource_id=session_id,
                         detail={"batch_size": len(events), **summary},
                         occurred_at=self._now())
            return {"session_id": session_id, "results": results}

    def _ingest_event(self, connection, actor_id: str, session, participant,
                      by_id: dict[str, dict[str, Any]], raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("事件必须是对象")
        client_seq = self._integer(raw.get("client_seq"), "client_seq", 1, 1000000)
        kind = raw.get("kind")
        if kind not in EVENT_KINDS:
            raise ValidationError("事件类型必须是 answer、hint 或 skip")
        question_id = self._identifier(str(raw.get("question_id", "")), "question_id")
        payload = raw.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        if kind == "answer":
            answer = str(payload.get("answer", "")).strip()
            if not answer or len(answer) > 200:
                raise ValidationError("answer 不能为空且不能超过 200 个字符")
            payload = {"answer": answer}
        elif kind == "hint":
            payload = {"hint_id": self._identifier(str(payload.get("hint_id", "")), "hint_id")}
        else:
            payload = {}
        session_id = session["session_id"]
        content_hash = digest({"session_id": session_id, "client_seq": client_seq,
                               "kind": kind, "question_id": question_id, "payload": payload})
        existing = connection.execute(
            "SELECT * FROM quiz_events WHERE session_id=? AND client_seq=? AND status='effective'",
            (session_id, client_seq),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] == content_hash:
                return {"client_seq": client_seq, "status": "replayed",
                        "event_id": existing["event_id"]}
            return self._record_conflict(connection, actor_id, session, existing, client_seq,
                                         kind, question_id, payload, content_hash)
        self._validate_event_content(participant, by_id, kind, question_id, payload)
        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO quiz_events(event_id,session_id,client_seq,kind,question_id,"
            "payload_json,content_hash,status,received_at) VALUES(?,?,?,?,?,?,?,'effective',?)",
            (event_id, session_id, client_seq, kind, question_id, canonical_json(payload),
             content_hash, self._now()),
        )
        return {"client_seq": client_seq, "status": "recorded", "event_id": event_id}

    def _record_conflict(self, connection, actor_id: str, session, existing, client_seq: int,
                         kind: str, question_id: str, payload: dict[str, Any],
                         content_hash: str) -> dict[str, Any]:
        """同序号分叉保留为待解释冲突，系统不擅自取舍。"""

        session_id = session["session_id"]
        row = connection.execute(
            "SELECT * FROM quiz_conflicts WHERE session_id=? AND client_seq=?",
            (session_id, client_seq),
        ).fetchone()
        contender = {"kind": kind, "question_id": question_id, "payload": payload,
                     "content_hash": content_hash, "received_at": self._now()}
        if row is None:
            conflict_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO quiz_conflicts(conflict_id,session_id,client_seq,contenders_json,"
                "status,created_at) VALUES(?,?,?,?,'pending',?)",
                (conflict_id, session_id, client_seq, canonical_json([contender]), self._now()),
            )
        else:
            conflict_id = row["conflict_id"]
            contenders = json.loads(row["contenders_json"])
            if any(item["content_hash"] == content_hash for item in contenders):
                return {"client_seq": client_seq, "status": "conflict_replayed",
                        "conflict_id": conflict_id}
            contenders.append(contender)
            connection.execute(
                "UPDATE quiz_conflicts SET contenders_json=?, status='pending', resolution=NULL,"
                "resolved_by=NULL, resolved_at=NULL WHERE conflict_id=?",
                (canonical_json(contenders), conflict_id),
            )
        append_event(connection, actor_id=actor_id, action="quiz.conflict_recorded",
                     resource_type="quiz_conflict", resource_id=conflict_id,
                     detail={"session_id": session_id, "client_seq": client_seq,
                             "first_event_id": existing["event_id"],
                             "contender_hash": content_hash},
                     occurred_at=self._now())
        return {"client_seq": client_seq, "status": "conflict", "conflict_id": conflict_id}

    def resolve_conflict(self, *, request_id: str, actor_id: str, conflict_id: str,
                         decision: str, contender_hash: str | None = None) -> WriteReceipt:
        """由运营人员显式解释同序号分叉：保留先到达事件或采用指定分支。"""

        payload = {"actor_id": actor_id, "conflict_id": conflict_id, "decision": decision,
                   "contender_hash": contender_hash}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            conflict = connection.execute(
                "SELECT * FROM quiz_conflicts WHERE conflict_id=?", (conflict_id,)
            ).fetchone()
            if conflict is None:
                raise NotFoundError("冲突记录不存在")
            session, participant, _bank, questions = self._session_context(
                connection, conflict["session_id"])
            self._site_for_write(connection, actor, participant["site_id"])
            if decision not in ("keep_first", "use_contender"):
                raise ValidationError("decision 必须是 keep_first 或 use_contender")

            def create() -> tuple[str, str, dict[str, Any]]:
                if conflict["status"] != "pending":
                    raise ConflictError("冲突已经被处理")
                if session["status"] != "active":
                    raise ConflictError("会话已经结算，不能调整冲突")
                contender = None
                if decision == "use_contender":
                    contenders = json.loads(conflict["contenders_json"])
                    contender = next((c for c in contenders
                                      if c["content_hash"] == contender_hash), None)
                    if contender is None:
                        raise ValidationError("contender_hash 未匹配任何分支")
                    by_id = {q["question_id"]: q for q in questions}
                    self._validate_event_content(participant, by_id, contender["kind"],
                                                 contender["question_id"], contender["payload"])
                if contender is not None:
                    connection.execute(
                        "UPDATE quiz_events SET status='superseded' WHERE session_id=? "
                        "AND client_seq=? AND status='effective'",
                        (conflict["session_id"], conflict["client_seq"]),
                    )
                    connection.execute(
                        "INSERT INTO quiz_events(event_id,session_id,client_seq,kind,question_id,"
                        "payload_json,content_hash,status,received_at) "
                        "VALUES(?,?,?,?,?,?,?,'effective',?)",
                        (uuid.uuid4().hex, conflict["session_id"], conflict["client_seq"],
                         contender["kind"], contender["question_id"],
                         canonical_json(contender["payload"]), contender["content_hash"],
                         self._now()),
                    )
                connection.execute(
                    "UPDATE quiz_conflicts SET status='resolved', resolution=?, resolved_by=?,"
                    "resolved_at=? WHERE conflict_id=?",
                    (decision, actor_id, self._now(), conflict_id),
                )
                append_event(connection, actor_id=actor_id, action="quiz.conflict_resolved",
                             resource_type="quiz_conflict", resource_id=conflict_id,
                             detail={"session_id": conflict["session_id"],
                                     "client_seq": conflict["client_seq"],
                                     "decision": decision,
                                     "contender_hash": contender_hash},
                             occurred_at=self._now())
                return "quiz_conflict", conflict_id, {"conflict_id": conflict_id,
                                                      "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="quiz.resolve_conflict", payload=payload,
                                    create=create)

    # ---------- 结算 ----------

    def settle_session(self, *, actor_id: str, session_id: str) -> dict[str, Any]:
        """结算会话并保存不可改写的成绩快照；重复与并发调用返回同一结果。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session, participant, bank, questions = self._session_context(connection, session_id)
            self._site_for_write(connection, actor, participant["site_id"])
            existing = connection.execute(
                "SELECT * FROM quiz_settlements WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing is not None:
                return {"session_id": session_id, "score": existing["score"],
                        "effective_events": json.loads(existing["adopted_json"]),
                        "settled_at": existing["settled_at"], "replayed": True}
            pending = self._pending_conflicts(connection, session_id)
            if pending:
                raise ConflictError("存在未解释的同序号冲突，不能结算")
            events = self._effective_events(connection, session_id)
            score, adopted = self._score_snapshot(questions, events, participant["age"])
            detail = {"bank_id": session["bank_id"], "bank_version": bank["version"],
                      "participant_id": session["participant_id"],
                      "alias": participant["alias"]}
            settled_at = self._now()
            connection.execute(
                "INSERT INTO quiz_settlements(session_id,score,adopted_json,detail_json,"
                "settled_by,settled_at) VALUES(?,?,?,?,?,?)",
                (session_id, score, canonical_json(adopted), canonical_json(detail),
                 actor_id, settled_at),
            )
            connection.execute(
                "UPDATE quiz_sessions SET status='settled', settled_at=? WHERE session_id=?",
                (settled_at, session_id),
            )
            append_event(connection, actor_id=actor_id, action="quiz.session_settled",
                         resource_type="quiz_session", resource_id=session_id,
                         detail={"score": score, "bank_id": session["bank_id"],
                                 "bank_version": bank["version"]},
                         occurred_at=self._now())
            return {"session_id": session_id, "score": score, "effective_events": adopted,
                    "settled_at": settled_at, "replayed": False}

    # ---------- 后台查询 ----------

    def list_banks(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM quiz_questions q WHERE q.bank_id=b.bank_id) "
            "AS question_count FROM quiz_banks b WHERE b.site_id=? "
            "ORDER BY b.lineage_id, b.version",
            (site_id,),
        ).fetchall()
        return [{"bank_id": row["bank_id"], "lineage_id": row["lineage_id"],
                 "version": row["version"], "title": row["title"], "status": row["status"],
                 "question_count": row["question_count"], "frozen_at": row["frozen_at"]}
                for row in rows]

    def get_bank(self, bank_id: str) -> dict[str, Any]:
        connection = self.database.connection
        bank = self._bank(connection, bank_id)
        return {"bank_id": bank["bank_id"], "site_id": bank["site_id"],
                "lineage_id": bank["lineage_id"], "version": bank["version"],
                "title": bank["title"], "status": bank["status"],
                "frozen_at": bank["frozen_at"], "questions": self._questions_of(connection, bank_id)}

    def session_score(self, session_id: str) -> dict[str, Any]:
        """返回成绩与实际采用的有效事件；已结算会话返回快照。"""

        connection = self.database.connection
        session, participant, bank, questions = self._session_context(connection, session_id)
        pending = self._pending_conflicts(connection, session_id)
        settlement = connection.execute(
            "SELECT * FROM quiz_settlements WHERE session_id=?", (session_id,)
        ).fetchone()
        base = {"session_id": session_id, "participant_id": session["participant_id"],
                "alias": participant["alias"], "bank_id": session["bank_id"],
                "bank_version": bank["version"], "status": session["status"],
                "pending_conflicts": pending}
        if settlement is not None:
            return {**base, "score": settlement["score"],
                    "effective_events": json.loads(settlement["adopted_json"]),
                    "settled_at": settlement["settled_at"]}
        events = self._effective_events(connection, session_id)
        score, adopted = self._score_snapshot(questions, events, participant["age"])
        return {**base, "score": score, "effective_events": adopted, "settled_at": None}

    def session_progress(self, session_id: str) -> dict[str, Any]:
        """返回按会话题库版本推导的关卡解锁与完成情况。"""

        connection = self.database.connection
        session, participant, bank, questions = self._session_context(connection, session_id)
        events = self._effective_events(connection, session_id)
        levels = self._progress_levels(questions, events, participant["age"])
        next_level = next((item["level"] for item in levels
                           if item["unlocked"] and not item["completed"]), None)
        return {"session_id": session_id, "bank_id": session["bank_id"],
                "bank_version": bank["version"], "status": session["status"],
                "levels": levels, "next_level": next_level,
                "pending_conflicts": self._pending_conflicts(connection, session_id)}

    def session_events(self, session_id: str) -> dict[str, Any]:
        """返回会话完整事件日志与冲突记录，供后台核对。"""

        connection = self.database.connection
        self._session_context(connection, session_id)
        rows = connection.execute(
            "SELECT * FROM quiz_events WHERE session_id=? ORDER BY client_seq, received_at",
            (session_id,),
        ).fetchall()
        events = [{"event_id": row["event_id"], "client_seq": row["client_seq"],
                   "kind": row["kind"], "question_id": row["question_id"],
                   "payload": json.loads(row["payload_json"]), "status": row["status"],
                   "received_at": row["received_at"]} for row in rows]
        conflict_rows = connection.execute(
            "SELECT * FROM quiz_conflicts WHERE session_id=? ORDER BY client_seq",
            (session_id,),
        ).fetchall()
        conflicts = [{"conflict_id": row["conflict_id"], "client_seq": row["client_seq"],
                      "status": row["status"], "resolution": row["resolution"],
                      "contenders": json.loads(row["contenders_json"]),
                      "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"]}
                     for row in conflict_rows]
        return {"session_id": session_id, "events": events, "conflicts": conflicts}

    def session_sync(self, session_id: str) -> dict[str, Any]:
        """返回已归并的客户端序号，供断线终端确定补传断点。"""

        connection = self.database.connection
        self._session_context(connection, session_id)
        rows = connection.execute(
            "SELECT client_seq FROM quiz_events WHERE session_id=? AND status='effective' "
            "ORDER BY client_seq",
            (session_id,),
        ).fetchall()
        recorded = [row["client_seq"] for row in rows]
        conflicted = [row["client_seq"] for row in connection.execute(
            "SELECT client_seq FROM quiz_conflicts WHERE session_id=? AND status='pending' "
            "ORDER BY client_seq",
            (session_id,),
        ).fetchall()]
        return {"session_id": session_id, "recorded_seqs": recorded,
                "conflicted_seqs": conflicted,
                "next_seq": (recorded[-1] + 1) if recorded else 1}

    def question_view(self, session_id: str, question_id: str) -> dict[str, Any]:
        """返回面向参与者的题目视图，提示按参与者年龄过滤。"""

        connection = self.database.connection
        session, participant, _bank, questions = self._session_context(connection, session_id)
        by_id = {q["question_id"]: q for q in questions}
        question = by_id.get(question_id)
        if question is None:
            raise NotFoundError("题目不属于该会话采用的题库版本")
        if question["min_age"] > participant["age"]:
            raise PermissionDenied("题目超出参与者适龄范围")
        hints = [{"hint_id": hint["hint_id"], "text": hint["text"]}
                 for hint in question["hints"] if hint["min_age"] <= participant["age"]]
        kinds = {row["kind"] for row in self._effective_events(connection, session_id)
                 if row["question_id"] == question_id}
        return {"question_id": question_id, "level": question["level"],
                "prompt": question["prompt"],
                "knowledge_source": question["knowledge_source"],
                "points": question["points"], "hints": hints,
                "answered": "answer" in kinds, "skipped": "skip" in kinds}

    def list_conflicts(self, *, site_id: str | None = None, session_id: str | None = None,
                       status: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT c.* FROM quiz_conflicts c "
                 "JOIN quiz_sessions s ON c.session_id=s.session_id "
                 "JOIN quiz_participants p ON s.participant_id=p.participant_id WHERE 1=1")
        parameters: list[Any] = []
        if site_id:
            query += " AND p.site_id=?"
            parameters.append(site_id)
        if session_id:
            query += " AND c.session_id=?"
            parameters.append(session_id)
        if status:
            query += " AND c.status=?"
            parameters.append(status)
        query += " ORDER BY c.created_at, c.conflict_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{"conflict_id": row["conflict_id"], "session_id": row["session_id"],
                 "client_seq": row["client_seq"], "status": row["status"],
                 "resolution": row["resolution"],
                 "contenders": json.loads(row["contenders_json"]),
                 "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"],
                 "created_at": row["created_at"]} for row in rows]

    def _session_entry(self, connection, participant, session) -> dict[str, Any]:
        """汇总单个会话在排行或队伍视图中的成绩条目。"""

        settlement = connection.execute(
            "SELECT score FROM quiz_settlements WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()
        questions = self._questions_of(connection, session["bank_id"])
        events = self._effective_events(connection, session["session_id"])
        if settlement is not None:
            score = settlement["score"]
        else:
            score, _adopted = self._score_snapshot(questions, events, participant["age"])
        levels = self._progress_levels(questions, events, participant["age"])
        return {"session_id": session["session_id"], "status": session["status"],
                "score": score,
                "levels_completed": sum(1 for item in levels if item["completed"])}

    def leaderboard(self, site_id: str) -> list[dict[str, Any]]:
        """公开排行：仅包含明确同意公开且已成年的参与者，儿童明细不进入。"""

        connection = self.database.connection
        participants = connection.execute(
            "SELECT * FROM quiz_participants WHERE site_id=? AND consent_public=1 AND age>=? "
            "ORDER BY alias",
            (site_id, ADULT_AGE),
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for participant in participants:
            sessions = connection.execute(
                "SELECT * FROM quiz_sessions WHERE participant_id=? ORDER BY created_at",
                (participant["participant_id"],),
            ).fetchall()
            if not sessions:
                continue
            entries = [self._session_entry(connection, participant, session)
                       for session in sessions]
            rows.append({"alias": participant["alias"],
                         "score": max(entry["score"] for entry in entries),
                         "sessions": len(sessions),
                         "settled": sum(1 for s in sessions if s["status"] == "settled")})
        rows.sort(key=lambda item: (-item["score"], item["alias"]))
        return rows

    def team_progress(self, team_id: str) -> dict[str, Any]:
        """家庭队伍进度：只共享成员明确同意公开的进度。"""

        connection = self.database.connection
        team = connection.execute(
            "SELECT * FROM quiz_teams WHERE team_id=?", (team_id,)
        ).fetchone()
        if team is None:
            raise NotFoundError("队伍不存在")
        members = connection.execute(
            "SELECT * FROM quiz_participants WHERE team_id=? ORDER BY alias", (team_id,)
        ).fetchall()
        shared: list[dict[str, Any]] = []
        hidden = 0
        for member in members:
            if not member["consent_public"]:
                hidden += 1
                continue
            sessions = connection.execute(
                "SELECT * FROM quiz_sessions WHERE participant_id=? ORDER BY created_at",
                (member["participant_id"],),
            ).fetchall()
            entries = [self._session_entry(connection, member, session)
                       for session in sessions]
            shared.append({"alias": member["alias"], "sessions": entries})
        return {"team_id": team_id, "name": team["name"], "members": shared,
                "hidden_members": hidden}
