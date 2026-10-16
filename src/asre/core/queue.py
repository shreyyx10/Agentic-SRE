"""Hand-off from INGEST to ANALYSE through Cloud Tasks.

Ingest does the cheap, fast work (authenticate, normalise, de-duplicate) and
returns in well under a second; the slow work (evidence, model, delivery) runs
in ANALYSE with its own timeout and Cloud Tasks' retry and backoff (P1-7).
Tasks call ANALYSE with an OIDC token for a dedicated invoker account.

Cold start. The first call from a new instance opens the gRPC channel and
fetches an access token, which can take well over the steady-state budget
(observed: 504 Deadline Exceeded at 500 ms). Until one call has succeeded, the
queue allows ``first_timeout_s``; after that, ``timeout_s``.

Idempotent hand-off. A client-side deadline does not mean the task was not
created: in the step-11 live tests (7 Oct 2026) every enqueue reported 504
while the Brief appeared ~0.5 s later. Ingest then nacked, Pub/Sub redelivered
and each redelivery created another task. A caller can therefore pass a
``task_id`` derived from the incident; Cloud Tasks refuses a second task with
the same name (AlreadyExists), which this queue treats as success, so a retried
hand-off confirms the first one instead of duplicating it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Protocol


def task_id_for(kind: str, customer_id: str, group_key: str, incident_id: str) -> str:
    """Stable Cloud Tasks task ID for one hand-off of one incident.

    Hashed so names are evenly distributed (Cloud Tasks recommends against
    sequential prefixes) and fit the ``[A-Za-z0-9_-]{1,500}`` name rule.
    """
    digest = hashlib.sha256(f"{customer_id}|{group_key}|{incident_id}|{kind}".encode()).hexdigest()
    return f"{kind}-{digest[:40]}"


class TaskQueue(Protocol):
    def enqueue(
        self, payload: dict[str, Any], trace_header: str | None = None, task_id: str | None = None
    ) -> str:
        """Create a task; return its name. Raise on failure.

        With ``task_id``, a task of that name that already exists counts as success.
        """


class CloudTasksQueue:
    def __init__(
        self,
        queue_path: str,
        target_url: str,
        invoker_service_account: str,
        audience: str,
        timeout_s: float = 0.5,
        first_timeout_s: float = 5.0,
    ) -> None:
        from google.cloud import tasks_v2

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksClient()
        self._queue_path = queue_path
        self._url = target_url
        self._sa = invoker_service_account
        self._audience = audience
        self._timeout = timeout_s
        self._first_timeout = max(first_timeout_s, timeout_s)
        self._warm = False  # a benign race: concurrent first calls all get the long budget

    def enqueue(
        self, payload: dict[str, Any], trace_header: str | None = None, task_id: str | None = None
    ) -> str:
        from google.api_core.exceptions import AlreadyExists

        headers = {"Content-Type": "application/json"}
        if trace_header:
            headers["X-Cloud-Trace-Context"] = trace_header
        name = f"{self._queue_path}/tasks/{task_id}" if task_id else None
        task = self._tasks_v2.Task(
            name=name,
            http_request=self._tasks_v2.HttpRequest(
                http_method=self._tasks_v2.HttpMethod.POST,
                url=self._url,
                headers=headers,
                body=json.dumps(payload, default=str).encode("utf-8"),
                oidc_token=self._tasks_v2.OidcToken(service_account_email=self._sa, audience=self._audience),
            ),
        )
        timeout = self._timeout if self._warm else self._first_timeout
        try:
            created = self._client.create_task(parent=self._queue_path, task=task, timeout=timeout)
        except AlreadyExists:
            if name is None:  # pragma: no cover — unnamed tasks cannot collide
                raise
            self._warm = True
            return name  # an earlier attempt created it: the hand-off already happened
        self._warm = True
        return created.name


class InMemoryQueue:
    """For tests and local runs."""

    def __init__(self, fail: bool = False, time_out_after_create: bool = False) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.fail = fail
        # Simulates the live failure: the task is created, but the caller's deadline expires.
        self.time_out_after_create = time_out_after_create
        self._names: set[str] = set()
        self._lock = threading.Lock()

    def enqueue(
        self, payload: dict[str, Any], trace_header: str | None = None, task_id: str | None = None
    ) -> str:
        if self.fail:
            raise RuntimeError("queue unavailable")
        with self._lock:
            if task_id is not None and task_id in self._names:
                return task_id  # AlreadyExists: treated as success, no second task
            if task_id is not None:
                self._names.add(task_id)
            self.tasks.append(
                {
                    "payload": json.loads(json.dumps(payload, default=str)),
                    "trace": trace_header,
                    "task_id": task_id,
                }
            )
            name = task_id or f"task-{len(self.tasks)}"
        if self.time_out_after_create:
            raise TimeoutError("504 Deadline Exceeded")
        return name
