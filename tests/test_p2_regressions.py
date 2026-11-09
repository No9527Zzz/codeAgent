"""Real Git regressions for staging/subdirectories and complete model context."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import agent


class P2RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / '订单 module'
        self.project.mkdir()
        self.write('app.py', 'value = 1\n')
        self.write('test_app.py', 'def test_value(): pass\n')
        self.write('.gitignore', '__pycache__/\n.pytest_cache/\n')
        (self.root / 'sibling.txt').write_text('original\n')
        for args in [('init',), ('config', 'user.name', 'Review'), ('config', 'user.email', 'review@example.local'), ('add', '.'), ('commit', '-m', 'initial')]:
            self.git(*args)

    def write(self, name, content):
        target = self.project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True, encoding='utf-8', check=True).stdout.strip()

    def task(self, request='app'):
        return agent.confirm_plan(agent.plan_change(str(self.project), request, {}))

    def review(self, task, files):
        task['code_draft'] = {'files': files}
        agent.prepare_change_review(task)
        task['status'] = 'awaiting_code_confirmation'
        task['test_command'] = ['python', '-m', 'pytest']
        return task

    def run_change(self, task, status='passed'):
        with patch('server.agent._run_tests', return_value={'status': status, 'output': status, 'duration_seconds': 0}):
            return agent.confirm_code_change(task)

    def test_nested_project_plan_and_code_commit_with_unchanged_test(self):
        task = self.review(self.task(), [
            {'path': 'app.py', 'content': 'value = 2\n'},
            {'path': 'test_app.py', 'content': (self.project / 'test_app.py').read_text()},
        ])
        result = self.run_change(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.git('status', '--porcelain'), '')
        committed = self.git('-c', 'core.quotePath=false', 'show', '--pretty=format:', '--name-only', 'HEAD').splitlines()
        self.assertIn('订单 module/app.py', committed)
        self.assertNotIn('订单 module/test_app.py', committed)
        self.assertNotIn('sibling.txt', committed)

    def test_repository_root_also_accepts_unchanged_files(self):
        self.project = self.root
        self.write('app.py', 'value = 1\n')
        self.write('test_app.py', 'def test_value(): pass\n')
        self.git('add', '.')
        self.git('commit', '-m', 'root files')
        task = self.review(self.task(), [{'path': 'app.py', 'content': 'value = 2\n'}, {'path': 'test_app.py', 'content': 'def test_value(): pass\n'}])
        self.assertEqual(self.run_change(task)['status'], 'completed')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_nested_document_baseline_commits_twice(self):
        for _ in range(2):
            task = agent.analyze_task(str(self.project), {})
            self.assertEqual(agent.confirm_task(task)['status'], 'completed')
            self.assertEqual(self.git('status', '--porcelain'), '')

    def test_staged_sibling_outside_project_is_still_rejected(self):
        task = self.review(self.task(), [{'path': 'app.py', 'content': 'value = 2\n'}])
        head = self.git('rev-parse', 'HEAD')
        original_run_git = agent.run_git

        def inject_staged_sibling(project, args, **kwargs):
            result = original_run_git(project, args, **kwargs)
            if args[0] == 'add':
                (self.root / 'sibling.txt').write_text('parallel work\n')
                self.git('add', 'sibling.txt')
            return result

        with patch('server.agent.run_git', side_effect=inject_staged_sibling):
            with self.assertRaisesRegex(ValueError, '暂存区文件集合'):
                self.run_change(task)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertIn('sibling.txt', self.git('diff', '--cached', '--name-only'))

    def test_unstaged_sibling_change_blocks_nested_plan(self):
        task = agent.plan_change(str(self.project), 'app', {})
        (self.root / 'sibling.txt').write_text('parallel work\n')
        with self.assertRaisesRegex(ValueError, '工作区'):
            agent.confirm_plan(task)
        self.assertFalse((self.project / 'docs').exists())

    def test_retry_with_unchanged_file_in_nested_project(self):
        task = self.review(self.task(), [{'path': 'app.py', 'content': 'value = 2\n'}, {'path': 'test_app.py', 'content': 'def test_value(): pass\n'}])
        result = self.run_change(task, 'failed')
        self.assertEqual(result['status'], 'testing_failed')
        with patch('server.agent._run_tests', return_value={'status': 'passed', 'output': 'passed', 'duration_seconds': 0}):
            self.assertEqual(agent.retry_tests(result)['status'], 'completed')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_staged_unicode_and_whitespace_path(self):
        name = ' 中文 file.py'
        self.write(name, 'value = 1\n')
        self.git('add', '.')
        self.git('commit', '-m', 'unicode file')
        self.write(name, 'value = 2\n')
        self.git('add', '.')
        agent._verify_staged_files(self.project, [name, 'test_app.py'])

    def test_git_line_ending_normalization_does_not_require_unchanged_path(self):
        self.git('config', 'core.autocrlf', 'true')
        self.write('normalized.py', 'value = 1\n')
        self.git('add', '.')
        self.git('commit', '-m', 'normalized')
        (self.project / 'normalized.py').write_bytes(b'value = 1\r\n')
        self.git('add', '.')
        agent._verify_staged_files(self.project, ['normalized.py'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def model_response(self, files, finish_reason='stop'):
        return io.BytesIO(json.dumps({'choices': [{'finish_reason': finish_reason, 'message': {'content': json.dumps({'files': files})}}]}).encode())

    def generate(self, task, response):
        with patch.dict(os.environ, {'P2_TEST_KEY': 'test-only'}), patch('server.agent.urllib.request.urlopen', return_value=response) as request:
            result = agent.draft_code_change(task, {'llm': {'api_key_env': 'P2_TEST_KEY'}})
        return result, request

    def test_all_candidates_and_large_file_tail_reach_model_without_disk_write(self):
        original = '# large file\n' + '# retained line\n' * 600 + 'value = 1\n'
        self.write('app.py', original)
        for index in range(25):
            self.write(f'src/helper{index:02}.py', f'value = {index}\n')
        self.git('add', '.')
        self.git('commit', '-m', 'many complete files')
        task = self.task('src app')
        result, request = self.generate(task, self.model_response([{'path': 'app.py', 'content': original.replace('value = 1', 'value = 2')}]))
        prompt = json.loads(request.call_args.args[0].data)['messages'][1]['content']
        context = {item['path']: item['content'] for item in json.loads(prompt.split('完整代码上下文（JSON）：', 1)[1])}
        expected = set(task['affected_files'] + task['affected_tests'])
        self.assertGreater(len(expected), 20)
        self.assertEqual(set(context), expected)
        self.assertEqual(context['app.py'].replace('\r\n', '\n'), original)
        self.assertIn('src/helper24.py', context)
        self.assertEqual(result['status'], 'awaiting_code_confirmation')
        self.assertEqual((self.project / 'app.py').read_text(), original)
        self.assertEqual(self.run_change(result)['status'], 'completed')
        self.assertEqual((self.project / 'app.py').read_text(), original.replace('value = 1', 'value = 2'))

    def test_oversized_context_refuses_before_network_or_write(self):
        self.write('app.py', '# ' + 'x' * agent.MAX_CHANGE_CONTEXT_BYTES)
        self.git('add', '.')
        self.git('commit', '-m', 'oversized source')
        task = self.task()
        before = (self.project / 'app.py').read_bytes()
        result, request = self.generate(task, self.model_response([]))
        request.assert_not_called()
        self.assertEqual(result['status'], 'approved_plan')
        self.assertIn('不会截断源码', result['code_draft']['note'])
        self.assertEqual((self.project / 'app.py').read_bytes(), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_unreadable_encoding_never_silently_skips_file(self):
        (self.project / 'app.py').write_bytes(b'\xff\xfe\xfd')
        self.git('add', '.')
        self.git('commit', '-m', 'non utf8 source')
        result, request = self.generate(self.task(), self.model_response([]))
        request.assert_not_called()
        self.assertEqual(result['status'], 'approved_plan')
        self.assertIn('无法完整读取代码文件', result['code_draft']['note'])

    def test_valid_json_with_truncated_finish_reason_is_not_reviewable(self):
        result, request = self.generate(self.task(), self.model_response([{'path': 'app.py', 'content': 'value = 2\n'}], finish_reason='length'))
        request.assert_called_once()
        self.assertEqual(result['status'], 'approved_plan')
        self.assertIn('截断', result['code_draft']['note'])
        self.assertNotIn('change_review', result)
        self.assertEqual((self.project / 'app.py').read_text(), 'value = 1\n')
