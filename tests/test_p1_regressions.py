"""Regression coverage for document protection, config parsing and test discovery."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import agent
from server.new_project import approve_project_design, generate_business_draft, plan_project


class DocumentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'app.py').write_text('value = 1\n', encoding='utf-8')
        self.document = self.root / 'docs/requirements/product-requirements.md'
        self.document.parent.mkdir(parents=True)
        self.document.write_text('original document\n', encoding='utf-8')
        for args in [('init',), ('config', 'user.name', 'Review'), ('config', 'user.email', 'review@example.local'), ('add', '.'), ('commit', '-m', 'initial')]:
            self.git(*args)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def test_clean_documents_commit_and_repeat_analysis_with_unchanged_documents(self):
        for _ in range(2):
            task = agent.analyze_task(str(self.root), {})
            result = agent.confirm_task(task)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertIn('产品需求文档', self.document.read_text(encoding='utf-8'))

    def test_dirty_workspace_or_index_rejected_before_analysis(self):
        self.document.write_text('user work\n', encoding='utf-8')
        for staged in [False, True]:
            if staged:
                self.git('add', '.')
            with self.subTest(staged=staged), patch('server.agent.generate_documents') as generate:
                with self.assertRaisesRegex(ValueError, '未提交修改'):
                    agent.analyze_task(str(self.root), {})
                generate.assert_not_called()
        self.assertEqual(self.document.read_text(encoding='utf-8'), 'user work\n')

    def test_document_changed_after_preview_is_preserved(self):
        task = agent.analyze_task(str(self.root), {})
        head = self.git('rev-parse', 'HEAD')
        self.document.write_text('user edit after preview\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            agent.confirm_task(task)
        self.assertEqual(self.document.read_text(encoding='utf-8'), 'user edit after preview\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertFalse((self.root / 'docs/architecture').exists())

    def test_staged_unrelated_file_after_preview_is_not_committed(self):
        task = agent.analyze_task(str(self.root), {})
        head = self.git('rev-parse', 'HEAD')
        (self.root / 'private.txt').write_text('unrelated user work\n')
        self.git('add', 'private.txt')
        with self.assertRaises(ValueError):
            agent.confirm_task(task)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'private.txt')
        self.assertEqual(self.document.read_text(encoding='utf-8'), 'original document\n')

    def test_head_change_after_preview_blocks_write(self):
        task = agent.analyze_task(str(self.root), {})
        self.git('commit', '--allow-empty', '-m', 'parallel commit')
        with self.assertRaisesRegex(ValueError, 'HEAD'):
            agent.confirm_task(task)
        self.assertEqual(self.document.read_text(encoding='utf-8'), 'original document\n')

    def test_ignored_document_change_is_checked_by_digest(self):
        (self.root / '.gitignore').write_text('docs/architecture/\n')
        self.git('add', '.gitignore')
        self.git('commit', '-m', 'ignore architecture')
        task = agent.analyze_task(str(self.root), {})
        ignored = self.root / 'docs/architecture/overall-design.md'
        ignored.parent.mkdir(parents=True)
        ignored.write_text('ignored user content\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        with self.assertRaisesRegex(ValueError, '基线文件已变化'):
            agent.confirm_task(task)
        self.assertEqual(ignored.read_text(), 'ignored user content\n')

    def test_change_log_changed_after_preview_blocks_all_writes(self):
        task = agent.analyze_task(str(self.root), {})
        log = self.root / 'docs/changes/change-log.md'
        log.parent.mkdir(parents=True)
        log.write_text('user change log\n')
        with self.assertRaises(ValueError):
            agent.confirm_task(task)
        self.assertEqual(self.document.read_text(encoding='utf-8'), 'original document\n')
        self.assertEqual(log.read_text(), 'user change log\n')

    def test_legacy_task_without_baseline_requires_new_analysis(self):
        task = agent.analyze_task(str(self.root), {})
        task.pop('git_baseline')
        with self.assertRaisesRegex(ValueError, '重新分析'):
            agent.confirm_task(task)
        self.assertEqual(self.document.read_text(encoding='utf-8'), 'original document\n')

    def test_parallel_staging_during_write_is_rejected_before_commit(self):
        task = agent.analyze_task(str(self.root), {})
        head = self.git('rev-parse', 'HEAD')
        original_run_git = agent.run_git

        def stage_extra(project, args, **kwargs):
            result = original_run_git(project, args, **kwargs)
            if args[0] == 'add':
                (self.root / 'private.txt').write_text('parallel user work\n')
                self.git('add', 'private.txt')
            return result

        with patch('server.agent.run_git', side_effect=stage_extra):
            with self.assertRaisesRegex(ValueError, '暂存区文件集合'):
                agent.confirm_task(task)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Keep the HTTP module's import-time store out of the real data directory.
        with patch.dict(os.environ, {'CODEAGENT_DATABASE': str(self.root / 'state.db')}):
            from server.main import parse_simple_yaml
        self.parse = parse_simple_yaml

    def test_example_config_scalar_types(self):
        config = self.parse(Path(__file__).resolve().parents[1] / 'config/agent.example.yaml')
        self.assertIs(type(config['llm']['max_repair_attempts']), int)
        self.assertEqual(config['llm']['max_repair_attempts'], 0)
        self.assertIs(config['llm']['staged_java_generation'], False)
        self.assertIs(config['llm']['compact_generation_context'], False)

    def test_comments_preserve_quoted_values_and_url_fragments(self):
        path = self.root / 'agent.yaml'
        path.write_text('''llm: # section comment
  base_url: https://example.local/v1#fragment # actual comment
  model: "model # literal" # actual comment
  api_key_env: 'KEY # literal' # actual comment
  quoted_boolean: "false" # remains a string
  quoted_number: '0' # remains a string
  escaped: "model \\" # literal" # actual comment
  apostrophe: 'it''s # literal' # actual comment
  temperature: 0.2 # numeric
''', encoding='utf-8')
        result = self.parse(path)['llm']
        self.assertEqual(result['base_url'], 'https://example.local/v1#fragment')
        self.assertEqual(result['model'], 'model # literal')
        self.assertEqual(result['api_key_env'], 'KEY # literal')
        self.assertEqual(result['quoted_boolean'], 'false')
        self.assertEqual(result['quoted_number'], '0')
        self.assertEqual(result['escaped'], 'model " # literal')
        self.assertEqual(result['apostrophe'], "it's # literal")
        self.assertEqual(result['temperature'], 0.2)

    def test_example_config_reaches_model_and_keeps_full_context(self):
        config = self.parse(Path(__file__).resolve().parents[1] / 'config/agent.example.yaml')
        answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'example')
        task = approve_project_design(plan_project(str(self.root / 'new-project'), 'python', 'example', answers))
        files = [{'path': 'app.py', 'content': 'value = 1\n'}, {'path': 'tests/test_app.py', 'content': 'def test_value(): assert True\n'}]
        response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode())
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('server.new_project.urllib.request.urlopen', return_value=response) as request:
            result = generate_business_draft(task, config)
        self.assertEqual(result['generator'], 'gpt-business-draft')
        request.assert_called_once()
        context = json.loads(json.loads(request.call_args.args[0].data)['messages'][1]['content'])
        self.assertIn('approved_documents', context)
        self.assertFalse((self.root / 'new-project').exists())


class TestDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content=''):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_java_reports_resources_and_python_helpers_do_not_block_maven(self):
        self.write('pom.xml', '<project/>')
        self.write('src/main/java/example/Order.java')
        self.write('src/test/java/example/OrderTest.java')
        for name in ['src/test/java/example/Fixture.java', 'src/test/resources/test.json', 'docs/testing/old-unit-test-report.md', 'docs/testing/TEST-old.xml', 'test_helper.py']:
            self.write(name)
        analysis = agent.inspect_project(str(self.root))
        _, tests = agent._impact_files(analysis, '订单超过30分钟未支付时自动关闭')
        self.assertEqual(tests, ['src/test/java/example/OrderTest.java'])

        def execute(*args, **kwargs):
            self.write('target/surefire-reports/TEST-example.OrderTest.xml', '<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
            return subprocess.CompletedProcess(args[0], 0, 'ok', '')

        with patch('server.agent.subprocess.run', side_effect=execute) as run:
            result = agent._run_tests(self.root, ['mvn'], tests, analysis['build_tools'])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(run.call_args.args[0], ['mvn', '-Dtest=example.OrderTest', 'test'])

    def test_python_conventions_exclude_conftest_reports_and_resources(self):
        expected = ['test_app.py', 'tests/order_test.py', 'tests/test_order.py']
        for name in expected + ['conftest.py', 'src/latest.py', 'tests/helper.py', 'tests/test_data.json', 'docs/testing/test_example.py', 'docs/testing/report.md']:
            self.write(name)
        self.assertEqual(sorted(agent.inspect_project(str(self.root))['test_files']), sorted(expected))

    def test_gradle_module_test_naming_conventions(self):
        self.write('service/build.gradle.kts')
        expected = [f'service/src/test/java/example/{name}.java' for name in ['TestOrder', 'OrderTest', 'OrderTests', 'OrderTestCase']]
        for name in expected + ['service/src/main/java/example/OrderTest.java', 'service/src/test/java/example/Fixture.java']:
            self.write(name)
        self.assertEqual(sorted(agent.inspect_project(str(self.root))['test_files']), sorted(expected))
