import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.agent import _junit_snapshot, _parse_junit_reports, _run_tests


class TestExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def module(self, name="order", gradle=False):
        module = self.root / name
        module.mkdir()
        (module / ("build.gradle" if gradle else "pom.xml")).write_text("")
        test = module / "src/test/java/example/OrderTest.java"
        test.parent.mkdir(parents=True)
        test.write_text("package example; class OrderTest {}")
        return module, test.relative_to(self.root).as_posix()

    def report(self, module, gradle=False, failures=0):
        folder = module / ("build/test-results/test" if gradle else "target/surefire-reports")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "TEST-example.OrderTest.xml").write_text(
            f'<testsuite tests="3" failures="{failures}" errors="0" skipped="1" time="0.2"/>')

    @patch("server.agent.subprocess.run")
    def test_empty_scope_never_executes(self, run):
        for command, tools in [(["mvn"], ["Maven"]), (["gradlew.bat"], ["Gradle"]), (["python", "-m", "pytest"], [])]:
            self.assertEqual(_run_tests(self.root, command, [], tools)["status"], "not_run")
        run.assert_not_called()

    @patch("server.agent.subprocess.run")
    def test_maven_executes_in_module_with_class_filter(self, run):
        module, test = self.module()
        def execute(*args, **kwargs):
            self.report(module)
            return subprocess.CompletedProcess(args[0], 0, "ok", "")
        run.side_effect = execute
        result = _run_tests(self.root, ["mvn"], [test], ["Maven"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["summary"]["passed"], 2)
        self.assertEqual(run.call_args.args[0], ["mvn", "-Dtest=example.OrderTest", "test"])
        self.assertEqual(run.call_args.kwargs["cwd"], module)

    @patch("server.agent.subprocess.run")
    def test_gradle_filters_class(self, run):
        module, test = self.module(gradle=True)
        def execute(*args, **kwargs):
            self.report(module, gradle=True)
            return subprocess.CompletedProcess(args[0], 0, "ok", "")
        run.side_effect = execute
        self.assertEqual(_run_tests(self.root, ["gradlew.bat"], [test], ["Gradle"])["status"], "passed")
        self.assertEqual(run.call_args.args[0], ["gradle", "test", "--tests", "example.OrderTest"])

    @patch("server.agent.subprocess.run")
    def test_parent_wrapper_used_for_module(self, run):
        import os
        module, test = self.module(gradle=True)
        wrapper = self.root / ("gradlew.bat" if os.name == "nt" else "gradlew")
        wrapper.write_text("")
        run.return_value = subprocess.CompletedProcess([], 1, "", "build failed")
        _run_tests(self.root, ["gradlew.bat"], [test], ["Gradle"])
        self.assertEqual(run.call_args.args[0][0], str(wrapper))
        self.assertEqual(run.call_args.kwargs["cwd"], module)

    @patch("server.agent.subprocess.run")
    def test_multiple_maven_modules_aggregate(self, run):
        first_module, first = self.module("first")
        second_module, second = self.module("second")
        def execute(*args, **kwargs):
            self.report(kwargs['cwd'])
            return subprocess.CompletedProcess(args[0], 0, 'ok', '')
        run.side_effect = execute
        result = _run_tests(self.root, ["mvn"], [first, second], ["Maven"])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['summary']['total'], 6)
        self.assertEqual([call.kwargs['cwd'] for call in run.call_args_list], [first_module, second_module])

    @patch("server.agent.subprocess.run")
    def test_one_failed_module_fails_aggregate(self, run):
        _, first = self.module('first')
        _, second = self.module('second')
        def execute(*args, **kwargs):
            failed = kwargs['cwd'].name == 'second'
            self.report(kwargs['cwd'], failures=int(failed))
            return subprocess.CompletedProcess(args[0], int(failed), 'test output', '')
        run.side_effect = execute
        result = _run_tests(self.root, ['mvn'], [first, second], ['Maven'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['modules']), 2)
        self.assertEqual(result['summary']['failed'], 1)

    @patch("server.agent.subprocess.run")
    def test_java_home_only_passed_to_build_subprocess(self, run):
        import os
        module, test = self.module()
        original = os.environ.get('JAVA_HOME')
        def execute(*args, **kwargs):
            self.report(module)
            return subprocess.CompletedProcess(args[0], 0, 'ok', '')
        run.side_effect = execute
        home = str(self.root / 'jdk17')
        result = _run_tests(self.root, ['mvn'], [test], ['Maven'], java_home=home, maven_goal='package')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(run.call_args.kwargs['env']['JAVA_HOME'], home)
        self.assertEqual(run.call_args.args[0][-1], 'package')
        self.assertEqual(os.environ.get('JAVA_HOME'), original)

    @patch("server.agent.subprocess.run")
    def test_old_report_cannot_prove_success(self, run):
        module, test = self.module()
        self.report(module)
        snapshot = _junit_snapshot(module)
        self.assertIsNone(_parse_junit_reports(module, snapshot))
        run.return_value = subprocess.CompletedProcess([], 0, "cached", "")
        self.assertEqual(_run_tests(self.root, ["mvn"], [test], ["Maven"])["status"], "failed")

    @patch("server.agent.subprocess.run")
    def test_xml_failure_overrides_zero_exit(self, run):
        module, test = self.module()
        def execute(*args, **kwargs):
            self.report(module, failures=1)
            return subprocess.CompletedProcess(args[0], 0, "", "")
        run.side_effect = execute
        self.assertEqual(_run_tests(self.root, ["mvn"], [test], ["Maven"])["status"], "failed")

    @patch("server.agent.subprocess.run")
    def test_python_keeps_explicit_scope(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "passed", "")
        result = _run_tests(self.root, ["python", "-m", "pytest"], ["tests/test_order.py"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(run.call_args.args[0], ["python", "-m", "pytest", "tests/test_order.py"])

    def test_invalid_report_is_not_zero_test_success(self):
        module, _ = self.module()
        folder = module / "target/surefire-reports"
        folder.mkdir(parents=True)
        (folder / "TEST-broken.xml").write_text('<testsuite tests="invalid"/>')
        self.assertIsNone(_parse_junit_reports(module))


if __name__ == "__main__":
    unittest.main()
