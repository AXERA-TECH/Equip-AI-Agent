/* ============================================================
   Equip AI Agent · 调试控制台 — 前端逻辑(零依赖原生 JS)
   复用后端 API 契约:/api/state /api/turn /api/config/* /api/remember
   ============================================================ */

let MACHINE_ID = 'cat-306-demo';
let SESSION_ID = 'debug-session';
const OPERATOR_ID = 'debug-operator';

const state = {
  machine: null,
  events: [],
  messages: [],
  configuration: null,
  runtimePlugins: [],
  eventFilter: '',
  activeView: 'console',
  activeSettings: 'model',
  turnController: null,
  machines: [],
  sessions: [],
  active: { machine: null, byMachine: {} },
  sessionMenuOpen: false,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]
));
const csv = (value) => String(value || '').split(',').map((s) => s.trim()).filter(Boolean);
const dateFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'short', timeStyle: 'medium',
});
const timeFormatter = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' });

const STEP_LABELS = {
  observe: '观察', state: '状态', retrieve: '检索', diagnose: '诊断', propose: '建议',
  execute: '执行', verify: '验证', approve: '审批', clarify: '澄清', plan: '规划',
};
const ARTIFACT_LABELS = {
  machine_snapshot: '设备快照', telemetry_summary: '遥测摘要', knowledge_evidence: '知识证据',
  diagnostic_assessment: '诊断评估', action_proposal: '行动建议',
  indicator_report: '指示灯报告', audio_alarm_report: '声音报警',
};
const CATEGORY_LABELS = {
  information: '信息', diagnostic: '诊断', clarification: '澄清',
  error: '错误', approval_required: '需审批',
};

/* ---------------- 网络 ---------------- */
async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}
async function post(path, payload, options = {}) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    ...options,
  });
}

let toastTimer;
function toast(message, error = false) {
  const node = $('toast');
  node.textContent = message;
  node.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast'; }, 2800);
}

function setConnection(online) {
  const node = $('conn-status');
  node.className = online ? 'conn' : 'conn offline';
  node.innerHTML = `<i></i>${online ? '本地调试' : '连接断开'}`;
}

function setBusy(button, busy, loadingLabel = '处理中…') {
  if (!button) return;
  button.disabled = busy;
  const label = button.querySelector('.button-label');
  if (label) {
    if (busy) {
      button.dataset.idleLabel = label.textContent;
      label.textContent = loadingLabel;
    } else {
      label.textContent = button.dataset.idleLabel || label.textContent;
    }
  }
  button.setAttribute('aria-busy', String(busy));
}

function showFormError(id, message = '') {
  const node = $(id);
  if (!node) return;
  node.textContent = message;
  node.hidden = !message;
  if (message) node.focus();
}

function updateUrl() {
  const url = new URL(window.location.href);
  url.searchParams.set('view', state.activeView);
  if (state.activeView === 'settings') url.searchParams.set('settings', state.activeSettings);
  else url.searchParams.delete('settings');
  if (state.activeView === 'audit' && state.eventFilter) url.searchParams.set('event', state.eventFilter);
  else url.searchParams.delete('event');
  if (url.href !== window.location.href) {
    window.history.pushState({ view: state.activeView, settings: state.activeSettings }, '', url);
  }
}

/* ---------------- 渲染:设备 ---------------- */
function renderStatusbar(machine) {
  $('q-model').textContent = machine.model;
  $('q-id').textContent = machine.machine_id;
  $('q-state').textContent = machine.operating_state;
  $('q-engine').textContent = machine.engine_running ? '运行中' : '已停止';
  $('q-fuel').textContent = `${Math.round(machine.fuel_percent)}%`;
  const faults = machine.fault_codes || [];
  $('q-fault').textContent = faults.length ? faults.join(' · ') : '无';
  document.querySelector('.sb-metric-fault').classList.toggle('has-fault', faults.length > 0);
}

