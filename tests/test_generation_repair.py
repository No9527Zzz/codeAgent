import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.new_project import plan_project, approve_project_design, generate_business_draft


class GenerationRepairTests(unittest.TestCase):
    def test_truncated_reasoning_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            envelope = {'choices': [{'message': {'content': '', 'reasoning': 'thinking'}, 'finish_reason': 'length'}]}
            with patch.dict(os.environ, {'REPAIR_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(envelope).encode())) as request:
                with self.assertRaisesRegex(ValueError, 'token 上限截断'):
                    generate_business_draft(self.task(folder), {'llm': {'api_key_env': 'REPAIR_KEY', 'max_tokens': 4096, 'compact_generation_context': True}})
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload['max_tokens'], 4096)
            self.assertNotIn('approved_documents', json.loads(payload['messages'][1]['content']))

    def test_java_stages_preserve_source_and_supply_test_context(self):
        from test_java_generation import JAVA_FILES
        with tempfile.TemporaryDirectory() as folder:
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'example')
            task = approve_project_design(plan_project(str(Path(folder) / 'example'), 'java', 'example', answers))
            with patch.dict(os.environ, {'REPAIR_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', side_effect=[self.response(json.dumps({'files': [JAVA_FILES[0]]})), self.response(json.dumps({'files': [JAVA_FILES[1]]}))]) as request:
                result = generate_business_draft(task, {'llm': {'api_key_env': 'REPAIR_KEY', 'staged_java_generation': True}})
            payload = json.loads(request.call_args.args[0].data)
            context = json.loads(payload['messages'][1]['content'])
            self.assertEqual(context['existing_sources'][0]['content'], JAVA_FILES[0]['content'])
            self.assertEqual([item['stage'] for item in result['generation_attempts']], ['source', 'tests'])
            self.assertEqual(len([item for item in result['change_review'] if item['kind'] == 'business-code']), 2)

    def test_failed_test_stage_does_not_replace_review(self):
        from test_java_generation import JAVA_FILES
        with tempfile.TemporaryDirectory() as folder:
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'example')
            task = approve_project_design(plan_project(str(Path(folder) / 'example'), 'java', 'example', answers))
            before = list(task['change_review'])
            with patch.dict(os.environ, {'REPAIR_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', side_effect=[self.response(json.dumps({'files': [JAVA_FILES[0]]})), self.response(json.dumps({'files': [JAVA_FILES[0]]}))]):
                with self.assertRaises(ValueError):
                    generate_business_draft(task, {'llm': {'api_key_env': 'REPAIR_KEY', 'staged_java_generation': True}})
            self.assertEqual(task['change_review'], before)

    def task(self, folder):
        answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'example')
        return approve_project_design(plan_project(str(Path(folder) / 'example'), 'python', 'example', answers))

    def response(self, content):
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': content}}]}).encode())

    def test_feedback_repair_then_review_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            task = self.task(folder)
            valid = json.dumps({'files': [{'path': 'app.py', 'content': 'def value(): return 1'}, {'path': 'tests/test_app.py', 'content': 'from app import value\ndef test_value(): assert value() == 1'}]})
            with patch.dict(os.environ, {'REPAIR_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', side_effect=[self.response('not JSON'), self.response(valid)]) as request:
                result = generate_business_draft(task, {'llm': {'api_key_env': 'REPAIR_KEY', 'max_repair_attempts': 2}})
            self.assertEqual([item['status'] for item in result['generation_attempts']], ['rejected', 'validated'])
            payload = json.loads(request.call_args.args[0].data)
            self.assertIn('校验拒绝', payload['messages'][-1]['content'])
            self.assertFalse((Path(folder) / 'example').exists())

    def test_exhaustion_preserves_review_and_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            task = self.task(folder)
            before = list(task['change_review'])
            invalid = json.dumps({'files': [{'path': '../escape.py', 'content': 'bad'}]})
            with patch.dict(os.environ, {'REPAIR_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', side_effect=[self.response(invalid) for _ in range(3)]) as request:
                with self.assertRaisesRegex(ValueError, '3 次尝试'):
                    generate_business_draft(task, {'llm': {'api_key_env': 'REPAIR_KEY', 'max_repair_attempts': 2}})
            self.assertEqual(request.call_count, 3)
            self.assertEqual(task['change_review'], before)
            self.assertEqual(len(task['generation_attempts']), 3)

    def test_network_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'REPAIR_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', side_effect=TimeoutError('timeout')) as request:
                with self.assertRaises(TimeoutError):
                    generate_business_draft(self.task(folder), {'llm': {'api_key_env': 'REPAIR_KEY', 'max_repair_attempts': 2}})
            self.assertEqual(request.call_count, 1)
