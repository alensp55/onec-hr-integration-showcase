from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mock-api"))

from hr_mock import (  # noqa: E402
    ExchangeClient,
    ExchangeCoordinator,
    IdentifierConflictError,
    IdentifierMap,
    MockHrService,
    QueueItem,
    QueueState,
    Response,
    safe_log_payload,
    stable_idempotency_key,
)


class ExchangeShowcaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MockHrService()
        self.client = ExchangeClient(self.service, "demo-client", "demo-credential")

    def test_document_submission_is_idempotent(self) -> None:
        key = stable_idempotency_key("hire", "employee-demo-001", 1)
        first = self.client.request("POST", "/demo/v1/hr-documents", {"kind": "hire"}, key)
        second = self.client.request("POST", "/demo/v1/hr-documents", {"kind": "hire"}, key)

        self.assertEqual(201, first.status)
        self.assertEqual(200, second.status)
        self.assertEqual(first.body["id"], second.body["id"])
        self.assertEqual("duplicate", second.body["result"])
        self.assertEqual(1, len(self.service.documents_by_key))

    def test_concurrent_duplicate_submission_creates_one_document(self) -> None:
        token = self.service.authenticate(
            {"client_id": "demo-client", "client_secret": "demo-credential"}
        ).body["session_token"]
        key = stable_idempotency_key("hire", "employee-demo-012", 1)
        barrier = Barrier(2)

        def submit() -> Response:
            barrier.wait()
            return self.service.request("POST", "/demo/v1/hr-documents", token, {"kind": "hire"}, key)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: submit(), range(2)))

        self.assertEqual([200, 201], sorted(response.status for response in responses))
        self.assertEqual(1, len(self.service.documents_by_key))
        self.assertEqual(1, len(self.service.documents_by_id))

    def test_new_revision_gets_a_new_idempotency_key(self) -> None:
        first = stable_idempotency_key("transfer", "employee-demo-002", 1)
        second = stable_idempotency_key("transfer", "employee-demo-002", 2)

        self.assertNotEqual(first, second)

    def test_idempotency_key_normalizes_kind_and_outer_spaces(self) -> None:
        canonical = stable_idempotency_key("hire", "employee-demo-011", 1)
        equivalent = stable_idempotency_key(" Hire ", " employee-demo-011 ", 1)

        self.assertEqual(canonical, equivalent)

    def test_expired_session_is_refreshed_once(self) -> None:
        self.client.authenticate()
        old_token = self.client.session_token
        self.service.invalidate(old_token or "")

        response = self.client.request(
            "POST",
            "/demo/v1/references/departments",
            {"source_id": "department-demo-001", "name": "Demo department"},
        )

        self.assertEqual(201, response.status)
        self.assertEqual(2, self.service.auth_calls)
        self.assertNotEqual(old_token, self.client.session_token)
        self.assertTrue(self.client.log_entries[-1].reauthorized)
        self.assertEqual(201, self.client.log_entries[-1].http_code)

    def test_transient_failure_is_scheduled_for_retry(self) -> None:
        fixed_now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        coordinator = ExchangeCoordinator(self.client, now=lambda: fixed_now)
        item = QueueItem("employee-demo-003", "dismissal", 1)
        self.service.transient_failures_remaining = 1

        coordinator.process(item, {"kind": "dismissal"})

        self.assertEqual(QueueState.RETRY_SCHEDULED, item.state)
        self.assertEqual(503, item.last_http_code)
        self.assertGreater(item.next_retry_at, fixed_now)

    def test_retry_budget_moves_item_to_failed(self) -> None:
        fixed_now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        coordinator = ExchangeCoordinator(self.client, now=lambda: fixed_now, max_attempts=2)
        item = QueueItem(
            "employee-demo-013",
            "dismissal",
            1,
            state=QueueState.RETRY_SCHEDULED,
            attempts=1,
            next_retry_at=fixed_now,
        )
        self.service.transient_failures_remaining = 1

        coordinator.process(item, {"kind": "dismissal"})

        self.assertEqual(QueueState.FAILED, item.state)
        self.assertEqual(2, item.attempts)
        self.assertIsNone(item.next_retry_at)

    def test_transport_timeout_is_scheduled_for_retry_and_logged(self) -> None:
        fixed_now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        coordinator = ExchangeCoordinator(self.client, now=lambda: fixed_now)
        item = QueueItem("employee-demo-014", "transfer", 1)

        with patch.object(self.service, "request", side_effect=TimeoutError):
            coordinator.process(item, {"kind": "transfer"})

        self.assertEqual(QueueState.RETRY_SCHEDULED, item.state)
        self.assertEqual(0, item.last_http_code)
        self.assertEqual("transport_error", self.client.log_entries[-1].safe_body["error"])

    def test_delivered_item_cannot_be_processed_again(self) -> None:
        coordinator = ExchangeCoordinator(self.client)
        item = QueueItem("employee-demo-015", "hire", 1)
        coordinator.process(item, {"kind": "hire"})

        with self.assertRaises(ValueError):
            coordinator.process(item, {"kind": "hire"})

    def test_retry_cannot_run_before_its_window(self) -> None:
        fixed_now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        coordinator = ExchangeCoordinator(self.client, now=lambda: fixed_now)
        item = QueueItem(
            "employee-demo-016",
            "vacation",
            1,
            state=QueueState.RETRY_SCHEDULED,
            next_retry_at=fixed_now + timedelta(minutes=1),
        )

        with self.assertRaises(ValueError):
            coordinator.process(item, {"kind": "vacation"})

    def test_successful_queue_transition_is_recorded(self) -> None:
        coordinator = ExchangeCoordinator(self.client)
        item = QueueItem("employee-demo-004", "vacation", 1)

        coordinator.process(item, {"kind": "vacation"})

        self.assertEqual(QueueState.DELIVERED, item.state)
        self.assertEqual(
            [QueueState.NEW, QueueState.PROCESSING, QueueState.DELIVERED],
            item.transitions,
        )

    def test_incoming_vacation_task_can_be_completed(self) -> None:
        task_id = self.service.seed_task(
            "vacation",
            {"employee_id": "employee-demo-005", "starts_on": "2026-02-02", "ends_on": "2026-02-06"},
        )

        tasks = self.client.request("GET", "/demo/v1/tasks")
        completed = self.client.request("POST", f"/demo/v1/tasks/{task_id}/complete")

        self.assertEqual([task_id], [item["id"] for item in tasks.body["items"]])
        self.assertEqual("completed", completed.body["state"])

    def test_attachment_is_deduplicated_by_digest(self) -> None:
        key = stable_idempotency_key("hire", "employee-demo-006", 1)
        document = self.client.request("POST", "/demo/v1/hr-documents", {"kind": "hire"}, key)
        path = f"/demo/v1/documents/{document.body['id']}/attachments"
        body = {"name": "order.pdf", "sha256": "demo-digest"}

        first = self.client.request("POST", path, body)
        second = self.client.request("POST", path, body)

        self.assertEqual(201, first.status)
        self.assertEqual(200, second.status)
        self.assertEqual(first.body["id"], second.body["id"])

    def test_log_payload_redacts_secrets_and_binary_data(self) -> None:
        payload = {
            "employee": {"personal_number": "demo-value", "display_name": "Demo Employee"},
            "authorization": "demo-value",
            "document_bytes": b"demo",
        }

        safe = safe_log_payload(payload)

        self.assertEqual("<omitted>", safe["employee"])
        self.assertEqual("<redacted>", safe["authorization"])
        self.assertEqual("<redacted>", safe["document_bytes"])

    def test_log_payload_omits_free_form_error_text(self) -> None:
        safe = safe_log_payload({"error": "Employee name must not reach the log"})

        self.assertEqual("<omitted>", safe["error"])

    def test_exchange_log_contains_only_safe_response_and_timing(self) -> None:
        response = self.client.request(
            "POST",
            "/demo/v1/references/employees",
            {"source_id": "employee-demo-010", "display_name": "Demo Employee"},
        )

        entry = self.client.log_entries[-1]
        self.assertEqual(response.status, entry.http_code)
        self.assertGreaterEqual(entry.duration_ms, 0)
        self.assertEqual("outbound", entry.direction)
        self.assertNotIn("session_token", entry.safe_body)

    def test_identifier_migration_is_idempotent(self) -> None:
        mapping = IdentifierMap()

        first = mapping.migrate("employee", "employee-demo-007", "legacy-demo-007", "remote-demo-007")
        second = mapping.migrate("employee", "employee-demo-007", "legacy-demo-007", "remote-demo-007")

        self.assertEqual(first, second)

    def test_identifier_migration_rejects_conflict(self) -> None:
        mapping = IdentifierMap()
        mapping.migrate("employee", "employee-demo-008", "legacy-demo-008", "remote-demo-008")

        with self.assertRaises(IdentifierConflictError):
            mapping.migrate("employee", "employee-demo-009", "legacy-demo-009", "remote-demo-008")

        with self.assertRaises(IdentifierConflictError):
            mapping.migrate("employee", "employee-demo-009", "legacy-demo-008", "remote-demo-009")


if __name__ == "__main__":
    unittest.main()
