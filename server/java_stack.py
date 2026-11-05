"""Explicit, reviewable Java build settings; never accept model-written POMs."""

import json
import os
import re
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET


def java17_home() -> str:
    home = os.getenv('CODEAGENT_JAVA17_HOME') or os.getenv('JAVA_HOME', '')
    executable = Path(home) / 'bin' / ('javac.exe' if os.name == 'nt' else 'javac')
    if not home or not executable.is_file():
        raise ValueError('Spring 构建需要完整 JDK17+；请设置 CODEAGENT_JAVA17_HOME 或 JAVA_HOME')
    try:
        result = subprocess.run([str(executable), '-version'], capture_output=True, text=True, timeout=15, check=False)
        version = re.search(r'javac\s+(\d+)', result.stdout + result.stderr)
        if result.returncode or not version or int(version.group(1)) < 17:
            raise ValueError('Spring 构建需要 JDK17+，当前指定 JDK 版本过低或不可用')
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('无法检查 Spring JDK 工具链') from exc
    return str(Path(home).resolve())


def java_settings(answers: dict, language: str) -> dict:
    stack = answers.get('java_stack', '').strip() or 'standard'
    raw = answers.get('dependencies', '').strip()
    if stack not in {'standard', 'spring-boot', 'spring-cloud'}:
        raise ValueError('Java 技术栈必须为 standard、spring-boot 或 spring-cloud')
    if language != 'java' and (stack != 'standard' or raw):
        raise ValueError('Spring 和 Maven 依赖配置仅适用于 Java')
    try:
        dependencies = json.loads(raw) if raw else []
    except json.JSONDecodeError as exc:
        raise ValueError('第三方依赖必须为 JSON 数组') from exc
    if not isinstance(dependencies, list) or len(dependencies) > 20:
        raise ValueError('最多配置 20 个第三方依赖')
    seen = set()
    for dep in dependencies:
        if not isinstance(dep, dict) or set(dep) != {'groupId', 'artifactId', 'version', 'scope', 'packages'}:
            raise ValueError('依赖必须包含 groupId、artifactId、version、scope、packages')
        if any(not isinstance(dep[key], str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', dep[key]) for key in ('groupId', 'artifactId', 'version')):
            raise ValueError('依赖坐标和版本必须为固定值，禁止 XML、变量和版本区间')
        identity = (dep['groupId'], dep['artifactId'])
        if identity in seen or dep['groupId'].startswith(('org.springframework', 'org.junit', 'org.mockito')) or dep['groupId'] == 'junit':
            raise ValueError('依赖重复或覆盖了系统管理的 Spring/测试依赖')
        seen.add(identity)
        if dep['scope'] not in {'compile', 'runtime', 'test'}:
            raise ValueError('依赖 scope 仅支持 compile/runtime/test')
        if not isinstance(dep['packages'], list) or not dep['packages'] or any(not isinstance(package, str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', package) for package in dep['packages']):
            raise ValueError('依赖 packages 必须为非空包名前缀数组')
    return {'stack': stack, 'java_version': 17 if stack != 'standard' else 8, 'dependencies': dependencies}


def configure_pom(original: str, settings: dict) -> str:
    namespace = 'http://maven.apache.org/POM/4.0.0'
    ET.register_namespace('', namespace)
    tag = lambda name: '{' + namespace + '}' + name
    root = ET.fromstring(original)
    def add(parent, name, value=None):
        element = ET.SubElement(parent, tag(name))
        element.text = value
        return element
    deps = root.find(tag('dependencies'))
    def dependency(parent, group, artifact, version=None, scope=None):
        entry = add(parent, 'dependency')
        for key, value in [('groupId', group), ('artifactId', artifact), ('version', version), ('scope', scope)]:
            if value: add(entry, key, value)
        return entry
    if settings['stack'] != 'standard':
        parent = ET.Element(tag('parent'))
        for key, value in [('groupId', 'org.springframework.boot'), ('artifactId', 'spring-boot-starter-parent'), ('version', '3.5.8'), ('relativePath', '')]:
            add(parent, key, value)
        root.insert(1, parent)
        properties = root.find(tag('properties'))
        for name in ('maven.compiler.source', 'maven.compiler.target'):
            properties.find(tag(name)).text = '17'
        add(properties, 'java.version', '17')
        deps.clear()
        dependency(deps, 'org.springframework.boot', 'spring-boot-starter-web')
        dependency(deps, 'org.springframework.boot', 'spring-boot-starter-test', scope='test')
        plugins = root.find(tag('build')).find(tag('plugins'))
        compiler = plugins.find(tag('plugin')).find(tag('configuration'))
        for child in compiler: child.text = '17'
        plugin = add(plugins, 'plugin')
        add(plugin, 'groupId', 'org.springframework.boot')
        add(plugin, 'artifactId', 'spring-boot-maven-plugin')
        if settings['stack'] == 'spring-cloud':
            management = add(root, 'dependencyManagement')
            bom = dependency(add(management, 'dependencies'), 'org.springframework.cloud', 'spring-cloud-dependencies', '2025.0.0', 'import')
            add(bom, 'type', 'pom')
            dependency(deps, 'org.springframework.cloud', 'spring-cloud-starter-openfeign')
            dependency(deps, 'org.springframework.cloud', 'spring-cloud-starter-loadbalancer')
    for dep in settings['dependencies']:
        dependency(deps, dep['groupId'], dep['artifactId'], dep['version'], dep['scope'])
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='unicode') + '\n'
