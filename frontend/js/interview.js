// 模拟面试页：双图上传 → 开始 → 逐轮追问 → 结束总评+推荐

let sessionId = null;
let streaming = false;

const setup = document.getElementById('setup');
const chatBox = document.getElementById('iv-chat');
const msgs = document.getElementById('iv-msgs');
const input = document.getElementById('iv-input');

// 文件选择回显
for (const [fid, sid] of [['f-jd', 'f-jd-name'], ['f-resume', 'f-resume-name']]) {
  document.getElementById(fid).addEventListener('change', (e) => {
    document.getElementById(sid).textContent = e.target.files[0] ? e.target.files[0].name : '未选择';
  });
}

function scrollBottom() { msgs.scrollTop = msgs.scrollHeight; }

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + (role === 'user' ? 'user' : 'ai');
  div.innerHTML = `<div class="avatar">${role === 'user' ? '🧑' : '🎯'}</div>`;
  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (role === 'ai' ? ' rich' : '');
  if (role === 'ai') bubble.innerHTML = renderRich(text);
  else bubble.textContent = text;
  div.appendChild(bubble);
  msgs.appendChild(div);
  scrollBottom();
  return bubble;
}

async function start() {
  const jd = document.getElementById('f-jd').files[0];
  const resume = document.getElementById('f-resume').files[0];
  const err = document.getElementById('setup-err');
  if (!jd || !resume) { err.textContent = '请同时选择 JD 截图和简历截图'; return; }
  err.textContent = '';
  const btn = document.getElementById('btn-start');
  btn.disabled = true; btn.textContent = '分析中（OCR+解析，约半分钟）…';

  const fd = new FormData();
  fd.append('jd', jd);
  fd.append('resume', resume);
  let d;
  try {
    // FormData：只加鉴权头，勿手工设 Content-Type（会破坏 multipart boundary）
    const resp = await fetch(API_BASE + '/api/interview/start', {
      method: 'POST', body: fd, headers: authHeaders(),
    });
    if (resp.status === 401) { handleAuthError(resp); return; }
    // 先读文本再解析：后端异常时可能返回非 JSON，直接 .json() 会崩
    const raw = await resp.text();
    try { d = JSON.parse(raw); } catch (_) { d = null; }
    if (!resp.ok) {
      const detail = d && d.detail;
      throw new Error(typeof detail === 'string' ? detail : (raw || `HTTP ${resp.status}`));
    }
    if (!d) throw new Error('响应不是有效 JSON');
  } catch (e) {
    err.textContent = e.message; btn.disabled = false; btn.textContent = '开始面试';
    return;
  }

  sessionId = d.session_id;
  setup.style.display = 'none';
  chatBox.style.display = '';
  document.getElementById('iv-title').textContent = ' ' + (d.title || '模拟面试');
  const tags = document.getElementById('iv-tags');
  tags.innerHTML = '';
  for (const t of d.tech_stack.slice(0, 10)) {
    const c = document.createElement('span');
    c.className = 'source-chip';
    c.textContent = `${t.name}·${t.level === 'required' ? '必问' : '加分'}`;
    tags.appendChild(c);
  }
  for (const s of d.resume_skills.slice(0, 8)) {
    const c = document.createElement('span');
    c.className = 'source-chip';
    c.style.borderColor = 'var(--green)'; c.style.color = 'var(--green)';
    c.textContent = '简历·' + s;
    tags.appendChild(c);
  }
  addMsg('ai', d.first_question);
  // 不自动聚焦输入框：平板聚焦会弹软键盘，等用户准备作答时自己点
}

async function send(finish = false) {
  if (streaming || !sessionId) return;
  const text = input.value.trim();
  if (!finish && !text) return;
  streaming = true;
  document.getElementById('iv-send').disabled = true;
  document.getElementById('iv-undo').disabled = true;
  input.value = '';
  if (!finish) addMsg('user', text);

  const bubble = addMsg('ai', '');
  bubble.classList.add('cursor');
  bubble.textContent = finish ? '评分中…' : '思考中…';
  let reply = '';

  try {
    await streamChatLike({ session_id: sessionId, message: finish ? '' : text, finish }, (ev) => {
      if (ev.type === 'token') { reply += ev.content; bubble.textContent = reply; scrollBottom(); }
      else if (ev.type === 'round') document.getElementById('iv-round').textContent = `第 ${ev.rounds}/${ev.max_rounds} 轮`;
      else if (ev.type === 'recs') renderRecs(ev.items);
      else if (ev.type === 'record') loadRecords();   // 面试记录已落库，刷新列表
      else if (ev.type === 'error') bubble.textContent = '⚠️ ' + ev.message;
    });
    bubble.classList.remove('cursor');
    if (reply) { bubble.classList.add('rich'); bubble.innerHTML = renderRich(reply); }
    if (finish) { input.disabled = true; document.getElementById('btn-finish').disabled = true; }
  } catch (e) {
    bubble.classList.remove('cursor');
    bubble.textContent = '⚠️ ' + e.message;
  }
  streaming = false;
  document.getElementById('iv-send').disabled = false;
  document.getElementById('iv-undo').disabled = false;
  scrollBottom();
}

