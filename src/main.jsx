import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

const API = 'http://127.0.0.1:8765/api'

function App() {
  const [path, setPath] = useState('')
  const [project, setProject] = useState(null)
  const [tasks, setTasks] = useState([])
  const [selectedTask, setSelectedTask] = useState(null)
  const [request, setRequest] = useState('')
  const [mode, setMode] = useState('ask')
  const [serviceOnline, setServiceOnline] = useState(false)
  const [message, setMessage] = useState('等待连接本地 Agent 服务')
  const [events, setEvents] = useState([])
  const [busy, setBusy] = useState(false)
  const [newPath, setNewPath] = useState('')
  const [language, setLanguage] = useState('python')
  const [goal, setGoal] = useState('')
  const [answers, setAnswers] = useState({})
  const clarificationFields = { users: '用户角色', boundary: '交易或微服务边界', workflow: '核心业务流程及输入输出', acceptance: '可验证的验收标准', constraints: '安全、数据和运行约束', services: 'Java 微服务清单（可选）：2 至 10 个小写服务名，英文逗号分隔，例如 order-service,payment-service。请注明职责、端口和接口契约。', java_stack: 'Java 技术栈（可选）：standard（Java 8/JUnit4）、spring-boot 或 spring-cloud（Java 17/JUnit5，Boot 3.5.8 / Cloud 2025.0.0）', dependencies: '第三方 Maven 依赖 JSON（可选，应用于各服务）：例如 [{"groupId":"com.google.guava","artifactId":"guava","version":"33.4.8-jre","scope":"compile","packages":["com.google.common"]}]。支持 compile/runtime/test；请审核来源、许可证和漏洞，构建会下载并执行第三方代码。' }

  async function reviewRequirements(action) {
    setBusy(true)
    try {
      const response = await fetch(`${API}/tasks/${selectedTask.id}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(action === 'clarify' ? { answers } : { confirmed: true }) })
      const data = await response.json()
      if (data.task) setSelectedTask(data.task)
      if (data.task?.answers) setAnswers(data.task.answers)
      if (!response.ok) throw new Error(data.error)
      await loadTasks()
      setMessage(`当前阶段：${data.task.status}`)
    } catch (error) { setMessage(error.message) }
    finally { setBusy(false) }
  }

  async function generateBusiness() {
    setBusy(true)
    try {
      const response = await fetch(`${API}/tasks/${selectedTask.id}/generate-business`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      const data = await response.json()
      if (data.task) setSelectedTask(data.task)
      if (!response.ok) throw new Error(data.error)
      await loadTasks()
      setMessage('业务草案已生成，请审核全部文件差异再确认创建')
    } catch (error) { setMessage(error.message) }
    finally { setBusy(false) }
  }

  async function previewProject() {
    setBusy(true)
    try {
      const response = await fetch(`${API}/tasks/new-project`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: newPath, language, goal, answers }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error)
      setSelectedTask(data.task)
      await loadTasks()
      setMessage('需求与设计草稿已生成，请补充澄清项；业务需求尚未实现')
    } catch (error) { setMessage(error.message) }
    finally { setBusy(false) }
  }

  async function openTask(id) {
    try {
      const response = await fetch(`${API}/tasks/${id}/preview`)
      const data = await response.json()
      if (!response.ok) throw new Error(data.error)
      setSelectedTask(data.task)
      setAnswers(data.task.answers || {})
      setPath(data.task.analysis?.path || '')
      setProject(data.task.analysis || null)
      setRequest(data.task.request || '')
    } catch (error) { setMessage(error.message) }
  }

  async function retryTests() {
    setBusy(true)
    try {
      const response = await fetch(`${API}/tasks/${selectedTask.id}/retry-tests`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirmed: true }) })
      const data = await response.json()
      if (data.task) setSelectedTask(data.task)
      if (!response.ok) throw new Error(data.error)
      setMessage(data.task.status === 'completed' ? '测试通过并已提交' : '测试仍未通过，请检查输出')
      await loadTasks()
    } catch (error) { setMessage(error.message) }
    finally { setBusy(false) }
  }

  const loadTasks = () => fetch(`${API}/tasks`).then((r) => r.json()).then((data) => setTasks(data.tasks || [])).catch(() => {})
  useEffect(() => {
    let active = true
    const check = () => fetch(`${API}/health`).then((response) => active && setServiceOnline(response.ok)).catch(() => active && setServiceOnline(false))
    check()
    const timer = setInterval(check, 3000)
    loadTasks()
    return () => { active = false; clearInterval(timer) }
  }, [])

  useEffect(() => {
    setEvents([])
    if (!selectedTask?.id || typeof EventSource === 'undefined') return undefined
    const source = new EventSource(`${API}/tasks/${selectedTask.id}/events`)
    source.onmessage = (event) => {
      const item = JSON.parse(event.data)
      setEvents((current) => [...current.slice(-29), item])
      setMessage(item.message)
    }
    source.onerror = () => source.close()
    return () => source.close()
  }, [selectedTask?.id])

  async function inspect() {
    setMessage('正在扫描项目...')
    try {
      const response = await fetch(`${API}/projects/inspect`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error)
      setProject(data.project); setMessage('项目扫描完成')
    } catch (error) { setMessage(error.message) }
  }

  async function setAuthorization(nextMode) {
    await fetch(`${API}/authorization`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: nextMode }) })
    setMode(nextMode); setMessage(`已切换为${nextMode === 'ask' ? '逐次确认' : nextMode === 'task' ? '任务授权' : '自动执行'}模式`)
  }

  async function createTask() {
    if (!path) { setMessage('请先输入项目路径'); return }
    setMessage('正在分析项目并生成文档草稿...')
    try {
      const response = await fetch(`${API}/tasks/analyze`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error)
      setSelectedTask(data.task); await loadTasks(); setMessage('文档草稿已生成，请确认后写入项目')
    } catch (error) { setMessage(error.message) }
  }

  async function confirmTask() {
    if (!selectedTask) return
    setMessage('正在写入文档并提交 Git...')
    const response = await fetch(`${API}/tasks/${selectedTask.id}/confirm`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirmed: true }) })
    const data = await response.json()
    if (data.task) setSelectedTask(data.task)
    if (!response.ok) { setMessage(data.error); return }
    await loadTasks(); setMessage(`当前阶段：${data.task.status}`)
  }

  async function draftCode() {
    setMessage('正在生成代码修改草案...')
    const response = await fetch(`${API}/tasks/${selectedTask.id}/draft-code`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    const data = await response.json()
    if (!response.ok) { setMessage(data.error); return }
    setSelectedTask(data.task); setMessage(data.task.code_draft?.files?.length ? '代码草案已生成，请检查后授权写入' : data.task.code_draft?.note)
  }

  async function planChange() {
    if (!path) { setMessage('请先输入项目路径'); return }
    if (!request.trim()) { setMessage('请描述要新增的功能或要修复的问题'); return }
    setMessage('正在分析需求影响范围...')
    try {
      const response = await fetch(`${API}/tasks/plan-change`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, request }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error)
      setSelectedTask(data.task); await loadTasks(); setMessage('影响分析完成，请确认计划后进入代码修改阶段')
    } catch (error) { setMessage(error.message) }
  }

  return <main>
    <header><div><span className="eyebrow">LOCAL ENGINEERING AGENT</span><h1>CodeAgent</h1></div><span className={serviceOnline ? 'status' : 'status offline'}><i />{serviceOnline ? '本地服务在线' : '正在连接本地服务'}</span></header>
    <section className="hero"><div><p className="kicker">从需求到交付的工程闭环</p><h2>让每一次代码修改<br /><em>都有依据、可验证、可追溯。</em></h2><p className="intro">面向 Java 与 Python 项目的本地代码助手。先梳理文档，再修改代码，最后用测试和 Git 留下完整证据。</p></div><div className="orb"><span>AGENT<br /><b>0.1</b></span></div></section>
    <section className="workspace"><div className="panel project-panel"><div className="panel-title"><span>01 / PROJECT</span><strong>接入项目</strong></div><label>本地项目路径</label><div className="path-row"><input value={path} onChange={(e) => setPath(e.target.value)} placeholder="例如 D:\\projects\\order-service" /><button onClick={inspect}>扫描</button></div>{project && <div className="project-card"><strong>{project.name}</strong><span>{project.languages.join(' · ')}</span><small>{project.git.is_repository ? `Git / ${project.git.branch || '未命名分支'}` : '不是 Git 仓库'}</small></div>}<p className="hint">首次接入会分析目录、语言、构建工具和已有测试。</p></div><div className="panel auth-panel"><div className="panel-title"><span>02 / CONTROL</span><strong>修改授权</strong></div><p>Agent 修改任何文件前，需要经过你的授权。授权只影响当前本地服务会话。</p><div className="modes">{[['ask', '逐次确认', '每次修改前询问'], ['task', '任务授权', '确认范围后连续执行'], ['auto', '自动执行', '适合明确的小任务']].map(([key, title, desc]) => <button className={mode === key ? 'mode active' : 'mode'} onClick={() => setAuthorization(key)} key={key}><b>{title}</b><small>{desc}</small></button>)}</div></div></section>
    <section className="panel change-panel"><div className="panel-title"><span>02 / ITERATION</span><strong>提出变更</strong><button className="primary" onClick={planChange}>分析影响范围</button></div><label>功能需求或缺陷描述</label><textarea value={request} onChange={(e) => setRequest(e.target.value)} placeholder="例如：订单超过 30 分钟未支付时自动关闭，并增加超时关闭的回归测试。" /><p className="hint">Agent 先生成影响分析和测试计划。确认计划后才会进入代码修改阶段。</p></section>
    <section className="bottom"><div className="panel task-panel"><div className="panel-title"><span>03 / WORKFLOW</span><strong>任务队列</strong><button className="primary" onClick={createTask}>+ 分析并生成文档</button></div>{tasks.length === 0 ? <div className="empty">输入项目路径后，Agent 将分析代码并生成需求、总体设计和详细设计草稿。</div> : tasks.map((task) => <div className="task" key={task.id}><div><b>{task.title}</b><small>{task.id} · {task.status}</small></div><span>{task.status === 'completed' ? '已完成' : '待确认'}</span></div>)}</div><div className="panel config-panel"><div className="panel-title"><span>04 / MODEL</span><strong>模型配置</strong></div><div className="config-row"><span>Provider</span><b>OpenAI Compatible</b></div><div className="config-row"><span>Model</span><b>读取 config/agent.yaml</b></div><div className="config-row"><span>文档基线</span><b>三份 Markdown 草稿</b></div></div></section>
    {selectedTask && <section className="panel review-panel"><div className="panel-title"><span>05 / REVIEW</span><strong>{selectedTask.status === 'awaiting_code_confirmation' ? '代码变更确认' : selectedTask.kind === 'change-plan' ? '影响分析确认' : '文档变更确认'}</strong>{selectedTask.status === 'approved_plan' && <button className="primary" onClick={draftCode}>生成代码草案</button>}{selectedTask.status === 'awaiting_code_confirmation' && <button className="primary" onClick={confirmTask}>确认写入并测试</button>}{selectedTask.status === 'awaiting_confirmation' && <button className="primary" onClick={confirmTask}>确认并提交计划</button>}</div><p className="hint">任务 `{selectedTask.id}` · 生成方式：{selectedTask.generator}。代码写入前会再次校验授权路径；测试失败时不会自动提交 Git。</p>{events.length > 0 && <div className="event-log"><b>实时执行日志</b>{events.map((item) => <div key={item.id}><time>{new Date(item.created_at).toLocaleTimeString()}</time><span>{item.message}</span></div>)}</div>}{selectedTask.kind === 'change-plan' && <div className="impact-grid"><div><b>预计修改代码</b><span>{selectedTask.affected_files?.length || 0} 个文件</span><small>{selectedTask.affected_files?.map((name) => <i key={name}>{name}</i>)}</small></div><div><b>关联测试</b><span>{selectedTask.affected_tests?.length || 0} 个文件</span><small>{selectedTask.test_plan?.commands?.map((name) => <i key={name}>{name}</i>)}</small></div></div>}{selectedTask.code_draft?.files?.length > 0 && <div className="doc-tabs code-draft">{selectedTask.code_draft.files.map((file) => <details key={file.path}><summary>{file.path}</summary><pre>{file.content}</pre></details>)}</div>}{selectedTask.test_result && <><div className={`test-result ${selectedTask.test_result.status}`}><b>单元测试 / 回归测试：{selectedTask.test_result.status}</b>{selectedTask.test_result.summary && <div className="test-summary">{Object.entries(selectedTask.test_result.summary).map(([key, value]) => <span key={key}>{key}: <b>{value}</b></span>)}</div>}<pre>{selectedTask.test_result.output}</pre></div><div className="delivery-list"><b>交付物</b>{(selectedTask.written_files || []).map((file) => <span key={file}>{file}</span>)}</div></>}{<div className="doc-tabs">{Object.entries(selectedTask.documents || {}).map(([name, content]) => <details key={name}><summary>{name}</summary><pre>{content}</pre></details>)}</div>}</section>}
    {selectedTask?.change_review && <section className="panel review-panel"><div className="panel-title"><span>DIFF / REVIEW</span><strong>代码与设计文档差异</strong></div><p className="hint">确认写入将应用以下审核内容。测试报告和执行记录在测试后生成，不属于预先冻结的设计正文。</p><div className="doc-tabs">{selectedTask.change_review.map((file) => <details key={file.path} open><summary>{file.kind === 'design' ? '设计文档' : '代码'} · {file.path}</summary><pre className="diff-view">{file.diff ? file.diff.split('\n').map((line, index) => <span className={line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : ''} key={index}>{line}{'\n'}</span>) : '文件内容未变化'}</pre></details>)}</div></section>}
    <section className="panel history-panel"><div className="panel-title"><span>06 / HISTORY</span><strong>恢复历史任务</strong><button className="primary" onClick={loadTasks}>刷新</button></div><div className="history-list">{tasks.map((task) => <button className={selectedTask?.id === task.id ? 'mode active' : 'mode'} key={task.id} onClick={() => openTask(task.id)}><b>{task.title}</b><small>{task.id} · {task.status}</small></button>)}</div></section>
    {selectedTask?.last_error && <section className="panel error-panel" role="alert"><strong>最近失败原因</strong><p>{selectedTask.last_error.message}</p><small>{selectedTask.last_error.at}</small></section>}
    {selectedTask?.status === 'testing_failed' && selectedTask.retry_baseline && <section className="panel retry-panel"><strong>安全重试</strong><p className="hint">仅当失败后 HEAD、工作区和文件内容未变化时重新执行测试。不会重新写入业务代码；通过后更新交付文档并提交 Git。</p><button className="primary" disabled={busy} onClick={retryTests}>{busy ? '正在重试...' : '确认重新测试'}</button></section>}
    <section className="panel new-project-panel"><div className="panel-title"><span>NEW / PROJECT</span><strong>从零创建项目</strong></div><label>目标目录（必须不存在，父目录必须存在且不在 Git 仓库内）</label><input value={newPath} onChange={(event) => setNewPath(event.target.value)} placeholder="D:\\Projects\\my-service" /><label>语言</label><select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="python">Python</option><option value="java">Java / Maven</option></select><label>项目目标</label><textarea value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="描述用户、业务交易和验收目标" /><p className="hint">当前只生成固定工程骨架和需求/设计草稿，不自动实现业务目标。审核上方差异后，通过任务确认按钮创建目录。测试通过后尝试首次 Git 提交，使用现有 Git 身份配置。</p><button className="primary" disabled={busy} onClick={previewProject}>生成骨架与文档预览</button></section>
    {selectedTask?.kind === 'new-project' && ['needs_clarification', 'awaiting_design_confirmation'].includes(selectedTask.status) && <section className="panel new-project-panel"><div className="panel-title"><span>REQUIREMENTS / REVIEW</span><strong>澄清与设计评审</strong></div><p className="hint">{selectedTask.open_questions?.length ? `待补充：${selectedTask.open_questions.join('、')}` : '澄清字段已填写。请审阅上方三份文档，批准后仍需审核骨架差异并确认创建。'}</p>{Object.entries(clarificationFields).map(([key, label]) => <div key={key}><label>{label}</label><textarea value={answers[key] || ''} onChange={(event) => setAnswers({ ...answers, [key]: event.target.value })} /></div>)}<button className="primary" disabled={busy} onClick={() => reviewRequirements('clarify')}>保存答案并更新文档</button>{selectedTask.status === 'awaiting_design_confirmation' && <button className="primary" disabled={busy || Object.keys(clarificationFields).some((key) => (answers[key] || '') !== selectedTask.answers[key])} onClick={() => reviewRequirements('approve-design')}>确认需求与设计草稿</button>}</section>}
    {selectedTask?.kind === 'new-project' && selectedTask.status === 'awaiting_confirmation' && selectedTask.design_approved_at && <section className="panel new-project-panel"><strong>按需求生成业务草案</strong><p className="hint">需要本地模型配置。将向配置的模型服务发送已批准需求与设计文档；生成后替换示例源码和测试，不立即写入磁盘。模型生成的代码及测试必须审核，确认创建后会在本机执行。</p><button className="primary" disabled={busy} onClick={generateBusiness}>{busy ? '正在生成...' : '授权调用模型并生成业务草案'}</button>{selectedTask.business_draft_note && <p className="hint">{selectedTask.business_draft_note}</p>}</section>}
    <footer><span>{message}</span><span>Markdown · Git 强制提交 · Java / Python</span></footer>
  </main>
}

createRoot(document.getElementById('root')).render(<App />)
