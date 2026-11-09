# CodeAgent

完整功能清单、使用边界与修订记录见 [项目功能说明](docs/project-features.md)。该文档按当前实现整理，区分模拟验证、实机验证与待验收能力。

## 从零创建骨架

桌面端“从零创建项目”支持 Python 或 Java/Maven。输入一个不存在的目标目录（父目录必须存在且不在 Git 仓库内）和项目目标，先生成文件差异与需求、总体设计、详细设计草稿。预览不写入文件；确认后创建目录、初始化 Git、执行示例测试、生成报告和变更记录，测试通过后尝试首次提交。

接口：`POST /api/tasks/new-project`，请求字段 `path`、`language`（`python` 或 `java`）、`goal` 和可选 `answers`。先完成下述需求评审流程，再通过 `POST /api/tasks/{id}/confirm` 明确授权创建。

### 需求澄清与设计评审

新项目先进入 `needs_clarification`，需要填写 `answers` 中的 `users`（用户角色）、`boundary`（交易或微服务边界）、`workflow`（流程及输入输出）、`acceptance`（验收标准）、`constraints`（安全/数据/运行约束）。可通过 `POST /api/tasks/{id}/clarify` 提交补充答案。

答案完整后进入 `awaiting_design_confirmation`，三份 Markdown 草稿可在任务详情展开评审。通过 `POST /api/tasks/{id}/approve-design`，请求体 `{"confirmed": true}`，批准后进入 `awaiting_confirmation` 并生成骨架差异。最后再次确认才会创建文件。需求澄清、文档评审和差异预览均不会创建目标目录。

结构化答案只记录用户提供的信息，不保证内容正确或完整；当前设计仍是待完善草稿，不宣称已经形成业务级实现方案。界面编辑答案后必须先保存并更新文档，才允许批准设计。

### 业务代码草案

### Java 微服务生成与单元测试

### Spring 与第三方依赖

### 本地 Ollama 真实模型验证

请求资源诊断：本机 Ollama `/api/ps` 显示该模型使用 4096 上下文，量化 Q4_K_M，约 4.76 GB 显存；检查时显存未满，不能据此确定之前超时原因。一个上限 128 token 的简单 JSON 请求约 4.6 秒返回，但输出全用于 reasoning，正文为空且 `finish_reason=length`。当前对此明确报告截断，不把推理内容当作 JSON。

可通过 `llm.max_tokens` 设置输出上限（128 到 32768，包括部分服务的推理 token）；可选 `compact_generation_context: true` 仅发送目标、结构化答案及测试阶段实际源码，不重复发送完整草稿文档。该选项可能丢失仅存在于文档内的信息，默认关闭。真实测试现设 4096 tokens、180 秒、关闭自动修复以控制资源，记录逐请求内容、原始响应、耗时和网络错误。最近受限请求约 18 秒完成，但源码阶段仍夹带测试，被拒绝，真实生成仍未验收。

可选 `llm.staged_java_generation: true` 将 Java 请求拆为源码阶段和测试阶段，测试请求携带已生成源码，禁止修改源码。两个阶段分别有限修复，只有全部通过才替换任务的完整审核草案；测试阶段失败保留原审核内容，不创建目标项目。默认关闭，开启后至少两个模型请求，最多 `2 * (1 + max_repair_attempts)` 个。它是按阶段生成，不是逐文件生成，也不包含编译诊断反馈修复。

最近一次启用分阶段真实 DeepSeek 验证在源码请求等待 600 秒后超时；随后模型列表接口仍可访问。没有足够证据判断是推理耗时、上下文限制还是服务资源竞争，未自动重复请求，分阶段真实生成尚未验收。模拟测试已覆盖源码上下文传递、测试阶段不可改源码及失败时原审核不变。

模型草案支持 `llm.max_repair_attempts`（0 到 2，默认 0）。开启后，输出 JSON 或静态文件校验失败会携带具体错误再次请求完整文件集合，最多总计 3 次请求；每次重新校验，不落盘、不执行代码、不削减测试标准。`generation_attempts` 记录每次校验结果；修复耗尽保留原审核内容，网络错误不自动重试。调用方需考虑额外请求耗时和模型费用。当前只修复静态预检错误，不会自动运行或修复编译失败代码。

开启两次修复后的真实 DeepSeek 追加验证：Java8 分支仍重复混用 Jupiter；Spring 分支通过静态预检但测试源码存在语法错误，真实 Maven testCompile 失败，未提交。因此有限反馈机制已通过模拟测试，但真实 DeepSeek 生成仍未成功验收，需要后续逐文件生成与编译诊断反馈，而不能用模拟通过替代真实结果。

