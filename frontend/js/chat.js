// 对话页逻辑：多会话管理 + 外部模型配置 + 讲解/费曼双模式 + SSE 流式渲染

let mode = 'ask';              // 当前模式
let streaming = false;         // 是否正在流式输出
let teachTopic = null;         // 费曼模式当前主题（无主题=待开局）
let teachRounds = 0;
let teachMaxRounds = 5;
let pendingSources = [];       // 讲解模式本轮引用来源
let serverSessions = [];       // 服务端会话列表缓存（含 mode 字段）

function newSessionId() {
  return 'c-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// 讲解/费曼各自维护「当前会话」，历史互不串扰
function getCur(m) {
  let id = localStorage.getItem('rag_cur_' + m);
  if (!id) {
    id = newSessionId();
    localStorage.setItem('rag_cur_' + m, id);
  }
  return id;
}
let currentId = getCur(mode);

const msgList = document.getElementById('msg-list');
const input = document.getElementById('input');
const btnSend = document.getElementById('btn-send');
const btnFinish = document.getElementById('btn-finish');
const topicBar = document.getElementById('topic-bar');
const hint = document.getElementById('hint');

// ---------- 外部模型配置（阶段 11：存服务端，按用户隔离，不再用 localStorage）----------

// 当前用户的模型配置快照（含 has_custom / api_key_masked），启动与保存后刷新
let llmState = { has_custom: false, provider: 'auto', base_url: '', model: '', api_key_masked: '' };

async function refreshLLMState() {
  try {
    llmState = await apiGet('/api/user/llm');
  } catch (e) { /* 401 已跳登录页；其余暂按默认模型展示 */ }
  updateLLMBadge();
}

function updateLLMBadge() {
  const cfg = llmState.has_custom ? llmState : null;
  const text = cfg
    ? ' ' + (cfg.model || (cfg.provider === 'anthropic' ? 'claude 默认' : '自定义模型'))
      + (cfg.provider !== 'auto' ? `（${cfg.provider}）` : '')
    : '🤖 默认模型';
  const longText = cfg
    ? '自定义模型：' + (cfg.model || '默认型号') + '（⚙️ 可修改）'
    : '默认模型（可在 ⚙️ 配置外部模型）';
  document.getElementById('llm-badge').textContent = longText;
  const chip = document.getElementById('model-chip');
  chip.textContent = text;
  chip.title = cfg
    ? `${cfg.base_url}（协议 ${cfg.provider}）· 点击左侧 ⚙️ 修改`
    : '系统默认模型 · 点击左侧 ⚙️ 可配置外部模型';
}

// ---------- 渲染工具 ----------

function scrollBottom() {
  msgList.scrollTop = msgList.scrollHeight;
}

function addUserBubble(text) {
  const div = document.createElement('div');
  div.className = 'msg user';
  div.innerHTML = '<div class="avatar">🧑</div>';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  div.appendChild(bubble);
  msgList.appendChild(div);
  scrollBottom();
}

function addAiBubble(placeholder = '思考中…') {
  const div = document.createElement('div');
  div.className = 'msg ai';
  div.innerHTML = '<div class="avatar">🤖</div>';
  const bubble = document.createElement('div');
  bubble.className = 'bubble cursor';
  bubble.textContent = placeholder;
  div.appendChild(bubble);
  msgList.appendChild(div);
  scrollBottom();
  return bubble;
}

// 判断一段回复是不是评分报告（含评分标题）
function isEvaluation(text) {
  return /掌握度评分/.test(text) && /^#|^##/.test(text.trim());
}

// 极简 markdown → HTML（评分卡片用；完整渲染见 highlight.js 的 renderRich）
function mdToHtml(md) {
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  let html = '', inList = false;
  for (const raw of md.split('\n')) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*[-*]\s+(.*)$/);
    if (item) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(esc(item[1])) + '</li>';
      continue;
    }
    if (inList) { html += '</ul>'; inList = false; }
    if (!line.trim()) continue;
    const h = line.match(/^#{1,3}\s+(.*)$/);
    if (h) {
      const cls = /评分/.test(h[1]) ? ' class="score-line"' : '';
      html += `<h2${cls}>${inline(esc(h[1]))}</h2>`;
      continue;
    }
    html += '<p>' + inline(esc(line)) + '</p>';
  }
  if (inList) html += '</ul>';
  return html;
}

