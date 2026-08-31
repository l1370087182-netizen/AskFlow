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
    const resp = await fetch(API_BASE + '/api/interview/start', { method: 'POST', body: fd });
    d = await resp.json();
    if (!resp.ok) throw new Error(d.detail || 'start 失败');
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
  input.focus();
}

async function send(finish = false) {
  if (streaming || !sessionId) return;
  const text = input.value.trim();
  if (!finish && !text) return;
  streaming = true;
  document.getElementById('iv-send').disabled = true;
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
  scrollBottom();
}

// answer 端点 SSE 读取（复用 chat 的解析逻辑）
async function streamChatLike(payload, onEvent) {
  const resp = await fetch(API_BASE + '/api/interview/answer', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(resp.statusText);
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
document.getElementById('btn-finish').addEventListener('click', () => send(true));
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(false); }
});
