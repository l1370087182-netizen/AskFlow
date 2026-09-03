// 公共顶栏用户区：登录门禁 + 注入「通知铃铛 + 邮箱 + 退出」
// 约定：所有业务页面在 api.js 之后引入本脚本（依赖 getToken / apiGet / apiPostJson）。
// 铃铛：红点=有未读；下拉面板未读高亮/已读灰；批量已读/删除；点击跳转对应页面。

(function () {
  // 未登录 → 直接去登录页（带回访地址）
  if (!getToken()) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.replace('login.html?next=' + next);
    return;
  }

  const POLL_MS = 5000;          // 红点轮询间隔
  const PANEL_LIMIT = 30;        // 面板一次拉的条数

  let panel = null;              // 下拉面板元素（首次打开才建）
  let panelOpen = false;
  let dot = null;

  // 注入用户信息；顺带探活：token 失效时 apiGet 会触发 401 跳转
  apiGet('/api/auth/me')
    .then((me) => {
      const inner = document.querySelector('.topbar-inner');
      if (!inner) return;
      const box = document.createElement('div');
      box.className = 'topbar-user';

      // 铃铛（放在用户名左边）
      const bell = document.createElement('button');
      bell.className = 'tu-bell';
      bell.title = '通知';
      bell.innerHTML = '🔔<span class="tu-dot" hidden></span>';
      dot = bell.querySelector('.tu-dot');
      bell.addEventListener('click', (e) => {
        e.stopPropagation();
        togglePanel(bell);
      });

      const mail = document.createElement('span');
      mail.className = 'tu-email';
      mail.title = '当前登录账号';
      mail.textContent = '📧 ' + me.email;

      const out = document.createElement('button');
      out.className = 'tu-logout';
      out.textContent = '退出';
      out.addEventListener('click', logout);

      box.appendChild(bell);
      box.appendChild(mail);
      box.appendChild(out);
      inner.appendChild(box);

      refreshDot();
      setInterval(refreshDot, POLL_MS);
    })
    .catch(() => { /* 401 已由 apiGet 跳转；其他网络错误不打扰 */ });

  // ---------- 红点 ----------

  async function refreshDot() {
    if (document.hidden || !dot) return;   // 后台标签页不发请求
    try {
      const d = await apiGet('/api/notification/unread-count');
      dot.hidden = !(d.unread > 0);
      // 面板开着时顺带刷新列表（有新通知进来即时可见）
      if (panelOpen && panel) loadList();
    } catch (e) { /* 静默 */ }
  }

  // ---------- 下拉面板 ----------

  function togglePanel(bell) {
    if (panelOpen) { closePanel(); return; }
    if (!panel) buildPanel(bell);
    panel.style.display = '';
    panelOpen = true;
    loadList();
    // 面板外点击关闭
    setTimeout(() => document.addEventListener('click', onDocClick), 0);
  }

  function closePanel() {
    panelOpen = false;
    if (panel) panel.style.display = 'none';
    document.removeEventListener('click', onDocClick);
  }

  function onDocClick(e) {
    if (panel && !panel.contains(e.target)) closePanel();
  }

  function buildPanel(bell) {
    panel = document.createElement('div');
    panel.className = 'notif-panel';
    panel.addEventListener('click', (e) => e.stopPropagation());
    panel.innerHTML = `
      <div class="np-head">
        <b>通知</b>
        <span class="np-actions">
          <button class="np-btn" data-act="read">全部已读</button>
          <button class="np-btn np-danger" data-act="del">全部删除</button>
        </span>
      </div>
      <div class="np-list"></div>`;
    panel.querySelector('[data-act="read"]').addEventListener('click', async () => {
      try {
        await apiPostJson('/api/notification/read', { all: true });
        await loadList();
        refreshDot();
      } catch (e) { /* 静默 */ }
    });
    panel.querySelector('[data-act="del"]').addEventListener('click', async () => {
      if (!confirm('清空全部通知？')) return;
      try {
        await apiPostJson('/api/notification/delete', { all: true });
        await loadList();
        refreshDot();
      } catch (e) { /* 静默 */ }
    });
    bell.parentElement.style.position = 'relative';
    bell.parentElement.appendChild(panel);
  }

  async function loadList() {
    const listBox = panel.querySelector('.np-list');
    let data;
    try {
      data = await apiGet(`/api/notification/?limit=${PANEL_LIMIT}`);
    } catch (e) {
      listBox.innerHTML = '<div class="np-empty">加载失败</div>';
      return;
    }
    if (!data.items.length) {
      listBox.innerHTML = '<div class="np-empty">暂无通知</div>';
      return;
    }
    listBox.innerHTML = '';
    for (const it of data.items) listBox.appendChild(renderItem(it));
  }

  function renderItem(it) {
    const row = document.createElement('div');
    row.className = 'np-item ' + (it.is_read ? 'read' : 'unread');
    row.innerHTML = `
      <div class="np-title"></div>
      <div class="np-body"></div>
      <div class="np-time"></div>`;
    row.querySelector('.np-title').textContent = it.title;
    row.querySelector('.np-body').textContent = it.body || '';
    row.querySelector('.np-time').textContent = relTime(it.created_at);
    row.addEventListener('click', async () => {
      // 先标记已读，再跳转（跳转后本页面卸载，请求可能丢——尽力而为）
      try { await apiPostJson('/api/notification/read', { ids: [it.id] }); } catch (e) {}
      if (it.link) location.href = it.link;
      else closePanel();
    });
    return row;
  }

  // ---------- 工具 ----------

  function relTime(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    const diff = (Date.now() - t) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + ' 天前';
    return new Date(t).toLocaleDateString();
  }

  function logout() {
    // 清登录态与会话本地状态，回登录页
    ['rag_token', 'rag_cur_ask', 'rag_cur_teach', 'rag_current_session', 'rag_mode']
      .forEach((k) => localStorage.removeItem(k));
    location.replace('login.html');
  }
})();
