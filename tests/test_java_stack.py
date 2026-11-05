import io
import json
import os
import tempfile
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from server.java_stack import java_settings, configure_pom, java17_home
from server.new_project import plan_project, approve_project_design, generate_business_draft, validate_java_file


class JavaStackTests(unittest.TestCase):
    def test_java17_version_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'bin' / ('javac.exe' if os.name == 'nt' else 'javac')
            executable.parent.mkdir()
            executable.touch()
            with patch.dict(os.environ, {'CODEAGENT_JAVA17_HOME': folder}), patch('server.java_stack.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, 'javac 17.0.6', '')
                self.assertEqual(java17_home(), str(Path(folder).resolve()))
                run.return_value = subprocess.CompletedProcess([], 0, 'javac 1.8.0', '')
                with self.assertRaises(ValueError): java17_home()

    def test_boot_cloud_poms(self):
        original = (Path(__file__).resolve().parents[1] / 'examples/java-orders/pom.xml').read_text()
        for stack in ['standard', 'spring-boot', 'spring-cloud']:
            settings = java_settings({'java_stack': stack}, 'java')
            pom = configure_pom(original, settings)
            ET.fromstring(pom)
            if stack != 'standard':
                self.assertIn('3.5.8', pom)
                self.assertIn('<source>17</source>', pom)
                self.assertIn('spring-boot-starter-test', pom)
                self.assertNotIn('<groupId>junit</groupId>', pom)
            if stack == 'spring-cloud':
                self.assertIn('2025.0.0', pom)
                self.assertIn('spring-cloud-starter-openfeign', pom)

    def test_custom_dependencies_and_invalid_config(self):
        dep = {'groupId': 'com.google.guava', 'artifactId': 'guava', 'version': '33.4.8-jre', 'scope': 'compile', 'packages': ['com.google.common']}
        self.assertEqual(java_settings({'dependencies': json.dumps([dep])}, 'java')['dependencies'], [dep])
        for invalid in [{**dep, 'version': '${secret}'}, {**dep, 'scope': 'system'}, {**dep, 'packages': ['*']}, {**dep, 'groupId': 'org.springframework.boot'}, {**dep, 'artifactId': '<script>'}]:
            with self.subTest(dep=invalid), self.assertRaises(ValueError):
                java_settings({'dependencies': json.dumps([invalid])}, 'java')
        with self.assertRaises(ValueError): java_settings({'java_stack': 'spring-boot'}, 'python')

    def test_spring_junit5_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'billing')
            answers['java_stack'] = 'spring-cloud'
            task = approve_project_design(plan_project(str(Path(folder) / 'billing'), 'java', 'billing API', answers))
            files = [
                {'path': 'src/main/java/example/BillingApplication.java', 'content': 'package example;\nimport org.springframework.boot.SpringApplication;\nimport org.springframework.boot.autoconfigure.SpringBootApplication;\n@SpringBootApplication public class BillingApplication { public static void main(String[] args) { SpringApplication.run(BillingApplication.class, args); } }'},
                {'path': 'src/test/java/example/BillingTest.java', 'content': 'package example;\nimport org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.assertEquals;\npublic class BillingTest { @Test public void total() { assertEquals(2, 1 + 1); } }'}]
            response = json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode()
            with patch.dict(os.environ, {'STACK_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(response)):
                task = generate_business_draft(task, {'llm': {'api_key_env': 'STACK_KEY'}})
            self.assertEqual(task['affected_tests'], ['src/test/java/example/BillingTest.java'])
            self.assertFalse((Path(folder) / 'billing').exists())
            self.assertIn('spring-cloud-starter-openfeign', next(item['content'] for item in task['change_review'] if item['path'] == 'pom.xml'))

    def test_test_scope_import_not_allowed_in_source(self):
        code = 'package example;\nimport org.mockito.Mockito;\npublic class Service {}'
        with self.assertRaises(ValueError): validate_java_file('src/main/java/example/Service.java', code, False)
        validate_java_file('src/main/java/example/Service.java', code, False, allowed_packages=['org.mockito'])

    def test_cloud_microservice_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'commerce')
            answers.update(java_stack='spring-cloud', services='orders,payments')
            task = approve_project_design(plan_project(str(Path(folder) / 'commerce'), 'java', 'commerce', answers))
            files = {item['path']: item['content'] for item in task['change_review']}
            self.assertIn('${SERVER_PORT:8080}', files['orders/src/main/resources/application.properties'])
            self.assertIn('${SERVER_PORT:8081}', files['payments/src/main/resources/application.properties'])
            for service in task['services']:
                self.assertIn('spring-cloud-dependencies', files[service + '/pom.xml'])
