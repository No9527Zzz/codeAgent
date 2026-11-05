"""Local HTTP acceptance test: fake model, real pytest and Git."""

import importlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        content = {'files': [{'path': 'app.py', 'content': 'def value():\n    return 2\n'}]}
        body = json.dumps({'choices': [{'message': {'content': json.dumps(content)}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpWorkflowTests(unittest.TestCase):
    def start_server(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(cleanup)
        return f'http://127.0.0.1:{server.server_port}'

    def test_plan_review_confirm_test_commit_and_reload(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        project = root / 'project'
        project.mkdir()
        (project / 'app.py').write_text('def value():\n    return 1\n')
        (project / 'test_app.py').write_text('from app import value\n\ndef test_value():\n    assert value() == 2\n')
        (project / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
        for args in [('init',), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.local'), ('add', '.'), ('commit', '-m', 'initial')]:
            subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True)
        environment = patch.dict(os.environ, {'CODEAGENT_DATABASE': str(root / 'state/tasks.db'), 'CODEAGENT_TEST_KEY': 'local-test-only'})
        environment.start()
        self.addCleanup(environment.stop)
        api = importlib.import_module('server.main')
        from server.storage import TaskStore
        isolated_store = TaskStore(root / 'state/tasks.db')
        store_patch = patch.object(api, 'STORE', isolated_store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        model_url = self.start_server(ModelHandler)
        config_patch = patch.object(api, 'config', return_value={'llm': {'base_url': model_url + '/v1', 'api_key_env': 'CODEAGENT_TEST_KEY', 'model': 'local-fixture'}})
        config_patch.start()
        self.addCleanup(config_patch.stop)
        base_url = self.start_server(api.Handler)

        def request(path, payload=None):
            body = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base_url + path, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())

        task = request('/api/tasks/plan-change', {'path': str(project), 'request': 'app'})['task']
        prefix = '/api/tasks/' + task['id']
        self.assertEqual(request(prefix + '/confirm', {'confirmed': True})['task']['status'], 'approved_plan')
        draft = request(prefix + '/draft-code', {})['task']
        self.assertEqual(draft['status'], 'awaiting_code_confirmation')
        self.assertEqual(len(draft['change_review']), 4)
        self.assertEqual((project / 'app.py').read_text(), 'def value():\n    return 1\n')
        result = request(prefix + '/confirm', {'confirmed': True})['task']
        self.assertEqual(result['status'], 'completed', result.get('test_result'))
        self.assertEqual(result['test_result']['status'], 'passed')
        reopened = TaskStore(root / 'state/tasks.db')
        self.assertEqual(reopened.get(task['id'])['status'], 'completed')
        self.assertEqual(request(prefix + '/preview')['task']['status'], 'completed')
        self.assertTrue(reopened.events(task['id']))
        head = subprocess.run(['git', '-C', str(project), 'rev-parse', 'HEAD'], capture_output=True, check=True).stdout
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            request(prefix + '/confirm', {'confirmed': True})
        self.assertEqual(rejected.exception.code, 400)
        self.assertEqual(subprocess.run(['git', '-C', str(project), 'rev-parse', 'HEAD'], capture_output=True, check=True).stdout, head)
        status = subprocess.run(['git', '-C', str(project), 'status', '--porcelain'], capture_output=True, check=True).stdout
        self.assertEqual(status, b'')
