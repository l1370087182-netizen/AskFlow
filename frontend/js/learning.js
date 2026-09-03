// 学习任务板：发布目标 → planner 拆解子题 → 逐题学习材料
// 有进行中任务时自动轮询刷新；全部落定后停止

const boardList = document.getElementById('board-list');
const boardState = document.getElementById('board-state');
let pollTimer = null;

const GOAL_STATUS = {
  pending: ['排队中', 'badge-pending'],
  in_progress: ['拆解中', 'badge-running'],
  completed: ['已拆解', 'badge-done'],
  failed: ['失败', 'badge-failed'],
  canceled: ['已取消', 'badge-pending'],
};
const ITEM_STATUS = {
  pending: ['排队中', 'badge-pending'],
  in_progress: ['编写中', 'badge-running'],
  completed: ['可学习', 'badge-done'],
  failed: ['失败', 'badge-failed'],
  canceled: ['已取消', 'badge-pending'],
};
const PRIO_ZH = { high: '高优', medium: '中', low: '低' };

function hasActive(data) {
  return data.goals.some(g =>
    ['pending', 'in_progress'].includes(g.status) ||
    g.items.some(it => ['pending', 'in_progress'].includes(it.status)));
}

async function loadBoard() {
  let data;
  try {
    data = await apiGet('/api/board/');
  } catch (e) {
    boardState.textContent = '加载失败：' + e.message;
    return;
  }
  if (!data.goals.length) {
    boardState.style.display = '';
    boardState.textContent = '还没有学习目标——发布一个，或到「模拟面试」页把面试计划发上任务板';
    boardList.innerHTML = '';
    stopPoll();
    return;
  }
  boardState.style.display = 'none';
  boardList.innerHTML = '';
  for (const g of data.goals) boardList.appendChild(renderGoal(g));

  // 有进行中任务 → 轮询；全部落定 → 停
  if (hasActive(data)) ensurePoll();
  else stopPoll();
}

function renderGoal(g) {
  const card = document.createElement('section');
  card.className = 'daily-card';
  card.style.padding = '18px 24px';
  card.style.marginBottom = '16px';

  const [sLabel, sCls] = GOAL_STATUS[g.status] || [g.status, ''];
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap';
  const title = document.createElement('b');
  title.textContent = g.goal || '（未命名目标）';
  const badge = document.createElement('span');
  badge.className = 'badge ' + sCls;
  badge.textContent = g.source === 'interview' ? '面试' : '目标';
  const progress = document.createElement('span');
  progress.className = 'row-meta';
  progress.textContent = `已完成 ${g.progress.done}/${g.progress.total}`;
  head.append(title, badge, progress);

  // 取消按钮：目标未结束时可用
  if (['pending', 'in_progress'].includes(g.status) ||
      g.items.some(it => ['pending', 'in_progress'].includes(it.status))) {
    const sp = document.createElement('span');
    sp.style.flex = '1';
    const btn = document.createElement('button');
    btn.className = 'btn btn-danger-soft btn-mini';
    btn.textContent = '取消目标';
    btn.addEventListener('click', () => cancelGoal(g.task_id, g.goal));
    head.append(sp, btn);
  }
  card.appendChild(head);

  const list = document.createElement('div');
  list.className = 'kb-list';
  list.style.marginTop = '12px';
  if (!g.items.length) {
    const empty = document.createElement('div');
    empty.className = 'row-meta';
    empty.textContent = g.status === 'pending' ? '排队中，等待 AI 拆解…' : '（暂无子题）';
    list.appendChild(empty);
  }
  for (const it of g.items) list.appendChild(renderItem(it));
  card.appendChild(list);
  return card;
}

