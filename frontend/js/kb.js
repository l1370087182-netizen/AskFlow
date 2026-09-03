// 知识库页：全局浏览（分类聚合 → 条目列表 → 正文弹窗）+ 我的知识（个人知识库）
// 我的知识：手工添加 / 编辑 / 删除 / 爬取整站（后台任务进度轮询）

const ICONS = { fastapi: '⚡', python: '🐍', ai: '🤖', general: '📚' };

// 当前 tab：'global' | 'mine'
let currentTab = 'global';
// 爬取进度轮询定时器
let crawlTimer = null;
// 正在编辑的条目 id（null = 新建）
let editingId = null;
// 分页：每页 10 条；「我的知识」记住当前页，增删改后停在原页刷新
const PAGE_SIZE = 10;
let myPage = 1;
// 详情弹窗按需翻译：原文/译文与当前视图状态（弹窗关闭即失效）
let detailId = null;
let detailOriginal = null;    // {title, content}
let detailTranslated = null;  // {title, content}
let detailShowingTrans = false;

// ---------- tab 切换 ----------

function switchTab(tab) {
  if (tab === currentTab) return;
  currentTab = tab;
  document.getElementById('tab-global').classList.toggle('active', tab === 'global');
  document.getElementById('tab-mine').classList.toggle('active', tab === 'mine');
  document.getElementById('pane-global').style.display = tab === 'global' ? '' : 'none';
  document.getElementById('pane-mine').style.display = tab === 'mine' ? '' : 'none';
  // 切 tab 前清轮询，避免后台持续请求
  stopCrawlPolling();
  if (tab === 'mine') {
    loadMyList(1);
    // 刷新/重进页面后，进行中的爬取任务自动恢复进度面板（置顶显示）
    resumeActiveCrawl();
  }
}

document.getElementById('tab-global').addEventListener('click', () => switchTab('global'));
document.getElementById('tab-mine').addEventListener('click', () => switchTab('mine'));

// ---------- 全局知识库 ----------

async function loadCategories() {
  let data;
  try {
    data = await apiGet('/api/knowledge/categories');
  } catch (e) {
    document.getElementById('kb-state').textContent = '加载失败：' + e.message;
    return;
  }
  if (!data.categories.length) {
    document.getElementById('kb-state').textContent = '知识库还是空的，先跑爬虫或上传文档吧';
    return;
  }
  document.getElementById('kb-state').style.display = 'none';
  document.getElementById('kb-sub').textContent =
    `共 ${data.total} 条知识 · ${data.categories.length} 个分类`;

  const grid = document.getElementById('kb-grid');
  grid.style.display = '';
  grid.innerHTML = '';
  for (const c of data.categories) {
    const tile = document.createElement('div');
    tile.className = 'kb-tile';
    tile.innerHTML = `
      <div class="tile-icon">${ICONS[c.category] || '📚'}</div>
      <b>${c.count}</b>
      <span>${categoryLabel(c.category)}</span>`;
    tile.title = '点击查看该分类的知识';
    tile.addEventListener('click', () => loadList(c.category));
    grid.appendChild(tile);
  }
}