// 渲染一条助手消息：评分报告走卡片，其余走富文本气泡
function renderAssistant(text) {
  const div = document.createElement('div');
  div.className = 'msg ai';
  div.innerHTML = '<div class="avatar">🤖</div>';
  if (isEvaluation(text)) {
    const card = document.createElement('div');
    card.className = 'eval-card';
    card.innerHTML = mdToHtml(text);
    div.appendChild(card);
  } else {
    const bubble = document.createElement('div');
    bubble.className = 'bubble rich';
    bubble.innerHTML = renderRich(text);
    div.appendChild(bubble);
  }
  msgList.appendChild(div);
  scrollBottom();
}

function renderSources(bubble) {
  if (!pendingSources.length) return;
  const box = document.createElement('div');
  box.className = 'sources';
  for (const s of pendingSources) {
    const chip = document.createElement('span');
    chip.className = 'source-chip';
    chip.textContent = `${categoryLabel(s.category)} · 相关度 ${s.score}`;
    box.appendChild(chip);
  }
  bubble.parentElement.appendChild(box);
  scrollBottom();
}

// 知识库无资料提示条（检索分数低于阈值）：插在回复气泡上方，
// 带跳转「我的知识」看联网补爬进度的链接
function showKbGap(ev) {
  const note = document.createElement('div');
  note.className = 'kb-gap-note';
  const txt = document.createElement('span');
  txt.textContent = '📭 ' + (ev.message || '知识库暂无相关资料');
  note.appendChild(txt);
  if (ev.search_task_id) {
    const link = document.createElement('a');
    link.href = 'kb.html';
    link.textContent = '查看补充进度 →';
    note.appendChild(link);
  }
  // 插在当前 AI 气泡（最后一个 .msg）之前
  const lastMsg = msgList.querySelector('.msg:last-child');
  if (lastMsg) msgList.insertBefore(note, lastMsg);
  else msgList.appendChild(note);
  scrollBottom();
}

// ---------- 模式与主题栏 ----------

function applyModeUI() {
  document.getElementById('mode-ask').classList.toggle('active', mode === 'ask');
  document.getElementById('mode-teach').classList.toggle('active', mode === 'teach');
  if (mode === 'ask') {
    topicBar.style.display = 'none';
    input.placeholder = '输入想学的问题，如：什么是 AIGC？';
    hint.textContent = '讲解模式：AI 基于知识库检索结果给你讲解。';
  } else {
    input.placeholder = teachTopic
      ? '继续向 AI 讲解，或点「结束讲解，开始评分」…'
      : '输入要教的主题开始，如：aigc';
    hint.textContent = '费曼模式：你当老师讲给 AI 听，它追问最多 '
      + teachMaxRounds + ' 轮，结束后给你评分。';
    topicBar.style.display = teachTopic ? '' : 'none';
    if (teachTopic) {
      document.getElementById('topic-name').textContent = teachTopic;
      document.getElementById('topic-rounds').textContent =
        `已追问 ${teachRounds}/${teachMaxRounds} 轮`;
    }
  }
}

function switchMode(next) {
  if (streaming || next === mode) return;
  mode = next;
  localStorage.setItem('rag_mode', mode);
  currentId = getCur(mode);   // 切模式 = 切到该模式自己的当前会话
  msgList.innerHTML = '';
  pendingSources = [];
  teachTopic = null;
  teachRounds = 0;
  renderSessions();
  loadHistory();
}

// ---------- 会话管理 ----------

async function refreshSessions() {
  try {
    serverSessions = (await apiGet('/api/chat/sessions')).sessions;
  } catch (e) {
    serverSessions = [];
  }
  renderSessions();
}

