"""已有项目分析、文档基线生成和确认落盘逻辑。"""

from __future__ import annotations

import hashlib
import difflib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from glob import glob
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IGNORE_DIRS = {".git", "node_modules", "target", "build", "dist", ".venv", "venv", "__pycache__"}
SOURCE_SUFFIXES = {".py", ".java", ".kt", ".xml", ".yml", ".yaml", ".json", ".sql", ".md"}
MAX_CHANGE_CONTEXT_BYTES = 512 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_git(project: Path, args: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = result.stdout or result.stderr
    return result.returncode == 0, output if "-z" in args else output.strip()


def _file_digest(project: Path, relative: str) -> str | None:
    path = project / relative
    if not path.exists():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"无法读取基线文件 `{relative}`：{exc}") from exc
    return digest.hexdigest()


def _capture_git_baseline(project: Path, files: list[str]) -> dict[str, Any]:
    ok, status = run_git(project, ["status", "--porcelain", "--untracked-files=all"])
    if not ok:
        raise ValueError(f"无法读取 Git 工作区状态：{status}")
    if status:
        raise ValueError("项目工作区或暂存区存在未提交修改，请先提交或清理后再创建 Agent 任务")
    ok, head = run_git(project, ["rev-parse", "HEAD"])
    if not ok:
        raise ValueError("项目尚无 Git 提交，无法建立安全修改基线")
    return {"head": head, "status": status, "files": {path: _file_digest(project, path) for path in files}}


def _verify_git_baseline(task: dict[str, Any], files: list[str]) -> None:
    project = Path(task["analysis"]["path"]).resolve()
    baseline = task.get("git_baseline")
    if not baseline:
        raise ValueError("任务缺少 Git 安全基线，拒绝修改文件")
    ok, head = run_git(project, ["rev-parse", "HEAD"])
    if not ok or head != baseline["head"]:
        raise ValueError("Git HEAD 已变化，任务基线失效，请重新分析需求")
    ok, status = run_git(project, ["status", "--porcelain", "--untracked-files=all"])
    if not ok or status:
        raise ValueError("检测到工作区或暂存区发生变化，已停止 Agent 操作以保护用户修改")
    for path in files:
        if _file_digest(project, path) != baseline["files"].get(path):
            raise ValueError(f"检测到基线文件已变化：`{path}`，已停止 Agent 操作")


def _verify_staged_files(project: Path, expected: list[str]) -> None:
    # Compare repository-relative, NUL-delimited paths, including files outside
    # the selected project. Ask Git which authorized files really differ from
    # HEAD so unchanged files and line-ending normalization do not block commits.
    ok, output = run_git(project, ["diff", "--cached", "--name-only", "--no-relative", "--no-renames", "-z"])
    actual = {name for name in output.split("\0") if name}
    expected_ok, changed = (True, "")
    if expected:
        expected_ok, changed = run_git(project, ["--literal-pathspecs", "diff", "HEAD", "--name-only", "--no-relative", "--no-renames", "-z", "--", *expected])
    authorized_changes = {name for name in changed.split("\0") if name}
    if not ok or not expected_ok or actual != authorized_changes:
        raise ValueError("暂存区文件集合与 Agent 预期不一致，拒绝提交以避免混入其他修改")


def _files(project: Path) -> list[Path]:
    return [p for p in project.rglob("*") if p.is_file() and not any(part in IGNORE_DIRS for part in p.parts)]


def inspect_project(path_value: str) -> dict[str, Any]:
    project = Path(path_value).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("项目路径不存在或不是目录")
    paths = _files(project)
    relative = [p.relative_to(project).as_posix() for p in paths]
    names = {p.name for p in paths}
    java = any(p.suffix == ".java" for p in paths) or bool(names & {"pom.xml", "build.gradle", "build.gradle.kts"})
    python = any(p.suffix == ".py" for p in paths)
    modules = sorted({p.split("/")[0] for p in relative if "/" in p})[:40]
    build_tools = []
    if "pom.xml" in names: build_tools.append("Maven")
    if names & {"build.gradle", "build.gradle.kts", "gradlew"}: build_tools.append("Gradle")
    if "pytest.ini" in names or "pyproject.toml" in names or "setup.cfg" in names: build_tools.append("pytest 配置")
    # Match executable unit-test files, not reports, resources or helper modules.
    if set(build_tools) & {"Maven", "Gradle"}:
        tests = [name for name in relative
                 if "/src/test/java/" in "/" + name and name.endswith(".java")
                 and (Path(name).stem.startswith("Test") or Path(name).stem.endswith(("Test", "Tests", "TestCase")))]
    else:
        tests = [name for name in relative
                 if Path(name).parts[0] != "docs" and name.endswith(".py")
                 and (Path(name).name.startswith("test_") or Path(name).name.endswith("_test.py"))]
    git_ok, _ = run_git(project, ["rev-parse", "--is-inside-work-tree"])
    _, branch = run_git(project, ["branch", "--show-current"])
    _, status = run_git(project, ["status", "--short"])
    return {
        "path": str(project), "name": project.name,
        "languages": (["Java"] if java else []) + (["Python"] if python else []) or ["未识别"],
        "build_tools": build_tools, "modules": modules, "test_files": tests[:100],
        "source_files": [p for p in relative if Path(p).suffix in SOURCE_SUFFIXES][:300],
        "git": {"is_repository": git_ok, "branch": branch or None, "status": status, "has_uncommitted_changes": bool(status)},
    }