已连通 `http://localhost:11434/v1` 并确认 `deepseek-r1:7b` 存在。真实请求发现输出存在缺少 `files`、包声明/测试路径错误、混用 JUnit4/5，以及重复 Java 类型等问题；一次 Spring 草案进入真实 Maven 后编译失败，现场保留且未提交。因此当前没有真实 DeepSeek 代码生成成功验收结论。提示词已移除 Java 请求中的 Python 路径说明并增加完整文件格式要求，默认生成温度为 0；安全校验未放宽。

显式开启 `CODEAGENT_LOCAL_MODEL_TEST=1` 后执行 `python -m unittest discover -s tests -p test_local_model.py -v` 可重现。`CODEAGENT_LOCAL_MODEL_STACK=spring-boot` 切换 Spring，需同时设置 JDK17。可用 `CODEAGENT_LOCAL_MODEL_URL` 和 `CODEAGENT_LOCAL_MODEL_NAME` 覆盖地址、模型。此测试会调用真实模型并在本机执行生成代码，没有沙箱；原始响应、审核草案和构建结果保存在打印的临时目录，不自动删除。占位认证值仅在测试进程内设置，不写入正式配置。

Spring 创建时先检查 `CODEAGENT_JAVA17_HOME`（优先）或 `JAVA_HOME` 指向的完整 JDK，要求 `javac` 主版本至少为 17；版本不符会在创建目标目录之前拒绝。只向构建子进程传入 Java 环境，不修改系统环境。Spring 模块执行带测试类过滤的 `mvn package`，测试或打包失败均保留现场且不提交。默认 Java8 任务仍沿用原环境。

已在本机 IntelliJ JBR 17.0.6 上完成真实 Spring Cloud Maven 集成：MockMvc Controller、Mockito 下游替身、边界及下游失败共 3 个 JUnit5 用例，生成 Boot 可执行 JAR，并检查清单启动器与应用入口，随后首次 Git 提交。模型响应仍为模拟；未验证真实模型、真实 HTTP 进程启动或服务发现。

PowerShell 验证命令（JDK 路径请按本机调整）：

```powershell
$env:CODEAGENT_JAVA17_HOME = 'D:\Developer\IntelliJ IDEA 2023.1.2\jbr'
$env:CODEAGENT_SPRING_INTEGRATION = '1'
python -m unittest discover -s tests -p test_spring_integration.py -v
```

需求澄清新增 `java_stack`（默认 `standard`，也支持 `spring-boot`、`spring-cloud`）及 `dependencies`。Spring 方案固定使用 Java 17、Boot 3.5.8、JUnit5；Cloud 方案导入 2025.0.0 BOM，增加 OpenFeign 和 LoadBalancer。固定版本组合不是漏洞审计或持续安全支持承诺，生产使用前需单独评估。每个服务生成独立 `application.properties`，默认端口从 8080 递增，支持 `SERVER_PORT` 覆盖。Cloud 不自动部署注册中心，Feign 应配置明确 URL。Spring 项目必须先生成业务代码，不允许直接确认创建旧示例代码。

自定义依赖示例（JSON 数组，最多 20 项，应用到每个业务模块）：

```json
[{"groupId":"com.google.guava","artifactId":"guava","version":"33.4.8-jre","scope":"compile","packages":["com.google.common"]}]
```

作用域支持 `compile`、`runtime`、`test`；`runtime` 依赖不能用于源码导入，`test` 依赖仅允许测试导入。坐标和版本必须是固定文本，不允许变量、版本区间、XML 或自定义仓库。Spring 和测试框架依赖由系统管理，不允许用自定义清单覆盖。`packages` 是用户声明的预检范围，不是依赖真实性验证，最终以 Maven 解析和编译为准。不会自动识别许可证、漏洞或恶意依赖；Maven 下载与执行第三方代码不在沙箱内，必须审核 POM 差异。

下面关于 Java 8/JUnit4、禁止新增依赖和不支持 Spring 的限制仅适用于默认 `standard` 且无自定义依赖的旧方案。Spring 模式改用 JUnit5/Mockito，提示词要求隔离外部服务，不使用 `@SpringBootTest` 启动完整上下文。默认 Java 仍为 8，Spring 验证通过显式指定 JDK17 执行。原 Java8 真实 Maven 测试仍通过。

