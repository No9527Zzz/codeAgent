"""Reviewed, deterministic project scaffolding; no business implementation claims."""

from pathlib import Path
import difflib
import uuid
import json
import os
import re
import urllib.request
import copy
from pathlib import PureWindowsPath

if __package__:
    from .agent import _run_tests, now, run_git
    from .java_stack import java_settings, configure_pom, java17_home
else:
    from agent import _run_tests, now, run_git
    from java_stack import java_settings, configure_pom, java17_home


def draft_project(path_value: str, language: str, goal: str) -> dict:
    target = Path(path_value).expanduser().absolute()
    if not goal.strip():
        raise ValueError("请描述项目目标")
    if language not in {"python", "java"}:
        raise ValueError("项目语言必须为 python 或 java")
    if target.exists() or target.is_symlink():
        raise ValueError("目标路径必须不存在，禁止覆盖现有目录")
    if not target.parent.is_dir():
        raise ValueError("目标项目的父目录必须存在")
    parent = target.parent.resolve()
    target = parent / target.name
    inside, _ = run_git(parent, ["rev-parse", "--show-toplevel"])
    if inside:
        raise ValueError("不能在已有 Git 仓库内创建嵌套项目")
    task_id = "TASK-" + uuid.uuid4().hex[:8].upper()
    files = {
        "README.md": f"# {target.name}\n\n项目目标：{goal.strip()}\n\n当前仅为固定示例骨架，尚未实现业务需求。\n",
        ".gitignore": "__pycache__/\n.pytest_cache/\n.venv/\ntarget/\nbuild/\n",
        "docs/requirements/product-requirements.md": f"# 产品需求草稿\n\n## 项目目标\n\n{goal.strip()}\n\n## 待澄清\n\n- 用户角色、交易或微服务边界。\n- 输入输出、异常、安全约束和验收标准。\n\n## 修订记录\n\n- {now()[:10]} / {task_id}：建立需求草稿，业务规则待确认。\n",
        "docs/architecture/overall-design.md": f"# 总体设计草稿\n\n语言：{language}。当前为单模块示例，业务模块边界待确认。\n\n## 修订记录\n\n- {now()[:10]} / {task_id}：创建固定工程骨架设计。\n",
        "docs/detailed-design/module-design.md": f"# 详细设计草稿\n\n只提供示例函数和示例测试，不代表业务实现。接口、数据模型和异常设计待澄清。\n\n## 修订记录\n\n- {now()[:10]} / {task_id}：建立示例详细设计。\n",
    }
    if language == "python":
        files.update({"app.py": 'def greeting():\n    return "Hello, CodeAgent"\n', "tests/test_app.py": 'from app import greeting\n\ndef test_greeting():\n    assert greeting() == "Hello, CodeAgent"\n', "requirements-dev.txt": "pytest>=8,<10\n", "pytest.ini": "[pytest]\ntestpaths = tests\n"})
        tests = ["tests/test_app.py"]
        command, tools = ["python", "-m", "pytest"], []
    else:
        fixture = Path(__file__).resolve().parents[1] / "examples/java-orders"
        for name in ["pom.xml", "src/main/java/example/OrderService.java", "src/test/java/example/OrderServiceTest.java"]:
            files[name] = (fixture / name).read_text(encoding="utf-8")
        artifact = re.sub(r'[^a-z0-9.-]+', '-', target.name.lower()).strip('-.') or 'generated-service'
        files['pom.xml'] = files['pom.xml'].replace('<artifactId>java-orders</artifactId>', f'<artifactId>{artifact}</artifactId>')
        files["README.md"] += "\nJava 示例仅演示订单超时判断，与用户业务目标无自动关联。\n"
        tests = ["src/test/java/example/OrderServiceTest.java"]
        command, tools = ["mvn"], ["Maven"]
    review = [{"path": name, "content": content, "diff": "".join(difflib.unified_diff([], content.splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + name)), "kind": "scaffold"} for name, content in files.items()]
    return {"id": task_id, "kind": "new-project", "title": f"创建 {target.name}", "status": "awaiting_confirmation", "created_at": now(), "request": goal.strip(), "language": language, "generator": "fixed-scaffold", "analysis": {"path": str(target), "name": target.name, "languages": [language], "git": {"is_repository": False}}, "change_review": review, "test_command": command, "affected_tests": tests, "build_tools": tools}


