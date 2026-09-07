// 学习任务板：发布目标 → planner 拆解子题 → 逐题学习材料
// 交互：前端分页（每页 5 条）+ 抽屉式目标卡片 + 取消/删除 + 深链定位（?goal=）
// 侧栏：左=Agent 角色泳道（气泡三态：忙碌脉冲/巡检/空闲，按 agent_id 原地更新不闪烁），
//       右=work_log 时间线（重绘保滚动/贴底跟随、新条目高亮、复制全部）
// 窄屏：三页签互斥切换（任务板/Agent/日志），点气泡看日志自动跳日志页签
// 轮询：页面可见就 3s 一轮（任务数据 + Agent 动态一起刷）；本地 1s tick 只刷新忙碌气泡的时长文本

const boardList = document.getElementById('board-list');
const boardState = document.getElementById('board-state');
const agentsLanes = document.getElementById('agents-lanes');
const agentsSummary = document.getElementById('agents-summary');
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
const KIND_ICON = {
  crawl: '📥', web_search: '🌐', quality_review: '🔎',
  term_curate: '📇', study_plan: '📋',
  learning_goal: '🧭', learning_item: '📖',
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

// ---------- 左栏：Agent 角色泳道 ----------
// 结构：每个角色一条泳道，行内一个气泡/实例。轮询按 agent_id 原地更新
// （不整栏重绘 → 无闪烁）；忙碌气泡的持续时长由本地 1s tick 递增。

const LANE_ORDER = ['爬取生产', '联网检索', '知识质检', '术语整理', '学习规划', '超时回收'];
const laneEls = new Map();     // role -> lane 元素
const bubbleEls = new Map();   // agent_id -> { el, timeEl, state, since, taskId }

async function loadAgents() {
  let data;
  try { data = await apiGet('/api/board/agents'); } catch (e) { return; }
  const agents = data.agents || [];
  const seen = new Set();

  for (const a of agents) {
    seen.add(a.agent_id);
    const lane = ensureLane(a.role);
    let b = bubbleEls.get(a.agent_id);
    if (!b) { b = createBubble(a, lane); bubbleEls.set(a.agent_id, b); }
    updateBubble(b, a);
  }
  // Agent 池缩容等场景：消失的实例摘除气泡
  for (const [id, b] of bubbleEls) {
    if (!seen.has(id)) { b.el.remove(); bubbleEls.delete(id); }
  }
  // 泳道高亮按实际气泡状态重算（同角色多实例一忙一闲时不受更新顺序影响）
  for (const [role, lane] of laneEls) {
    const anyBusy = [...bubbleEls.values()].some((b) =>
      (b.state === 'working' || b.state === 'watching') && lane.contains(b.el));
    lane.classList.toggle('active', anyBusy);
  }
  const working = agents.filter((a) => a.status === 'working').length;
  agentsSummary.textContent = agents.length ? `忙碌 ${working} / 共 ${agents.length} 个 Agent` : '';
}

function ensureLane(role) {
  if (laneEls.has(role)) return laneEls.get(role);
  const lane = document.createElement('div');
  lane.className = 'lane';
  lane.innerHTML = `
    <span class="lane-name"></span>
    <div class="lane-track"></div>`;
  lane.querySelector('.lane-name').textContent = role;
  // 固定顺序插入；未知角色排后面
  const idx = LANE_ORDER.indexOf(role);
  const next = [...laneEls.entries()]
    .filter(([r]) => LANE_ORDER.indexOf(r) > idx || (idx < 0 && LANE_ORDER.indexOf(r) >= 0))
    .map(([, el]) => el)[0];
  agentsLanes.insertBefore(lane, next || null);
  laneEls.set(role, lane);
  return lane;
}

function createBubble(a, lane) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'bubble';
  el.innerHTML = `
    <span class="b-dot"></span>
    <span class="b-kind"></span>
    <span class="b-time"></span>`;
  el.addEventListener('click', () => {
    const b = bubbleEls.get(a.agent_id);
    if (b && b.taskId) loadWorklog(b.taskId);
  });
  lane.querySelector('.lane-track').appendChild(el);
  return { el, timeEl: el.querySelector('.b-time'), kindEl: el.querySelector('.b-kind'),
           state: '', since: 0, taskId: '' };
}

function updateBubble(b, a) {
  const el = b.el;
  const working = a.status === 'working';
  const watching = a.status === 'watching';
  el.classList.toggle('working', working);
  el.classList.toggle('watching', watching);
  el.classList.toggle('idle', !working && !watching);

  b.state = a.status;
  b.since = working ? (a.since || 0) : 0;
  b.taskId = a.task_id || '';
  b.kindEl.textContent = working ? (KIND_ICON[a.kind] || '⚙') : '';

  const desc = a.desc || (working && a.kind ? `处理 ${KIND_ZH[a.kind] || a.kind} 任务` : '');
  el.title = desc ? `${desc}${b.taskId ? '\n（点击查看日志）' : ''}`
    : watching ? '巡检中' : '空闲';
  el.classList.toggle('clickable', !!b.taskId);
  b.timeEl.textContent = working && b.since ? duration(Date.now() / 1000 - b.since) : '';
}

// 本地秒表：忙碌气泡的时长文本每秒递增（轮询间隔 3s，精度靠它补）
setInterval(() => {
  for (const b of bubbleEls.values()) {
    if (b.state === 'working' && b.since) {
      b.timeEl.textContent = duration(Date.now() / 1000 - b.since);
    }
  }
}, 1000);

function duration(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  if (sec < 60) return Math.floor(sec) + ' 秒';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟';
  return Math.floor(sec / 3600) + ' 小时';
}