function renderSessions() {
  const box = document.getElementById('session-items');
  document.getElementById('session-mode-label').textContent =
    mode === 'ask' ? '讲解模式历史' : '费曼模式历史';
  box.innerHTML = '';
  // 只显示当前模式的会话，讲解/费曼历史分开
  let list = serverSessions.filter(s => s.mode === mode);
  // 当前会话还没在服务端出现过（空白新对话）时，前端补一条
  if (!list.some(s => s.session_id === currentId)) {
    list.unshift({ session_id: currentId, mode, title: '新对话', messages: 0, updated: null });
  }
  for (const s of list) {
    const item = document.createElement('div');
    item.className = 'session-item' + (s.session_id === currentId ? ' active' : '');
    const title = document.createElement('span');
    title.className = 's-title';
    title.textContent = s.title || '新对话';
    const del = document.createElement('button');
    del.className = 's-del';
    del.title = '删除对话';
    del.textContent = '✕';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(s.session_id);
    });
    item.appendChild(title);
    item.appendChild(del);
    item.addEventListener('click', () => switchSession(s.session_id));
    box.appendChild(item);
  }
}

function switchSession(id) {
  if (streaming || id === currentId) return;
  currentId = id;
  localStorage.setItem('rag_current_session', currentId);
  msgList.innerHTML = '';
  pendingSources = [];
  teachTopic = null;
  teachRounds = 0;
  renderSessions();
  loadHistory();
  // 不自动聚焦输入框：平板/手机聚焦会弹软键盘，等用户自己点输入框
}

function newSession() {
  if (streaming) return;
  currentId = newSessionId();
  localStorage.setItem('rag_cur_' + mode, currentId);
  msgList.innerHTML = '';
  pendingSources = [];
  teachTopic = null;
  teachRounds = 0;
  renderSessions();
  applyModeUI();
  // 不自动聚焦：新建会话后用户想输入自然会被动点输入框
}

async function deleteSession(id) {
  if (streaming) return;
  if (!confirm('删除这个对话？删除后不可恢复。')) return;
  try {
    // 只删当前模式的历史，另一种模式不受影响
    await apiDelete(`/api/chat/sessions/${encodeURIComponent(id)}?mode=${mode}`);
  } catch (e) {
    alert('删除失败：' + e.message);
    return;
  }
  if (id === currentId) {
    currentId = newSessionId();
    localStorage.setItem('rag_cur_' + mode, currentId);
    msgList.innerHTML = '';
    teachTopic = null;
    teachRounds = 0;
    loadHistory();
  }
  refreshSessions();
}

// ---------- 历史 ----------

async function isBlank(id, m) {
  try {
    const d = await apiGet(
      `/api/chat/history?session_id=${encodeURIComponent(id)}&mode=${m}`);
    return d.messages.length === 0;
  } catch (e) {
    return true;
  }
}

async function loadHistory() {
  try {
    const data = await apiGet(
      `/api/chat/history?session_id=${encodeURIComponent(currentId)}&mode=${mode}`);
    msgList.innerHTML = '';
    for (const m of data.messages) {
      if (m.role === 'user') addUserBubble(m.content);
      else renderAssistant(m.content);
    }
    applyModeUI();
  } catch (e) {
    applyModeUI();
  }
}

// ---------- 发送与流式 ----------