function renderMachine(machine) {
  state.machine = machine;
  const faults = machine.fault_codes || [];
  const remaining = Math.max(0, machine.next_service_hours - machine.hour_meter);
  const fuel = Math.round(machine.fuel_percent);

  $('m-captured').textContent = timeFormatter.format(new Date(machine.captured_at));
  $('m-model').textContent = machine.model;
  $('m-serial').textContent = machine.serial_number;
  $('m-id').textContent = machine.machine_id;
  $('m-state').textContent = machine.operating_state;
  $('m-state-pill').classList.toggle('warn', faults.length > 0);
  $('m-fuel').textContent = `${fuel}%`;
  const bar = $('m-fuel-bar');
  bar.style.width = `${fuel}%`;
  bar.classList.toggle('low', fuel < 25);
  $('m-engine').textContent = machine.engine_running ? '运行中' : '已停止';
  $('m-hours').textContent = Number(machine.hour_meter).toFixed(1);
  $('m-service').textContent = `${remaining.toFixed(1)}h`;
  $('m-fault-count').textContent = faults.length;

  $('fault-list').innerHTML = faults.length
    ? faults.map((code) => `<div class="fault-item"><span class="f-dot"></span><span class="f-code">${esc(code)}</span><span class="f-label">活动故障码</span></div>`).join('')
    : '<span class="muted">无活动故障码</span>';

  const facts = [
    ['机型', machine.model],
    ['序列号', machine.serial_number],
    ['设备 ID', machine.machine_id],
    ['工况', machine.operating_state],
    ['发动机', machine.engine_running ? '运行中' : '已停止'],
    ['燃油', `${fuel}%`],
    ['累计小时', `${Number(machine.hour_meter).toFixed(1)} h`],
    ['距下次保养', `${remaining.toFixed(1)} h`],
    ['故障码', faults.join('、') || '无'],
  ];
  $('facts-list').innerHTML = facts
    .map(([k, v]) => `<div class="fact-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
}

/* ---------------- 渲染:对话 ---------------- */
function renderConversation() {
  const box = $('conversation');
  if (!state.messages.length) {
    box.innerHTML = '<div class="empty-state"><span class="empty-glyph" aria-hidden="true"></span><b>开始一轮查询或故障诊断</b><span>例如：查看状态，查询 E123 手册并给出处理方案。</span></div>';
    return;
  }
  box.innerHTML = state.messages.map((m) => `
    <div class="message ${m.role}${m.error ? ' error' : ''}${m.pending ? ' pending' : ''}">
      <div class="message-label">${m.role === 'user' ? 'OPERATOR' : 'ASSISTANT'}</div>
      <div class="message-body">${esc(m.text)}</div>
      ${m.meta ? `<div class="response-meta">${esc(m.meta)}</div>` : ''}
      ${m.approval ? '<div class="approval-actions"><button class="mini-button approval-yes" data-action="approve-turn">执行</button><button class="mini-button" data-action="reject-turn">取消</button></div>' : ''}
    </div>`).join('');
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 渲染:执行流水线 ---------------- */
function renderPipeline(metadata) {
  const meta = metadata || {};
  const steps = meta.steps || [];
  const caps = meta.capabilities || [];
  const artifacts = meta.artifacts || [];

  $('plan-steps').innerHTML = steps.length
    ? steps.map((s, i) => `<li><b>${esc(STEP_LABELS[s] || s)}</b><small>${esc(caps[i] || s)}</small></li>`).join('')
    : '<li class="muted">运行一轮后显示计划阶段。</li>';

  $('cap-chain').innerHTML = caps.length
    ? caps.map((c) => `<span class="cap-pill">${esc(c)}</span>`).join('')
    : '<span class="muted">—</span>';

  $('artifact-count').textContent = artifacts.length;
  $('artifact-list').innerHTML = artifacts.length
    ? artifacts.map((a) => `
        <div class="artifact-item">
          <div>
            <div class="a-type">${esc(ARTIFACT_LABELS[a.artifact_type] || a.artifact_type)}</div>
            <div class="a-src">${esc(a.source_capability)}</div>
          </div>
          <span class="a-conf">${Math.round((a.confidence ?? 1) * 100)}%</span>
        </div>`).join('')
    : '<span class="muted">回答后显示产物链。</span>';

  const sources = artifacts.filter((a) => /knowledge|evidence|manual|source/i.test(a.artifact_type));
  $('source-list').innerHTML = sources.length
    ? sources.map((a) => `<div class="source-item"><b>${esc(a.source_capability)}</b><span>置信度 ${Math.round((a.confidence ?? 1) * 100)}% · ${esc(ARTIFACT_LABELS[a.artifact_type] || a.artifact_type)}</span></div>`).join('')
    : '<span class="muted">本轮未引用外部知识来源。</span>';
}

/* ---------------- 渲染:能力目录 ---------------- */
function renderCapabilities(data) {
  const caps = data.capabilities || [];
  const tools = data.tools || [];
  const plugins = data.plugins || [];
  const mcp = data.mcp_servers || [];
  state.runtimePlugins = plugins;

  $('cap-count').textContent = caps.length;
  $('tool-count').textContent = tools.length;
  $('plugin-runtime-count').textContent = plugins.length;
  $('mcp-count').textContent = mcp.length;

  $('capability-list').innerHTML = caps.length ? caps.map((c) => {
    const tone = !c.enabled ? '' : c.requires_approval ? 'warn' : 'ok';
    const label = !c.enabled ? 'DISABLED' : c.requires_approval ? 'APPROVAL' : String(c.risk_level || 'low').toUpperCase();
    const flow = [(c.consumes || []).join(', ') || '∅', (c.produces || []).join(', ') || '∅'].join(' → ');
    return `<div class="tile"><div class="tile-icon c">C</div><div class="tile-copy"><b>${esc(c.name)}</b><span>v${esc(c.version)} · ${esc(c.kind)} · ${esc(flow)}</span></div><span class="tile-status ${tone}">${esc(label)}</span></div>`;
  }).join('') : '<div class="empty-card"><b>暂无能力</b><span>组合根尚未注册能力。</span></div>';

  $('tool-list').innerHTML = tools.length ? tools.map((t) =>
    `<div class="tile"><div class="tile-icon t">T</div><div class="tile-copy"><b>${esc(t.name)}</b><span>${esc(t.description || '')}</span></div><span class="tile-status ${t.read_only ? 'read' : 'warn'}">${t.read_only ? 'READ' : 'WRITE'}</span></div>`
  ).join('') : '<div class="empty-card"><b>暂无工具</b><span>启用能力后会投影为工具。</span></div>';

  $('plugin-runtime-list').innerHTML = plugins.length ? plugins.map((p) =>
    `<div class="tile"><div class="tile-icon p">P</div><div class="tile-copy"><b>${esc(p.name)}</b><span>v${esc(p.version)} · ${esc(p.description || '内置插件')}</span></div><span class="tile-status ok">运行中</span></div>`
  ).join('') : '<div class="empty-card"><b>暂无插件</b><span>组合根尚未加载插件。</span></div>';

  $('mcp-runtime-list').innerHTML = mcp.length ? mcp.map((s) =>
    `<div class="tile"><div class="tile-icon m">M</div><div class="tile-copy"><b>${esc(s.name)}</b><span>${esc(s.transport)} · ${esc(s.endpoint || s.command || '')}</span></div><span class="tile-status warn">待连接</span></div>`
  ).join('') : '<div class="empty-card"><b>尚未连接 MCP Server</b><span>可在运行时配置中添加连接声明。</span></div>';
}

/* ---------------- 渲染:审计事件 ---------------- */
function renderEvents(events) {
  state.events = events || [];
  const types = [...new Set(state.events.map((e) => e.event_type))].sort();
  const filter = $('event-filter');
  const current = state.eventFilter;
  filter.innerHTML = '<option value="">全部事件类型</option>' +
    types.map((t) => `<option value="${esc(t)}"${t === current ? ' selected' : ''}>${esc(t)}</option>`).join('');

  const rows = state.events
    .filter((e) => !current || e.event_type === current)
    .slice().reverse();
  $('event-list').innerHTML = rows.length ? rows.map((e) => {
    const payload = JSON.stringify(e.payload || {}, null, 2);
    return `<details class="event-row">
      <summary><span class="event-time">${esc(dateFormatter.format(new Date(e.occurred_at)))}</span>
        <b class="event-type">${esc(e.event_type)}</b>
        <span class="event-payload">${esc(JSON.stringify(e.payload || {}))}</span></summary>
      <pre class="event-details">${esc(payload)}</pre>
    </details>`;
  }).join('') : '<div class="empty-state"><b>暂无事件</b><span>运行一轮诊断后会显示追加日志。</span></div>';
}

/* ---------------- 渲染:运行时配置 ---------------- */
function configRow(icon, name, detail, status, tone, actions) {
  return `<div class="config-row"><div class="config-icon">${esc(icon)}</div><div class="config-copy"><b>${esc(name)}</b><span>${esc(detail || '—')}</span></div><span class="chip ${tone}">${esc(status)}</span>${actions ? `<div class="config-actions">${actions}</div>` : ''}</div>`;
}

function renderConfiguration(configuration) {
  state.configuration = configuration;
  const model = configuration.model;
  $('model-provider').value = model.provider;
  $('model-name').value = model.model;
  $('model-base-url').value = model.base_url;
  $('model-api-key').value = model.api_key;
  $('model-temperature').value = model.temperature;
  $('model-max-tokens').value = model.max_tokens;
  $('model-timeout').value = model.timeout_seconds;
  $('model-max-steps').value = model.max_steps;
  $('model-call-timeout').value = model.model_call_timeout_seconds;
  $('tool-call-timeout').value = model.tool_call_timeout_seconds;
  $('turn-timeout').value = model.turn_timeout_seconds;
  $('model-planning-mode').value = model.planning_mode || 'auto';
  $('model-disable-thinking').checked = Boolean(model.disable_thinking);

  const applied = configuration.runtime.model_config_applied;
  $('runtime-adapter').textContent = configuration.runtime.model_adapter;
  $('runtime-applied').textContent = applied ? '当前已应用' : '已保存 · 待适配器接入';
  $('runtime-planning').textContent = model.planning_mode || 'auto';
  const chip = $('model-status-chip');
  chip.textContent = applied ? 'Demo 运行中' : '配置待应用';
  chip.className = `chip ${applied ? 'chip-success' : 'chip-warning'}`;

  $('mcp-config-list').innerHTML = configuration.mcp_servers.map((s) => configRow(
    'MCP', s.name,
    s.transport === 'stdio' ? s.command : s.endpoint,
    s.enabled ? '请求启用' : '已配置', s.enabled ? 'chip-warning' : 'chip-neutral',
    `<button class="mini-button" data-action="edit-mcp" data-id="${esc(s.id)}">管理</button><button class="mini-button danger" data-action="delete-mcp" data-id="${esc(s.id)}">移除</button>`
  )).join('');

  $('builtin-plugin-list').innerHTML = state.runtimePlugins.map((p) => configRow(
    'CORE', p.name, `v${p.version} · ${p.description || '内置插件'}`, '运行中', 'chip-success', ''
  )).join('');

  $('plugin-config-list').innerHTML = configuration.plugins.map((p) => configRow(
    'PLG', p.name, `${p.reference}${p.version ? ` · ${p.version}` : ''}`,
    p.enabled ? '请求启用' : '已配置', p.enabled ? 'chip-warning' : 'chip-neutral',
    `<button class="mini-button" data-action="edit-plugin" data-id="${esc(p.id)}">管理</button><button class="mini-button danger" data-action="delete-plugin" data-id="${esc(p.id)}">移除</button>`
  )).join('');
}

/* ---------------- 加载 ---------------- */
async function loadState() {
  try {
    const data = await request(`/api/state?machine_id=${encodeURIComponent(MACHINE_ID)}&session_id=${encodeURIComponent(SESSION_ID)}&operator_id=${encodeURIComponent(OPERATOR_ID)}`);
    renderStatusbar(data.machine);
    renderMachine(data.machine);
    renderCapabilities(data);
    renderEvents(data.events);
    renderConfiguration(data.configuration);
    if (Array.isArray(data.turns) && !state.messages.length) {
      state.messages = data.turns.flatMap((turn) => [
        { role: 'user', text: turn.user_text },
        { role: 'assistant', text: turn.assistant_text, meta: CATEGORY_LABELS[turn.category] || turn.category },
      ]);
      renderConversation();
    }
    setConnection(true);
  } catch (error) {
    setConnection(false);
    throw error;
  }
}

/* ---------------- 设备切换 + 会话管理 ---------------- */
const LS_SESSIONS = 'catedge.sessions.v1';
const LS_ACTIVE = 'catedge.active.v1';

function readStore(key, fallback) {
  try { const raw = JSON.parse(localStorage.getItem(key)); return raw ?? fallback; }
  catch { return fallback; }
}
function persistSessions() { try { localStorage.setItem(LS_SESSIONS, JSON.stringify(state.sessions)); } catch {} }
function persistActive() { try { localStorage.setItem(LS_ACTIVE, JSON.stringify(state.active)); } catch {} }

function newSessionId() {
  return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `s-${Date.now()}-${Math.round(Math.random() * 1e6)}`;
}
function sessionsForMachine(machineId) {
  return state.sessions.filter((s) => s.machineId === machineId).sort((a, b) => b.updatedAt - a.updatedAt);
}
function currentSession() {
  return state.sessions.find((s) => s.id === SESSION_ID) || null;
}
function createSession(machineId, { activate = true } = {}) {
  const now = Date.now();
  const session = { id: newSessionId(), title: '新会话', machineId, createdAt: now, updatedAt: now };
  state.sessions.push(session);
  persistSessions();
  if (activate) { SESSION_ID = session.id; state.active.byMachine[machineId] = session.id; persistActive(); }
  return session;
}
function ensureSessionForMachine(machineId) {
  const wanted = state.active.byMachine[machineId];
  const existing = state.sessions.find((s) => s.id === wanted && s.machineId === machineId) || sessionsForMachine(machineId)[0];
  if (existing) { SESSION_ID = existing.id; state.active.byMachine[machineId] = existing.id; persistActive(); return existing; }
  return createSession(machineId);
}
function touchSession(userText) {
  const session = currentSession();
  if (!session) return;
  session.updatedAt = Date.now();
  if (!session.title || session.title === '新会话') {
    const trimmed = userText.trim();
    session.title = trimmed.slice(0, 18) + (trimmed.length > 18 ? '…' : '');
  }
  persistSessions();
  renderSessions();
}

function renderMachineSwitcher() {
  const box = $('machine-switcher');
  $('machine-total').textContent = state.machines.length;
  if (!state.machines.length) { box.innerHTML = '<div class="switcher-empty">无可用设备</div>'; return; }
  box.innerHTML = state.machines.map((m) => {
    const faults = m.fault_codes || [];
    const tone = faults.length ? 'fault' : (m.engine_running ? 'ok' : 'off');
    const active = m.machine_id === MACHINE_ID;
    return `<button type="button" class="machine-item${active ? ' active' : ''}" role="option" aria-selected="${active}" data-machine="${esc(m.machine_id)}">
      <span class="mi-dot ${tone}" aria-hidden="true"></span>
      <span class="mi-copy"><b>${esc(m.model)}</b><span>${esc(m.machine_id)}</span></span>
      ${faults.length ? `<span class="mi-badge" title="活动故障">${faults.length}</span>` : ''}
    </button>`;
  }).join('');
}

function renderSessions() {
  const list = sessionsForMachine(MACHINE_ID);
  const cur = currentSession();
  $('session-name').textContent = cur ? cur.title : '新会话';
  const label = $('session-label');
  if (label) label.textContent = SESSION_ID.slice(0, 8);
  $('session-menu').innerHTML = list.length ? list.map((s) => `
    <div class="session-item${s.id === SESSION_ID ? ' active' : ''}" role="option" aria-selected="${s.id === SESSION_ID}" data-session="${esc(s.id)}">
      <span class="si-copy"><b>${esc(s.title)}</b><span>${esc(dateFormatter.format(new Date(s.updatedAt)))}</span></span>
      <button type="button" class="si-del" data-del-session="${esc(s.id)}" aria-label="删除会话">×</button>
    </div>`).join('') : '<div class="switcher-empty">该设备暂无会话</div>';
}

function openSessionMenu() { $('session-menu').hidden = false; $('session-toggle').setAttribute('aria-expanded', 'true'); state.sessionMenuOpen = true; }
function closeSessionMenu() { $('session-menu').hidden = true; $('session-toggle').setAttribute('aria-expanded', 'false'); state.sessionMenuOpen = false; }

async function reloadConsole() {
  state.messages = [];
  renderConversation();
  renderPipeline({});
  renderSessions();
  try { await loadState(); } catch (error) { toast(error.message, true); }
}

async function selectMachine(machineId) {
  if (machineId === MACHINE_ID) return;
  MACHINE_ID = machineId;
  state.active.machine = machineId;
  persistActive();
  ensureSessionForMachine(machineId);
  renderMachineSwitcher();
  await reloadConsole();
  const machine = state.machines.find((m) => m.machine_id === machineId);
  toast(`已切换到 ${machine ? machine.model : machineId}`);
}

async function selectSession(id) {
  if (id === SESSION_ID) { closeSessionMenu(); return; }
  if (!state.sessions.some((s) => s.id === id)) { closeSessionMenu(); return; }
  SESSION_ID = id;
  state.active.byMachine[MACHINE_ID] = id;
  persistActive();
  closeSessionMenu();
  await reloadConsole();
}

async function newSession() {
  createSession(MACHINE_ID);
  closeSessionMenu();
  renderMachineSwitcher();
  await reloadConsole();
  $('turn-input')?.focus();
}

async function deleteSession(id) {
  const index = state.sessions.findIndex((s) => s.id === id);
  if (index < 0) return;
  // Delete the backend history/summary first so the UI never diverges from the
  // store. A frontend-only session that never ran a turn simply removes 0 rows.
  try {
    await post('/api/session/delete', { session_id: id });
  } catch (error) {
    toast(`后端会话历史删除失败：${error.message}`, true);
    return;
  }
  const wasCurrent = id === SESSION_ID;
  state.sessions.splice(index, 1);
  persistSessions();
  if (wasCurrent) {
    ensureSessionForMachine(MACHINE_ID);
    await reloadConsole();
  } else {
    renderSessions();
  }
  toast('会话及后端历史已删除。');
}

async function loadMachines() {
  try {
    const data = await request('/api/machines');
    state.machines = Array.isArray(data.machines) ? data.machines : [];
  } catch { state.machines = []; }
  renderMachineSwitcher();
}

async function bootstrapConsole() {
  state.sessions = readStore(LS_SESSIONS, []);
  if (!Array.isArray(state.sessions)) state.sessions = [];
  const active = readStore(LS_ACTIVE, { machine: null, byMachine: {} });
  state.active = (active && typeof active === 'object' && active.byMachine) ? active : { machine: null, byMachine: {} };

  await loadMachines();
  const ids = state.machines.map((m) => m.machine_id);
  MACHINE_ID = (state.active.machine && ids.includes(state.active.machine)) ? state.active.machine
    : (ids.includes('cat-306-demo') ? 'cat-306-demo' : (ids[0] || 'cat-306-demo'));
  state.active.machine = MACHINE_ID;
  ensureSessionForMachine(MACHINE_ID);
  persistActive();
  renderMachineSwitcher();
  renderSessions();
  await loadState();
}

/* 设备切换器 / 会话下拉的事件委托 */
$('machine-switcher').addEventListener('click', (event) => {
  const button = event.target.closest('[data-machine]');
  if (button) selectMachine(button.dataset.machine);
});
$('session-toggle').addEventListener('click', () => (state.sessionMenuOpen ? closeSessionMenu() : openSessionMenu()));
$('new-session-btn').addEventListener('click', () => newSession());
$('delete-session-btn').addEventListener('click', () => {
  if (!currentSession()) { toast('当前没有可删除的会话。'); return; }
  deleteSession(SESSION_ID);
});
$('session-menu').addEventListener('click', (event) => {
  const del = event.target.closest('[data-del-session]');
  if (del) { event.stopPropagation(); deleteSession(del.dataset.delSession); return; }
  const item = event.target.closest('[data-session]');
  if (item) selectSession(item.dataset.session);
});
document.addEventListener('click', (event) => {
  if (state.sessionMenuOpen && !event.target.closest('.session-picker')) closeSessionMenu();
});

/* ---------------- 对话框 ---------------- */
function openMcp(server = null) {
  $('mcp-form').reset();
  $('mcp-id').value = server?.id || '';
  $('mcp-name').value = server?.name || '';
  $('mcp-transport').value = server?.transport || 'streamable_http';
  $('mcp-endpoint').value = server?.endpoint || '';
  $('mcp-command').value = server?.command || '';
  $('mcp-arguments').value = (server?.arguments || []).join(', ');
  $('mcp-env-keys').value = (server?.env_keys || []).join(', ');
  $('mcp-tools').value = (server?.tool_allowlist || ['*']).join(', ');
  $('mcp-enabled').checked = Boolean(server?.enabled);
  $('mcp-dialog-title').textContent = server ? '管理 Server' : '添加 Server';
  $('mcp-dialog').showModal();
}
function openPlugin(plugin = null) {
  $('plugin-form').reset();
  $('plugin-id').value = plugin?.id || '';
  $('plugin-name').value = plugin?.name || '';
  $('plugin-version').value = plugin?.version || '';
  $('plugin-reference').value = plugin?.reference || '';
  $('plugin-config').value = JSON.stringify(plugin?.config || {}, null, 2);
  $('plugin-enabled').checked = Boolean(plugin?.enabled);
  $('plugin-dialog-title').textContent = plugin ? '管理插件' : '添加插件';
  $('plugin-dialog').showModal();
}

/* ---------------- 事件:任务编排 ---------------- */
$('turn-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('turn-input');
  const text = input.value.trim();
  if (!text) return;
  const button = $('turn-submit');
  const cancel = $('turn-cancel');
  const formError = $('turn-form-error');
  showFormError('turn-form-error');
  state.turnController?.abort();
  state.turnController = new AbortController();
  setBusy(button, true, '执行中…');
  cancel.hidden = false;
  state.messages.push({ role: 'user', text });
  state.messages.push({ role: 'assistant', text: '思考中…', pending: true });
  renderConversation();
  input.value = '';
  try {
    const data = await post('/api/turn', {
      text, session_id: SESSION_ID, machine_id: MACHINE_ID, operator_id: OPERATOR_ID,
    }, { signal: state.turnController.signal });
    state.messages = state.messages.filter((message) => !message.pending);
    const meta = data.response.metadata || {};
    const category = CATEGORY_LABELS[data.response.category] || data.response.category;
    const stepLabel = (meta.steps || []).map((s) => STEP_LABELS[s] || s).join(' → ');
    state.messages.push({ role: 'assistant', text: data.response.text, meta: `${category}${stepLabel ? ` · ${stepLabel}` : ''}`, approval: Boolean(data.response.requires_confirmation) });
    renderConversation();
    renderPipeline(meta);
    touchSession(text);
    if (data.state) {
      renderStatusbar(data.state.machine);
      renderMachine(data.state.machine);
      renderEvents(data.state.events);
    }
  } catch (error) {
    state.messages = state.messages.filter((message) => !message.pending);
    const message = error.name === 'AbortError' ? '已停止等待本轮响应。' : `调试请求失败：${error.message}`;
    state.messages.push({ role: 'assistant', text: message, meta: '错误 · 可重试', error: true });
    showFormError('turn-form-error', message);
    renderConversation();
  } finally {
    state.turnController = null;
    setBusy(button, false);
    cancel.hidden = true;
  }
});

$('turn-cancel').addEventListener('click', () => state.turnController?.abort());
$('turn-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('turn-form').requestSubmit();
  }
});

/* ---------------- 事件:模型配置 ---------------- */
$('model-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.submitter;
  setBusy(button, true, '保存中…');
  showFormError('model-form-error');
  try {
    await post('/api/config/model', {
      provider: $('model-provider').value, model: $('model-name').value,
      base_url: $('model-base-url').value, api_key: $('model-api-key').value,
      temperature: $('model-temperature').value, max_tokens: $('model-max-tokens').value,
      timeout_seconds: $('model-timeout').value, max_steps: $('model-max-steps').value,
      model_call_timeout_seconds: $('model-call-timeout').value,
      tool_call_timeout_seconds: $('tool-call-timeout').value,
      turn_timeout_seconds: $('turn-timeout').value,
      planning_mode: $('model-planning-mode').value,
      disable_thinking: $('model-disable-thinking').checked,
    });
    await loadState();
    $('model-form').dataset.dirty = 'false';
    $('model-save-msg').textContent = '配置已保存';
    toast('模型配置已保存；非 Demo Provider 待运行时适配器接入。');
  } catch (error) { showFormError('model-form-error', error.message); toast(error.message, true); }
  finally { setBusy(button, false); }
});

/* ---------------- 事件:MCP / 插件 / 记忆 ---------------- */
$('mcp-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('save-mcp-btn');
  setBusy(button, true, '保存中…');
  showFormError('mcp-form-error');
  try {
    await post('/api/config/mcp', {
      id: $('mcp-id').value, name: $('mcp-name').value, transport: $('mcp-transport').value,
      endpoint: $('mcp-endpoint').value, command: $('mcp-command').value,
      arguments: csv($('mcp-arguments').value), env_keys: csv($('mcp-env-keys').value),
      tool_allowlist: csv($('mcp-tools').value), enabled: $('mcp-enabled').checked,
    });
    $('mcp-dialog').close();
    await loadState();
    toast('MCP Server 声明已保存。');
  } catch (error) { showFormError('mcp-form-error', error.message); toast(error.message, true); }
  finally { setBusy(button, false); }
});

$('plugin-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('save-plugin-btn');
  setBusy(button, true, '保存中…');
  showFormError('plugin-form-error');
  try {
    let config;
    try { config = JSON.parse($('plugin-config').value || '{}'); }
    catch { throw new Error('插件配置必须是有效的 JSON 对象。'); }
    if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('插件配置必须是 JSON 对象。');
    await post('/api/config/plugin', {
      id: $('plugin-id').value, name: $('plugin-name').value, version: $('plugin-version').value,
      reference: $('plugin-reference').value, config, enabled: $('plugin-enabled').checked,
    });
    $('plugin-dialog').close();
    await loadState();
    toast('插件声明已保存。');
  } catch (error) { showFormError('plugin-form-error', error.message); toast(error.message, true); }
  finally { setBusy(button, false); }
});

/* ---------------- 事件:导航 ---------------- */
function confirmUnsavedModelChanges() {
  return $('model-form').dataset.dirty !== 'true'
    || window.confirm('模型配置尚未保存。放弃更改并离开吗？');
}

function activateView(viewName, { updateHistory = true } = {}) {
  const item = document.querySelector(`.nav-item[data-view="${viewName}"]`);
  if (!item) return;
  document.querySelectorAll('.nav-item').forEach((n) => n.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach((n) => {
    n.removeAttribute('aria-current');
  });
  document.querySelectorAll('.view').forEach((v) => { v.classList.remove('active'); v.hidden = true; });
  item.classList.add('active');
  item.setAttribute('aria-current', 'page');
  const view = $(`view-${viewName}`);
  view.classList.add('active');
  view.hidden = false;
  state.activeView = viewName;
  if (updateHistory) updateUrl();
}

document.querySelectorAll('.nav-item').forEach((item) => item.addEventListener('click', () => {
  if (state.activeView === 'settings' && state.activeSettings === 'model' && item.dataset.view !== 'settings' && !confirmUnsavedModelChanges()) return;
  activateView(item.dataset.view);
}));

function activateSettings(settingsName, { updateHistory = true } = {}) {
  const tab = document.querySelector(`.settings-tab[data-settings="${settingsName}"]`);
  if (!tab) return;
  document.querySelectorAll('.settings-tab').forEach((t) => {
    const active = t === tab;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
    t.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('.settings-pane').forEach((p) => {
    const active = p.id === `settings-${settingsName}`;
    p.classList.toggle('active', active);
    p.hidden = !active;
  });
  state.activeSettings = settingsName;
  if (updateHistory) updateUrl();
}

document.querySelectorAll('.settings-tab').forEach((tab) => tab.addEventListener('click', () => {
  if (state.activeSettings === 'model' && tab.dataset.settings !== 'model' && !confirmUnsavedModelChanges()) return;
  activateSettings(tab.dataset.settings);
}));
document.querySelectorAll('.settings-tab').forEach((tab) => tab.addEventListener('keydown', (event) => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const tabs = [...document.querySelectorAll('.settings-tab')];
  const current = tabs.indexOf(tab);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  tabs[next].focus();
  activateSettings(tabs[next].dataset.settings);
}));

/* ---------------- 事件:委托(配置行 / 关闭对话框) ---------------- */
document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-action], [data-close]');
  if (!target) return;
  if (target.dataset.close) { $(target.dataset.close).close(); return; }
  const id = target.dataset.id;
  const action = target.dataset.action;
  if (action === 'approve-turn' || action === 'reject-turn') {
    const input = $('turn-input');
    input.value = action === 'approve-turn' ? '确认执行' : '取消执行';
    $('turn-form').requestSubmit();
    return;
  }
  if (action === 'edit-mcp') openMcp(state.configuration.mcp_servers.find((s) => s.id === id));
  if (action === 'edit-plugin') openPlugin(state.configuration.plugins.find((p) => p.id === id));
  if (action === 'delete-mcp' && window.confirm('移除此 MCP Server 配置?')) {
    try { await post('/api/config/mcp/delete', { id }); await loadState(); toast('MCP 配置已移除。'); }
    catch (error) { toast(error.message, true); }
  }
  if (action === 'delete-plugin' && window.confirm('移除此插件配置?')) {
    try { await post('/api/config/plugin/delete', { id }); await loadState(); toast('插件配置已移除。'); }
    catch (error) { toast(error.message, true); }
  }
});

/* ---------------- 事件:其它 ---------------- */
$('event-filter').addEventListener('change', (e) => { state.eventFilter = e.target.value; renderEvents(state.events); updateUrl(); });
$('model-form').addEventListener('input', () => { $('model-form').dataset.dirty = 'true'; });
window.addEventListener('beforeunload', (event) => {
  if ($('model-form').dataset.dirty !== 'true') return;
  event.preventDefault();
  event.returnValue = '';
});
$('add-mcp-btn').addEventListener('click', () => openMcp());
$('add-plugin-btn').addEventListener('click', () => openPlugin());
$('refresh-btn').addEventListener('click', () => loadState().then(() => toast('已刷新。')).catch((e) => toast(e.message, true)));
$('audit-refresh').addEventListener('click', () => loadState().catch((e) => toast(e.message, true)));

/* ---------------- 初始路由 + 首次加载 ---------------- */
const route = new URLSearchParams(window.location.search);
const initView = ['console', 'machine', 'capabilities', 'settings', 'audit'].includes(route.get('view')) ? route.get('view') : 'console';
const initSettings = ['model', 'mcp', 'plugins'].includes(route.get('settings')) ? route.get('settings') : 'model';
state.eventFilter = route.get('event') || '';
activateView(initView, { updateHistory: false });
activateSettings(initSettings, { updateHistory: false });
window.addEventListener('popstate', () => {
  const current = new URLSearchParams(window.location.search);
  state.eventFilter = current.get('event') || '';
  activateView(['console', 'machine', 'capabilities', 'settings', 'audit'].includes(current.get('view')) ? current.get('view') : 'console', { updateHistory: false });
  activateSettings(['model', 'mcp', 'plugins'].includes(current.get('settings')) ? current.get('settings') : 'model', { updateHistory: false });
  renderEvents(state.events);
});

bootstrapConsole().catch((error) => {
  $('conversation').innerHTML = `<div class="empty-state"><b>无法连接调试服务</b><span>${esc(error.message)}</span></div>`;
});
