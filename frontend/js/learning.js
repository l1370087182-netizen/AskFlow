// 学习任务板：发布目标 → planner 拆解子题 → 逐题学习材料
// 交互：前端分页（每页 5 条）+ 抽屉式目标卡片 + 取消/删除 + 深链定位（?goal=）
// 侧栏：左=Agent 实时动态（/api/board/agents），右=work_log 查看器
// 轮询：页面可见就 3s 一轮（任务数据 + Agent 动态一起刷），不再「全终态停轮询」

const boardList = document.getElementById('board-list');
const boardState = document.getElementById('board-state');
const agentsList = document.getElementById('agents-list');
const worklogBody = document.getElementById('worklog-body');

const PAGE_SIZE = 5;
let allGoals = [];          // 数据源（轮询整体替换）
let curPage = 1;            // 页码独立于数据：轮询不重置
const openState = new Map();  // goalId -> 是否展开（用户手动开合说了算）
const seenGoals = new Set();  // 已套用过「默认展开规则」的目标
let pollTimer = null;
let curWorklogTask = null;  // 右栏正在展示的日志任务（in_progress 时随轮询刷新）
let deepGoalId = new URLSearchParams(location.search).get('goal');

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
const KIND_ZH = {
  crawl: '爬取', web_search: '联网检索', quality_review: '质检',
  term_curate: '术语整理', study_plan: '学习计划',
  learning_goal: '目标拆解', learning_item: '学习材料',
};
const LOG_ACTION_ZH = {
  create: '创建', claim: '认领', complete: '完成',
  fail: '失败', retry: '重试', cancel: '取消',
};

// ---------- 加载与渲染 ----------

async function loadBoard() {
  let data;
  try {
    data = await apiGet('/api/board/');
  } catch (e) {
    boardState.textContent = '加载失败：' + e.message;
    return;
  }
  allGoals = data.goals || [];
  renderBoard();
  loadAgents();
  // 右栏日志：选中任务仍在跑时跟随刷新
  if (curWorklogTask) loadWorklog(curWorklogTask, true);
}

