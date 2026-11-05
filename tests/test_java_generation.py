import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.new_project import approve_project_design, generate_business_draft, plan_project, validate_java_file


JAVA_FILES = [
    {'path': 'src/main/java/com/acme/orders/OrderService.java', 'content': '''package com.acme.orders;
public class OrderService {
    public boolean shouldClose(int minutes) {
        if (minutes < 0) throw new IllegalArgumentException("negative minutes");
        return minutes >= 30;
    }
}
'''},
    {'path': 'src/test/java/com/acme/orders/OrderServiceTest.java', 'content': '''package com.acme.orders;
import org.junit.Test;
import static org.junit.Assert.*;
public class OrderServiceTest {
    @Test public void boundary() { assertTrue(new OrderService().shouldClose(30)); }
    @Test public void unexpired() { assertFalse(new OrderService().shouldClose(29)); }
    @Test(expected = IllegalArgumentException.class)
    public void rejectsNegative() { new OrderService().shouldClose(-1); }
}
'''},
]


class JavaGenerationTests(unittest.TestCase):
    def test_invalid_service_names_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            for services in ['only-one', 'order,order', '../escape,payment', 'src,payment']:
                with self.subTest(services=services), self.assertRaises(ValueError):
                    plan_project(str(Path(raw) / 'commerce'), 'java', 'commerce', {'services': services})

    def test_missing_microservice_or_entrypoint_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'orders')
            answers['services'] = 'order-service,payment-service'
            task = approve_project_design(plan_project(str(Path(raw) / 'commerce'), 'java', 'commerce', answers))
            before = list(task['change_review'])
            for services in [['order-service'], ['order-service', 'payment-service']]:
                files = [{'path': service + '/' + item['path'], 'content': item['content']} for service in services for item in JAVA_FILES]
                response = json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode()
                with patch.dict(os.environ, {'JAVA_TEST_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(response)):
                    with self.assertRaises(ValueError): generate_business_draft(task, {'llm': {'api_key_env': 'JAVA_TEST_KEY'}})
                self.assertEqual(task['change_review'], before)

    def test_valid_packaged_source_and_test(self):
        for item in JAVA_FILES:
            validate_java_file(item['path'], item['content'], '/test/' in item['path'])

    def test_invalid_package_public_type_or_junit_rejected(self):
        path = JAVA_FILES[1]['path']
        code = JAVA_FILES[1]['content']
        for invalid in [code.replace('package com.acme.orders;', 'package wrong;'),
                        code.replace('class OrderServiceTest', 'class WrongName'),
                        code.replace('import org.junit.Test;', 'import org.junit.jupiter.api.Test;'),
                        code.replace('import org.junit.Test;', 'import org.mockito.Mockito;'),
                        code.replace('@Test', '@NotATest')]:
            with self.subTest(code=invalid):
                with self.assertRaises(ValueError): validate_java_file(path, invalid, True)

    def test_comments_cannot_fake_package_or_test(self):
        with self.assertRaises(ValueError):
            validate_java_file(JAVA_FILES[1]['path'], '// package com.acme.orders;\npublic class OrderServiceTest {}', True)

    def test_duplicate_public_types_rejected(self):
        with self.assertRaises(ValueError):
            validate_java_file('src/main/java/example/Service.java', 'package example; public interface Service {} public class Service {}', False)

    def test_generated_application_cross_package_import(self):
        code = 'package com.acme.api;\nimport com.acme.domain.Order;\npublic class Service {}'
        validate_java_file('src/main/java/com/acme/api/Service.java', code, False, {'com.acme.api', 'com.acme.domain'})
        with self.assertRaises(ValueError):
            validate_java_file('src/main/java/com/acme/api/Service.java', code, False)

    def test_generated_java_preview_retains_build_and_documents(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / 'billing-service'
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'orders')
            task = approve_project_design(plan_project(str(destination), 'java', 'order timeout', answers))
            content = json.dumps({'choices': [{'message': {'content': json.dumps({'files': JAVA_FILES})}}]}).encode()
            with patch.dict(os.environ, {'JAVA_TEST_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(content)):
                result = generate_business_draft(task, {'llm': {'api_key_env': 'JAVA_TEST_KEY'}})
            self.assertFalse(destination.exists())
            self.assertEqual(result['affected_tests'], [JAVA_FILES[1]['path']])
            pom = next(item['content'] for item in result['change_review'] if item['path'] == 'pom.xml')
            self.assertIn('<artifactId>billing-service</artifactId>', pom)
            self.assertFalse(any(item['path'].startswith('src/main/java/example/') for item in result['change_review']))