async function send(text, opts = {}) {
  if (streaming || !text.trim()) return;
  text = text.trim();
  streaming = true;
  btnSend.disabled = true;
  btnUndo.disabled = true;
  input.value = '';
  pendingSources = [];

  if (!opts.hideUser) addUserBubble(text);

  const payload = { session_id: currentId, mode, message: text };
  if (opts.finish) payload.finish = true;
  // 阶段 11：模型配置由后端按当前登录用户读取，不再随请求透传

  let curBubble = addAiBubble(opts.finish ? '评分中…' : '思考中…');
  let curText = '';
  let inEval = false;
  let sawError = null;

  // 收尾一个气泡：评分→换卡片；否则富文本渲染
  const closeBubble = (asEval) => {
    curBubble.classList.remove('cursor');
    if (asEval && curText) {
      curBubble.parentElement.remove();
      renderAssistant(curText);
      return null;
    }
    if (curText) {
      curBubble.classList.add('rich');
      curBubble.innerHTML = renderRich(curText);
    } else {
      curBubble.textContent = '（没有收到回复）';
    }
    return curBubble;
  };

  const handleEvent = (ev) => {
    if (ev.type === 'meta') {
      if (mode === 'ask' && Array.isArray(ev.sources)) pendingSources = ev.sources;
      if (mode === 'teach') {
        if (ev.topic) teachTopic = ev.topic;
        if (typeof ev.rounds === 'number') teachRounds = ev.rounds;
        if (ev.max_rounds) teachMaxRounds = ev.max_rounds;
        // 进入评分轮且还没出字：占位显示「评分中…」而非「思考中…」
        if (ev.stage === 'evaluation' && curText === '') curBubble.textContent = '评分中…';
        applyModeUI();
      }
    } else if (ev.type === 'kb_gap') {
      // 知识库无资料（检索分数低于阈值）：提示条 + 已自动联网补爬
      showKbGap(ev);
    } else if (ev.type === 'token') {
      curText += ev.content;
      curBubble.textContent = curText;
      scrollBottom();
    } else if (ev.type === 'error') {
      sawError = ev.message;
    }
  };

  try {
    await streamChat(payload, handleEvent);
  } catch (e) {
    sawError = e.message;
  }

  if (sawError) {
    curBubble.classList.remove('cursor');
    curBubble.textContent = '⚠️ ' + sawError;
  } else {
    const closed = closeBubble(inEval || isEvaluation(curText));
    if (mode === 'ask' && pendingSources.length && closed) renderSources(closed);
  }

  // 费曼评分结束：服务端已重置上下文，前端同步
  if (mode === 'teach' && (inEval || isEvaluation(curText))) {
    teachTopic = null;
    teachRounds = 0;
    applyModeUI();
  }

  streaming = false;
  btnSend.disabled = false;
  btnUndo.disabled = false;
  refreshSessions();   // 首条消息后列表里出现标题
  // 只有用户主动发送（此刻本就在打字）才保持聚焦；
  // 卡片带参进入的自动发送（opts.auto）不聚焦——平板上会误弹软键盘
  if (!opts.auto) input.focus();
}

// ---------- 撤回 ----------

const btnUndo = document.getElementById('btn-undo');

// 撤回上一轮：删掉最后「用户提问 + 助手回复」两条气泡，原文回填输入框续写
async function undoLast() {
  if (streaming) return;
  const kids = [...msgList.children];
  const aiEl = kids[kids.length - 1];
  const userEl = kids[kids.length - 2];
  btnUndo.disabled = true;
  try {
    const d = await apiPostJson('/api/chat/undo', { session_id: currentId, mode });
    // 后端已删成功，前端同步 DOM（兜底校验：结构不符就整体重载历史）
    if (aiEl && userEl && aiEl.classList.contains('ai') && userEl.classList.contains('user')) {
      aiEl.remove();
      userEl.remove();
    } else {
      await loadHistory();
    }
    input.value = d.restored || '';
    if (mode === 'teach' && teachRounds > 0) {
      teachRounds -= 1;   // 与后端 rounds 回退保持同步
      applyModeUI();
    }
    // 不自动聚焦：用户要续写自然会被动点输入框（平板避免误弹键盘）
  } catch (e) {
    if (!String(e.message).includes('没有可撤回')) alert('撤回失败：' + e.message);
    btnUndo.disabled = false;
  }
}

btnUndo.addEventListener('click', undoLast);

// ---------- 设置弹窗 ----------

const setModal = document.getElementById('set-modal');

async function openSettings() {
  try {
    const c = await apiGet('/api/user/llm');
    llmState = c;
    document.getElementById('set-provider').value = c.provider || 'auto';
    document.getElementById('set-url').value = c.base_url || '';
    document.getElementById('set-key').value = '';
    document.getElementById('set-key').placeholder = c.api_key_masked
      ? c.api_key_masked + '（留空保持不变）'
      : 'API Key';
    document.getElementById('set-model').value = c.model || '';
    document.getElementById('set-result').textContent = '';
    setModal.style.display = '';
  } catch (e) {
    alert('读取配置失败：' + e.message);
  }
}

