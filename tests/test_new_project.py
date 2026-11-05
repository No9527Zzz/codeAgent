import os
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.new_project import approve_project_design, clarify_project, confirm_project, draft_project, generate_business_draft, plan_project


class NewProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / 'new-service'

    def test_preview_does_not_write(self):
        task = draft_project(str(self.target), 'python', 'Order API')
        self.assertFalse(self.target.exists())
        self.assertEqual(task['generator'], 'fixed-scaffold')
        self.assertTrue(all(item['diff'] for item in task['change_review']))

    def test_confirm_real_python_scaffold_and_git(self):
        task = draft_project(str(self.target), 'python', 'Order API')
        identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.local', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.local'}
        with patch.dict(os.environ, identity):
            result = confirm_project(task)
        self.assertEqual(result['status'], 'completed', result.get('test_result'))
        self.assertEqual(result['test_result']['status'], 'passed')
        self.assertTrue((self.target / 'docs/requirements/product-requirements.md').exists())
        self.assertEqual(subprocess.run(['git', '-C', str(self.target), 'status', '--porcelain'], capture_output=True, check=True).stdout, b'')
        with self.assertRaises(ValueError): confirm_project(task)

    def test_existing_directory_not_overwritten(self):
        task = draft_project(str(self.target), 'python', 'Order API')
        self.target.mkdir()
        sentinel = self.target / 'user.txt'
        sentinel.write_text('user data')
        with self.assertRaises(ValueError): confirm_project(task)
        self.assertEqual(sentinel.read_text(), 'user data')

    def test_nested_git_rejected(self):
        subprocess.run(['git', '-C', str(self.root), 'init'], capture_output=True, check=True)
        with self.assertRaises(ValueError): draft_project(str(self.target), 'python', 'Order API')

    def test_failure_keeps_files_without_commit(self):
        task = draft_project(str(self.target), 'python', 'Order API')
        with patch('server.new_project._run_tests', return_value={'status': 'error', 'output': 'missing pytest', 'command': 'pytest'}):
            result = confirm_project(task)
        self.assertEqual(result['status'], 'scaffold_testing_failed')
        self.assertTrue((self.target / 'app.py').exists())
        self.assertNotIn('commit', result)

    def test_java_preview_contains_maven_configuration(self):
        task = draft_project(str(self.target), 'java', 'Order API')
        self.assertIn('pom.xml', [item['path'] for item in task['change_review']])
        self.assertFalse(self.target.exists())

    def test_clarification_blocks_creation_and_design_approval(self):
        task = plan_project(str(self.target), 'python', 'Order API', {})
        self.assertEqual(task['status'], 'needs_clarification')
        self.assertEqual(len(task['open_questions']), 5)
        self.assertNotIn('change_review', task)
        with self.assertRaises(ValueError): approve_project_design(task)
        with self.assertRaises(ValueError): confirm_project(task)
        self.assertFalse(self.target.exists())

    def test_complete_answers_then_two_stage_review(self):
        task = plan_project(str(self.target), 'python', 'Order API', {})
        original_id = task['id']
        answers = {'users': 'operators', 'boundary': 'orders', 'workflow': 'create then pay', 'acceptance': 'reject duplicate ID', 'constraints': 'local sqlite'}
        task = clarify_project(task, answers)
        self.assertEqual(task['id'], original_id)
        self.assertEqual(task['status'], 'awaiting_design_confirmation')
        self.assertIn('reject duplicate ID', task['documents']['requirements/product-requirements.md'])
        with self.assertRaises(ValueError): confirm_project(task)
        task = approve_project_design(task)
        self.assertEqual(task['status'], 'awaiting_confirmation')
        self.assertFalse(self.target.exists())
        document = next(item for item in task['change_review'] if item['path'] == 'docs/requirements/product-requirements.md')
        self.assertEqual(document['content'], task['documents']['requirements/product-requirements.md'])
        with self.assertRaises(ValueError): clarify_project(task, answers)

    def test_nontext_answer_rejected(self):
        with self.assertRaises(ValueError): plan_project(str(self.target), 'python', 'Order API', {'users': ['invalid']})

    def approved_task(self):
        answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'user supplied')
        return approve_project_design(plan_project(str(self.target), 'python', 'Order API', answers))

    def model_response(self, files):
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode())

    def test_business_draft_requires_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): generate_business_draft(self.approved_task(), {})

    def test_model_draft_review_without_writing(self):
        task = self.approved_task()
        docs = [item for item in task['change_review'] if item['path'].startswith('docs/')]
        files = [{'path': 'app.py', 'content': 'def total(): return 5\n'}, {'path': 'tests/test_total.py', 'content': 'from app import total\ndef test_total(): assert total() == 5\n'}]
        with patch.dict(os.environ, {'TEST_MODEL_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=self.model_response(files)):
            task = generate_business_draft(task, {'llm': {'api_key_env': 'TEST_MODEL_KEY'}})
        self.assertEqual(task['generator'], 'gpt-business-draft')
        self.assertEqual(task['affected_tests'], ['tests/test_total.py'])
        self.assertFalse(self.target.exists())
        self.assertEqual([item for item in task['change_review'] if item['path'].startswith('docs/')], docs)
        identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.local', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.local'}
        with patch.dict(os.environ, identity):
            result = confirm_project(task)
        self.assertEqual(result['status'], 'completed', result.get('test_result'))

    def test_unsafe_model_path_rejected_without_changing_review(self):
        task = self.approved_task()
        before = list(task['change_review'])
        with patch.dict(os.environ, {'TEST_MODEL_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=self.model_response([{'path': '../escape.py', 'content': 'bad'}])):
            with self.assertRaises(ValueError): generate_business_draft(task, {'llm': {'api_key_env': 'TEST_MODEL_KEY'}})
        self.assertEqual(task['change_review'], before)
        self.assertFalse(self.target.exists())
