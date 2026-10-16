from __future__ import annotations

import hashlib
import itertools
import re
import time
from threading import RLock
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Callable


@dataclass(frozen=True)
class Response:
    status: int
    body: dict[str, Any]


class QueueState(StrEnum):
    NEW = "new"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True)
class ExchangeLogEntry:
    operation: str
    direction: str
    http_code: int
    duration_ms: int
    safe_body: dict[str, Any]
    reauthorized: bool


@dataclass
class QueueItem:
    source_id: str
    kind: str
    revision: int
    state: QueueState = QueueState.NEW
    attempts: int = 0
    next_retry_at: datetime | None = None
    remote_id: str | None = None
    last_http_code: int | None = None
    transitions: list[QueueState] = field(default_factory=lambda: [QueueState.NEW])

    def transition(self, state: QueueState) -> None:
        allowed = {
            QueueState.NEW: {QueueState.PROCESSING},
            QueueState.PROCESSING: {
                QueueState.DELIVERED,
                QueueState.FAILED,
                QueueState.RETRY_SCHEDULED,
            },
            QueueState.RETRY_SCHEDULED: {QueueState.PROCESSING},
        }
        if state not in allowed.get(self.state, set()):
            raise ValueError(f"Invalid queue transition: {self.state} -> {state}")
        self.state = state
        self.transitions.append(state)


class IdentifierConflictError(ValueError):
    pass


@dataclass(frozen=True)
class IdentifierMapping:
    entity_type: str
    source_id: str
    legacy_id: str
    remote_id: str


class IdentifierMap:
    def __init__(self) -> None:
        self._lock = RLock()
        self._by_legacy: dict[tuple[str, str], IdentifierMapping] = {}
        self._by_remote: dict[tuple[str, str], IdentifierMapping] = {}
        self._by_source: dict[tuple[str, str], IdentifierMapping] = {}

    def migrate(self, entity_type: str, source_id: str, legacy_id: str, remote_id: str) -> IdentifierMapping:
        if not legacy_id or not remote_id:
            raise ValueError("Both identifiers are required")
        with self._lock:
            legacy_key = (entity_type, legacy_id)
            remote_key = (entity_type, remote_id)
            source_key = (entity_type, source_id)
            legacy_mapping = self._by_legacy.get(legacy_key)
            remote_mapping = self._by_remote.get(remote_key)
            source_mapping = self._by_source.get(source_key)
            if legacy_mapping and legacy_mapping.source_id != source_id:
                raise IdentifierConflictError("Legacy identifier belongs to another source object")
            if remote_mapping and remote_mapping.source_id != source_id:
                raise IdentifierConflictError("Remote identifier belongs to another source object")
            if source_mapping and source_mapping.remote_id != remote_id:
                raise IdentifierConflictError("Source object already has another remote identifier")
            mapping = IdentifierMapping(entity_type, source_id, legacy_id, remote_id)
            self._by_legacy[legacy_key] = mapping
            self._by_remote[remote_key] = mapping
            self._by_source[source_key] = mapping
            return mapping


def stable_idempotency_key(kind: str, source_id: str, revision: int) -> str:
    canonical = f"{kind.strip().lower()}|{source_id.strip()}|{revision}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def safe_log_payload(value: Any) -> Any:
    hidden_names = {
        "authorization",
        "client_secret",
        "document_bytes",
        "password",
        "personal_number",
        "refresh_token",
        "session_token",
        "tax_id",
    }
    if isinstance(value, dict):
        safe_names = {"error", "expires_in", "result", "state", "status"}
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower()
            if normalized in hidden_names:
                result[key] = "<redacted>"
            elif normalized in safe_names:
                result[key] = safe_log_payload(item)
            elif normalized == "id":
                result[key] = "<identifier>"
            else:
                result[key] = "<omitted>"
        return result
    if isinstance(value, list):
        return f"<list:{len(value)} items>"
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, str):
        return value if re.fullmatch(r"[a-z0-9_:-]{1,64}", value) else "<omitted>"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return "<omitted>"


