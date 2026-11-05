import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.agent import confirm_code_change, confirm_plan, confirm_task, plan_change, prepare_change_review, retry_tests
from server.storage import TaskStore


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'app.py').write_text('value = 1\n')
        (self.root / 'test_app.py').write_text('def test_value(): pass\n')
        for args in [('init',), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.local'), ('add', '.'), ('commit', '-m', 'initial')]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

    def failed_task(self):
        task = confirm_plan(plan_change(str(self.root), 'app', {}))
        task['status'] = 'awaiting_code_confirmation'
        task['code_draft'] = {'files': [{'path': 'app.py', 'content': 'value = 2\n'}]}
        task['test_command'] = ['python', '-m', 'pytest']
        prepare_change_review(task)
        with patch('server.agent._run_tests', return_value={'status': 'failed', 'output': 'failed', 'duration_seconds': 0}):
            return confirm_code_change(task)

    def test_retry_preserves_code_and_commits_on_pass(self):
        task = self.failed_task()
        before = (self.root / 'app.py').read_bytes()
        with patch('server.agent._run_tests', return_value={'status': 'passed', 'output': 'passed', 'duration_seconds': 0}):
            result = retry_tests(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.root / 'app.py').read_bytes(), before)
        self.assertNotIn('last_error', result)

    def test_retry_blocks_changed_file(self):
        task = self.failed_task()
        (self.root / 'app.py').write_text('user change\n')
        with patch('server.agent._run_tests') as run:
            with self.assertRaises(ValueError): retry_tests(task)
            run.assert_not_called()
        self.assertEqual((self.root / 'app.py').read_text(), 'user change\n')

    def test_retry_blocks_staged_user_file(self):
        task = self.failed_task()
        subprocess.run(['git', '-C', str(self.root), 'add', 'app.py'], check=True)
        with self.assertRaises(ValueError): retry_tests(task)

    def test_completed_task_cannot_repeat_write(self):
        for operation in [confirm_plan, confirm_task, confirm_code_change, retry_tests]:
            with self.assertRaises(ValueError): operation({'status': 'completed'})

    def test_failure_snapshot_survives_store_reopen(self):
        task = self.failed_task()
        store = TaskStore(self.root / 'store.db')
        store.save(task)
        restored = TaskStore(self.root / 'store.db').get(task['id'])
        self.assertEqual(restored['retry_baseline'], task['retry_baseline'])
        self.assertEqual(restored['last_error'], task['last_error'])

    def test_review_contains_code_and_three_design_documents(self):
        task = confirm_plan(plan_change(str(self.root), 'app', {}))
        task['code_draft'] = {'files': [{'path': 'app.py', 'content': 'value = 2\n'}]}
        prepare_change_review(task)
        self.assertEqual(len(task['change_review']), 4)
        self.assertIn('-value = 1', task['change_review'][0]['diff'])
        self.assertIn('+value = 2', task['change_review'][0]['diff'])
        self.assertFalse((self.root / 'docs/architecture/overall-design.md').exists())

    def test_reviewed_document_change_blocks_all_code_writes(self):
        task = confirm_plan(plan_change(str(self.root), 'app', {}))
        task['code_draft'] = {'files': [{'path': 'app.py', 'content': 'value = 2\n'}]}
        prepare_change_review(task)
        task['status'] = 'awaiting_code_confirmation'
        document = self.root / 'docs/architecture/overall-design.md'
        document.parent.mkdir(parents=True)
        document.write_text('user design\n')
        with self.assertRaises(ValueError): confirm_code_change(task)
        self.assertEqual((self.root / 'app.py').read_text(), 'value = 1\n')