在需求澄清表单中填写可选 `services`，例如 `order-service,payment-service`。支持 2 至 10 个不重复的小写服务名。请在业务边界和流程中描述各服务职责、接口契约、端口、数据归属及失败处理。保存答案、重新评审设计后，系统生成父聚合 POM 和每个服务独立 POM，必须先生成业务代码与测试，不能直接创建空微服务骨架。

模型文件限定为 `<service>/src/main/java/` 与 `<service>/src/test/java/`，每个服务必须包含源码、JUnit4 测试和独立 `main` 入口。提示词要求使用 Java 8 标准库 HTTP 入口、可配置端口、接口注入和手写替身，单元测试不依赖真实下游。当前不支持 Spring Boot/Cloud、数据库依赖、服务间 Maven 源码依赖、注册中心、网关或自动部署。入口和依赖预检不是完整语法或行为检查，HTTP 行为、网络隔离与端口配置仍须审核，不能仅凭通过预检认定已实现。

确认创建后逐服务执行精确 JUnit 类过滤，汇总各模块的 XML 统计和执行输出；任一模块编译失败、测试失败或没有有效测试报告，整个任务均不通过且不提交。已有 Maven 项目的跨模块关联测试也使用此编排，Gradle 跨模块仍暂不支持。各模块需能独立解析依赖；不自动安装其他模块或推断模块依赖顺序。

真实 Maven 集成验证覆盖两个独立服务共 6 个用例及首次 Git 提交，模型响应为模拟数据；这不等于真实模型生成质量、HTTP 服务启动、跨服务契约或集成测试已验收。

Java 生成当前支持 Java 8、单模块 Maven、JUnit4。Maven `artifactId` 从目标目录名生成，不再固定为示例项目名。提示词要求业务正常流程、边界和非法输入测试；预检会检查 package 与路径一致、公开类型与文件名一致、测试类以 `Test` 结尾且有 JUnit4 注解，拒绝可识别的未配置第三方导入（包括 JUnit5、Spring、Mockito、Lombok）。预检是保守文本检查，不是 Java 语法解析器，最终编译结果以 Maven/javac 为准，也不是安全沙箱。

新增 Java 生成集成测试用模拟模型提供 `com.acme.orders.OrderService`，再真实创建项目、Maven 编译、执行 3 个 JUnit4 用例（超时边界、未超时、负数输入）、读取 XML 统计并完成首次 Git 提交。开启方式与其他 Java 集成测试相同：设置 `CODEAGENT_JAVA_INTEGRATION=1` 后执行全部 unittest。此验证不需要真实 GPT 密钥。

设计批准后，可点击“授权调用模型并生成业务草案”，对应 `POST /api/tasks/{id}/generate-business`。该操作向配置的 OpenAI 兼容接口发送需求与已批准文档，保留构建配置和文档，替换固定示例源码与测试，生成新 diff 供审核。缺少密钥或模型结果非法时不会创建项目，也不会用固定示例冒充业务实现。

当前只允许 Python 的 `app.py`、`src/*.py` 和 `tests/test_*.py`，或 Java 的 `src/main/java/*.java`、`src/test/java/*.java`（支持子目录）；限制为 50 个文件，必须同时有源码和测试。不支持模型新增依赖或构建脚本，业务实现限于标准库以及已有 pytest/JUnit4 配置。需求与设计正文仍是已批准的草稿，不自动宣称与模型实现完全一致。

模型代码和测试不是沙箱隔离运行：用户必须审核差异后才确认创建，测试会在本机执行。测试通过仅表明生成的测试通过，不代表独立业务验收通过。当前自动化验证使用模拟模型响应，未验证真实 GPT 输出质量。

当前为固定示例骨架，不调用模型实现用户业务目标；需求澄清项和设计待确认项保留在文档。Python 测试需要当前解释器安装 pytest；Java 需要 Java 和 Maven。Git 提交使用现有身份配置，不自动修改全局配置。测试或提交失败时保留创建现场，不自动删除文件，当前不支持新项目创建失败自动重试。

本地部署的代码工程助手，第一版支持 Java 和 Python 项目。

## 当前版本

当前仓库提供一个可运行的本地服务和桌面界面原型，包含：

- 本地项目路径配置
- Git 仓库状态读取
- Python/Java 项目类型识别
- GPT 配置文件读取
- 修改前授权模式
- 任务创建和任务状态展示
- 已有项目静态分析
- 需求文档、总体设计、详细设计草稿生成
- 文档预览和用户确认
- 文档基线写入与 Git 强制提交

后续将在此基础上增加需求文档、总体设计、详细设计、代码修改、单元测试和受影响范围回归测试。

