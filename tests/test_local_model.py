"""Explicit opt-in: executes locally generated model code, without sandboxing."""

import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.request
import time

from server.new_project import plan_project, approve_project_design, generate_business_draft, confirm_project


@unittest.skipUnless(os.getenv('CODEAGENT_LOCAL_MODEL_TEST') == '1', 'Real model execution is opt-in')
class LocalModelTests(unittest.TestCase):
    def test_generate_compile_and_test(self):
        stack = os.getenv('CODEAGENT_LOCAL_MODEL_STACK', 'standard')
        root = Path(tempfile.mkdtemp(prefix='codeagent-real-model-'))
        print(f'\n真实模型验证目录：{root}', flush=True)
        answers = {
            'users': '订单业务调用者',
            'boundary': '单一订单超时判断，不需要数据库、外部服务或额外依赖。包名 example。',
            'workflow': 'OrderService.shouldClose(int minutes)：负数抛 IllegalArgumentException，minutes >= 30 返回 true，否则 false。',
            'acceptance': '必须测试 0、29、30、31、负数，共至少 5 个测试用例。',
            'constraints': '只生成最少文件，所有测试独立，不启动服务器。',
            'java_stack': stack,
        }
        if stack != 'standard':
            answers['workflow'] += '增加 Spring Boot Application 和 OrderController，GET /should-close?minutes=30 返回布尔值，Controller 构造器注入 OrderService；Service 标注 @Service。'
            answers['acceptance'] += '另加 MockMvc standaloneSetup Controller 测试，不用 @SpringBootTest。'
        task = approve_project_design(plan_project(str(root / 'orders'), 'java', '实现最小订单超时判断及单元测试', answers))
        config = {'llm': {'base_url': os.getenv('CODEAGENT_LOCAL_MODEL_URL', 'http://localhost:11434/v1'), 'model': os.getenv('CODEAGENT_LOCAL_MODEL_NAME', 'deepseek-r1:7b'), 'api_key_env': 'LOCAL_MODEL_TEST_KEY', 'timeout_seconds': 180, 'max_repair_attempts': 0, 'max_tokens': 4096, 'compact_generation_context': True}}
        config['llm']['staged_java_generation'] = True
        urlopen = urllib.request.urlopen
        responses = []
        def capture(request, **kwargs):
            number = len(responses) + 1
            (root / f'model-request-{number}.json').write_bytes(request.data)
            started = time.monotonic()
            print(f'请求 {number} 开始，上限 4096 tokens，超时 180 秒', flush=True)
            try:
                with urlopen(request, **kwargs) as response:
                    content = response.read()
            except Exception as exc:
                (root / f'model-error-{number}.json').write_text(json.dumps({'error': str(exc), 'seconds': time.monotonic() - started}), encoding='utf-8')
                raise
            print(f'请求 {number} 完成：{time.monotonic() - started:.1f} 秒', flush=True)
            responses.append(content)
            (root / f'model-response-{len(responses)}.json').write_bytes(content)
            return io.BytesIO(content)
        with patch.dict(os.environ, {'LOCAL_MODEL_TEST_KEY': 'ollama'}), patch('server.new_project.urllib.request.urlopen', side_effect=capture):
            task = generate_business_draft(task, config)
        (root / 'review.json').write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
        identity = {'GIT_AUTHOR_NAME': 'CodeAgent Test', 'GIT_AUTHOR_EMAIL': 'test@example.local', 'GIT_COMMITTER_NAME': 'CodeAgent Test', 'GIT_COMMITTER_EMAIL': 'test@example.local'}
        with patch.dict(os.environ, identity):
            task = confirm_project(task)
        (root / 'result.json').write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertEqual(task['status'], 'completed', task.get('test_result'))
        self.assertGreaterEqual(task['test_result']['summary']['total'], 5)