async function loadList(category, page = 1) {
  const wrap = document.getElementById('kb-list-wrap');
  const listBox = document.getElementById('kb-list');
  const pagerBox = document.getElementById('kb-pager');
  document.getElementById('kb-list-title').textContent = categoryLabel(category);
  document.getElementById('kb-list-count').textContent = '';
  wrap.style.display = '';
  listBox.innerHTML = '<div class="state-box">加载中…</div>';
  pagerBox.innerHTML = '';

  let data;
  try {
    data = await apiGet(
      `/api/knowledge/?category=${encodeURIComponent(category)}` +
      `&limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`);
  } catch (e) {
    listBox.innerHTML = '<div class="state-box">加载失败：' + e.message + '</div>';
    return;
  }

  // 页码越界钳制（分类数据变化等极端情况）
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  if (data.total > 0 && page > pages) return loadList(category, pages);

  document.getElementById('kb-list-count').textContent = `${data.total} 条`;
  listBox.innerHTML = '';
  for (const item of data.items) {
    const row = document.createElement('div');
    row.className = 'kb-row';
    row.innerHTML = `
      <span class="row-title"></span>
      <span class="row-meta">${statusLabel(item.status)} · ${sourceLabel(item.source_type)}` +
      (item.created_at ? ' · ' + item.created_at.slice(0, 10) : '') + '</span>';
    row.querySelector('.row-title').textContent = item.title;
    row.addEventListener('click', () => openDetail(item.id));
    listBox.appendChild(row);
  }
  renderPager(pagerBox, data.total, page, (p) => loadList(category, p));
  // 仅换分类（第 1 页）时滚动到列表，翻页不打断阅读位置
  if (page === 1) wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function openDetail(id) {
  let d;
  try {
    d = await apiGet(`/api/knowledge/${id}`);
  } catch (e) {
    alert('加载详情失败：' + e.message);
    return;
  }
  document.getElementById('modal-title').textContent = d.title;

  // 个人条目加「我的」徽标；manual:// 占位键不渲染超链接
  const mineBadge = d.user_id > 0 ? '<span class="badge badge-mine">我的</span>' : '';
  const isManual = d.source_url && d.source_url.startsWith('manual://');
  const link = (d.source_url && !isManual)
    ? ` <a href="${d.source_url}" target="_blank" rel="noopener">原文链接 ↗</a>`
    : '';
  document.getElementById('modal-meta').innerHTML =
    mineBadge +
    `<span class="category-tag">${categoryLabel(d.category)}</span>` +
    `<span class="row-meta">${sourceLabel(d.source_type)} · ` +
    (d.created_at ? d.created_at.slice(0, 10) : '') + '</span>' + link;
  document.getElementById('modal-body').innerHTML = renderKnowledge(d.content);
  document.getElementById('kb-modal').style.display = '';

  // 按需翻译：记住原文，翻译按钮复位
  detailId = d.id;
  detailOriginal = { title: d.title, content: d.content };
  detailTranslated = null;
  detailShowingTrans = false;
  const btn = document.getElementById('btn-translate');
  btn.style.display = '';
  btn.disabled = false;
  btn.textContent = '🌐 翻译';
}

// 详情弹窗：整篇翻译（代码块保留），可切回原文
async function toggleTranslate() {
  const btn = document.getElementById('btn-translate');
  if (detailShowingTrans) {
    document.getElementById('modal-title').textContent = detailOriginal.title;
    document.getElementById('modal-body').innerHTML = renderKnowledge(detailOriginal.content);
    detailShowingTrans = false;
    btn.textContent = '🌐 翻译';
    return;
  }
  if (!detailTranslated) {
    btn.disabled = true;
    btn.textContent = '翻译中…';
    try {
      const r = await apiPostJson(`/api/translate/knowledge/${detailId}`, {});
      if (r.same_language) {
        btn.disabled = false;
        btn.textContent = '🌐 翻译';
        alert('内容已经是中文，无需翻译');
        return;
      }
      detailTranslated = { title: r.title, content: r.content };
    } catch (e) {
      btn.disabled = false;
      btn.textContent = '🌐 翻译';
      alert('翻译失败：' + e.message);
      return;
    }
  }
  document.getElementById('modal-title').textContent =
    detailTranslated.title || detailOriginal.title;
  document.getElementById('modal-body').innerHTML = renderKnowledge(detailTranslated.content);
  detailShowingTrans = true;
  btn.disabled = false;
  btn.textContent = '原文';
}

document.getElementById('btn-translate').addEventListener('click', toggleTranslate);
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('kb-modal').addEventListener('click', (e) => {
  if (e.target.id === 'kb-modal') closeModal();
});
function closeModal() {
  document.getElementById('kb-modal').style.display = 'none';
  document.getElementById('btn-translate').style.display = 'none';
}

// ---------- 我的知识：列表 ----------

async function loadMyList(page = myPage) {
  const listBox = document.getElementById('my-list');
  const stateBox = document.getElementById('my-state');
  const pagerBox = document.getElementById('my-pager');
  listBox.innerHTML = '';
  pagerBox.innerHTML = '';
  stateBox.style.display = '';
  stateBox.textContent = '加载中…';

  let data;
  try {
    data = await apiGet(
      `/api/knowledge/my?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`);
  } catch (e) {
    stateBox.textContent = '加载失败：' + e.message;
    return;
  }

  // 页码越界钳制（如删光末页最后一条）：回到最后一页
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  if (data.total > 0 && page > pages) return loadMyList(pages);
  myPage = page;

  document.getElementById('my-count').textContent = `共 ${data.total} 条`;
  if (!data.items.length) {
    stateBox.textContent = '还没有个人知识，点「手工添加」或「爬取整站」开始积累吧';
    return;
  }
  stateBox.style.display = 'none';

  for (const item of data.items) {
    const row = document.createElement('div');
    row.className = 'kb-row';
    row.innerHTML = `
      <span class="row-title"></span>
      <span class="row-right">
        <span class="row-meta">${statusLabel(item.status)} · ${sourceLabel(item.source_type)}` +
        (item.created_at ? ' · ' + item.created_at.slice(0, 10) : '') + `</span>
        <button class="btn btn-ghost btn-mini act-edit" type="button">编辑</button>
        <button class="btn btn-danger-soft btn-mini act-del" type="button">删除</button>
      </span>`;
    row.querySelector('.row-title').textContent = item.title;
    row.addEventListener('click', () => openDetail(item.id));
    row.querySelector('.act-edit').addEventListener('click', (e) => {
      e.stopPropagation();
      openEditModal(item.id);
    });
    row.querySelector('.act-del').addEventListener('click', (e) => {
      e.stopPropagation();
      deleteMy(item.id, item.title);
    });
    listBox.appendChild(row);
  }
  renderPager(pagerBox, data.total, page, (p) => loadMyList(p));
}

async function deleteMy(id, title) {
  if (!confirm(`确定删除「${title}」？向量与记录都会移除，不可恢复。`)) return;
  try {
    const r = await apiDelete(`/api/knowledge/my/${id}`);
    if (r.vectors_removed === -1) {
      alert('条目已删除，但向量库暂时不可用，残留向量将在下次全量重建时清理');
    }
    loadMyList();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

// ---------- 我的知识：添加 / 编辑弹窗 ----------

function openAddModal() {
  editingId = null;
  document.getElementById('edit-modal-title').textContent = '添加知识';
  document.getElementById('edit-tip').textContent =
    '正文至少 50 字；保存后立即向量化，随即可在对话中检索。手工添加不做 AI 清洗。';
  document.getElementById('edit-title').value = '';
  document.getElementById('edit-category').value = '';
  document.getElementById('edit-content').value = '';
  setEditMsg('');
  document.getElementById('edit-modal').style.display = '';
}

async function openEditModal(id) {
  let d;
  try {
    d = await apiGet(`/api/knowledge/${id}`);
  } catch (e) {
    alert('加载条目失败：' + e.message);
    return;
  }
  editingId = id;
  document.getElementById('edit-modal-title').textContent = '编辑知识';
  document.getElementById('edit-tip').textContent =
    '保存后立即重新向量化；期间检索可能短暂命中旧内容。';
  document.getElementById('edit-title').value = d.title;
  document.getElementById('edit-category').value = d.category;
  document.getElementById('edit-content').value = d.content;
  setEditMsg('');
  document.getElementById('edit-modal').style.display = '';
}

function closeEditModal() {
  document.getElementById('edit-modal').style.display = 'none';
}

function setEditMsg(text, isErr) {
  const el = document.getElementById('edit-msg');
  el.textContent = text;
  el.className = 'auth-msg' + (isErr ? ' err' : ' ok');
}

async function saveEdit() {
  const title = document.getElementById('edit-title').value.trim();
  const category = document.getElementById('edit-category').value.trim() || 'general';
  const content = document.getElementById('edit-content').value.trim();

  if (!title) { setEditMsg('请填写标题', true); return; }
  if (content.length < 50) { setEditMsg('正文至少 50 字', true); return; }

  const btn = document.getElementById('edit-save');
  btn.disabled = true;
  setEditMsg('保存中…');
  try {
    let item;
    if (editingId) {
      item = await apiPutJson(`/api/knowledge/my/${editingId}`, { title, category, content });
    } else {
      item = await apiPostJson('/api/knowledge/my', { title, category, content });
    }
    closeEditModal();
    if (item.status === 2) {
      alert('已保存，但向量化失败（可能是模型服务暂不可用）。可稍后重新编辑保存重试。');
    }
    // 新建跳第 1 页看新条目（id 倒序）；编辑停在当前页刷新
    loadMyList(editingId ? myPage : 1);
  } catch (e) {
    setEditMsg(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-add').addEventListener('click', openAddModal);
document.getElementById('edit-close').addEventListener('click', closeEditModal);
document.getElementById('edit-save').addEventListener('click', saveEdit);
document.getElementById('edit-modal').addEventListener('click', (e) => {
  if (e.target.id === 'edit-modal') closeEditModal();
});

// ---------- 我的知识：爬取整站 ----------

function openCrawlModal() {
  document.getElementById('crawl-url').value = '';
  document.getElementById('crawl-category').value = '';
  document.getElementById('crawl-max-pages').value = '10';
  setCrawlMsg('');
  document.getElementById('crawl-modal').style.display = '';
}

function closeCrawlModal() {
  document.getElementById('crawl-modal').style.display = 'none';
}

function setCrawlMsg(text, isErr) {
  const el = document.getElementById('crawl-msg');
  el.textContent = text;
  el.className = 'auth-msg' + (isErr ? ' err' : ' ok');
}

async function submitCrawl() {
  const url = document.getElementById('crawl-url').value.trim();
  const category = document.getElementById('crawl-category').value.trim() || 'general';
  const maxPages = parseInt(document.getElementById('crawl-max-pages').value, 10) || 10;

  if (!url) { setCrawlMsg('请填写种子 URL', true); return; }
  if (maxPages < 1 || maxPages > 20) { setCrawlMsg('页数需在 1–20 之间', true); return; }

  const btn = document.getElementById('crawl-submit');
  btn.disabled = true;
  setCrawlMsg('提交中…');
  try {
    const r = await apiPostJson('/api/knowledge/my/crawl', { url, category, max_pages: maxPages });
    closeCrawlModal();
    trackCrawlTask(r.task_id);
  } catch (e) {
    if (e.status === 400) {
      setCrawlMsg(e.message + '（到对话学习页 ⚙️ 配置模型：chat.html）', true);
    } else {
      // 409 = 活跃任务达上限，其余错误原样提示
      setCrawlMsg(e.message, true);
    }
  } finally {
    btn.disabled = false;
  }
}

// ---------- 爬取面板：多任务并行跟踪 ----------

// task_id -> {el, done}；单个定时器（顶部 crawlTimer）轮询全部跟踪中的任务
const crawlTasks = new Map();

const CRAWL_STATUS_LABEL = {
  pending: '排队中',
  running: '爬取中',
  searching: '联网检索中',
  done: '已完成',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
};
const CRAWL_BADGE_CLASS = {
  pending: 'badge-pending', running: 'badge-running', searching: 'badge-running',
  done: 'badge-done', partial: 'badge-partial', failed: 'badge-failed',
  canceled: 'badge-pending',
};
// 终态：停止轮询、隐藏取消按钮（canceled 前已入库的页面保留，列表照常刷新）
const CRAWL_TERMINAL = ['done', 'partial', 'failed', 'canceled'];

function trackCrawlTask(taskId, initialState) {
  if (crawlTasks.has(taskId)) return;
  const el = document.createElement('div');
  el.className = 'crawl-task';
  el.innerHTML = `
    <div class="crawl-task-head">
      <span class="ct-badge badge">等待中</span>
      <span class="ct-url row-meta"></span>
      <button class="ct-cancel btn btn-danger-soft btn-mini" style="margin-left:auto">取消</button>
    </div>
    <div class="progress"><div class="ct-bar progress-bar" style="width:0%"></div></div>
    <div class="ct-current row-meta"></div>
    <div class="ct-pages crawl-pages"></div>
    <div class="ct-summary crawl-summary"></div>`;
  el.querySelector('.ct-cancel').addEventListener('click', () => cancelCrawlTask(taskId));
  document.getElementById('crawl-tasks').appendChild(el);
  crawlTasks.set(taskId, { el, done: false });
  document.getElementById('crawl-panel').style.display = '';
  renderCrawlTask(taskId, initialState ||
    { status: 'pending', max_pages: 0, pages: [], current_url: '', url: '' });
  updateCrawlCount();
  ensureCrawlTimer();
}

async function cancelCrawlTask(taskId) {
  if (!confirm('取消这个任务？已爬到的页面会保留在知识库里。')) return;
  try {
    await apiPostJson(`/api/knowledge/my/crawl/${taskId}/cancel`, {});
    pollCrawlTasks();  // 立即刷一轮，状态尽快变「已取消」
  } catch (e) {
    alert('取消失败：' + e.message);
  }
}

function removeCrawlTask(taskId) {
  const rec = crawlTasks.get(taskId);
  if (!rec) return;
  rec.el.remove();
  crawlTasks.delete(taskId);
  updateCrawlCount();
  if (!crawlTasks.size) {
    stopCrawlPolling();
    document.getElementById('crawl-panel').style.display = 'none';
  }
}

function updateCrawlCount() {
  let running = 0;
  for (const rec of crawlTasks.values()) if (!rec.done) running++;
  document.getElementById('crawl-count').textContent =
    crawlTasks.size ? `${running} 进行中 / 共 ${crawlTasks.size} 个` : '';
}

function ensureCrawlTimer() {
  if (crawlTimer) return;
  crawlTimer = setInterval(pollCrawlTasks, 2000);
  pollCrawlTasks();
}

function stopCrawlPolling() {
  if (crawlTimer) {
    clearInterval(crawlTimer);
    crawlTimer = null;
  }
}

async function pollCrawlTasks() {
  for (const [taskId, rec] of [...crawlTasks]) {
    if (rec.done) continue;
    let task;
    try {
      task = await apiGet(`/api/knowledge/my/crawl/${taskId}`);
    } catch (e) {
      // 404（过期/不存在）等：移除任务块
      removeCrawlTask(taskId);
      continue;
    }
    renderCrawlTask(taskId, task);
    // 联网检索交接出的子爬取任务：自动开一个新任务块跟踪
    if (task.child_task_id && !crawlTasks.has(task.child_task_id)) {
      trackCrawlTask(task.child_task_id, {
        status: 'pending', max_pages: 0, pages: [], current_url: '',
        url: task.topic || task.url || '',
      });
    }
    if (CRAWL_TERMINAL.includes(task.status)) {
      rec.done = true;
      // 新入库条目排在最前，回第 1 页刷新
      loadMyList(1);
      updateCrawlCount();
    }
  }
}

function renderCrawlTask(taskId, task) {
  const rec = crawlTasks.get(taskId);
  if (!rec) return;
  const el = rec.el;

  const badge = el.querySelector('.ct-badge');
  badge.textContent = CRAWL_STATUS_LABEL[task.status] || task.status;
  badge.className = 'ct-badge badge ' + (CRAWL_BADGE_CLASS[task.status] || '');

  // 联网检索任务没有种子 URL，展示检索主题
  const headUrl = el.querySelector('.ct-url');
  if (task.url) headUrl.textContent = task.url;
  else if (task.topic) headUrl.textContent = '检索主题：' + task.topic;

  const finished = (task.done_pages || 0) + (task.failed_pages || 0) + (task.skipped_pages || 0);
  const pct = task.max_pages ? Math.min(100, Math.round(finished / task.max_pages * 100)) : 0;
  const bar = el.querySelector('.ct-bar');
  if (task.status === 'searching') {
    // 检索阶段无页数可算 → 不定长流光动画
    bar.classList.add('progress-bar-indeterminate');
    bar.style.width = '100%';
  } else {
    bar.classList.remove('progress-bar-indeterminate');
    bar.style.width = pct + '%';
  }

  const current = el.querySelector('.ct-current');
  if (task.status === 'searching') {
    current.textContent = task.phase || '正在联网检索…';
  } else if (task.status === 'running' && task.current_url) {
    current.textContent = '正在处理：' + task.current_url;
  } else {
    current.textContent = '';
  }

  const pagesBox = el.querySelector('.ct-pages');
  pagesBox.innerHTML = '';
  for (const p of task.pages || []) {
    const line = document.createElement('div');
    line.className = 'crawl-page-row';
    if (p.ok) {
      line.innerHTML = `<span class="pg-ok">✓</span><span class="pg-url"></span>` +
        (p.cleaned ? '<span class="row-meta">（AI 清洗）</span>' : '<span class="row-meta">（原文）</span>');
    } else {
      line.innerHTML = `<span class="pg-fail">✗</span><span class="pg-url"></span>` +
        `<span class="row-meta">${p.error || '失败'}</span>`;
    }
    line.querySelector('.pg-url').textContent = p.url;
    pagesBox.appendChild(line);
  }

  const summary = el.querySelector('.ct-summary');
  if (CRAWL_TERMINAL.includes(task.status)) {
    if (task.status === 'canceled') {
      summary.textContent = `任务已取消（已入库 ${task.done_pages || 0} 页，保留在知识库）`;
    } else {
      let text = `结果：成功 ${task.done_pages} · 失败 ${task.failed_pages} · 跳过 ${task.skipped_pages}`;
      if (task.error) text += ' · ' + task.error;
      summary.textContent = text;
    }
    el.querySelector('.ct-cancel').style.display = 'none';
  } else {
    summary.textContent = '';
    el.querySelector('.ct-cancel').style.display = '';
  }
}

// 进入「我的知识」时恢复进行中的爬取任务（刷新页面后面板不丢）
async function resumeActiveCrawl() {
  let data;
  try {
    data = await apiGet('/api/knowledge/my/crawl/active');
  } catch (e) {
    return; // 出错静默，不影响列表浏览
  }
  const live = new Set(
    (data.tasks || [])
      .filter((t) => ['pending', 'running', 'searching'].includes(t.status))
      .map((t) => t.task_id)
  );
  // 面板上残留但已不活跃（完成/过期）的任务块：移除
  for (const taskId of [...crawlTasks.keys()]) {
    if (!live.has(taskId)) removeCrawlTask(taskId);
  }
  for (const t of data.tasks || []) {
    if (live.has(t.task_id)) trackCrawlTask(t.task_id, t);
  }
  // 切 tab 时停掉的轮询，若还有未完成任务则恢复
  if ([...crawlTasks.values()].some((r) => !r.done)) ensureCrawlTimer();
}

document.getElementById('btn-crawl').addEventListener('click', openCrawlModal);
document.getElementById('crawl-close').addEventListener('click', closeCrawlModal);
document.getElementById('crawl-submit').addEventListener('click', submitCrawl);
document.getElementById('crawl-modal').addEventListener('click', (e) => {
  if (e.target.id === 'crawl-modal') closeCrawlModal();
});
document.getElementById('crawl-panel-close').addEventListener('click', () => {
  stopCrawlPolling();
  // 清空任务块；仍在进行的任务下次进入「我的知识」会自动恢复
  crawlTasks.clear();
  document.getElementById('crawl-tasks').innerHTML = '';
  document.getElementById('crawl-count').textContent = '';
  document.getElementById('crawl-panel').style.display = 'none';
});

// ---------- 我的知识：AI 添加（对话式定题 → 自动爬取） ----------

// 对话历史（模态框打开期间内存保持；关闭即重置）
let aiHistory = [];

function openAiModal() {
  aiHistory = [];
  document.getElementById('ai-msgs').innerHTML = '';
  appendAiMsg('assistant', '想了解什么？告诉我主题，我来找最合适的官方文档爬进你的知识库。');
  setAiTip('');
  document.getElementById('ai-modal').style.display = '';
  document.getElementById('ai-input').focus();
}

function closeAiModal() {
  document.getElementById('ai-modal').style.display = 'none';
}

function appendAiMsg(role, text) {
  const box = document.getElementById('ai-msgs');
  const div = document.createElement('div');
  div.className = 'ai-msg ' + role;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setAiTip(text, isErr) {
  const el = document.getElementById('ai-tip');
  el.textContent = text;
  el.className = 'auth-msg' + (isErr ? ' err' : ' ok');
}

async function sendAiAdd() {
  const input = document.getElementById('ai-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  appendAiMsg('user', text);
  aiHistory.push({ role: 'user', content: text });

  const btn = document.getElementById('ai-send');
  btn.disabled = true;
  setAiTip('AI 正在思考…');
  try {
    const r = await apiPostJson('/api/knowledge/my/ai-add', { messages: aiHistory });
    aiHistory.push({ role: 'assistant', content: r.message });
    appendAiMsg('assistant', r.message);
    setAiTip('');
    // action=crawl（已提交任务）→ 直接挂上进度面板
    if (r.task_id) {
      closeAiModal();
      trackCrawlTask(r.task_id);
    }
  } catch (e) {
    setAiTip(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-ai-add').addEventListener('click', openAiModal);
document.getElementById('ai-close').addEventListener('click', closeAiModal);
document.getElementById('ai-send').addEventListener('click', sendAiAdd);
document.getElementById('ai-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendAiAdd();
});
document.getElementById('ai-modal').addEventListener('click', (e) => {
  if (e.target.id === 'ai-modal') closeAiModal();
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  closeModal();
  closeEditModal();
  closeCrawlModal();
  closeAiModal();
});

// ---------- 小工具 ----------

// 通用分页条：‹ 上一页 / 第 x / y 页 · 共 N 条 / 下一页 ›；单页不渲染
function renderPager(container, total, page, onPage) {
  container.innerHTML = '';
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (pages <= 1) return;

  const prev = document.createElement('button');
  prev.className = 'btn btn-ghost btn-mini';
  prev.type = 'button';
  prev.textContent = '‹ 上一页';
  prev.disabled = page <= 1;
  prev.addEventListener('click', () => onPage(page - 1));

  const info = document.createElement('span');
  info.className = 'row-meta';
  info.textContent = `第 ${page} / ${pages} 页 · 共 ${total} 条`;

  const next = document.createElement('button');
  next.className = 'btn btn-ghost btn-mini';
  next.type = 'button';
  next.textContent = '下一页 ›';
  next.disabled = page >= pages;
  next.addEventListener('click', () => onPage(page + 1));

  container.append(prev, info, next);
}

function statusLabel(status) {
  return status === 1 ? '已向量化' : status === 2 ? '向量化失败' : '待向量化';
}

function sourceLabel(sourceType) {
  return { upload: '上传', spider: '爬虫', personal: '个人' }[sourceType] || sourceType;
}

loadCategories();

// 通知深链接：?tab=mine → 直接进「我的知识」（爬取面板会自动恢复）
if (new URLSearchParams(location.search).get('tab') === 'mine') {
  switchTab('mine');
  history.replaceState(null, '', location.pathname);
}
