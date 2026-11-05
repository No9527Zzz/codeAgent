"""Opt-in Maven integration tests; dependencies may require network access."""

import os
import shutil
import tempfile
import unittest
import io
import json
from unittest.mock import patch
from pathlib import Path

from server.agent import _run_tests
from server.agent import run_git
from server.new_project import approve_project_design, confirm_project, generate_business_draft, plan_project
from test_java_generation import JAVA_FILES


@unittest.skipUnless(os.getenv("CODEAGENT_JAVA_INTEGRATION") == "1", "Java integration is opt-in")
class JavaIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("mvn") or not shutil.which("java"):
            self.skipTest("Maven and Java are required")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        fixture = Path(__file__).resolve().parents[1] / "examples/java-orders"
        shutil.copytree(fixture, self.project / "orders", ignore=shutil.ignore_patterns("target"))
        self.module = self.project / "orders"
        self.test_file = "orders/src/test/java/example/OrderServiceTest.java"

    def test_real_maven_filters_and_parses_current_reports(self):
        result = _run_tests(self.project, ["mvn"], [self.test_file], ["Maven"])
        self.assertEqual(result["status"], "passed", result["output"])
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["summary"]["passed"], 2)
        self.assertEqual(result["summary"]["reports"], 1)
        self.assertFalse((self.module / "target/surefire-reports/TEST-example.UnrelatedTest.xml").exists())
        # A second run must produce fresh reports rather than reuse the first run.
        repeated = _run_tests(self.project, ["mvn"], [self.test_file], ["Maven"])
        self.assertEqual(repeated["status"], "passed", repeated["output"])
        self.assertEqual(repeated["summary"]["total"], 2)

    def test_generated_business_java_compiles_tests_and_commits(self):
        destination = self.project / 'generated-orders'
        answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'orders timeout: boundary 30 minutes, reject negative')
        task = approve_project_design(plan_project(str(destination), 'java', 'order timeout', answers))
        response = json.dumps({'choices': [{'message': {'content': json.dumps({'files': JAVA_FILES})}}]}).encode()
        with patch.dict(os.environ, {'JAVA_TEST_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(response)):
            task = generate_business_draft(task, {'llm': {'api_key_env': 'JAVA_TEST_KEY'}})
        self.assertFalse(destination.exists())
        identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.local', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.local'}
        with patch.dict(os.environ, identity):
            result = confirm_project(task)
        self.assertEqual(result['status'], 'completed', result['test_result']['output'])
        self.assertEqual(result['test_result']['summary']['total'], 3)
        self.assertEqual(result['test_result']['summary']['passed'], 3)
        self.assertIn('com.acme.orders.OrderServiceTest', result['test_result']['command'])
        self.assertTrue((destination / 'docs/testing/scaffold-test-report.md').exists())

    def test_generated_java_compile_failure_keeps_report_without_commit(self):
        destination = self.project / 'broken-orders'
        answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'orders')
        task = approve_project_design(plan_project(str(destination), 'java', 'order timeout', answers))
        files = [dict(item) for item in JAVA_FILES]
        files[0]['content'] = files[0]['content'].replace('return minutes >= 30;', 'return undefinedVariable;')
        response = json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode()
        with patch.dict(os.environ, {'JAVA_TEST_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(response)):
            task = generate_business_draft(task, {'llm': {'api_key_env': 'JAVA_TEST_KEY'}})
        result = confirm_project(task)
        self.assertEqual(result['status'], 'scaffold_testing_failed')
        self.assertNotEqual(result['test_result']['status'], 'passed')
        self.assertTrue((destination / files[0]['path']).exists())
        report = (destination / 'docs/testing/scaffold-test-report.md').read_text(encoding='utf-8')
        self.assertIn('undefinedVariable', report)
        self.assertFalse(run_git(destination, ['rev-parse', '--verify', 'HEAD'])[0])
        self.assertFalse(run_git(destination, ['diff', '--cached', '--name-only'])[1].strip())

    def test_microservices_generate_test_and_commit(self):
        destination = self.project / 'commerce'
        answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'independent order and payment services; fake downstream in unit tests')
        answers['services'] = 'order-service,payment-service'
        task = approve_project_design(plan_project(str(destination), 'java', 'commerce services', answers))
        with self.assertRaises(ValueError): confirm_project(task)
        files = []
        for service in task['services']:
            for item in JAVA_FILES:
                code = item['content']
                if '/main/' in item['path']:
                    code = code.replace('public class OrderService {', 'public class OrderService { public static void main(String[] args) {}')
                files.append({'path': service + '/' + item['path'], 'content': code})
        response = json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode()
        with patch.dict(os.environ, {'JAVA_TEST_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(response)):
            task = generate_business_draft(task, {'llm': {'api_key_env': 'JAVA_TEST_KEY'}})
        identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.local', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.local'}
        with patch.dict(os.environ, identity): result = confirm_project(task)
        self.assertEqual(result['status'], 'completed', result['test_result']['output'])
        self.assertEqual(result['test_result']['summary']['total'], 6)
        self.assertEqual(len(result['test_result']['modules']), 2)