// ---------- 撤回 ----------

// 撤回上一轮回答：删掉最后「我的回答 + 面试官点评」，原文回填输入框续写
async function undoLastIv() {
  if (streaming || !sessionId) return;
  const btn = document.getElementById('iv-undo');
  btn.disabled = true;
  try {
    const d = await apiPostJson('/api/chat/undo', { session_id: sessionId, mode: 'interview' });
    const kids = [...msgs.children];
    let aiIdx = -1;
    for (let i = kids.length - 1; i >= 0; i--) {
      if (kids[i].classList.contains('ai')) { aiIdx = i; break; }
    }
    if (aiIdx > 0 && kids[aiIdx - 1].classList.contains('user')) {
      kids[aiIdx].remove();
      kids[aiIdx - 1].remove();
    }
    input.value = d.restored || '';
    // 不自动聚焦：要续写自然会被动点输入框（平板避免误弹键盘）
  } catch (e) {
    if (!String(e.message).includes('没有可撤回')) alert('撤回失败：' + e.message);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 面试记录 ----------

let currentRecordId = null;

async function loadRecords() {
  const listBox = document.getElementById('records-list');
  try {
    const d = await apiGet('/api/interview/records');
    document.getElementById('records-count').textContent =
      d.items.length ? `共 ${d.items.length} 次` : '';
    if (!d.items.length) {
      listBox.innerHTML = '<div class="row-meta">还没有面试记录</div>';
      return;
    }
    listBox.innerHTML = '';
    for (const r of d.items) {
      const row = document.createElement('div');
      row.className = 'kb-row';
      row.innerHTML = `
        <span class="row-title"></span>
        <span class="row-meta">${(r.created_at || '').slice(0, 10)} · ${r.rounds} 轮 ·
          弱项 ${r.weaknesses} · 缺口 ${r.gaps}${r.has_plan ? ' · 📝 已生成计划' : ''}</span>`;
      row.querySelector('.row-title').textContent = r.jd_title || '模拟面试';
      row.addEventListener('click', () => openRecord(r.id));
      listBox.appendChild(row);
    }
  } catch (e) {
    listBox.innerHTML = '<div class="row-meta">记录加载失败</div>';
  }
}

function chipsHtml(items) {
  const box = document.createElement('div');
  box.style.marginTop = '6px';
  if (!items || !items.length) { box.innerHTML = '<span class="row-meta">（无）</span>'; return box; }
  for (const t of items) {
    const c = document.createElement('span');
    c.className = 'source-chip';
    c.textContent = t;
    box.appendChild(c);
  }
  return box;
}

async function openRecord(id) {
  currentRecordId = id;
  let d;
  try { d = await apiGet(`/api/interview/records/${id}`); }
  catch (e) { alert('加载记录失败：' + e.message); return; }

  document.getElementById('record-title').textContent = d.jd_title || '模拟面试';
  const meta = document.getElementById('record-meta');
  meta.innerHTML = `<span class="row-meta">${(d.created_at || '').slice(0, 16).replace('T', ' ')} · ${d.rounds} 轮</span>`;
  const wRow = document.createElement('div');
  wRow.innerHTML = '<b>薄弱点：</b>';
  wRow.appendChild(chipsHtml(d.weaknesses));
  const gRow = document.createElement('div');
  gRow.innerHTML = '<b>JD 缺口：</b>';
  gRow.appendChild(chipsHtml(d.gap_topics));
  meta.appendChild(wRow);
  meta.appendChild(gRow);

  document.getElementById('record-body').innerHTML = renderRich(d.final_summary || '（无总评）');
  document.getElementById('plan-view').style.display = 'none';
  document.getElementById('plan-msg').textContent = '';
  const btn = document.getElementById('btn-gen-plan');
  btn.disabled = false;
  btn.textContent = d.plan_task_id ? '重新生成学习计划' : '生成学习计划';
  document.getElementById('record-modal').style.display = '';
  if (d.plan_task_id) refreshPlanView();   // 已有计划任务：直接展示/续查
}

async function genPlan() {
  const btn = document.getElementById('btn-gen-plan');
  const msg = document.getElementById('plan-msg');
  btn.disabled = true;
  msg.textContent = '正在派发规划任务…';
  try {
    await apiPostJson(`/api/interview/records/${currentRecordId}/plan`, {});
    refreshPlanView();
  } catch (e) {
    msg.textContent = e.message;
    btn.disabled = false;
  }
}

// 面试弱项+缺口直接发布到任务板（确定性拆解，planner 建学习子题）
async function toBoard() {
  const btn = document.getElementById('btn-to-board');
  const msg = document.getElementById('plan-msg');
  btn.disabled = true;
  msg.textContent = '发布中…';
  try {
    await apiPostJson('/api/board/from-interview', { record_id: currentRecordId });
    msg.textContent = '✓ 已上任务板';
    if (confirm('已发布到任务板，现在去查看？')) location.href = 'learning.html';
  } catch (e) {
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function refreshPlanView() {
  const msg = document.getElementById('plan-msg');
  const view = document.getElementById('plan-view');
  const btn = document.getElementById('btn-gen-plan');
  let d;
  try { d = await apiGet(`/api/interview/records/${currentRecordId}/plan`); }
  catch (e) { msg.textContent = e.message; btn.disabled = false; return; }

  if (d.status === 'pending' || d.status === 'in_progress') {
    msg.textContent = '规划中（检索知识库+生成计划）…';
    // 弹窗仍开着才继续轮询
    setTimeout(() => {
      if (document.getElementById('record-modal').style.display === '') refreshPlanView();
    }, 2000);
    return;
  }
  if (d.status === 'completed' && d.output && d.output.plan_md) {
    view.innerHTML = renderRich(d.output.plan_md);
    view.style.display = '';
    msg.textContent = '';
    btn.textContent = '重新生成学习计划';
  } else if (d.status === 'failed') {
    msg.textContent = '计划生成失败：' + ((d.output && d.output.error) || '未知原因');
  } else {
    msg.textContent = '';
  }
  btn.disabled = false;
  loadRecords();   // 同步列表上的「已生成计划」标记
}

function closeRecordModal() {
  document.getElementById('record-modal').style.display = 'none';
}

// answer 端点 SSE 读取（复用 chat 的解析逻辑）
async function streamChatLike(payload, onEvent) {
  const resp = await fetch(API_BASE + '/api/interview/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    handleAuthError(resp);
    throw new Error(resp.statusText);
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder('utf-8');
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data:')) {
          try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) {}
        }
      }
    }
  }
}

