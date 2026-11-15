"""Durable task state and delivery outbox, separate from human-readable audit logs."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from robotsoul.models import (
    AuditRecord, Command, ConversationContext, Evaluation, Feedback, FeedbackStatus,
    InvocationContext, InvocationSource, Task, TaskResult, TaskStatus, utc_now,
)


TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED})


def encode(value: Any) -> str:
    def default(item: Any) -> Any:
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"Cannot persist {type(item).__name__}")
    return json.dumps(value, default=default, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_result(raw: str) -> TaskResult:
    item = json.loads(raw)
    task = item["task"]
    task["status"] = TaskStatus(task["status"])
    for key in ("created_at", "updated_at"):
        task[key] = datetime.fromisoformat(task[key])
    feedback = []
    for value in item["feedback"]:
        feedback.append(_decode_feedback(value))
    evaluation = item.get("evaluation")
    if evaluation is not None:
        evaluation["evidence"] = tuple(evaluation["evidence"])
        evaluation = Evaluation(**evaluation)
    history = []
    for record in item["history"]:
        record["timestamp"] = datetime.fromisoformat(record["timestamp"])
        history.append(AuditRecord(**record))
    return TaskResult(Task(**task), tuple(feedback), evaluation, tuple(history), item.get("reply", ""))


def _decode_feedback(value: dict[str, Any]) -> Feedback:
    value = dict(value)
    value["timestamp"] = datetime.fromisoformat(value["timestamp"])
    value["status"] = FeedbackStatus(value["status"])
    return Feedback(**value)


class TaskStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._initialize()
        except Exception:
            self._db.close()
            raise

    def _initialize(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, conversation TEXT NOT NULL, invocation TEXT NOT NULL,
                result TEXT NOT NULL, metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                task_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(task_id,id)
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, feedback TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS resume_events (
                task_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(task_id,id)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            );
        """)
        self._recover()

    def _recover(self) -> None:
        with self._lock, self._db:
            for task_id, raw in self._db.execute("SELECT id,result FROM tasks").fetchall():
                result = decode_result(raw)
                if result.task.status not in TERMINAL:
                    feedback = list(result.feedback)
                    seen = {item.feedback_id for item in feedback}
                    for (command_feedback,) in self._db.execute("SELECT feedback FROM commands WHERE task_id=? ORDER BY rowid", (task_id,)):
                        for value in json.loads(command_feedback):
                            if value["feedback_id"] not in seen:
                                feedback.append(_decode_feedback(value))
                                seen.add(value["feedback_id"])
                    result.task.status = TaskStatus.INTERRUPTED
                    result.task.error = "Runtime restarted; previous execution outcome must be checked before continuing"
                    result.task.updated_at = utc_now()
                    result = TaskResult(result.task, tuple(feedback), None, result.history, result.task.error)
                    self._db.execute("UPDATE tasks SET result=? WHERE id=?", (encode(result), task_id))
            self._db.execute("UPDATE commands SET status='unknown' WHERE status='dispatched'")

    def create(self, result: TaskResult, conversation: ConversationContext, invocation: InvocationContext, metadata: dict[str, Any]) -> bool:
        with self._lock, self._db:
            row = self._db.execute("SELECT result,conversation,invocation,metadata FROM tasks WHERE id=?", (result.task.task_id,)).fetchone()
            if row is not None:
                previous = decode_result(row[0])
                if previous.task.user_input != result.task.user_input or row[1] != encode(conversation) or row[2] != encode(invocation):
                    raise ValueError("Task ID already belongs to a different request")
                original = json.loads(row[3])
                if any(original.get(key) != metadata.get(key) for key in ("goal_kind", "expected_evaluator", "request_intent")):
                    raise ValueError("Task ID already belongs to a different goal or action request")
                return False
            self._db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (
                result.task.task_id, encode(conversation), encode(invocation), encode(result), encode(metadata),
            ))
            return True

    def save(self, result: TaskResult, metadata: dict[str, Any], *, note: tuple[str, dict[str, Any]] | None = None) -> None:
        with self._lock, self._db:
            cursor = self._db.execute("UPDATE tasks SET result=?,metadata=? WHERE id=?", (encode(result), encode(metadata), result.task.task_id))
            if cursor.rowcount != 1:
                raise ValueError("Cannot save an unknown task")
            if note is not None:
                self._enqueue_note(note[0], result.task.task_id, note[1])

    def load_all(self) -> list[tuple[TaskResult, ConversationContext, InvocationContext, dict[str, Any]]]:
        with self._lock:
            records = self._db.execute("SELECT result,conversation,invocation,metadata FROM tasks").fetchall()
        results = []
        for result, conversation, invocation, metadata in records:
            inv = json.loads(invocation)
            inv["source"] = InvocationSource(inv["source"])
            results.append((decode_result(result), ConversationContext(**json.loads(conversation)), InvocationContext(**inv), json.loads(metadata)))
        return results

    def decision(self, task_id: str, decision_id: str, decision: Any) -> bool:
        return self._unique_record("decisions", task_id, decision_id, encode(decision))

    def resume_event(self, task_id: str, event_id: str, payload: Any) -> bool:
        return self._unique_record("resume_events", task_id, event_id, encode(payload))

    def has_resume_event(self, task_id: str, event_id: str, payload: Any) -> bool:
        with self._lock:
            row = self._db.execute("SELECT payload FROM resume_events WHERE task_id=? AND id=?", (task_id, event_id)).fetchone()
        if row is not None and row[0] != encode(payload):
            raise ValueError("Record ID was reused with different content")
        return row is not None

    def _unique_record(self, table: str, task_id: str, record_id: str, payload: str) -> bool:
        # Table names are internal constants, never model/user input.
        with self._lock, self._db:
            row = self._db.execute(f"SELECT payload FROM {table} WHERE task_id=? AND id=?", (task_id, record_id)).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ValueError("Record ID was reused with different content")
                return False
            self._db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (task_id, record_id, payload))
            return True

    def dispatch(self, command: Command) -> None:
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM tasks WHERE id=?", (command.task_id,)).fetchone():
                raise ValueError("Cannot dispatch a command for an unknown task")
            if self._db.execute("SELECT 1 FROM commands WHERE id=?", (command.command_id,)).fetchone():
                raise ValueError("Command has already been dispatched; replay is prohibited")
            self._db.execute("INSERT INTO commands(id,task_id,payload,status) VALUES(?,?,?,'dispatched')", (
                command.command_id, command.task_id, encode(command),
            ))

    def finish_command(self, command_id: str, feedback: tuple[Feedback, ...]) -> None:
        terminal = next((item for item in reversed(feedback) if item.command_id == command_id and item.status in {
            FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED, FeedbackStatus.CANCELLED,
        }), None)
        uncertain = terminal is not None and terminal.status == FeedbackStatus.FAILED and (
            terminal.evidence.get("outcome_known") is False
            or terminal.error_code in {"timeout", "deadline_expired", "device_error", "cancel_failed", "malformed_cancel_feedback"}
        )
        with self._lock, self._db:
            self._db.execute("UPDATE commands SET status=?,feedback=? WHERE id=?", (
                terminal.status.value if terminal is not None and not uncertain else "unknown", encode(feedback), command_id,
            ))

    def command_ids(self, task_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(row[0] for row in self._db.execute("SELECT id FROM commands WHERE task_id=?", (task_id,)))

    def unknown_command_ids(self, task_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(row[0] for row in self._db.execute(
                "SELECT id FROM commands WHERE task_id=? AND status IN ('dispatched','unknown')", (task_id,),
            ))

    def enqueue_note(self, event_id: str, task_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._enqueue_note(event_id, task_id, payload)

    def _enqueue_note(self, event_id: str, task_id: str, payload: dict[str, Any]) -> None:
        encoded = encode(payload)
        row = self._db.execute("SELECT payload,task_id FROM outbox WHERE id=?", (event_id,)).fetchone()
        if row and (row[0] != encoded or row[1] != task_id):
            raise ValueError("Note event ID reused with different content")
        self._db.execute("INSERT OR IGNORE INTO outbox(id,task_id,payload) VALUES(?,?,?)", (event_id, task_id, encoded))

    def pending_notes(self) -> list[tuple[str, str, dict[str, Any]]]:
        with self._lock:
            rows = self._db.execute("SELECT id,task_id,payload FROM outbox WHERE delivered=0 ORDER BY rowid").fetchall()
        return [(event_id, task_id, json.loads(payload)) for event_id, task_id, payload in rows]

    def delivered(self, event_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (event_id,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