function readSettingsForm() {
  return {
    provider: document.getElementById('set-provider').value,
    base_url: document.getElementById('set-url').value.trim(),
    api_key: document.getElementById('set-key').value.trim(),
    model: document.getElementById('set-model').value.trim(),
  };
}

document.getElementById('btn-settings').addEventListener('click', openSettings);
document.getElementById('set-close').addEventListener('click', () => setModal.style.display = 'none');
setModal.addEventListener('click', (e) => { if (e.target === setModal) setModal.style.display = 'none'; });

document.getElementById('set-save').addEventListener('click', async () => {
  const cfg = readSettingsForm();
  const result = document.getElementById('set-result');
  try {
    llmState = await apiPutJson('/api/user/llm', cfg);
    updateLLMBadge();
    setModal.style.display = 'none';
  } catch (e) {
    result.textContent = e.message;
  }
});

document.getElementById('set-clear').addEventListener('click', async () => {
  try {
    llmState = await apiPutJson('/api/user/llm', { provider: 'auto', base_url: '', api_key: '', model: '' });
    updateLLMBadge();
    setModal.style.display = 'none';
  } catch (e) {
    alert('清除失败：' + e.message);
  }
});

document.getElementById('set-test').addEventListener('click', async () => {
  const cfg = readSettingsForm();
  const result = document.getElementById('set-result');
  if (!cfg.base_url || !cfg.api_key) {
    result.textContent = '先填 API 地址和 Key';
    return;
  }
  result.textContent = '测试中…';
  try {
    const r = await apiPostJson('/api/chat/ping', { llm: cfg });
    result.textContent = r.ok
      ? `✓ 连通（${r.provider} · ${r.model}）`
      : '✗ ' + (r.error || '未知错误');
  } catch (e) {
    result.textContent = '✗ ' + e.message;
  }
});

// ---------- 事件绑定 ----------

btnSend.addEventListener('click', () => send(input.value));
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send(input.value);
  }
});
document.getElementById('mode-ask').addEventListener('click', () => switchMode('ask'));
document.getElementById('mode-teach').addEventListener('click', () => switchMode('teach'));
document.getElementById('btn-new').addEventListener('click', newSession);
btnFinish.addEventListener('click', () => {
  if (!teachTopic || streaming) return;
  send('结束', { finish: true });
});

// ---------- 启动 ----------

async function boot() {
  const params = new URLSearchParams(location.search);
  const urlMode = params.get('mode');
  const q = params.get('q');
  const topic = params.get('topic');
  const deepSession = params.get('session');

  // 模式：URL 参数优先；刷新（无参数）时恢复上次使用的模式
  if (urlMode === 'teach' || urlMode === 'ask') {
    mode = urlMode;
  } else if (localStorage.getItem('rag_mode') === 'teach') {
    mode = 'teach';
  }
  localStorage.setItem('rag_mode', mode);
  currentId = getCur(mode);   // 该模式自己的当前会话

  // 通知深链接：?session=xxx 落到指定会话（需与 &mode= 同用）
  if (deepSession) {
    let exists = false;
    try {
      const data = await apiGet('/api/chat/sessions');
      exists = (data.sessions || []).some(
        (s) => s.session_id === deepSession && s.mode === mode
      );
    } catch (e) { /* 查询失败按不存在处理 */ }
    if (exists) {
      currentId = deepSession;
      localStorage.setItem('rag_cur_' + mode, deepSession);
    } else {
      alert('该会话不存在或已删除');
    }
  } else if (q || topic) {
    // 从知识卡片「去问 AI / 我来教」进入：默认新建对话，除非当前（该模式）对话是空白的
    const blank = await isBlank(currentId, mode);
    if (!blank) {
      currentId = newSessionId();
      localStorage.setItem('rag_cur_' + mode, currentId);
    }
  }

  // 跳转参数处理完立即清掉，刷新不会重复提问
  if (q || topic || urlMode || deepSession) {
    history.replaceState(null, '', location.pathname);
  }

  await refreshSessions();
  await loadHistory();
  refreshLLMState();

  if (mode === 'ask' && q) {
    send(q, { auto: true });
  } else if (mode === 'teach' && topic) {
    send(topic, { auto: true });
  }
}

boot();
