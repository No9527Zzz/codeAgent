"""CodeAgent 本地任务和事件持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with closing(self._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.commit()
            connection.execute("""
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, task: dict[str, Any]) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO tasks(id, status, title, payload, updated_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, title=excluded.title, payload=excluded.payload, updated_at=excluded.updated_at",
                (task["id"], task.get("status", "unknown"), task.get("title", "未命名任务"), json.dumps(task, ensure_ascii=False), timestamp),
            )
            connection.commit()

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.lock, closing(self._connect()) as connection:
            rows = connection.execute("SELECT payload FROM tasks ORDER BY updated_at DESC").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def add_event(self, task_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {"task_id": task_id, "type": event_type, "message": message, "payload": payload or {}, "created_at": datetime.now(timezone.utc).isoformat()}
        with self.lock, closing(self._connect()) as connection:
            cursor = connection.execute("INSERT INTO task_events(task_id, event_type, message, payload, created_at) VALUES(?, ?, ?, ?, ?)", (task_id, event_type, message, json.dumps(event["payload"], ensure_ascii=False), event["created_at"]))
            connection.commit()
            event["id"] = cursor.lastrowid
        return event

    def events(self, task_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.lock, closing(self._connect()) as connection:
            rows = connection.execute("SELECT id, task_id, event_type, message, payload, created_at FROM task_events WHERE task_id = ? AND id > ? ORDER BY id", (task_id, after_id)).fetchall()
        return [{"id": row["id"], "task_id": row["task_id"], "type": row["event_type"], "message": row["message"], "payload": json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]