def confirm_project(task: dict) -> dict:
    if task.get("kind") != "new-project" or task.get("status") != "awaiting_confirmation":
        raise ValueError("当前阶段不允许创建项目")
    if (task.get('services') or task.get('java_settings', {}).get('stack', 'standard') != 'standard') and task.get('generator') != 'gpt-business-draft':
        raise ValueError('微服务项目必须先生成并审核各服务业务代码与测试')
    project = Path(task["analysis"]["path"])
    spring = task.get('java_settings', {}).get('stack', 'standard') != 'standard'
    home = java17_home() if spring else None
    # Revalidate destination immediately before mkdir; mkdir must not merge directories.
    draft_project(str(project), task["language"], task["request"])
    for item in task["change_review"]:
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise ValueError("项目草案包含非法路径")
    project.mkdir()
    task["status"] = "creating"
    for item in task["change_review"]:
        target = project / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
    ok, output = run_git(project, ["init"])
    if not ok: raise ValueError(f"初始化 Git 失败：{output}")
    if spring:
        result = _run_tests(project, task['test_command'], task['affected_tests'], task['build_tools'], java_home=home, maven_goal='package')
        result['java_home'] = home
    else:
        result = _run_tests(project, task["test_command"], task["affected_tests"], task["build_tools"])
    task["test_result"] = result
    report = project / "docs/testing/scaffold-test-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    scope = "验证模型生成的测试，不代表业务验收已通过。" if task.get("generator") == "gpt-business-draft" else "只验证固定示例，不验证业务需求。"
    statistics = json.dumps(result.get('summary'), ensure_ascii=False, indent=2)
    report.write_text(f"# 创建项目测试报告\n\n{scope}\n\n- 状态：{result['status']}\n- 命令：{result.get('command')}\n\n## 测试统计\n\n```json\n{statistics}\n```\n\n## 执行输出\n\n```text\n{result['output']}\n```\n", encoding="utf-8")
    written = [item["path"] for item in task["change_review"]] + ["docs/testing/scaffold-test-report.md", "docs/changes/change-log.md"]
    log = project / "docs/changes/change-log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"# 变更记录\n\n## {task['id']}\n\n生成方式：{task['generator']}。{scope}\n\n" + "\n".join(f"- `{name}`：新增代码、测试或文档文件。" for name in written) + f"\n\n测试：{result['status']}\n", encoding="utf-8")
    task["written_files"] = written
    if result["status"] != "passed":
        task["status"] = "scaffold_testing_failed"
        task["last_error"] = {"message": "项目编译或测试未通过，保留现场且未提交；请检查环境或测试输出", "at": now()}
        return task
    ok, output = run_git(project, ["add", "--", *written])
    if not ok: raise ValueError(f"暂存失败：{output}")
    ok, output = run_git(project, ["commit", "-m", "chore: initialize reviewed project scaffold"])
    if not ok: raise ValueError(f"首次提交失败，文件已保留，请检查 Git 身份配置：{output}")
    task.update(status="completed", commit=output, completed_at=now())
    return task


REQUIREMENT_FIELDS = {
    "users": "用户角色",
    "boundary": "交易或微服务边界",
    "workflow": "核心业务流程及输入输出",
    "acceptance": "可验证的验收标准",
    "constraints": "安全、数据和运行约束",
}