## 启动本地服务

```powershell
python server\main.py
```

服务默认监听 `http://127.0.0.1:8765`。

## 已有项目文档基线流程

1. 在桌面端输入一个已有 Java/Python Git 项目的本地路径。
2. 点击“扫描”确认项目语言、模块、构建工具和测试文件。
3. 点击“分析并生成文档”，服务会生成需求、总体设计和详细设计草稿。
4. 在文档预览区检查内容，确认后才会写入目标项目的 `docs/` 目录。
5. Agent 会追加 `docs/changes/change-log.md`，并将本次文档修改提交到 Git。

文档基线分析需要已有 Git 提交，且工作区和暂存区干净。确认前会重新检查 HEAD、目标文档和变更记录的指纹，暂存后核对提交文件集合；检测到人工修改时停止执行。历史文档任务若没有完整安全基线，必须重新分析项目。

没有配置 GPT Key 时，系统使用本地静态分析草稿，并明确标注需要用户确认的推断内容；配置 GPT 后会优先调用 OpenAI 兼容接口，失败时自动回退到本地草稿。

## 迭代计划流程

在项目路径下输入功能需求或缺陷描述后，点击“分析影响范围”。Agent 会根据需求关键词、模块目录和测试目录生成：

- 预计修改的源码和配置文件
- 受影响模块
- 关联测试文件
- Maven、Gradle 或 pytest 测试命令
- 需求、总体设计、详细设计的待修改项

用户确认后，Agent 只会先提交迭代计划和详细变更记录，不会未经单独授权修改业务代码。对应接口为：

```text
POST /api/tasks/plan-change
POST /api/tasks/{id}/confirm
```

代码修改确认后，Agent 会同步更新需求、总体设计和详细设计，并生成以下交付物：

```text
docs/requirements/product-requirements.md
docs/architecture/overall-design.md
docs/detailed-design/module-design.md
docs/testing/<task>-unit-test-report.md
docs/testing/<task>-regression-test-report.md
docs/changes/change-log.md
```

单元测试和回归测试使用当前受影响范围执行。测试通过后才会提交 Git；测试失败时会保留代码、文档和测试报告在工作区，并将任务标记为失败，不生成完成提交。

## Git 安全基线

创建迭代计划时，项目必须满足以下条件：

- Git 工作区和暂存区为空。
- 项目已有至少一个 Git 提交。
- Agent 记录当前 `HEAD` 和受影响文件的 SHA-256 指纹。

后续确认阶段会重新校验 `HEAD`、工作区、暂存区和受影响文件指纹。任何用户或其他进程的并行修改都会终止任务，避免覆盖用户内容。Agent 执行 `git add` 后还会核对暂存区文件集合，防止把用户文件混入自动提交。

支持将仓库内的子目录作为项目路径；暂存校验使用仓库根目录相对路径，仍检查整个仓库，拒绝混入兄弟目录文件。未产生 Git 实际差异的候选文件不会再阻断提交。

已有项目生成代码草案时，会发送全部候选文件的完整 UTF-8 内容；序列化后的代码上下文超过 512 KiB、文件无法完整读取或编码不支持时，不调用模型并提示拆分任务，不再截断源码后要求模型补全整个文件。模型报告输出截断时也不会进入确认写入阶段。

## 统一差异审核

本地 HTTP 闭环验收使用模拟 OpenAI 兼容服务，但真实执行 pytest 和 Git；不需要 API Key，不向外部发送源码：

```powershell
python -m unittest discover -s tests -p test_http_workflow.py -v
```

覆盖需求计划、计划确认、模型草案响应、统一差异预览、确认写入、测试、Git 提交、SQLite 重载和重复确认拦截。可通过 `CODEAGENT_DATABASE` 指定 SQLite 路径，测试使用临时数据库，不污染真实任务。运行该测试需要 Python 环境安装 pytest。

真实 GPT 和桌面人工验收需要另行执行：配置 `config/agent.yaml`，在启动桌面应用的环境中设置模型密钥变量，再使用专用且已有初始提交的干净 Git 测试仓库。不要直接使用包含重要未备份代码的目录进行首次验收。当前自动化 HTTP 验收不能代替真实模型质量和桌面交互验收。

代码草案生成后，“代码与设计文档差异”展示代码和三份设计文档的 unified diff。设计修订在审核阶段冻结，包含需求、实际修改文件及待人工核对项，不再在测试后追加未预览的设计正文。确认前会统一校验所有预览路径和文件指纹，再写入审核内容；测试报告和执行记录仍在运行后生成。测试失败重试不会重复追加设计修订。历史任务若没有统一预览数据，不能直接写入代码，需要重新生成草案或创建新任务。