def _read_samples(project: Path, files: list[str]) -> str:
    chunks: list[str] = []
    for name in files[:20]:
        path = project / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunks.append(f"\n### {name}\n{text[:5000]}")
    return "".join(chunks)


def _read_change_context(project: Path, files: list[str]) -> str:
    """Read all authorized files in full, or reject before contacting the model."""
    project = project.resolve()
    sources = []
    total_bytes = 0
    for name in dict.fromkeys(files):
        path = (project / name).resolve()
        if project not in path.parents or ".git" in path.relative_to(project).parts:
            raise ValueError(f"代码上下文路径越界：{name}")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_CHANGE_CONTEXT_BYTES + 1)
            total_bytes += len(raw)
            if total_bytes > MAX_CHANGE_CONTEXT_BYTES:
                raise ValueError("完整代码上下文超过 512 KiB，请拆分任务或缩小影响范围；不会截断源码")
            sources.append({"path": name, "content": raw.decode("utf-8")})
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"无法完整读取代码文件 {name}：{exc}") from exc
    context = json.dumps(sources, ensure_ascii=False)
    if len(context.encode("utf-8")) > MAX_CHANGE_CONTEXT_BYTES:
        raise ValueError("完整代码上下文超过 512 KiB，请拆分任务或缩小影响范围；不会截断源码")
    return context


def _local_documents(analysis: dict[str, Any]) -> dict[str, str]:
    languages = "、".join(analysis["languages"])
    modules = "、".join(analysis["modules"]) or "待用户确认"
    tests = "\n".join(f"- `{item}`" for item in analysis["test_files"]) or "- 尚未识别到测试文件"
    sources = "\n".join(f"- `{item}`" for item in analysis["source_files"][:80]) or "- 尚未识别到源码文件"
    return {
        "requirements/product-requirements.md": f"""# {analysis['name']} 产品需求文档\n\n> 本文由 CodeAgent 根据现有代码生成，推断内容需要用户确认。\n\n## 1. 项目范围\n\n- 项目：{analysis['name']}\n- 技术语言：{languages}\n- 业务边界：以交易或微服务模块为边界，当前识别模块：{modules}\n\n## 2. 已确认事实\n\n- 已扫描源码文件：{len(analysis['source_files'])} 个\n- 已识别测试文件：{len(analysis['test_files'])} 个\n- 构建工具：{'、'.join(analysis['build_tools']) or '待确认'}\n\n## 3. 待确认需求\n\n- 请补充系统服务对象、核心交易流程和业务验收标准。\n- 请确认各模块的输入、输出、异常和权限规则。\n\n## 4. 验收标准\n\n- 正式需求由用户确认后作为基线。\n\n## 5. 修订记录\n\n| 版本 | 日期 | 修改内容 | 状态 |\n|---|---|---|---|\n| 0.1 | {now()[:10]} | 根据现有代码生成初始草稿 | 待确认 |\n""",
        "architecture/overall-design.md": f"""# {analysis['name']} 总体设计\n\n## 1. 系统概览\n\n项目使用 {languages}，当前识别构建工具为：{'、'.join(analysis['build_tools']) or '待确认'}。\n\n## 2. 模块划分\n\n{chr(10).join(f'- `{m}`' for m in analysis['modules']) or '- 待用户确认模块边界'}\n\n## 3. 代码事实\n\n系统当前包含约 {len(analysis['source_files'])} 个候选源码/配置文件。架构依赖和外部系统需要结合运行配置进一步确认。\n\n## 4. 风险与待确认项\n\n- 当前文档由静态代码分析生成，不代表所有运行时行为。\n- 需要用户确认服务边界、部署方式、数据存储和外部依赖。\n\n## 5. 修订记录\n\n| 版本 | 日期 | 修改内容 | 状态 |\n|---|---|---|---|\n| 0.1 | {now()[:10]} | 生成总体设计草稿 | 待确认 |\n""",
        "detailed-design/module-design.md": f"""# {analysis['name']} 详细设计\n\n## 1. 模块与文件\n\n{sources}\n\n## 2. 测试现状\n\n{tests}\n\n## 3. 接口与数据\n\n当前版本尚未对运行时接口和数据模型做确定性推断，请在确认需求后补充。\n\n## 4. 异常处理\n\n需要根据业务交易规则补充异常分类、错误码、重试和日志要求。\n\n## 5. 修订记录\n\n| 版本 | 日期 | 修改内容 | 状态 |\n|---|---|---|---|\n| 0.1 | {now()[:10]} | 生成详细设计草稿 | 待确认 |\n""",
    }


