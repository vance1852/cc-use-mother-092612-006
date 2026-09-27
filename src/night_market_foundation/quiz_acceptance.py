"""药材知识闯关模块的离线端到端验收。

在临时 SQLite 数据库中完整演练：冻结题库 → 开启独立会话 → 断网乱序补传 →
同序号分叉保留冲突 → 运营裁决 → 结算 → 撤题换版不影响已结束成绩 →
隐私视图核对，并校验审计链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .quiz import QuizService
from .storage import Database

LEVELS = [
    {"code": "L1", "title": "初识本草", "prerequisites": []},
    {"code": "L2", "title": "配伍入门", "prerequisites": ["L1"]},
]
QUESTIONS = [
    {"code": "q1", "level_code": "L1", "prompt": "人参的功效",
     "options": ["大补元气", "发汗解表"], "answer_key": "大补元气",
     "knowledge_source": "神农本草经", "min_age": 0},
    {"code": "q2", "level_code": "L1", "prompt": "甘草的作用",
     "options": ["调和诸药", "软坚散结"], "answer_key": "调和诸药",
     "knowledge_source": "本草纲目", "min_age": 6},
    {"code": "q3", "level_code": "L2", "prompt": "四君子汤",
     "options": ["参术苓草", "麻桂姜枣"], "answer_key": "参术苓草",
     "knowledge_source": "太平惠民和剂局方", "min_age": 8},
]


def _answer(seq, question, answer, alias):
    return {"client_seq": seq, "event_kind": "answer", "question_code": question,
            "answer": answer, "member_alias": alias}


def run() -> dict[str, object]:
    """执行闯关全链路验收并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "quiz_acceptance.sqlite3")
        service = QuizService(database, FixedClock(datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范活动机构")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="活动负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="文化展示区",
                              timezone_name="Asia/Shanghai")
        service.create_bank(request_id="bank", actor_id="operator-001", bank_id="bank-001",
                            site_id="site-001", name="药材知识闯关题库")
        service.create_bank_version(request_id="version", actor_id="operator-001", bank_id="bank-001")
        service.replace_version_content(request_id="content", actor_id="operator-001",
                                        version_id="bank-001-v1",
                                        levels=LEVELS, questions=QUESTIONS)
        service.freeze_bank_version(request_id="freeze", actor_id="operator-001",
                                    version_id="bank-001-v1")
        frozen_version = service.get_bank_version(actor_id="operator-001",
                                                  version_id="bank-001-v1")
        service.open_session(
            request_id="session", actor_id="operator-001", session_id="session-001",
            site_id="site-001", bank_id="bank-001", family_alias="山药一家",
            members=[
                {"member_alias": "山爸", "is_child": False, "age": 38, "share_consent": True},
                {"member_alias": "小豆", "is_child": True, "age": 5, "share_consent": False},
            ],
        )
        # 断网后乱序补传：q3 先到，L1 完成后自动并入有效集。
        service.ingest_events(request_id="batch-3", actor_id="operator-001",
                              session_id="session-001", batch_id="batch-3",
                              events=[_answer(3, "q3", "参术苓草", "山爸")])
        service.ingest_events(request_id="batch-1", actor_id="operator-001",
                              session_id="session-001", batch_id="batch-1",
                              events=[_answer(1, "q1", "大补元气", "山爸")])
        # 儿童超龄提示被拒绝。
        hint = service.ingest_events(request_id="hint", actor_id="operator-001",
                                     session_id="session-001", batch_id="hint",
                                     events=[{"client_seq": 2, "event_kind": "hint",
                                              "question_code": "q2", "member_alias": "小豆"}])
        # q1 同序号分叉：保留两份，运营裁决采用正确答案。
        service.ingest_events(request_id="batch-2", actor_id="operator-001",
                              session_id="session-001", batch_id="batch-2",
                              events=[_answer(4, "q1", "大补元气", "山爸")])
        diverged = service.ingest_events(request_id="batch-2b", actor_id="operator-001",
                                         session_id="session-001", batch_id="batch-2b",
                                         events=[_answer(4, "q1", "发汗解表", "山爸")])
        conflicts = service.list_conflicts(actor_id="operator-001", session_id="session-001")
        chosen = next(variant["event_id"] for variant in conflicts[0]["variants"]
                      if variant["payload"]["answer"] == "大补元气")
        service.resolve_conflict(request_id="resolve", actor_id="operator-001",
                                 conflict_id=conflicts[0]["conflict_id"],
                                 chosen_event_id=chosen, note="终端A补传时间更早")
        service.ingest_events(request_id="batch-skip", actor_id="operator-001",
                              session_id="session-001", batch_id="batch-skip",
                              events=[{"client_seq": 5, "event_kind": "skip",
                                       "question_code": "q2", "member_alias": "山爸"}])
        settlement = service.finalize_session(request_id="finalize", actor_id="operator-001",
                                              session_id="session-001")
        effective = service.list_effective_events(actor_id="operator-001",
                                                  session_id="session-001")
        # 撤题只影响新会话：已结束成绩保持 2 分不变。
        service.withdraw_question(request_id="withdraw", actor_id="operator-001",
                                  bank_id="bank-001", question_code="q3")
        backend = service.get_session_backend(actor_id="operator-001", session_id="session-001")
        child = service.child_view(session_id="session-001", member_alias="小豆")
        board = service.public_leaderboard(site_id="site-001")
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "frozen_content_hash": frozen_version["content_hash"],
            "hint_rejected": hint.results[0]["status"] == "rejected",
            "divergence_conflicted": diverged.results[0]["status"] == "conflicted",
            "conflict_variants": len(conflicts[0]["variants"]),
            "settled_score": settlement.score,
            "effective_event_count": settlement.effective_event_count,
            "effective_client_seqs": [event["client_seq"] for event in effective],
            "score_after_withdraw": backend["score"],
            "child_question_codes": [question["code"] for question in child["questions"]],
            "leaderboard_size": len(board),
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (
        result["status"] == "ok"
        and result["audit_valid"]
        and result["hint_rejected"]
        and result["divergence_conflicted"]
        and result["conflict_variants"] == 2
        and result["settled_score"] == 2
        and result["score_after_withdraw"] == 2
        and result["child_question_codes"] == ["q1"]
        and result["leaderboard_size"] == 1
        and result["effective_client_seqs"] == [1, 3, 4, 5]
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
