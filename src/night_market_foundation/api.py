"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .quiz import QuizService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        result = _route_quiz(service, method, parsed, body, actor_id)
        if result is not None:
            return result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _dataclass_dict(value: Any) -> dict[str, Any]:
    return value.__dict__ if hasattr(value, "__dict__") else value


def _route_quiz(service: DomainService, method: str, parsed, body: dict[str, Any],
                actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """分派药材知识闯关模块的路由；不属于本模块时返回 None。"""

    if not isinstance(service, QuizService):
        return None
    query = parse_qs(parsed.query)
    path = parsed.path
    parts = [segment for segment in path.split("/") if segment]
    if method == "POST" and path == "/quiz/banks":
        receipt = service.create_bank(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/bank-versions":
        receipt = service.create_bank_version(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/bank-versions/content":
        receipt = service.replace_version_content(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/bank-versions/freeze":
        receipt = service.freeze_bank_version(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/questions/withdraw":
        receipt = service.withdraw_question(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/sessions":
        receipt = service.open_session(actor_id=actor_id, **body)
        backend = service.get_session_backend(actor_id=actor_id, session_id=receipt.resource_id)
        payload = {**receipt.__dict__, "bank_version_id": backend["bank_version_id"],
                   "status": backend["status"]}
        return 200 if receipt.replayed else 201, payload
    if method == "POST" and path == "/quiz/share-consents":
        receipt = service.set_share_consent(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/events":
        result = service.ingest_events(actor_id=actor_id, **body)
        return 200 if result.replayed else 202, _dataclass_dict(result)
    if method == "POST" and path == "/quiz/conflicts/resolve":
        receipt = service.resolve_conflict(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and path == "/quiz/sessions/finalize":
        result = service.finalize_session(actor_id=actor_id, **body)
        return 200, _dataclass_dict(result)
    if method == "GET" and path == "/quiz/conflicts":
        return 200, {"items": service.list_conflicts(actor_id=actor_id,
                                                     session_id=query.get("session_id", [None])[0])}
    if method == "GET" and path == "/quiz/leaderboard":
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        limit = int(query.get("limit", ["20"])[0])
        return 200, {"items": service.public_leaderboard(site_id=site_id, limit=limit)}
    if method == "GET" and len(parts) == 4 and parts[:2] == ["quiz", "sessions"]:
        session_id = parts[2]
        if parts[3] == "backend":
            return 200, service.get_session_backend(actor_id=actor_id, session_id=session_id)
        if parts[3] == "effective-events":
            view = query.get("view", ["operator"])[0]
            return 200, {"items": service.list_effective_events(
                actor_id=actor_id, session_id=session_id, view=view)}
        if parts[3] == "family":
            member_alias = query.get("member_alias", [""])[0]
            return 200, service.family_view(session_id=session_id, member_alias=member_alias)
        if parts[3] == "child":
            member_alias = query.get("member_alias", [""])[0]
            return 200, service.child_view(session_id=session_id, member_alias=member_alias)
    if method == "GET" and len(parts) == 3 and parts[:2] == ["quiz", "banks"]:
        return 200, {"items": service.list_bank_versions(actor_id=actor_id, bank_id=parts[2])}
    if method == "GET" and len(parts) == 3 and parts[:2] == ["quiz", "bank-versions"]:
        return 200, service.get_bank_version(actor_id=actor_id, version_id=parts[2])
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = QuizService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
