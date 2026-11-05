"""CodeAgent 本地 HTTP 服务。"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__:
    from .agent import analyze_task, confirm_code_change, confirm_plan, confirm_task, draft_code_change, inspect_project, plan_change, retry_tests
    from .storage import TaskStore
    from .new_project import plan_project, clarify_project, approve_project_design, confirm_project, generate_business_draft
else:
    from agent import analyze_task, confirm_code_change, confirm_plan, confirm_task, draft_code_change, inspect_project, plan_change, retry_tests
    from storage import TaskStore
    from new_project import plan_project, clarify_project, approve_project_design, confirm_project, generate_business_draft

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "agent.yaml"
HOST, PORT = "127.0.0.1", 8765
AUTHORIZATION_MODE = "ask"
TASKS: dict[str, dict[str, Any]] = {}
STORE = TaskStore(Path(os.environ.get("CODEAGENT_DATABASE", str(ROOT / "data" / "codeagent.db"))))
SUBSCRIBERS: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
SUBSCRIBERS_LOCK = threading.RLock()
EXECUTION_LOCK = threading.Lock()

for persisted_task in STORE.list_tasks():
    TASKS[persisted_task["id"]] = persisted_task


def save_task(task: dict[str, Any], message: str | None = None, event_type: str = "state") -> None:
    TASKS[task["id"]] = task
    STORE.save(task)
    if message:
        publish_event(task["id"], event_type, message, {"status": task.get("status")})


def publish_event(task_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
    event = STORE.add_event(task_id, event_type, message, payload)
    with SUBSCRIBERS_LOCK:
        subscribers = list(SUBSCRIBERS.get(task_id, []))
    for subscriber in subscribers:
        subscriber.put(event)


def parse_simple_yaml(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    if not path.exists(): return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if not raw.startswith(" ") and line.endswith(":"):
            section = {}; result[line[:-1]] = section; continue
        if section is not None and ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            value = value.strip('"\'')
            if value.lower() in {"true", "false"}: value = value.lower() == "true"
            else:
                try: value = float(value) if "." in value else int(value)
                except ValueError: pass
            section[key] = value
    return result


def config() -> dict[str, Any]:
    result = parse_simple_yaml(CONFIG_FILE)
    llm = result.setdefault("llm", {})
    env_name = llm.get("api_key_env", "OPENAI_API_KEY")
    llm["api_key_configured"] = bool(os.getenv(env_name))
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "CodeAgent/0.2"

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Access-Control-Allow-Headers", "Content-Type"); self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS"); self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/health": self.send_json(200, {"ok": True, "service": "codeagent", "time": datetime.now().isoformat()})
        elif route == "/api/config": self.send_json(200, {"config": config(), "authorization_mode": AUTHORIZATION_MODE})
        elif route == "/api/tasks": self.send_json(200, {"tasks": STORE.list_tasks()})
        elif route.startswith("/api/tasks/") and route.endswith("/events"):
            self.stream_events(route.split("/")[3])
        elif route.startswith("/api/tasks/") and route.endswith("/preview"):
            task = STORE.get(route.split("/")[3]); self.send_json(200 if task else 404, {"task": task} if task else {"error": "任务不存在"})
        else: self.send_json(404, {"error": "接口不存在"})

    def stream_events(self, task_id: str) -> None:
        if not STORE.get(task_id):
            self.send_json(404, {"error": "任务不存在"})
            return
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with SUBSCRIBERS_LOCK:
            SUBSCRIBERS.setdefault(task_id, []).append(subscriber)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for event in STORE.events(task_id):
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")); self.wfile.flush()
            while True:
                try: event = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n"); self.wfile.flush(); continue
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with SUBSCRIBERS_LOCK:
                if task_id in SUBSCRIBERS and subscriber in SUBSCRIBERS[task_id]: SUBSCRIBERS[task_id].remove(subscriber)

    def do_POST(self) -> None:  # noqa: N802
        if not EXECUTION_LOCK.acquire(blocking=False):
            self.send_json(409, {"error": "已有操作正在执行，请等待完成后重试"})
            return
        try:
            self.handle_post()
        finally:
            EXECUTION_LOCK.release()

    def handle_post(self) -> None:
        task = None
        try: payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except json.JSONDecodeError: self.send_json(400, {"error": "请求体不是有效 JSON"}); return
        route = urlparse(self.path).path
        try:
            if route == "/api/projects/inspect": self.send_json(200, {"project": inspect_project(payload["path"])})
            elif route == "/api/authorization":
                global AUTHORIZATION_MODE
                if payload.get("mode") not in {"ask", "task", "auto"}: raise ValueError("授权模式必须是 ask、task 或 auto")
                AUTHORIZATION_MODE = payload["mode"]; self.send_json(200, {"authorization_mode": AUTHORIZATION_MODE})
            elif route == "/api/tasks/analyze":
                task = analyze_task(payload["path"], config()); save_task(task, "项目分析完成，已生成文档草稿", "analysis.completed"); self.send_json(201, {"task": task})
            elif route == "/api/tasks/plan-change":
                task = plan_change(payload["path"], payload["request"], config()); save_task(task, "影响分析完成，等待用户确认", "analysis.completed"); self.send_json(201, {"task": task})
            elif route == "/api/tasks/new-project":
                task = plan_project(payload["path"], payload["language"], payload["goal"], payload.get("answers", {}))
                save_task(task, "需求与设计草稿已生成，请补充澄清项并评审", "requirements.preview")
                self.send_json(201, {"task": task})
            elif route.startswith("/api/tasks/") and route.endswith("/clarify"):
                task = STORE.get(route.split("/")[3])
                if not task: raise ValueError("任务不存在")
                task = clarify_project(task, payload.get("answers", {}))
                save_task(task, "已更新需求澄清答案", "requirements.updated")
                self.send_json(200, {"task": task})
            elif route.startswith("/api/tasks/") and route.endswith("/generate-business"):
                task = STORE.get(route.split("/")[3])
                if not task: raise ValueError("任务不存在")
                task = generate_business_draft(task, config())
                save_task(task, "模型业务草案已生成，必须审核后确认创建", "business.preview")
                self.send_json(200, {"task": task})
            elif route.startswith("/api/tasks/") and route.endswith("/approve-design"):
                task = STORE.get(route.split("/")[3])
                if not task: raise ValueError("任务不存在")
                if payload.get("confirmed") is not True: raise ValueError("必须明确确认设计草稿")
                task = approve_project_design(task)
                save_task(task, "设计草稿已批准，请审核骨架差异并再次确认创建", "design.approved")
                self.send_json(200, {"task": task})
            elif route.startswith("/api/tasks/") and route.endswith("/draft-code"):
                task = STORE.get(route.split("/")[3])
                if not task: raise ValueError("任务不存在")
                publish_event(task["id"], "code.draft.started", "正在生成代码修改草案")
                task = draft_code_change(task, config()); save_task(task, "代码草案已生成，等待用户确认", "code.draft.completed"); self.send_json(200, {"task": task})
            elif route.startswith("/api/tasks/") and route.endswith("/retry-tests"):
                task = STORE.get(route.split("/")[3])
                if not task: raise ValueError("任务不存在")
                if payload.get("confirmed") is not True: raise ValueError("重试测试必须明确确认")
                publish_event(task["id"], "tests.retry.started", "正在校验失败现场并重新执行测试，不覆盖业务代码")
                task = retry_tests(task)
                save_task(task, "测试重试完成" if task["status"] == "completed" else "测试仍未通过", "tests.retry.finished")
                self.send_json(200, {"task": task})
            elif route.startswith("/api/tasks/") and route.endswith("/confirm"):
                task_id = route.split("/")[3]; task = STORE.get(task_id)
                if not task: raise ValueError("任务不存在")
                if payload.get("confirmed") is not True: raise ValueError("必须明确确认后才能写入文件")
                if task.get("status") not in {"awaiting_confirmation", "awaiting_code_confirmation"}:
                    raise ValueError("当前任务不在等待确认阶段，拒绝重复执行")
                publish_event(task_id, "confirmation.accepted", "用户已确认，开始执行任务")
                if task.get("kind") == "new-project":
                    confirmed = confirm_project(task)
                elif task.get("kind") == "change-plan" and task.get("status") == "awaiting_code_confirmation":
                    publish_event(task_id, "code.write.started", "正在写入代码、测试和设计文档")
                    confirmed = confirm_code_change(task)
                elif task.get("kind") == "change-plan":
                    publish_event(task_id, "plan.write.started", "正在写入迭代计划并提交 Git")
                    confirmed = confirm_plan(task)
                else:
                    publish_event(task_id, "documents.write.started", "正在写入项目文档并提交 Git")
                    confirmed = confirm_task(task)
                failed = confirmed.get("status") in {"testing_failed", "scaffold_testing_failed"}
                if not failed: confirmed.pop("last_error", None)
                save_task(confirmed, "测试未通过，等待处理" if failed else "操作完成，当前阶段：" + confirmed["status"], "task.failed" if failed else "state"); self.send_json(200, {"task": confirmed})
            elif route == "/api/tasks":
                task = {"id": f"TASK-{uuid.uuid4().hex[:8].upper()}", "title": payload.get("title", "未命名任务"), "status": "created", "created_at": datetime.now().isoformat()}; save_task(task, "任务已创建", "task.created"); self.send_json(201, {"task": task})
            else: self.send_json(404, {"error": "接口不存在"})
        except KeyError as exc: self.send_json(400, {"error": f"缺少参数: {exc.args[0]}"})
        except (ValueError, OSError) as exc:
            if task and task.get("id"):
                task["last_error"] = {"message": str(exc), "at": datetime.now().isoformat()}
                save_task(task, str(exc), "operation.failed")
            self.send_json(400, {"error": str(exc), "task": task})

    def log_message(self, fmt: str, *args: Any) -> None: print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")


if __name__ == "__main__":
    print(f"CodeAgent service listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
