// 知识库页：分类聚合 → 条目列表 → 正文弹窗

const ICONS = { fastapi: '⚡', python: '🐍', ai: '🤖', general: '📚' };

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
    tile.addEventListener('click', () => loadList(c.category, c.count));
    grid.appendChild(tile);
  }
}

async function loadList(category, count) {
  const wrap = document.getElementById('kb-list-wrap');
  const listBox = document.getElementById('kb-list');
  document.getElementById('kb-list-title').textContent = categoryLabel(category);
  document.getElementById('kb-list-count').textContent = `${count} 条`;
  wrap.style.display = '';
  listBox.innerHTML = '<div class="state-box">加载中…</div>';

  let data;
  try {
    data = await apiGet(
      `/api/knowledge/?category=${encodeURIComponent(category)}&limit=200`);
  } catch (e) {
    listBox.innerHTML = '<div class="state-box">加载失败：' + e.message + '</div>';
    return;
  }

  listBox.innerHTML = '';
  for (const item of data.items) {
    const row = document.createElement('div');
    row.className = 'kb-row';
    const status = item.status === 1 ? '已向量化' : item.status === 2 ? '失败' : '待向量化';
    row.innerHTML = `
      <span class="row-title"></span>
      <span class="row-meta">${status}${item.created_at ? ' · ' + item.created_at.slice(0, 10) : ''}</span>`;
    row.querySelector('.row-title').textContent = item.title;
    row.addEventListener('click', () => openDetail(item.id));
    listBox.appendChild(row);
  }
  wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
  document.getElementById('modal-meta').innerHTML =
    `<span class="category-tag">${categoryLabel(d.category)}</span>` +
    `<span class="row-meta">${d.source_type === 'upload' ? '上传' : '爬虫'} · ` +
    (d.created_at ? d.created_at.slice(0, 10) : '') + '</span>' +
    (d.source_url ? ` <a href="${d.source_url}" target="_blank" rel="noopener">原文链接 ↗</a>` : '');
  document.getElementById('modal-body').innerHTML = renderRich(d.content);
  document.getElementById('kb-modal').style.display = '';
}

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('kb-modal').addEventListener('click', (e) => {
  if (e.target.id === 'kb-modal') closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});
function closeModal() {
  document.getElementById('kb-modal').style.display = 'none';
}

loadCategories();