class MockHrService:
    """In-memory service with an invented contract. It is not a copy of a vendor API."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = itertools.count(1)
        self.sessions: set[str] = set()
        self.references: dict[str, dict[str, dict[str, Any]]] = {}
        self.documents_by_key: dict[str, dict[str, Any]] = {}
        self.documents_by_id: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.transient_failures_remaining = 0
        self.auth_calls = 0

    def authenticate(self, payload: dict[str, Any]) -> Response:
        if not payload.get("client_id") or not payload.get("client_secret"):
            return Response(400, {"error": "demo_credentials_required"})
        with self._lock:
            self.auth_calls += 1
            token = f"demo-session-{next(self._sequence)}"
            self.sessions.add(token)
            return Response(201, {"session_token": token, "expires_in": 300})

    def invalidate(self, token: str) -> None:
        with self._lock:
            self.sessions.discard(token)

    def seed_task(self, kind: str, payload: dict[str, Any]) -> str:
        with self._lock:
            task_id = f"task-{next(self._sequence)}"
            self.tasks[task_id] = {"id": task_id, "kind": kind, "payload": payload, "state": "ready"}
            return task_id

    def request(
        self,
        method: str,
        path: str,
        token: str | None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        with self._lock:
            body = body or {}
            if token not in self.sessions:
                return Response(401, {"error": "session_expired"})
            if self.transient_failures_remaining > 0:
                self.transient_failures_remaining -= 1
                return Response(503, {"error": "temporarily_unavailable"})

            segments = [segment for segment in path.split("/") if segment]
            if method == "POST" and segments[:3] == ["demo", "v1", "references"] and len(segments) == 4:
                return self._upsert_reference(segments[3], body)
            if method == "POST" and segments == ["demo", "v1", "hr-documents"]:
                return self._submit_document(body, idempotency_key)
            if method == "GET" and segments == ["demo", "v1", "tasks"]:
                tasks = [task for task in self.tasks.values() if task["state"] == "ready"]
                return Response(200, {"items": tasks})
            if method == "POST" and len(segments) == 5 and segments[:3] == ["demo", "v1", "tasks"]:
                if segments[4] == "complete":
                    return self._complete_task(segments[3])
            if method == "POST" and len(segments) == 5 and segments[:3] == ["demo", "v1", "documents"]:
                if segments[4] == "attachments":
                    return self._add_attachment(segments[3], body)
            return Response(404, {"error": "route_not_found"})

    def _upsert_reference(self, kind: str, body: dict[str, Any]) -> Response:
        source_id = body.get("source_id")
        if not source_id:
            return Response(422, {"error": "source_id_required"})
        bucket = self.references.setdefault(kind, {})
        existing = bucket.get(source_id)
        if existing:
            existing["data"] = body
            return Response(200, {"id": existing["id"], "result": "updated"})
        remote_id = f"{kind}-{next(self._sequence)}"
        bucket[source_id] = {"id": remote_id, "data": body}
        return Response(201, {"id": remote_id, "result": "created"})

    def _submit_document(self, body: dict[str, Any], idempotency_key: str | None) -> Response:
        if not idempotency_key:
            return Response(400, {"error": "idempotency_key_required"})
        existing = self.documents_by_key.get(idempotency_key)
        if existing:
            return Response(200, {"id": existing["id"], "result": "duplicate"})
        remote_id = f"document-{next(self._sequence)}"
        document = {"id": remote_id, "data": body, "attachments": []}
        self.documents_by_key[idempotency_key] = document
        self.documents_by_id[remote_id] = document
        return Response(201, {"id": remote_id, "result": "created"})

    def _complete_task(self, task_id: str) -> Response:
        task = self.tasks.get(task_id)
        if not task:
            return Response(404, {"error": "task_not_found"})
        task["state"] = "completed"
        return Response(200, {"id": task_id, "state": "completed"})

    def _add_attachment(self, document_id: str, body: dict[str, Any]) -> Response:
        document = self.documents_by_id.get(document_id)
        if not document:
            return Response(404, {"error": "document_not_found"})
        digest = body.get("sha256")
        if not digest:
            return Response(422, {"error": "sha256_required"})
        for attachment in document["attachments"]:
            if attachment["sha256"] == digest:
                return Response(200, {"id": attachment["id"], "result": "duplicate"})
        attachment_id = f"attachment-{next(self._sequence)}"
        document["attachments"].append({"id": attachment_id, **body})
        return Response(201, {"id": attachment_id, "result": "created"})


class ExchangeClient:
    def __init__(self, service: MockHrService, client_id: str, client_secret: str) -> None:
        self.service = service
        self.client_id = client_id
        self.client_secret = client_secret
        self.session_token: str | None = None
        self.log_entries: list[ExchangeLogEntry] = []

    def authenticate(self) -> None:
        response = self.service.authenticate(
            {"client_id": self.client_id, "client_secret": self.client_secret}
        )
        if response.status != 201:
            raise RuntimeError("Demo authentication failed")
        self.session_token = response.body["session_token"]

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        started_at = time.perf_counter()
        reauthorized = False
        try:
            if self.session_token is None:
                self.authenticate()
            response = self.service.request(method, path, self.session_token, body, idempotency_key)
            if response.status == 401:
                self.authenticate()
                reauthorized = True
                response = self.service.request(method, path, self.session_token, body, idempotency_key)
        except (ConnectionError, TimeoutError):
            duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
            self.log_entries.append(
                ExchangeLogEntry(
                    operation=path,
                    direction="outbound",
                    http_code=0,
                    duration_ms=duration_ms,
                    safe_body={"error": "transport_error"},
                    reauthorized=reauthorized,
                )
            )
            raise
        duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        self.log_entries.append(
            ExchangeLogEntry(
                operation=path,
                direction="outbound",
                http_code=response.status,
                duration_ms=duration_ms,
                safe_body=safe_log_payload(response.body),
                reauthorized=reauthorized,
            )
        )
        return response


class ExchangeCoordinator:
    def __init__(
        self,
        client: ExchangeClient,
        now: Callable[[], datetime] | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.client = client
        self.now = now or (lambda: datetime.now(UTC))
        self.max_attempts = max_attempts

    def process(self, item: QueueItem, payload: dict[str, Any]) -> QueueItem:
        current_time = self.now()
        if item.state not in {QueueState.NEW, QueueState.RETRY_SCHEDULED}:
            raise ValueError(f"Queue item cannot be processed from state {item.state}")
        if item.state == QueueState.RETRY_SCHEDULED and item.next_retry_at:
            if current_time < item.next_retry_at:
                raise ValueError("Retry window has not started")
        item.attempts += 1
        item.transition(QueueState.PROCESSING)
        item.next_retry_at = None
        key = stable_idempotency_key(item.kind, item.source_id, item.revision)
        try:
            response = self.client.request("POST", "/demo/v1/hr-documents", payload, key)
        except (ConnectionError, TimeoutError):
            item.last_http_code = 0
            self._handle_transient_failure(item, current_time)
            return item
        item.last_http_code = response.status

        if response.status in {200, 201}:
            item.remote_id = response.body["id"]
            item.next_retry_at = None
            item.transition(QueueState.DELIVERED)
        elif response.status in {408, 429} or response.status >= 500:
            self._handle_transient_failure(item, current_time)
        else:
            item.transition(QueueState.FAILED)
        return item

    def _handle_transient_failure(self, item: QueueItem, current_time: datetime) -> None:
        if item.attempts >= self.max_attempts:
            item.next_retry_at = None
            item.transition(QueueState.FAILED)
            return
        delay = min(2 ** item.attempts, 60)
        item.next_retry_at = current_time + timedelta(minutes=delay)
        item.transition(QueueState.RETRY_SCHEDULED)