function renderRecs(items) {
  if (!items || !items.length) return;
  const div = document.createElement('div');
  div.className = 'msg ai';
  div.innerHTML = '<div class="avatar">📚</div>';
  const card = document.createElement('div');
  card.className = 'eval-card';
  card.innerHTML = '<h2>推荐补强（JD 要求但简历未体现）</h2>';
  const ul = document.createElement('ul');
  for (const it of items) {
    const li = document.createElement('li');
    li.textContent = it.term ? `${it.topic} → 看卡片「${it.term}」：${it.brief}` : `${it.topic} → 知识库暂无对应卡片，建议自行补充`;
    ul.appendChild(li);
  }
  card.appendChild(ul);
  div.appendChild(card);
  msgs.appendChild(div);
  scrollBottom();
}

document.getElementById('btn-start').addEventListener('click', start);
document.getElementById('iv-send').addEventListener('click', () => send(false));
document.getElementById('iv-undo').addEventListener('click', undoLastIv);
document.getElementById('btn-finish').addEventListener('click', () => send(true));
document.getElementById('btn-refresh-records').addEventListener('click', loadRecords);
document.getElementById('btn-gen-plan').addEventListener('click', genPlan);
document.getElementById('btn-to-board').addEventListener('click', toBoard);
document.getElementById('record-close').addEventListener('click', closeRecordModal);
document.getElementById('record-modal').addEventListener('click', (e) => {
  if (e.target.id === 'record-modal') closeRecordModal();
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(false); }
});

loadRecords();

// 通知深链接：?record=<id> → 打开对应面试记录（不存在时 openRecord 内提示）
(function () {
  const rid = new URLSearchParams(location.search).get('record');
  if (rid && /^\d+$/.test(rid)) {
    openRecord(parseInt(rid, 10));
    history.replaceState(null, '', location.pathname);
  }
})();
