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
}

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('kb-modal').addEventListener('click', (e) => {
  if (e.target.id === 'kb-modal') closeModal();
});
function closeModal() {
  document.getElementById('kb-modal').style.display = 'none';
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
    startCrawlPolling(r.task_id);
  } catch (e) {
    if (e.status === 409 && e.detail && e.detail.task_id) {
      // 已有活跃任务：直接用返回的 task_id 续看进度
      closeCrawlModal();
      startCrawlPolling(e.detail.task_id);
      return;
    }
    if (e.status === 400) {
      setCrawlMsg(e.message + '（到对话学习页 ⚙️ 配置模型：chat.html）', true);
    } else {
      setCrawlMsg(e.message, true);
    }
  } finally {
    btn.disabled = false;
  }
}

function stopCrawlPolling() {
  if (crawlTimer) {
    clearInterval(crawlTimer);
    crawlTimer = null;
  }
}

function startCrawlPolling(taskId) {
  stopCrawlPolling();
  document.getElementById('crawl-panel').style.display = '';
  renderCrawlPanel({ status: 'pending', max_pages: 0, pages: [], current_url: '' });
  const tick = async () => {
    let task;
    try {
      task = await apiGet(`/api/knowledge/my/crawl/${taskId}`);
    } catch (e) {
      // 404（过期/不存在）等：停止轮询并提示
      stopCrawlPolling();
      document.getElementById('crawl-summary').textContent = '进度查询失败：' + e.message;
      return;
    }
    renderCrawlPanel(task);
    if (['done', 'partial', 'failed'].includes(task.status)) {
      stopCrawlPolling();
      // 新入库条目排在最前，回第 1 页刷新
      loadMyList(1);
    }
  };
  tick();
  crawlTimer = setInterval(tick, 2000);
}

// 进入「我的知识」时恢复进行中的爬取任务（刷新页面后面板不丢）
async function resumeActiveCrawl() {
  let task;
  try {
    task = await apiGet('/api/knowledge/my/crawl/active');
  } catch (e) {
    return; // 404 = 无进行中任务，静默
  }
  if (['pending', 'running'].includes(task.status)) {
    startCrawlPolling(task.task_id);
  }
}

const CRAWL_STATUS_LABEL = {
  pending: '排队中',
  running: '爬取中',
  done: '已完成',
  partial: '部分完成',
  failed: '失败',
};

function renderCrawlPanel(task) {
  const badge = document.getElementById('crawl-badge');
  badge.textContent = CRAWL_STATUS_LABEL[task.status] || task.status;
  badge.className = 'badge ' + ({
    pending: 'badge-pending', running: 'badge-running',
    done: 'badge-done', partial: 'badge-partial', failed: 'badge-failed',
  }[task.status] || '');

  const finished = (task.done_pages || 0) + (task.failed_pages || 0) + (task.skipped_pages || 0);
  const pct = task.max_pages ? Math.min(100, Math.round(finished / task.max_pages * 100)) : 0;
  document.getElementById('crawl-bar').style.width = pct + '%';

  document.getElementById('crawl-current').textContent =
    task.status === 'running' && task.current_url
      ? '正在处理：' + task.current_url
      : '';

  const pagesBox = document.getElementById('crawl-pages');
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

  const summary = document.getElementById('crawl-summary');
  if (['done', 'partial', 'failed'].includes(task.status)) {
    let text = `结果：成功 ${task.done_pages} · 失败 ${task.failed_pages} · 跳过 ${task.skipped_pages}`;
    if (task.error) text += ' · ' + task.error;
    summary.textContent = text;
  } else {
    summary.textContent = '';
  }
}

document.getElementById('btn-crawl').addEventListener('click', openCrawlModal);
document.getElementById('crawl-close').addEventListener('click', closeCrawlModal);
document.getElementById('crawl-submit').addEventListener('click', submitCrawl);
document.getElementById('crawl-modal').addEventListener('click', (e) => {
  if (e.target.id === 'crawl-modal') closeCrawlModal();
});
document.getElementById('crawl-panel-close').addEventListener('click', () => {
  stopCrawlPolling();
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
    // action=crawl（已提交任务）或 ask 但带回活跃任务（409）→ 直接跳进度面板
    if (r.task_id) {
      closeAiModal();
      startCrawlPolling(r.task_id);
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
