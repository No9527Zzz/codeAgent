"""Opt-in real JDK17/Maven acceptance with a simulated model response."""

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from server.new_project import plan_project, approve_project_design, generate_business_draft, confirm_project


@unittest.skipUnless(os.getenv('CODEAGENT_SPRING_INTEGRATION') == '1', 'Spring integration is opt-in')
class SpringIntegrationTests(unittest.TestCase):
    def test_cloud_controller_mockito_tests_and_executable_jar(self):
        files = [
            {'path': 'src/main/java/example/BillingApplication.java', 'content': '''package example;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
@SpringBootApplication
public class BillingApplication {
    public static void main(String[] args) { SpringApplication.run(BillingApplication.class, args); }
}
'''},
            {'path': 'src/main/java/example/BillingController.java', 'content': '''package example;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import java.util.function.IntSupplier;
@RestController
public class BillingController {
    private final IntSupplier downstream;
    public BillingController() { this(() -> 42); }
    public BillingController(IntSupplier downstream) { this.downstream = downstream; }
    @GetMapping("/total") public int total() { return downstream.getAsInt(); }
}
'''},
            {'path': 'src/test/java/example/BillingControllerTest.java', 'content': '''package example;
import org.junit.jupiter.api.Test;
import java.util.function.IntSupplier;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
public class BillingControllerTest {
    @Test public void controller() throws Exception {
        IntSupplier downstream = mock(IntSupplier.class);
        when(downstream.getAsInt()).thenReturn(42);
        standaloneSetup(new BillingController(downstream)).build().perform(get("/total"))
            .andExpect(status().isOk()).andExpect(content().string("42"));
        verify(downstream).getAsInt();
    }
    @Test public void boundary() {
        assertEquals(0, new BillingController(() -> 0).total());
    }
    @Test public void downstreamFailure() {
        IntSupplier downstream = mock(IntSupplier.class);
        when(downstream.getAsInt()).thenThrow(new IllegalStateException("unavailable"));
        assertThrows(IllegalStateException.class, () -> new BillingController(downstream).total());
    }
}
'''}]
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'billing'
            answers = dict.fromkeys(['users', 'boundary', 'workflow', 'acceptance', 'constraints'], 'billing HTTP API with isolated downstream')
            answers['java_stack'] = 'spring-cloud'
            task = approve_project_design(plan_project(str(destination), 'java', 'billing', answers))
            response = json.dumps({'choices': [{'message': {'content': json.dumps({'files': files})}}]}).encode()
            with patch.dict(os.environ, {'MODEL_TEST_KEY': 'fake'}), patch('server.new_project.urllib.request.urlopen', return_value=io.BytesIO(response)):
                task = generate_business_draft(task, {'llm': {'api_key_env': 'MODEL_TEST_KEY'}})
            identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.local', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.local'}
            with patch.dict(os.environ, identity): task = confirm_project(task)
            self.assertEqual(task['status'], 'completed', task['test_result']['output'])
            self.assertEqual(task['test_result']['summary']['total'], 3)
            jar = destination / 'target/billing-1.0-SNAPSHOT.jar'
            with zipfile.ZipFile(jar) as archive:
                self.assertIn('BOOT-INF/classes/example/BillingApplication.class', archive.namelist())
                manifest = archive.read('META-INF/MANIFEST.MF').decode()
                self.assertIn('Start-Class: example.BillingApplication', manifest)
                self.assertIn('JarLauncher', manifest)