function renderItem(it) {
  const row = document.createElement('div');
  row.className = 'kb-row';
  const cp = it.crawl_progress;
  const crawlActive = it.waiting_crawl && it.status === 'pending' && cp;
  let sLabel, sCls;
  if (crawlActive) {
    // 缺资料自动补爬：按链路阶段显示（检索中 / 排队 / 爬取中 x/y）
    if (cp.status === 'searching') [sLabel, sCls] = ['检索资料中', 'badge-running'];
    else if (cp.status === 'pending') [sLabel, sCls] = ['爬取排队', 'badge-pending'];
    else {
      const finished = (cp.done || 0) + (cp.failed || 0) + (cp.skipped || 0);
      [sLabel, sCls] = [
        cp.max_pages ? `爬取资料中 ${finished}/${cp.max_pages}` : '爬取资料中',
        'badge-running',
      ];
    }
  } else {
    [sLabel, sCls] = ITEM_STATUS[it.status] || [it.status, ''];
  }
  row.innerHTML = `
    <span class="row-title"></span>
    <span class="row-right">
      <span class="badge ${it.priority === 'high' ? 'badge-failed' : 'badge-mine'}">${PRIO_ZH[it.priority] || '中'}</span>
      <span class="badge ${sCls}">${sLabel}</span>
    </span>`;
  row.querySelector('.row-title').textContent = it.topic;

  // 补爬链路活跃：第二行进度条（检索阶段不定长动画；爬取阶段按页数百分比）
  if (crawlActive) {
    row.style.flexWrap = 'wrap';
    const wrap = document.createElement('div');
    wrap.className = 'board-crawl-progress';
    if (cp.status === 'searching' || !cp.max_pages) {
      wrap.innerHTML = `
        <div class="progress"><div class="progress-bar progress-bar-indeterminate" style="width:100%"></div></div>
        <div class="row-meta bc-cur"></div>`;
      wrap.querySelector('.bc-cur').textContent = cp.phase || '正在联网检索资料…';
    } else {
      const finished = (cp.done || 0) + (cp.failed || 0) + (cp.skipped || 0);
      const pct = Math.min(100, Math.round(finished / cp.max_pages * 100));
      wrap.innerHTML = `
        <div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
        <div class="row-meta bc-cur"></div>`;
      const cur = wrap.querySelector('.bc-cur');
      cur.textContent = cp.current_url
        ? '正在爬：' + (cp.current_url.length > 60 ? cp.current_url.slice(0, 60) + '…' : cp.current_url)
        : `已完成 ${finished}/${cp.max_pages} 页`;
    }
    row.appendChild(wrap);
  }

  if (it.status === 'completed') {
    row.style.cursor = 'pointer';
    row.title = '点击查看学习材料';
    row.addEventListener('click', () => openItem(it.task_id, it.topic));
  }
  return row;
}

async function openItem(taskId, topic) {
  let d;
  try { d = await apiGet(`/api/board/tasks/${taskId}`); }
  catch (e) { alert('加载失败：' + e.message); return; }
  document.getElementById('item-title').textContent = d.output.topic || topic;
  const reason = (d.payload || {}).reason || '';
  document.getElementById('item-meta').innerHTML =
    `<span class="row-meta">${reason}</span>`;
  document.getElementById('item-body').innerHTML =
    renderRich((d.output || {}).material_md || '（材料生成中或失败）');
  document.getElementById('item-modal').style.display = '';
}

document.getElementById('item-close').addEventListener('click', () => {
  document.getElementById('item-modal').style.display = 'none';
});
document.getElementById('item-modal').addEventListener('click', (e) => {
  if (e.target.id === 'item-modal') document.getElementById('item-modal').style.display = 'none';
});

async function publishGoal() {
  const input = document.getElementById('goal-input');
  const msg = document.getElementById('goal-msg');
  const goal = input.value.trim();
  if (!goal) { msg.textContent = '先填写学习目标'; return; }
  const btn = document.getElementById('btn-goal');
  btn.disabled = true;
  msg.textContent = '已提交，AI 正在拆解…';
  try {
    await apiPostJson('/api/board/goals', { goal });
    input.value = '';
    msg.textContent = '';
    loadBoard();
  } catch (e) {
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function cancelGoal(taskId, goal) {
  if (!confirm(`取消目标「${goal}」？未完成的子题会一并取消。`)) return;
  try {
    await apiPostJson(`/api/board/tasks/${taskId}/cancel`, {});
    loadBoard();
  } catch (e) {
    alert('取消失败：' + e.message);
  }
}

function ensurePoll() {
  if (pollTimer) return;
  pollTimer = setInterval(loadBoard, 3000);
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

document.getElementById('btn-goal').addEventListener('click', publishGoal);
document.getElementById('goal-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); publishGoal(); }
});

loadBoard();