## 任务持久化和实时日志

任务和执行事件保存在本地 `data/codeagent.db`，服务重启后仍可查看历史任务。桌面端会通过 Server-Sent Events 订阅当前任务，实时显示分析、确认、代码写入、测试和 Git 提交过程。

在“恢复历史任务”区域选择任务，可以重新载入项目路径、需求、草案、测试结果、交付文件和最近失败原因。已完成任务不能再次确认写入；代码草案生成失败或未配置模型时保留已批准计划，可配置好模型后再次生成。

测试失败任务若有现场快照，可点击“确认重新测试”，或调用 `POST /api/tasks/{id}/retry-tests`，请求体为 `{"confirmed": true}`。重试不重新写入业务代码，只执行测试和文档/报告交付；重试前要求 HEAD、工作区状态和快照文件内容保持不变，且暂存区为空。若用户已修改文件，请创建新任务重新分析，不能直接重试旧任务。当前不支持 Git 提交失败或进程中断后的自动恢复，不会自动回滚用户文件。

服务端串行执行变更请求，并对并发操作返回 HTTP 409，防止重复点击引发并行写入。错误原因和事件日志保存在数据库；错误本身不会解除任务原有的授权与安全限制。

## Java 测试

Java 项目优先使用 Maven 或 Gradle，即使项目同时包含 Python 文件也不会误用 pytest：

- Maven：执行 `mvn -Dtest=<受影响测试类> test`。
- Gradle：执行 `gradlew.bat test --tests <受影响测试类>`。
- 测试类从 `src/test/java/com/example/OrderTest.java` 转换为 `com.example.OrderTest`。
- 解析 Maven Surefire、Failsafe 和 Gradle JUnit XML 报告。
- 报告记录总数、通过、失败、跳过、错误、耗时和原始输出。

精准执行的保护规则：未确定关联测试时不启动测试；Java 测试必须属于同一可定位的构建模块，跨模块集合需要拆分任务。优先查找模块或父目录的 Maven/Gradle Wrapper，无 Wrapper 的 Gradle 项目使用系统 `gradle`。只统计本次执行新增或更新的单元测试 XML，不再使用历史 Failsafe 报告证明单元测试成功。Java 命令退出成功但没有新报告或没有测试用例时，不判定为通过。

运行本项目自动化回归测试：

```powershell
python -m unittest discover -s tests -v
```

真实 Maven 集成验证使用 `examples/java-orders`，复制到临时目录执行，不会在示例目录留下构建结果。需要系统安装 Java 8 或更新版本及 Maven，首次执行可能下载依赖：

```powershell
$env:CODEAGENT_JAVA_INTEGRATION="1"
python -m unittest discover -s tests -v
```

集成测试连续执行两次指定的 `example.OrderServiceTest`，验证每次仅执行 2 个用例、只产生对应 XML，且不会执行 `UnrelatedTest`。默认不开启该集成测试，以便普通回归无需 Java 环境或网络。当前已验证真实 Maven 链路，Gradle 和 Wrapper 仍只有模拟测试覆盖。

## 启动前端

```powershell
npm install
npm run dev
```

桌面端使用 Tauri 2 封装 React 前端。也可以继续使用浏览器访问 Vite 开发界面。

启动 Tauri 桌面窗口：

```powershell
npm run tauri:dev
```

构建 Windows 可执行文件：

```powershell
npm run tauri:build
```

Tauri 会自动启动 Vite 开发服务器，并管理本地 Python Agent 服务的启动和关闭。如果 `8765` 端口已有健康服务，桌面应用会复用它，不会重复启动。

默认使用系统 `python` 启动服务；如需指定解释器，可以设置：

```powershell
$env:CODEAGENT_PYTHON="D:\\venvs\\codeagent\\Scripts\\python.exe"
```

如果项目根目录不是 Tauri 源码推导出的目录，可以设置 `CODEAGENT_ROOT`。开发调试时也可以手动启动服务：

```powershell
python server\main.py
```

## 配置

复制 `config\agent.example.yaml` 为 `config\agent.yaml`，填写模型地址、模型名和密钥环境变量。

## 目录结构

```text
server/       本地 Agent 服务
src/          React 前端
src-tauri/    Tauri 桌面端壳
config/       配置文件模板
docs/         Agent 生成的项目文档约定
```