function renderBoard() {
  if (!allGoals.length) {
    boardState.style.display = '';
    boardState.textContent = '还没有学习目标——发布一个，或到「模拟面试」页把面试计划发上任务板';
    boardList.innerHTML = '';
    document.getElementById('board-pager').innerHTML = '';
    return;
  }
  boardState.style.display = 'none';

  // 深链定位（数据就绪后消费一次）
  if (deepGoalId) {
    const idx = allGoals.findIndex((g) => g.task_id === deepGoalId);
    deepGoalId = null;
    history.replaceState(null, '', location.pathname);
    if (idx >= 0) {
      curPage = Math.floor(idx / PAGE_SIZE) + 1;
      openState.set(allGoals[idx].task_id, true);
      pendingScroll = allGoals[idx].task_id;
    } else {
      alert('目标不存在或已删除');
    }
  }

  const totalPages = Math.max(1, Math.ceil(allGoals.length / PAGE_SIZE));
  if (curPage > totalPages) curPage = totalPages;
  const pageGoals = allGoals.slice((curPage - 1) * PAGE_SIZE, curPage * PAGE_SIZE);

  boardList.innerHTML = '';
  for (const g of pageGoals) boardList.appendChild(renderGoal(g));
  renderPager(totalPages);

  if (pendingScroll) {
    const el = boardList.querySelector(`[data-goal-id="${pendingScroll}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('goal-flash');
    }
    pendingScroll = null;
  }
}
let pendingScroll = null;

function renderPager(totalPages) {
  const pager = document.getElementById('board-pager');
  pager.innerHTML = '';
  if (totalPages <= 1) return;
  const mkBtn = (label, disabled, onClick) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.disabled = disabled;
    if (!disabled) b.addEventListener('click', onClick);
    return b;
  };
  pager.appendChild(mkBtn('‹ 上一页', curPage <= 1, () => { curPage--; renderBoard(); }));
  const info = document.createElement('span');
  info.textContent = `第 ${curPage}/${totalPages} 页 · 共 ${allGoals.length} 个目标`;
  pager.appendChild(info);
  pager.appendChild(mkBtn('下一页 ›', curPage >= totalPages, () => { curPage++; renderBoard(); }));
}

function goalHasActive(g) {
  return ['pending', 'in_progress'].includes(g.status) ||
    g.items.some((it) => ['pending', 'in_progress'].includes(it.status));
}

function renderGoal(g) {
  const card = document.createElement('section');
  card.className = 'daily-card goal-card';
  card.dataset.goalId = g.task_id;

  // 默认展开规则只对首次出现套用一次，之后用户手动开合说了算
  if (!seenGoals.has(g.task_id)) {
    seenGoals.add(g.task_id);
    openState.set(g.task_id, goalHasActive(g));
  }
  const open = !!openState.get(g.task_id);
  if (open) card.classList.add('open');

  const [sLabel, sCls] = GOAL_STATUS[g.status] || [g.status, ''];

  // ---- 头部（整行可点，切换抽屉）----
  const head = document.createElement('div');
  head.className = 'goal-head';
  head.innerHTML = `
    <span class="goal-chevron">▸</span>
    <b class="goal-title"></b>
    <span class="badge">${g.source === 'interview' ? '面试' : '目标'}</span>
    <span class="badge ${sCls}">${sLabel}</span>
    <span class="row-meta">已完成 ${g.progress.done}/${g.progress.total}</span>
    <span class="goal-actions"></span>`;
  head.querySelector('.goal-title').textContent = g.goal || '（未命名目标）';

  const actions = head.querySelector('.goal-actions');
  if (['pending', 'in_progress'].includes(g.status) ||
      g.items.some((it) => ['pending', 'in_progress'].includes(it.status))) {
    const c = document.createElement('button');
    c.className = 'btn btn-danger-soft btn-mini';
    c.textContent = '取消';
    c.addEventListener('click', (e) => { e.stopPropagation(); cancelGoal(g.task_id, g.goal); });
    actions.appendChild(c);
  }
  const d = document.createElement('button');
  d.className = 'btn btn-danger-soft btn-mini';
  d.textContent = '删除';
  d.addEventListener('click', (e) => { e.stopPropagation(); deleteGoal(g.task_id, g.goal); });
  actions.appendChild(d);
  const log = document.createElement('button');
  log.className = 'btn btn-ghost btn-mini';
  log.textContent = '日志';
  log.addEventListener('click', (e) => { e.stopPropagation(); loadWorklog(g.task_id); });
  actions.appendChild(log);

  head.addEventListener('click', () => {
    openState.set(g.task_id, !openState.get(g.task_id));
    card.classList.toggle('open', openState.get(g.task_id));
  });
  card.appendChild(head);

  // ---- 抽屉内容：子题列表 ----
  const body = document.createElement('div');
  body.className = 'goal-body';
  if (!g.items.length) {
    const empty = document.createElement('div');
    empty.className = 'row-meta';
    empty.style.padding = '6px 0 10px';
    empty.textContent = g.status === 'pending' ? '排队中，等待 AI 拆解…'
      : g.status === 'in_progress' ? 'AI 正在拆解子题，马上就好…'
      : g.status === 'failed' ? '拆解失败，可删除后重新发布'
      : '（拆解完成，暂无子题）';
    body.appendChild(empty);
  }
  for (const it of g.items) body.appendChild(renderItem(it));
  card.appendChild(body);
  return card;
}

function renderItem(it) {
  const row = document.createElement('div');
  row.className = 'kb-row';
  row.style.cursor = 'default';
  const cp = it.crawl_progress;
  const crawlActive = it.waiting_crawl && it.status === 'pending' && cp;
  let sLabel, sCls;
  if (crawlActive) {
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
      <button class="btn btn-ghost btn-mini it-log">日志</button>
    </span>`;
  row.querySelector('.row-title').textContent = it.topic;
  row.querySelector('.it-log').addEventListener('click', (e) => {
    e.stopPropagation();
    loadWorklog(it.task_id);
  });

  // 补爬链路活跃：第二行进度条（检索阶段不定长动画；爬取阶段百分比 + 当前页）
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

// ---------- 操作 ----------

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
    curPage = 1;          // 新任务置顶，回第 1 页看它
    loadBoard();
  } catch (e) {
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function cancelGoal(taskId, goal) {
  if (!confirm(`取消目标「${goal}」？未完成的子题与补爬任务会一并取消（已爬到的资料保留）。`)) return;
  try {
    await apiPostJson(`/api/board/tasks/${taskId}/cancel`, {});
    loadBoard();
  } catch (e) {
    alert('取消失败：' + e.message);
  }
}

async function deleteGoal(taskId, goal) {
  if (!confirm(`删除目标「${goal}」？\n子题与相关爬取/检索任务会一并删除，已爬到的知识条目保留在知识库。`)) return;
  try {
    await apiDelete(`/api/board/tasks/${taskId}`);
    curPage = 1;
    loadBoard();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
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

// ---------- 左栏：Agent 动态 ----------

async function loadAgents() {
  let data;
  try { data = await apiGet('/api/board/agents'); } catch (e) { return; }
  agentsList.innerHTML = '';
  for (const a of data.agents || []) agentsList.appendChild(renderAgent(a));
}

function renderAgent(a) {
  const card = document.createElement('div');
  const working = a.status === 'working';
  card.className = 'agent-card' + (working ? ' working' : '');
  const statusTxt = working ? '工作中' : a.status === 'watching' ? '巡检中' : '空闲';
  const cls = working ? 'badge-running' : a.status === 'watching' ? 'badge-mine' : 'badge-pending';
  card.innerHTML = `
    <div class="ag-head">
      <span>${a.role}${/^producer-\d+$/.test(a.agent_id) ? '' : ''}</span>
      <span class="badge ${cls}">${statusTxt}</span>
    </div>
    <div class="ag-desc"></div>
    <div class="ag-time"></div>`;
  const desc = card.querySelector('.ag-desc');
  const timeEl = card.querySelector('.ag-time');
  if (working) {
    desc.textContent = a.desc || (a.kind ? `处理 ${KIND_ZH[a.kind] || a.kind} 任务` : '处理任务中');
    if (a.since) timeEl.textContent = '已持续 ' + duration(Date.now() / 1000 - a.since);
    if (a.task_id) {
      card.style.cursor = 'pointer';
      card.title = '点击查看该任务日志';
      card.addEventListener('click', () => loadWorklog(a.task_id));
    }
  } else if (a.status === 'watching') {
    desc.textContent = a.desc || '';
  } else {
    desc.textContent = '等待新任务';
  }
  return card;
}

function duration(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  if (sec < 60) return Math.floor(sec) + ' 秒';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟';
  return Math.floor(sec / 3600) + ' 小时';
}

// ---------- 右栏：work_log 查看器 ----------

async function loadWorklog(taskId, silent = false) {
  let d;
  try { d = await apiGet(`/api/board/tasks/${taskId}`); }
  catch (e) {
    if (!silent) worklogBody.innerHTML = '<p class="row-meta">任务不存在或无权查看</p>';
    curWorklogTask = null;
    return;
  }
  curWorklogTask = taskId;
  renderWorklog(d);
}

function renderWorklog(d) {
  const [sLabel, sCls] =
    { pending: ['排队中', 'badge-pending'], in_progress: ['执行中', 'badge-running'],
      completed: ['已完成', 'badge-done'], failed: ['失败', 'badge-failed'],
      canceled: ['已取消', 'badge-pending'] }[d.status] || [d.status, ''];
  let html = `
    <div class="wl-head">
      <b>${KIND_ZH[d.kind] || d.kind}</b>
      <span class="badge ${sCls}">${sLabel}</span>
    </div>`;
  const logs = d.work_log || [];
  if (!logs.length) {
    html += '<p class="row-meta">暂无日志</p>';
    worklogBody.innerHTML = html;
    return;
  }
  html += '<div class="wl-list">';
  for (const w of logs) {
    const action = LOG_ACTION_ZH[w.action] || w.action;
    const ts = (w.ts || '').replace('T', ' ').slice(0, 16);
    html += `
      <div class="wl-entry">
        <div class="wl-action">${action} <span class="row-meta">· ${w.agent || ''}</span></div>
        ${w.description ? `<div class="wl-desc"></div>` : ''}
        <div class="wl-time">${ts}</div>
      </div>`;
  }
  html += '</div>';
  worklogBody.innerHTML = html;
  // description 用 textContent 填入防注入
  const entries = worklogBody.querySelectorAll('.wl-entry');
  logs.forEach((w, i) => {
    const descEl = entries[i] && entries[i].querySelector('.wl-desc');
    if (descEl) descEl.textContent = w.description || '';
  });
}

// ---------- 轮询（页面可见就开，不再全终态停） ----------

function ensurePoll() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    if (!document.hidden) loadBoard();
  }, 3000);
}

// ---------- 启动 ----------

document.getElementById('btn-goal').addEventListener('click', publishGoal);
document.getElementById('goal-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); publishGoal(); }
});

loadBoard();
ensurePoll();