def plan_project(path_value: str, language: str, goal: str, answers: dict) -> dict:
    if not isinstance(answers, dict):
        raise ValueError("澄清答案必须为对象")
    task = draft_project(path_value, language, goal)
    service_text = answers.get('services', '')
    if not isinstance(service_text, str):
        raise ValueError('服务清单必须是文本')
    services = [name.strip() for name in service_text.split(',') if name.strip()]
    if services and (language != 'java' or not 2 <= len(services) <= 10 or len(set(services)) != len(services) or any(not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', name) or name in {'src', 'docs', 'target', 'build'} for name in services)):
        raise ValueError('微服务仅支持 Java，请填写 2 到 10 个不重复的小写服务名，以英文逗号分隔；不能使用 src/docs/target/build')
    task['services'] = services
    if any(not isinstance(answers.get(key, ''), str) for key in ('java_stack', 'dependencies')):
        raise ValueError('技术栈和依赖配置必须为文本')
    task['java_settings'] = java_settings(answers, language)
    normalized = {}
    for key in REQUIREMENT_FIELDS:
        value = answers.get(key, "")
        if not isinstance(value, str):
            raise ValueError("澄清答案必须为文本")
        normalized[key] = value.strip()
    missing = [label for key, label in REQUIREMENT_FIELDS.items() if not normalized[key]]
    task["answers"] = normalized
    normalized['services'] = ','.join(services)
    normalized['java_stack'] = task['java_settings']['stack']
    normalized['dependencies'] = answers.get('dependencies', '').strip()
    task["open_questions"] = missing
    task["status"] = "needs_clarification" if missing else "awaiting_design_confirmation"
    task.pop("change_review")
    sections = "\n\n".join(f"## {label}\n\n{normalized[key] or '待补充'}" for key, label in REQUIREMENT_FIELDS.items())
    revision = f"\n\n## 修订记录\n\n- {now()[:10]} / {task['id']}：整理用户提供的需求和设计草稿，待评审。\n"
    task["documents"] = {
        "requirements/product-requirements.md": f"# 产品需求评审草稿\n\n## 目标\n\n{goal.strip()}\n\n{sections}\n\n## 实现状态\n\n未实现；固定示例测试不能证明业务验收通过。" + revision,
        "architecture/overall-design.md": f"# 总体设计评审草稿\n\n## 技术方案\n\n语言：{language}。拟创建单模块骨架，业务模块拆分待后续实现设计。\n\n## 业务边界\n\n{normalized['boundary'] or '待补充'}\n\n## 运行约束\n\n{normalized['constraints'] or '待补充'}\n\n## 待决项\n\n数据存储、模块依赖、部署拓扑和外部接口尚未确定，批准本草稿不表示这些设计已完成。" + revision,
        "detailed-design/module-design.md": f"# 详细设计评审草稿\n\n## 业务流程\n\n{normalized['workflow'] or '待补充'}\n\n## 验收依据\n\n{normalized['acceptance'] or '待补充'}\n\n## 实现限制\n\n当前仅提供固定示例函数和示例测试；业务接口、数据模型、异常码和用例映射尚待设计，不宣称能够通过业务验收。" + revision,
    }
    task["generator"] = "structured-requirements"
    if services:
        topology = '\n\n## 微服务工程方案\n\n' + '\n'.join(f'- `{name}`：独立 Maven 模块、HTTP 入口和 JUnit4 单元测试。' for name in services)
        topology += '\n\nJava 8 标准库 HTTP 服务，父 POM 仅聚合构建；服务之间禁止 Maven 源码依赖，通过接口和 HTTP 适配器交互。单元测试使用接口替身，不启动真实外部服务。端口、接口契约和数据归属应在业务边界与流程中说明。此方案不包含 Spring Cloud、注册中心、网关或分布式事务。\n'
        for name in task['documents']:
            task['documents'][name] += topology
        task['documents']['architecture/overall-design.md'] = task['documents']['architecture/overall-design.md'].replace('拟创建单模块骨架，业务模块拆分待后续实现设计。', '拟创建独立微服务 Maven 多模块工程。')
    if language == 'java':
        settings = task['java_settings']
        for name, content in task['documents'].items():
            if settings['stack'] != 'standard':
                content = content.replace('Java 8 标准库 HTTP 服务', 'Java 17 Spring HTTP 服务').replace('JUnit4', 'JUnit5').replace('此方案不包含 Spring Cloud、注册中心、网关或分布式事务。', '注册中心、网关、数据库和分布式事务不自动配置。')
            task['documents'][name] = content + '\n\n## 已选 Java 构建配置\n\n```json\n' + json.dumps(settings, ensure_ascii=False, indent=2) + '\n```\n\nSpring 选项固定 Boot 3.5.8 / Cloud 2025.0.0；依赖下载和构建会执行第三方代码，请审核来源、许可证和漏洞。\n'
    return task


def clarify_project(task: dict, answers: dict) -> dict:
    if task.get("kind") != "new-project" or task.get("status") not in {"needs_clarification", "awaiting_design_confirmation"}:
        raise ValueError("当前阶段不允许修改澄清答案")
    if not isinstance(answers, dict):
        raise ValueError("澄清答案必须为对象")
    updated = plan_project(task["analysis"]["path"], task["language"], task["request"], {**task["answers"], **answers})
    new_id = updated["id"]
    updated["id"] = task["id"]
    updated["created_at"] = task["created_at"]
    updated["documents"] = {key: value.replace(new_id, task["id"]) for key, value in updated["documents"].items()}
    return updated


def approve_project_design(task: dict) -> dict:
    if task.get("kind") != "new-project" or task.get("status") != "awaiting_design_confirmation":
        raise ValueError("需求澄清未完成或当前阶段不允许确认设计")
    scaffold = draft_project(task["analysis"]["path"], task["language"], task["request"])
    documents = {"docs/" + key: value for key, value in task["documents"].items()}
    for item in scaffold["change_review"]:
        if item["path"] in documents:
            item["content"] = documents[item["path"]]
            item["diff"] = "".join(difflib.unified_diff([], item["content"].splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + item["path"]))
    task["change_review"] = scaffold["change_review"]
    if task.get('services'):
        review = [item for item in task['change_review'] if not item['path'].startswith('src/') and item['path'] != 'pom.xml']
        original_pom = next(item['content'] for item in scaffold['change_review'] if item['path'] == 'pom.xml')
        artifact = re.search(r'<artifactId>(.*?)</artifactId>', original_pom).group(1)
        parent = f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>local.codeagent</groupId><artifactId>{artifact}</artifactId><version>1.0-SNAPSHOT</version><packaging>pom</packaging><modules>' + ''.join(f'<module>{name}</module>' for name in task['services']) + '</modules></project>\n'
        poms = {'pom.xml': parent, **{f'{name}/pom.xml': original_pom.replace(f'<artifactId>{artifact}</artifactId>', f'<artifactId>{name}</artifactId>') for name in task['services']}}
        review += [{'path': name, 'content': content, 'kind': 'build-config', 'diff': ''.join(difflib.unified_diff([], content.splitlines(keepends=True), fromfile='/dev/null', tofile='b/' + name))} for name, content in poms.items()]
        task['change_review'] = review
        task['affected_tests'] = []
    task["status"] = "awaiting_confirmation"
    if task['language'] == 'java':
        for item in task['change_review']:
            if item['path'].endswith('pom.xml') and (item['path'] != 'pom.xml' or not task.get('services')):
                item['content'] = configure_pom(item['content'], task['java_settings'])
                item['diff'] = ''.join(difflib.unified_diff([], item['content'].splitlines(keepends=True), fromfile='/dev/null', tofile='b/' + item['path']))
        if task['java_settings']['stack'] != 'standard':
            for index, service in enumerate(task.get('services') or ['']):
                name = (service + '/' if service else '') + 'src/main/resources/application.properties'
                content = f'spring.application.name={service or task["analysis"]["name"]}\nserver.port=${{SERVER_PORT:{8080 + index}}}\n'
                task['change_review'].append({'path': name, 'content': content, 'kind': 'build-config', 'diff': ''.join(difflib.unified_diff([], content.splitlines(keepends=True), fromfile='/dev/null', tofile='b/' + name))})
    task["design_approved_at"] = now()
    return task


def generate_business_draft(task: dict, config: dict) -> dict:
    limit = config.get('llm', {}).get('max_repair_attempts', 0)
    if type(limit) is not int or not 0 <= limit <= 2:
        raise ValueError('max_repair_attempts 必须为 0 到 2 的整数')
    staged = config.get('llm', {}).get('staged_java_generation', False)
    if type(staged) is not bool:
        raise ValueError('staged_java_generation 必须为布尔值')
    candidate = copy.deepcopy(task)
    attempts = []
    stages = ['source', 'tests'] if staged and task.get('language') == 'java' else ['all']
    for stage in stages:
        history = []
        for index in range(limit + 1):
            response_count = len(history)
            try:
                result = _generate_business_draft(copy.deepcopy(candidate), config, history, stage)
            except ValueError as exc:
                if len(history) == response_count:
                    raise
                attempts.append({'stage': stage, 'attempt': index + 1, 'status': 'rejected', 'error': str(exc)})
                task['generation_attempts'] = attempts
                if index == limit:
                    raise ValueError(f'模型草案 {stage} 阶段在 {index + 1} 次尝试后仍未通过校验：{exc}') from exc
                history.append({'role': 'user', 'content': f'上一次输出被校验拒绝：{exc}。请修复并返回当前阶段完整 files JSON，不要只返回修改片段。保持原业务规则和验收标准，不删除测试，不新增依赖。'})
                continue
            attempts.append({'stage': stage, 'attempt': index + 1, 'status': 'validated'})
            candidate = result
            break
    candidate['generation_attempts'] = attempts
    task.update(candidate)
    return task


def _generate_business_draft(task: dict, config: dict, history: list, stage: str = 'all') -> dict:
    if task.get("kind") != "new-project" or task.get("status") != "awaiting_confirmation" or not task.get("design_approved_at"):
        raise ValueError("必须先完成需求澄清和设计确认")
    llm = config.get("llm", {})
    key = os.getenv(llm.get("api_key_env", "OPENAI_API_KEY"))
    if not key:
        raise ValueError("未配置模型密钥，不能生成业务代码草案")
    draft_project(task["analysis"]["path"], task["language"], task["request"])
    prompt = {"goal": task["request"], "language": task["language"], "answers": task["answers"], "approved_documents": task["documents"]}
    if llm.get('compact_generation_context', False):
        prompt.pop('approved_documents')
    instruction = '根据用户已批准需求生成最小业务代码和测试。只返回 JSON：{"files":[{"path":"相对路径","content":"完整内容"}]}。仅生成源码和测试，不生成文档、依赖配置、脚本或 Git 文件。Python 源码放在 app.py 或 src/，测试放在 tests/test_*.py，使用标准库和 pytest。Java 使用 Java 8 语法，源码放在 src/main/java/，JUnit4 测试放在 src/test/java/；必须声明与目录一致的 package，公开类型名与文件名一致，测试类以 Test 结尾，使用 org.junit.Test 和 org.junit.Assert。只使用 JDK 标准库，不支持 Spring、Lombok、JUnit5、Mockito 或第三方库。为正常流程、边界条件和非法输入生成测试。不能实现的需求明确在代码中拒绝，不要声称通过验收。'
    if task.get('services'):
        instruction += '\n这是微服务工程。服务清单：' + ','.join(task['services']) + '。所有 Java 文件必须带对应服务目录前缀：<service>/src/main/java/ 或 <service>/src/test/java/。每个服务都必须有业务源码和单元测试，以及 public static void main 入口，使用 com.sun.net.httpserver.HttpServer 提供独立 HTTP 服务。各服务使用不同端口且可配置，不允许跨模块直接引用源码；跨服务调用通过接口注入，使用标准库 HTTP 适配器。单元测试用手写替身隔离网络、数据库和其他服务，不启动 HTTP 服务。必须测试正常、边界及下游失败场景。不要生成 pom.xml，系统提供每个服务独立 POM 和父聚合 POM。'
    settings = task.get('java_settings', {'stack': 'standard', 'dependencies': []})
    if settings['stack'] != 'standard':
        instruction = instruction.replace('Java 使用 Java 8 语法', 'Java 使用 Java 17 语法').replace('JUnit4', 'JUnit5').replace('org.junit.Test 和 org.junit.Assert', 'org.junit.jupiter.api.Test 和 org.junit.jupiter.api.Assertions').replace('只使用 JDK 标准库，不支持 Spring、Lombok、JUnit5、Mockito 或第三方库。', '使用 Spring Boot 3.5.8，JUnit5 和 Mockito；不使用 Lombok。').replace('使用 com.sun.net.httpserver.HttpServer 提供独立 HTTP 服务', '使用 @SpringBootApplication、SpringApplication.run 和 REST Controller 提供 HTTP 服务').replace('使用标准库 HTTP 适配器', '使用 Spring HTTP 适配器').replace('单元测试用手写替身', '单元测试用 Mockito 或手写替身')
        instruction += '\n单元测试不使用 @SpringBootTest，不启动真实网络、数据库或服务；可用 MockMvc standaloneSetup 测试 Controller。Cloud 模式已提供 OpenFeign 和 LoadBalancer，无注册中心；Feign 使用可配置 URL。'
    instruction += '\n只能使用系统配置的依赖，不能生成或修改 POM。当前配置：' + json.dumps(settings, ensure_ascii=False)
    if settings['dependencies']:
        instruction = instruction.replace('只使用 JDK 标准库，不支持 Spring、Lombok、JUnit5、Mockito 或第三方库。', '只使用 JDK 和明确配置的第三方依赖。')
    if task['language'] == 'java':
        instruction = instruction.replace('Python 源码放在 app.py 或 src/，测试放在 tests/test_*.py，使用标准库和 pytest。', '')
        instruction += '\n严格返回单个 JSON 对象，顶层只有 files 数组。每个数组元素只有 path 和 content，content 是完整可编译的 Java 文件（包含 package、import、类和方法），不是片段。路径示例 src/main/java/example/OrderService.java、src/test/java/example/OrderServiceTest.java，微服务时在这两个路径前加服务名。两个文件第一行均为 package example;。测试必须实例化被测类并调用方法，边界断言必须符合需求。'
    source_files = [item for item in task['change_review'] if '/src/main/java/' in '/' + item['path']]
    if stage == 'source':
        instruction += '\n当前仅为源码阶段：只返回 src/main/java 下的业务源码，不返回任何测试。若使用 Spring，必须同时生成独立 Application 启动入口、Service 和 Controller。下一阶段再生成测试。'
    elif stage == 'tests':
        instruction += '\n当前仅为测试阶段：只返回 src/test/java 下的单元测试，禁止生成或修改业务源码。严格使用用户材料中 existing_sources 的真实类名和方法签名，覆盖验收要求。'
        prompt['existing_sources'] = [{'path': item['path'], 'content': item['content']} for item in source_files]
    payload = {"model": llm.get("model", "gpt-4o"), "temperature": llm.get('temperature', 0), "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], "response_format": {"type": "json_object"}}
    payload['messages'] += history
    if 'max_tokens' in llm:
        if type(llm['max_tokens']) is not int or not 128 <= llm['max_tokens'] <= 32768:
            raise ValueError('max_tokens 必须为 128 到 32768 的整数')
        payload['max_tokens'] = llm['max_tokens']
    request = urllib.request.Request(llm.get("base_url", "https://api.openai.com/v1").rstrip('/') + '/chat/completions', data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=int(llm.get("timeout_seconds", 120))) as response:
        envelope = json.loads(response.read())
    try:
        content = envelope['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('模型接口返回无效响应结构') from exc
    if not isinstance(content, str):
        raise ValueError('模型接口未返回文本内容')
    history.append({'role': 'assistant', 'content': content})
    if envelope['choices'][0].get('finish_reason') == 'length':
        raise ValueError('模型输出被 token 上限截断（推理也可能占用输出额度），请调整上限或缩小生成范围')
    if not content.strip():
        raise ValueError('模型返回空正文，可能只有推理内容，不能作为代码草案')
    parsed = json.loads(content)
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, list) or not 1 <= len(files) <= 50:
        raise ValueError("模型草案必须包含 1 到 50 个文件")
    if stage in {'source', 'tests'}:
        marker = '/src/main/java/' if stage == 'source' else '/src/test/java/'
        if any(not isinstance(item, dict) or not isinstance(item.get('path'), str) or marker not in '/' + item['path'] for item in files):
            raise ValueError(f'{stage} 阶段返回了其他阶段的文件')
    if stage == 'tests':
        files = [{'path': item['path'], 'content': item['content']} for item in source_files] + files
        if len(files) > 50:
            raise ValueError('源码与测试总计不能超过 50 个文件')
    seen = set()
    tests = []
    validated = []
    application_packages = {str(Path(item['path'].split('/java/', 1)[1]).parent).replace('\\', '.').replace('/', '.') for item in files if isinstance(item, dict) and isinstance(item.get('path'), str) and '/java/' in item['path']}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise ValueError("模型返回无效文件")
        name = item["path"]
        path = Path(name)
        if '\\' in name or ':' in name or PureWindowsPath(name).is_absolute() or path.is_absolute() or '..' in path.parts or path.as_posix() != name or name.casefold() in seen:
            raise ValueError("模型返回越界或重复路径")
        seen.add(name.casefold())
        local_name = name
        if task.get('services'):
            service, _, local_name = name.partition('/')
            if service not in task['services']:
                raise ValueError('模型返回了未声明的微服务文件')
        is_test = local_name.startswith('tests/test_') and local_name.endswith('.py') if task['language'] == 'python' else local_name.startswith('src/test/java/') and local_name.endswith('.java')
        is_source = (local_name == 'app.py' or local_name.startswith('src/')) and local_name.endswith('.py') if task['language'] == 'python' else local_name.startswith('src/main/java/') and local_name.endswith('.java')
        if not (is_test or is_source) or len(item['content']) > 200000:
            raise ValueError("模型文件不在允许的源码或测试范围内")
        if task['language'] == 'java':
            packages = application_packages
            if task.get('services'):
                packages = {str(Path(entry['path'].split('/java/', 1)[1]).parent).replace('\\', '.').replace('/', '.') for entry in files if isinstance(entry, dict) and isinstance(entry.get('path'), str) and entry['path'].startswith(service + '/src/main/java/')}
            allowed = [prefix for dep in settings['dependencies'] if dep['scope'] == 'compile' or (is_test and dep['scope'] == 'test') for prefix in dep['packages']]
            if settings['stack'] != 'standard':
                allowed += ['org.springframework', 'com.fasterxml.jackson', 'jakarta']
                if is_test: allowed += ['org.mockito', 'org.assertj', 'org.hamcrest']
            validate_java_file(name, item['content'], is_test, packages, allowed, settings['stack'] != 'standard')
        if is_test: tests.append(name)
        content = item['content'].rstrip() + '\n'
        validated.append({"path": name, "content": content, "kind": "business-code", "diff": ''.join(difflib.unified_diff([], content.splitlines(keepends=True), fromfile='/dev/null', tofile='b/' + name))})
    if (stage != 'source' and not tests) or not any(item['path'] not in tests for item in validated):
        raise ValueError("业务草案必须同时包含源码和测试")
    for service in task.get('services', []):
        sources = [item for item in validated if item['path'].startswith(service + '/src/main/java/')]
        if not sources or (stage != 'source' and not any(name.startswith(service + '/') for name in tests)):
            raise ValueError(f'每个微服务都必须包含源码和单元测试：{service}')
        if not any(re.search(r'public\s+static\s+void\s+main\s*\(\s*String\s*(?:\[\s*\]|\.\.\.)', item['content']) for item in sources):
            raise ValueError(f'微服务缺少独立 main 启动入口：{service}')
    retained = [item for item in task['change_review'] if not (item['path'].endswith('.py') or item['path'].endswith('.java'))]
    for index, item in enumerate(retained):
        if item['path'] == 'README.md':
            content = f"# {task['analysis']['name']}\n\n项目目标：{task['request']}\n\n当前代码与测试由模型生成，尚未通过独立业务验收。需求与设计见 docs/。\n"
            if task['language'] == 'java':
                content += '\n## 构建与测试\n\n需要 JDK 8 和 Maven，使用 JUnit 4.13.2。\n\n```sh\nmvn test\n```\n\n仅使用 JDK 标准库，不包含 Spring 或其他业务框架。\n'
            if task.get('services'):
                content += '\n## 微服务模块\n\n' + '\n'.join(f'- `{service}`：独立编译 `mvn -f {service}/pom.xml test`，启动 `java -cp {service}/target/classes <入口全限定类名>`，入口类和端口见服务源码。' for service in task['services']) + '\n\n单元测试不代表跨服务集成测试、契约测试或部署验收通过。\n'
            if settings['stack'] != 'standard':
                content = content.replace('需要 JDK 8 和 Maven，使用 JUnit 4.13.2。', '需要 JDK 17 和 Maven，使用 JUnit5、Spring Boot 3.5.8。').replace('仅使用 JDK 标准库，不包含 Spring 或其他业务框架。', '使用已审核的 Spring 和第三方依赖。可执行 mvn package，使用 java -jar target/<artifact>-1.0-SNAPSHOT.jar 启动；微服务请进入各模块目录执行。').replace('启动 `java -cp', '非 Spring 工程启动方式（Spring 请使用可执行 JAR）：`java -cp')
            retained[index] = {**item, 'content': content, 'diff': ''.join(difflib.unified_diff([], content.splitlines(keepends=True), fromfile='/dev/null', tofile='b/README.md'))}
    task['change_review'] = retained + validated
    task['affected_tests'] = tests
    task['generator'] = 'gpt-business-draft'
    task['business_draft_note'] = '代码和测试由模型生成，必须审核；测试通过不代表业务验收完成。'
    return task


def validate_java_file(name: str, content: str, is_test: bool, application_packages: set[str] | None = None, allowed_packages: list[str] | None = None, junit5: bool = False) -> None:
    """Conservative preflight, not a Java parser; javac remains authoritative."""
    relative = name.split('/java/', 1)[1]
    parts = relative.split('/')
    if len(parts) < 2 or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', part) for part in [*parts[:-1], Path(parts[-1]).stem]):
        raise ValueError(f"Java 必须使用合法包目录和类型文件名：{name}")
    code = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', ' ', content, flags=re.DOTALL)
    expected_package = '.'.join(parts[:-1])
    declaration = re.search(r'\bpackage\s+([\w.]+)\s*;', code)
    if not declaration or declaration.group(1) != expected_package:
        raise ValueError(f"Java package 与文件目录不一致：{name}")
    public_types = re.findall(r'\bpublic\s+(?:(?:abstract|final|strictfp)\s+)*(?:class|interface|enum)\s+(\w+)', code)
    if not public_types or public_types[0] != Path(name).stem:
        raise ValueError(f"Java 公开类型名与文件名不一致：{name}")
    if len(public_types) != len(set(public_types)):
        raise ValueError(f'Java 文件包含重复公开类型：{name}')
    imports = re.findall(r'\bimport\s+(?:static\s+)?([\w.*]+)\s*;', code)
    for imported in imports:
        if any(imported.startswith(prefix + '.') for prefix in (allowed_packages or [])):
            continue
        if imported.startswith(('java.', 'javax.', 'com.sun.net.httpserver.')) or any(imported.startswith(package + '.') for package in (application_packages if application_packages is not None else {expected_package})):
            continue
        if is_test and ((junit5 and imported.startswith('org.junit.jupiter.')) or (not junit5 and imported.startswith('org.junit.') and not imported.startswith(('org.junit.jupiter.', 'org.junit.platform.')))):
            continue
        hint = '。当前是 JUnit4：只能 import org.junit.Test 和 static org.junit.Assert.*，将 Assertions 改为 Assert；不要使用 Jupiter。' if imported.startswith('org.junit.jupiter.') and not junit5 else ''
        raise ValueError(f"Java 引用了未配置的第三方依赖：{imported}{hint}")
    junit_package = r'org\.junit\.jupiter\.api' if junit5 else r'org\.junit'
    if is_test and (not Path(name).stem.endswith('Test') or not re.search(r'@(?:(?:' + junit_package + r')\.)?Test\b', code) or not re.search(r'\bimport\s+' + junit_package + r'\.(?:Test|\*)\s*;|@' + junit_package + r'\.Test\b', code)):
        raise ValueError(f"Java 测试必须以 Test 结尾并包含 {'JUnit5' if junit5 else 'JUnit4'} @Test：{name}")