def generate_documents(analysis: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, str], str]:
    llm = config.get("llm", {})
    api_key = os.getenv(llm.get("api_key_env", "OPENAI_API_KEY"))
    if not api_key:
        return _local_documents(analysis), "local-draft"
    project = Path(analysis["path"])
    prompt = f"""你是软件架构师。请根据项目静态分析生成三个 Markdown 文档。不要虚构无法从材料确认的业务事实，推断内容标记为待确认。返回严格 JSON，键必须是 requirements/product-requirements.md、architecture/overall-design.md、detailed-design/module-design.md。\n\n分析：{json.dumps(analysis, ensure_ascii=False)}\n源码样本：{_read_samples(project, analysis['source_files'])}"""
    payload = {"model": llm.get("model", "gpt-4o"), "temperature": float(llm.get("temperature", 0.2)), "messages": [{"role": "system", "content": "只输出合法 JSON。"}, {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
    request = urllib.request.Request(f"{llm.get('base_url', 'https://api.openai.com/v1').rstrip('/')}/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=int(llm.get("timeout_seconds", 120))) as response:
            content = json.loads(response.read().decode())["choices"][0]["message"]["content"]
        documents = json.loads(content)
        required = {"requirements/product-requirements.md", "architecture/overall-design.md", "detailed-design/module-design.md"}
        if not required.issubset(documents): raise ValueError("模型未返回完整文档")
        return {key: str(documents[key]) for key in required}, "gpt"
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _local_documents(analysis), f"local-fallback: {exc}"


def analyze_task(path_value: str, config: dict[str, Any]) -> dict[str, Any]:
    analysis = inspect_project(path_value)
    if not analysis["git"]["is_repository"]:
        raise ValueError("项目必须是 Git 仓库，才能建立文档基线并强制提交")
    project = Path(analysis["path"])
    paths = _document_paths(project, _local_documents(analysis))
    baseline = _capture_git_baseline(project, paths)
    documents, generator = generate_documents(analysis, config)
    return {"id": f"TASK-{uuid.uuid4().hex[:8].upper()}", "title": f"{analysis['name']} 文档基线", "status": "awaiting_confirmation", "created_at": now(), "analysis": analysis, "documents": documents, "generator": generator, "git_baseline": baseline}


def _impact_files(analysis: dict[str, Any], request: str) -> tuple[list[str], list[str]]:
    """用需求词和目录层级确定候选源码与测试，结果必须可解释。"""
    words = {word.lower() for word in request.replace("，", " ").replace("。", " ").split() if len(word) >= 2}
    source_files = analysis["source_files"]
    if not words:
        return source_files[:30], analysis["test_files"][:30]
    matched = [name for name in source_files if any(word in name.lower() for word in words)]
    if not matched:
        modules = analysis["modules"]
        matched = [name for name in source_files if any(name.startswith(module + "/") for module in modules[:3])][:30]
    matched = matched[:50] or source_files[:20]
    affected_roots = {name.split("/")[0] for name in matched}
    tests = [name for name in analysis["test_files"] if any(root in name.split("/") for root in affected_roots)]
    return matched, (tests[:50] or analysis["test_files"][:30])


def _test_plan(analysis: dict[str, Any], tests: list[str]) -> dict[str, Any]:
    tools = set(analysis["build_tools"])
    commands: list[str] = []
    if "Maven" in tools: commands.append("mvn -Dtest=<受影响测试类> test")
    if "Gradle" in tools: commands.append("gradlew.bat test --tests <受影响测试类>")
    if not commands and ("pytest 配置" in tools or "Python" in analysis["languages"]): commands.append("pytest <受影响测试路径>")
    return {"scope": "affected", "test_files": tests, "commands": commands or ["根据项目测试配置补充测试命令"], "reason": "仅执行受影响模块及其关联测试，避免无关全量回归。"}


def plan_change(path_value: str, request: str, config: dict[str, Any]) -> dict[str, Any]:
    if not request.strip():
        raise ValueError("需求描述不能为空")
    analysis = inspect_project(path_value)
    if not analysis["git"]["is_repository"]:
        raise ValueError("项目必须是 Git 仓库，才能创建迭代计划并提交变更记录")
    files, tests = _impact_files(analysis, request)
    baseline = _capture_git_baseline(Path(analysis["path"]), files + tests)
    test_plan = _test_plan(analysis, tests)
    module_names = sorted({name.split("/")[0] for name in files})
    request_doc = f"""# 迭代变更计划\n\n- 任务编号：待确认\n- 项目：{analysis['name']}\n- 需求类型：功能需求或缺陷修复\n- 需求描述：{request.strip()}\n- 生成时间：{now()}\n\n## 1. 影响模块\n\n{chr(10).join(f'- `{name}`' for name in module_names) or '- 待确认'}\n\n## 2. 预计修改文件\n\n{chr(10).join(f'- `{name}`' for name in files)}\n\n## 3. 设计文档变更\n\n- 需求文档：补充本次需求、验收标准和修订记录。\n- 总体设计：补充受影响模块和依赖关系。\n- 详细设计：补充实现流程、接口/数据和异常处理。\n\n## 4. 测试计划\n\n- 回归范围：{test_plan['scope']}\n- 原因：{test_plan['reason']}\n- 测试命令：\n{chr(10).join(f'  - `{command}`' for command in test_plan['commands'])}\n- 关联测试：\n{chr(10).join(f'  - `{name}`' for name in tests) or '  - 待补充'}\n\n## 5. 待用户确认\n\n- 影响模块和预计文件是否准确。\n- 需求验收标准和边界条件。\n- 是否允许进入代码修改阶段。\n"""
    return {"id": f"TASK-{uuid.uuid4().hex[:8].upper()}", "title": f"{analysis['name']} 迭代变更计划", "kind": "change-plan", "status": "awaiting_confirmation", "created_at": now(), "request": request.strip(), "analysis": analysis, "affected_files": files, "affected_tests": tests, "test_plan": test_plan, "git_baseline": baseline, "documents": {"changes/change-plan.md": request_doc}, "generator": "local-impact-analysis"}


def confirm_plan(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("status") != "awaiting_confirmation":
        raise ValueError("当前任务阶段不允许再次确认计划")
    project = Path(task["analysis"]["path"])
    if not project.is_dir(): raise ValueError("项目路径已不存在")
    _verify_git_baseline(task, task["affected_files"] + task["affected_tests"])
    target = project / "docs" / "changes" / f"{task['id'].lower()}-change-plan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = task["documents"]["changes/change-plan.md"].replace("任务编号：待确认", f"任务编号：{task['id']}")
    target.write_text(content.rstrip() + "\n", encoding="utf-8")
    change_log = project / "docs" / "changes" / "change-log.md"
    entry = f"\n## {now()[:10]} / {task['id']}\n\n- 修改原因：登记迭代需求并确认影响范围。\n- 需求：{task['request']}\n- 预计修改代码：\n" + "\n".join(f"  - `{item}`" for item in task["affected_files"]) + "\n- 关联测试：\n" + "\n".join(f"  - `{item}`" for item in task["affected_tests"]) + f"\n- 状态：计划已确认，等待代码修改。\n"
    change_log.write_text((change_log.read_text(encoding="utf-8") if change_log.exists() else "# 变更记录\n") + entry, encoding="utf-8")
    written = [str(target.relative_to(project)).replace("\\", "/"), "docs/changes/change-log.md"]
    ok, output = run_git(project, ["add", *written])
    if not ok: raise ValueError(f"Git add 失败：{output}")
    _verify_staged_files(project, written)
    ok, commit = run_git(project, ["commit", "-m", f"docs({project.name}): record change plan"])
    if not ok: raise ValueError(f"Git commit 失败：{commit}")
    ok, head = run_git(project, ["rev-parse", "HEAD"])
    if not ok: raise ValueError(f"无法读取计划提交：{head}")
    task["git_baseline"] = {"head": head, "status": "", "files": {path: _file_digest(project, path) for path in task["affected_files"] + task["affected_tests"]}}
    task["status"] = "approved_plan"; task["approved_at"] = now(); task["commit"] = commit; task["written_files"] = written
    return task


def _allowed_test_command(analysis: dict[str, Any]) -> list[str] | None:
    tools = set(analysis["build_tools"])
    if "Maven" in tools:
        return ["mvn"]
    if "Gradle" in tools:
        return ["gradlew.bat"]
    if "pytest 配置" in tools or "Python" in analysis["languages"]:
        return ["python", "-m", "pytest"]
    return None


def _java_test_class(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "/java/"
    if marker in normalized:
        name = normalized.split(marker, 1)[1].rsplit(".", 1)[0]
        return name.replace("/", ".")
    return Path(path).stem


def _junit_snapshot(project: Path) -> dict[str, tuple[int, int]]:
    patterns = ["target/surefire-reports/TEST-*.xml", "build/test-results/test/TEST-*.xml"]
    return {name: (Path(name).stat().st_mtime_ns, Path(name).stat().st_size)
            for pattern in patterns for name in glob(str(project / pattern))}


def _parse_junit_reports(project: Path, before: dict[str, tuple[int, int]] | None = None) -> dict[str, Any] | None:
    snapshot = _junit_snapshot(project)
    report_paths: set[str] = set()
    for name, fingerprint in snapshot.items():
        if before is None or before.get(name) != fingerprint:
            report_paths.add(name)
    if not report_paths:
        return None
    summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "duration_seconds": 0.0, "reports": 0}
    for report_path in report_paths:
        try:
            root = ET.parse(report_path).getroot()
        except (ET.ParseError, OSError):
            continue
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        try:
            counts = {key: sum(float(suite.get(attribute, "0")) for suite in suites if not list(suite.findall("testsuite")))
                      for key, attribute in [("total", "tests"), ("failed", "failures"), ("errors", "errors"), ("skipped", "skipped"), ("duration_seconds", "time")]}
        except ValueError:
            continue
        for key, value in counts.items():
            summary[key] += value if key == "duration_seconds" else int(value)
        summary["reports"] += 1
    if not summary["reports"]:
        return None
    summary["passed"] = max(summary["total"] - summary["failed"] - summary["errors"] - summary["skipped"], 0)
    return summary


def _run_tests(project: Path, command: list[str] | None, test_files: list[str], build_tools: list[str] | None = None, java_home: str | None = None, maven_goal: str = 'test') -> dict[str, Any]:
    if not command:
        return {"status": "not_run", "command": None, "output": "未识别项目测试工具", "duration_seconds": 0}
    if not test_files:
        return {"status": "not_run", "command": None, "output": "未确定关联测试范围，禁止退化为全量执行", "duration_seconds": 0}
    tools = set(build_tools or [])
    working = project.resolve()
    if tools & {"Maven", "Gradle"}:
        markers = ["pom.xml"] if "Maven" in tools else ["build.gradle", "build.gradle.kts"]
        modules = {}
        for name in test_files:
            target = (working / name).resolve()
            if working not in target.parents or target.suffix != ".java" or "/src/test/java/" not in target.as_posix():
                return {"status": "not_run", "command": None, "output": "测试范围包含非标准 Java 单元测试路径，请重新确认", "duration_seconds": 0}
            module = next((parent for parent in target.parents if (parent == working or working in parent.parents) and any((parent / marker).is_file() for marker in markers)), None)
            if module is None:
                return {"status": "not_run", "command": None, "output": "无法定位 Java 测试所属构建模块", "duration_seconds": 0}
            modules.setdefault(module, []).append(name)
        if len(modules) != 1:
            if 'Maven' not in tools:
                return {"status": "not_run", "command": None, "output": "关联测试跨多个构建模块，请拆分任务后执行", "duration_seconds": 0}
            results = []
            for module, names in sorted(modules.items()):
                result = _run_tests(project, command, names, build_tools, java_home, maven_goal)
                results.append({**result, 'module': module.relative_to(project.resolve()).as_posix()})
            summaries = [item['summary'] for item in results if item.get('summary')]
            summary = {key: sum(item.get(key, 0) for item in summaries) for key in ('total', 'passed', 'failed', 'skipped', 'errors', 'duration_seconds', 'reports')}
            return {'status': 'passed' if all(item['status'] == 'passed' for item in results) else 'failed', 'command': '\n'.join(item.get('command') or 'not_run' for item in results), 'output': '\n\n'.join(f"[{item['module']}]\n{item['output']}" for item in results), 'duration_seconds': round(sum(item.get('duration_seconds', 0) for item in results), 2), 'summary': summary, 'modules': results}
        working = next(iter(modules))
        wrapper_name = ("mvnw.cmd" if os.name == "nt" else "mvnw") if "Maven" in tools else ("gradlew.bat" if os.name == "nt" else "gradlew")
        wrapper = next((parent / wrapper_name for parent in [working, *working.parents]
                        if (parent == project.resolve() or project.resolve() in parent.parents)
                        and (parent / wrapper_name).is_file()), None)
        if wrapper:
            command = [str(wrapper)]
        elif "Gradle" in tools:
            command = ["gradle"]
    if "Maven" in tools:
        classes = [_java_test_class(path) for path in test_files]
        actual = command + ([f"-Dtest={','.join(classes)}"] if classes else []) + [maven_goal]
    elif "Gradle" in tools:
        classes = [_java_test_class(path) for path in test_files]
        actual = command + ["test"] + sum((["--tests", item] for item in classes), [])
    else:
        actual = command + test_files if command[:2] == ["python", "-m"] else command
    started = time.monotonic()
    before = _junit_snapshot(working)
    environment = {}
    if java_home:
        env = os.environ.copy()
        env['JAVA_HOME'] = java_home
        env['PATH'] = str(Path(java_home) / 'bin') + os.pathsep + env.get('PATH', '')
        environment['env'] = env
    try:
        result = subprocess.run(actual, executable=shutil.which(actual[0]), cwd=working, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False, **environment)
        output = (result.stdout + "\n" + result.stderr).strip()[-12000:]
        summary = _parse_junit_reports(working, before)
        passed = result.returncode == 0
        if tools & {"Maven", "Gradle"}:
            passed = passed and bool(summary and summary["total"] > 0 and not summary["failed"] and not summary["errors"])
        return {"status": "passed" if passed else "failed", "command": " ".join(actual), "working_directory": str(working), "exit_code": result.returncode, "output": output, "duration_seconds": round(time.monotonic() - started, 2), "summary": summary}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "command": " ".join(actual), "output": str(exc), "duration_seconds": round(time.monotonic() - started, 2)}


def _append_document_revision(project: Path, relative: str, title: str, body: str, task_id: str) -> str:
    target = project / "docs" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        content = target.read_text(encoding="utf-8")
    else:
        content = f"# {title}\n\n"
    revision = f"\n\n## 修订记录 / {task_id}\n\n- 日期：{now()[:10]}\n- 修改原因：{body.splitlines()[0] if body.splitlines() else '完成迭代需求'}\n- 关联任务：`{task_id}`\n\n{body.rstrip()}\n"
    target.write_text(content.rstrip() + revision + "\n", encoding="utf-8")
    return str(target.relative_to(project)).replace("\\", "/")


def _sync_design_documents(project: Path, task: dict[str, Any], test_result: dict[str, Any]) -> list[str]:
    files = "、".join(f"`{item}`" for item in task["affected_files"]) or "待确认"
    tests = "、".join(f"`{item}`" for item in task["affected_tests"]) or "未识别"
    result = test_result["status"]
    return [
        _append_document_revision(project, "requirements/product-requirements.md", "产品需求文档", f"本次需求：{task['request']}\n\n验收关注点：验证受影响模块行为，并覆盖关联测试。\n\n本次状态：{result}。", task["id"]),
        _append_document_revision(project, "architecture/overall-design.md", "总体设计", f"受影响模块和代码文件：{files}\n\n本次变更不改变未列出的模块边界；如实现过程中发现架构边界变化，需要在下一次设计评审中补充。\n\n本次验证状态：{result}。", task["id"]),
        _append_document_revision(project, "detailed-design/module-design.md", "详细设计", f"实现涉及文件：{files}\n\n关联测试：{tests}\n\n测试命令：`{test_result.get('command') or '未执行'}`\n测试结果：{result}。", task["id"]),
    ]


def _write_test_reports(project: Path, task: dict[str, Any], test_result: dict[str, Any]) -> list[str]:
    testing = project / "docs" / "testing"
    testing.mkdir(parents=True, exist_ok=True)
    output = test_result.get("output", "")
    summary = test_result.get("summary") or {}
    statistics = "\n".join(f"- {key}：{value}" for key, value in summary.items()) or "- 未发现 JUnit XML 结果文件"
    common = f"- 任务：`{task['id']}`\n- 需求：{task['request']}\n- 命令：`{test_result.get('command') or '未执行'}`\n- 结果：**{test_result['status']}**\n- 耗时：{test_result['duration_seconds']} 秒\n\n## 结构化统计\n\n{statistics}\n\n## 输出\n\n```text\n{output}\n```\n"
    unit = testing / f"{task['id'].lower()}-unit-test-report.md"
    unit.write_text(f"# 单元测试报告\n\n{common}\n## 测试范围\n\n" + "\n".join(f"- `{item}`" for item in task["affected_tests"]) + "\n", encoding="utf-8")
    regression = testing / f"{task['id'].lower()}-regression-test-report.md"
    regression.write_text(f"# 回归测试报告\n\n{common}\n## 回归范围\n\n仅执行受影响模块及其关联测试，避免无关全量回归。\n\n" + "\n".join(f"- `{item}`" for item in task["affected_tests"]) + "\n", encoding="utf-8")
    return [str(unit.relative_to(project)).replace("\\", "/"), str(regression.relative_to(project)).replace("\\", "/")]


def draft_code_change(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if task.get("kind") != "change-plan" or task.get("status") != "approved_plan":
        raise ValueError("只有已确认的迭代计划才能生成代码修改草案")
    analysis = task["analysis"]
    llm = config.get("llm", {})
    api_key = os.getenv(llm.get("api_key_env", "OPENAI_API_KEY"))
    draft: dict[str, Any] = {"files": [], "generator": "manual-required", "note": "未配置 GPT，CodeAgent 不会猜测业务代码。请配置模型后重新生成修改草案。"}
    if api_key:
        project = Path(analysis["path"])
        try:
            _verify_git_baseline(task, task["affected_files"] + task["affected_tests"])
            context = _read_change_context(project, task["affected_files"] + task["affected_tests"])
            prompt = f"""根据以下已批准的变更计划修改代码。只返回合法 JSON，格式为 {{\"files\":[{{\"path\":\"项目相对路径\",\"content\":\"修改后的完整文件内容\"}}]}}。只能修改这些候选文件：{json.dumps(task['affected_files'] + task['affected_tests'], ensure_ascii=False)}。需求：{task['request']}。下方包含所有候选文件的完整原文。保留与需求无关的代码，不得用省略号或片段代替完整文件。\n\n完整代码上下文（JSON）：{context}"""
            payload = {"model": llm.get("model", "gpt-4o"), "temperature": float(llm.get("temperature", 0.1)), "messages": [{"role": "system", "content": "你是谨慎的软件工程师，只输出 JSON。"}, {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
            request = urllib.request.Request(f"{llm.get('base_url', 'https://api.openai.com/v1').rstrip('/')}/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=int(llm.get("timeout_seconds", 120))) as response:
                envelope = json.loads(response.read().decode())
            choice = envelope["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("模型输出被 token 上限截断，请拆分任务或调整模型配置")
            parsed = json.loads(choice["message"]["content"])
            allowed = set(task["affected_files"] + task["affected_tests"])
            files = parsed.get("files", []) if isinstance(parsed, dict) else None
            if not isinstance(files, list) or not files or any(not isinstance(item, dict) or item.get("path") not in allowed or not isinstance(item.get("content"), str) for item in files):
                raise ValueError("模型返回了越界路径或无效文件内容")
            draft = {"files": files, "generator": "gpt", "note": "模型生成的完整文件草案，等待用户授权写入。"}
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
            draft["note"] = f"模型草案生成失败，未修改任何文件：{exc}"
    task["code_draft"] = draft
    if draft["files"]:
        prepare_change_review(task)
    task["status"] = "awaiting_code_confirmation" if draft["files"] else "approved_plan"
    task["test_command"] = _allowed_test_command(analysis)
    return task


def prepare_change_review(task: dict[str, Any]) -> None:
    project = Path(task["analysis"]["path"]).resolve()
    _verify_git_baseline(task, task["affected_files"] + task["affected_tests"])
    allowed = set(task["affected_files"] + task["affected_tests"])
    changes = []
    seen = set()
    for item in task["code_draft"]["files"]:
        name = item["path"]
        if name not in allowed or name in seen:
            raise ValueError("草案包含未授权或重复文件")
        seen.add(name)
        changes.append({"path": name, "content": item["content"].rstrip() + "\n", "kind": "code"})
    actual = "\n".join(f"- `{item['path']}`" for item in changes)
    for name, title, description in [
        ("requirements/product-requirements.md", "产品需求文档", f"需求：{task['request']}\n\n验收标准：待人工确认；测试证据见本任务测试报告。"),
        ("architecture/overall-design.md", "总体设计", "本次涉及以下文件；模块边界和依赖变化尚需人工审查，不据此推断架构保持不变。"),
        ("detailed-design/module-design.md", "详细设计", "具体实现以本次审核的代码差异为准；接口、异常和数据变化需要人工核对。"),
    ]:
        relative = "docs/" + name
        target = project / relative
        original = target.read_text(encoding="utf-8") if target.exists() else f"# {title}\n"
        content = original.rstrip() + f"\n\n## 修订记录 / {task['id']}\n\n- 日期：{now()[:10]}\n- 关联需求：{task['request']}\n\n{description}\n\n### 修改文件\n\n{actual}\n"
        changes.append({"path": relative, "content": content, "kind": "design"})
    for item in changes:
        target = (project / item["path"]).resolve()
        if project not in target.parents or ".git" in Path(item["path"]).parts:
            raise ValueError("草案路径越界或指向 Git 元数据")
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        item["baseline"] = _file_digest(project, item["path"])
        item["diff"] = "".join(difflib.unified_diff(original.splitlines(keepends=True), item["content"].splitlines(keepends=True), fromfile="a/" + item["path"], tofile="b/" + item["path"]))
    task["change_review"] = changes


def confirm_code_change(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("status") != "awaiting_code_confirmation":
        raise ValueError("当前任务阶段不允许写入代码")
    draft = task.get("code_draft", {})
    files = draft.get("files", [])
    if not files:
        raise ValueError("当前没有可写入的代码草案，请先配置 GPT 并重新生成")
    project = Path(task["analysis"]["path"]).resolve()
    _verify_git_baseline(task, task["affected_files"] + task["affected_tests"])
    allowed = set(task["affected_files"] + task["affected_tests"])
    review = task.get("change_review")
    if not review:
        raise ValueError("缺少统一差异预览，请重新生成代码草案")
    design_paths = {"docs/requirements/product-requirements.md", "docs/architecture/overall-design.md", "docs/detailed-design/module-design.md"}
    for item in review:
        target = (project / item["path"]).resolve()
        if item["path"] not in allowed | design_paths or project not in target.parents or ".git" in Path(item["path"]).parts:
            raise ValueError("拒绝写入未授权预览路径")
        if _file_digest(project, item["path"]) != item["baseline"]:
            raise ValueError("预览文件已变化，请重新生成草案")
    written: list[str] = []
    for item in review:
        relative = Path(item["path"])
        target = (project / relative).resolve()
        if relative.as_posix() not in allowed | design_paths or project not in target.parents:
            raise ValueError(f"拒绝写入未授权路径：{item['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
        written.append(relative.as_posix())
    return _finish_code_change(task, project, written)


def retry_tests(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("status") != "testing_failed" or not task.get("retry_baseline"):
        raise ValueError("只有具备失败现场快照的测试失败任务可以重试")
    project = Path(task["analysis"]["path"]).resolve()
    baseline = task["retry_baseline"]
    ok, head = run_git(project, ["rev-parse", "HEAD"])
    if not ok or head != baseline["head"]:
        raise ValueError("失败后 Git HEAD 已改变，请重新分析任务")
    ok, staged = run_git(project, ["diff", "--cached", "--name-only"])
    if not ok or staged:
        raise ValueError("暂存区非空，拒绝重试")
    ok, status = run_git(project, ["status", "--porcelain", "--untracked-files=all"])
    if not ok or status != baseline["status"]:
        raise ValueError("失败后工作区状态已改变，拒绝重试")
    for name, digest in baseline["files"].items():
        if _file_digest(project, name) != digest:
            raise ValueError(f"失败后文件已改变：{name}，拒绝重试")
    return _finish_code_change(task, project, [item["path"] for item in task["change_review"]])


def _finish_code_change(task: dict[str, Any], project: Path, written: list[str]) -> dict[str, Any]:
    test_result = _run_tests(project, task.get("test_command"), task.get("affected_tests", []), task["analysis"].get("build_tools", []))
    written.extend(_write_test_reports(project, task, test_result))
    change_log = project / "docs" / "changes" / "change-log.md"
    change_log.parent.mkdir(parents=True, exist_ok=True)
    entry = f"\n## {now()[:10]} / {task['id']} 代码修改\n\n- 需求：{task['request']}\n- 修改文件：\n" + "\n".join(f"  - `{item}`" for item in written) + f"\n- 单元测试结果：{test_result['status']}\n- 回归测试结果：{test_result['status']}\n- 文档同步：已更新需求、总体设计和详细设计。\n"
    change_log.write_text((change_log.read_text(encoding="utf-8") if change_log.exists() else "# 变更记录\n") + entry, encoding="utf-8")
    written.append("docs/changes/change-log.md")
    task["test_result"] = test_result; task["written_files"] = written
    if test_result["status"] != "passed":
        task["status"] = "testing_failed"
        task["last_error"] = {"message": "测试未通过，请查看测试输出；重试不会重新写入业务代码", "at": now()}
        ok, head = run_git(project, ["rev-parse", "HEAD"])
        status_ok, status = run_git(project, ["status", "--porcelain", "--untracked-files=all"])
        if ok and status_ok:
            task["retry_baseline"] = {"head": head, "status": status, "files": {name: _file_digest(project, name) for name in set(written + task["affected_files"] + task["affected_tests"])}}
        return task
    ok, output = run_git(project, ["add", *written])
    if not ok: raise ValueError(f"Git add 失败：{output}")
    _verify_staged_files(project, written)
    ok, commit = run_git(project, ["commit", "-m", f"feat({project.name}): implement approved change"])
    if not ok: raise ValueError(f"Git commit 失败：{commit}")
    task["status"] = "completed"; task["completed_at"] = now(); task["commit"] = commit
    task.pop("last_error", None)
    task.pop("retry_baseline", None)
    return task


def _document_paths(project: Path, documents: dict[str, str]) -> list[str]:
    paths = ["docs/" + name for name in documents] + ["docs/changes/change-log.md"]
    for name in paths:
        relative = Path(name)
        target = (project / relative).resolve()
        if ".." in relative.parts or ".git" in relative.parts or project.resolve() not in target.parents:
            raise ValueError("文档路径越界，拒绝写入")
    return paths


def confirm_task(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("status") != "awaiting_confirmation":
        raise ValueError("当前任务阶段不允许再次写入文档")
    project = Path(task["analysis"]["path"])
    if not project.is_dir(): raise ValueError("项目路径已不存在")
    paths = _document_paths(project, task["documents"])
    if not set(paths).issubset(task.get("git_baseline", {}).get("files", {})):
        raise ValueError("文档任务缺少完整安全基线，请重新分析项目")
    _verify_git_baseline(task, paths)
    written: list[str] = []
    for relative, content in task["documents"].items():
        target = project / "docs" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        written.append(str(target.relative_to(project)).replace("\\", "/"))
    change_log = project / "docs" / "changes" / "change-log.md"
    entry = f"\n## {now()[:10]} / {task['id']}\n\n- 修改原因：建立项目文档基线。\n- 修改文件：\n" + "\n".join(f"  - `{item}`" for item in written) + "\n- 生成方式：" + task["generator"] + "\n- 测试：本次仅生成文档，尚未执行代码测试。\n"
    change_log.parent.mkdir(parents=True, exist_ok=True)
    change_log.write_text((change_log.read_text(encoding="utf-8") if change_log.exists() else "# 变更记录\n") + entry, encoding="utf-8")
    written.append("docs/changes/change-log.md")
    ok, output = run_git(project, ["add", *written])
    if not ok: raise ValueError(f"Git add 失败：{output}")
    _verify_staged_files(project, written)
    ok, commit = run_git(project, ["commit", "-m", f"docs({project.name}): establish documentation baseline"])
    if not ok: raise ValueError(f"Git commit 失败：{commit}")
    task["status"] = "completed"; task["completed_at"] = now(); task["commit"] = commit; task["written_files"] = written
    return task