// ---------- 右栏：work_log 查看器（时间线） ----------
// 重绘策略：记录滚动位置与「贴底跟随」状态，重绘后还原——执行中的任务
// 每 3s 刷新一次，用户翻看历史时不再被弹回顶部；贴底时新条目自动滚入并高亮。

let wlPrevKeys = null;     // 上次渲染的日志 key 集合（切任务时重置，首次不高亮）

async function loadWorklog(taskId, silent = false) {
  if (!silent) setMobileTab('log');   // 主动看日志：窄屏切到日志页签（桌面端无副作用）
  let d;
  try { d = await apiGet(`/api/board/tasks/${taskId}`); }
  catch (e) {
    if (!silent) worklogBody.innerHTML = '<p class="row-meta">任务不存在或无权查看</p>';
    curWorklogTask = null;
    wlPrevKeys = null;
    return;
  }
  if (curWorklogTask !== taskId) { wlPrevKeys = null; worklogBody._wlScroll = null; }   // 换任务：不高亮、贴底看最新
  curWorklogTask = taskId;
  renderWorklog(d);
}

function logKey(w) { return `${w.ts}|${w.action}|${w.agent || ''}|${w.description || ''}`; }

function relTime(ts) {
  const t = Date.parse(ts || '');
  if (isNaN(t)) return '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return Math.floor(sec) + ' 秒前';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
  return Math.floor(sec / 86400) + ' 天前';
}

function renderWorklog(d) {
  const [sLabel, sCls] =
    { pending: ['排队中', 'badge-pending'], in_progress: ['执行中', 'badge-running'],
      completed: ['已完成', 'badge-done'], failed: ['失败', 'badge-failed'],
      canceled: ['已取消', 'badge-pending'] }[d.status] || [d.status, ''];
  const live = ['pending', 'in_progress'].includes(d.status);
  const logs = d.work_log || [];

  let html = `
    <div class="wl-head">
      <b></b>
      <span class="badge ${sCls}">${sLabel}</span>
      ${live ? '<span class="wl-live">实时跟随中</span>' : ''}
      ${logs.length ? '<button type="button" class="btn btn-ghost btn-mini wl-copy">复制全部</button>' : ''}
    </div>
    <div class="wl-list">`;
  if (!logs.length) {
    html += '<p class="row-meta">暂无日志</p></div>';
    worklogBody.innerHTML = html;
    wlPrevKeys = new Set();
    return;
  }
  for (const w of logs) {
    const action = LOG_ACTION_ZH[w.action] || w.action;
    const full = (w.ts || '').replace('T', ' ');
    html += `
      <div class="wl-entry">
        <div class="wl-action">${action} <span class="row-meta">· ${w.agent || ''}</span></div>
        ${w.description ? `<div class="wl-desc"></div>` : ''}
        <div class="wl-time" title="${full}"></div>
      </div>`;
  }
  html += '</div>';
  worklogBody.innerHTML = html;

  worklogBody.querySelector('.wl-head b').textContent = KIND_ZH[d.kind] || d.kind;
  const entries = worklogBody.querySelectorAll('.wl-entry');
  logs.forEach((w, i) => {
    const descEl = entries[i].querySelector('.wl-desc');
    if (descEl) descEl.textContent = w.description || '';
    const tEl = entries[i].querySelector('.wl-time');
    if (tEl) tEl.textContent = relTime(w.ts);
  });

  // 新条目高亮：与上次该任务的 key 集合对比（首次渲染不闪）
  const keys = logs.map(logKey);
  const newSet = wlPrevKeys ? keys.filter((k) => !wlPrevKeys.has(k)) : [];
  entries.forEach((el, i) => { if (newSet.includes(keys[i])) el.classList.add('wl-new'); });
  wlPrevKeys = new Set(keys);

  // 滚动：贴底跟随（新条目自动滚入），翻看历史则保持原位
  const list = worklogBody.querySelector('.wl-list');
  const prev = worklogBody._wlScroll;
  if (prev && !prev.bottom) {
    list.scrollTop = prev.top;
  } else {
    list.scrollTop = list.scrollHeight;   // 贴底 / 首次渲染都看最新
  }
  list.addEventListener('scroll', () => {
    worklogBody._wlScroll = {
      top: list.scrollTop,
      bottom: list.scrollTop + list.clientHeight >= list.scrollHeight - 30,
    };
  }, { passive: true });

  // 复制全部
  const copyBtn = worklogBody.querySelector('.wl-copy');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      const text = logs.map((w) =>
        `[${(w.ts || '').replace('T', ' ')}] ${LOG_ACTION_ZH[w.action] || w.action}`
          + `${w.agent ? ' · ' + w.agent : ''}`
          + (w.description ? ` — ${w.description}` : '')
      ).join('\n');
      copyText(text, copyBtn);
    });
  }
}

function copyText(text, btn) {
  const done = () => {
    const old = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = old; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { /* 忽略 */ }
  ta.remove();
}

// ---------- 窄屏页签：任务板 / Agent / 日志 三选一 ----------
// 桌面端三栏全显，页签隐藏（CSS 控制）；窄屏按 body class 互斥显示

function setMobileTab(tab) {   // 'board' | 'agents' | 'log'
  document.body.classList.toggle('mtab-agents', tab === 'agents');
  document.body.classList.toggle('mtab-log', tab === 'log');
  document.querySelectorAll('.bmtab').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
}

document.querySelectorAll('.bmtab').forEach((b) => {
  b.addEventListener('click', () => setMobileTab(b.dataset.tab));
});

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
