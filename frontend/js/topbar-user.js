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

      maybePromptModel();   // 未配置大模型 → 弹窗引导（每个浏览器会话最多一次）
    })
    .catch(() => { /* 401 已由 apiGet 跳转；其他网络错误不打扰 */ });

  // ---------- 未配置模型弹窗 ----------

  async function maybePromptModel() {
    // 管理员不用模型；本会话已弹过不再打扰
    if (localStorage.getItem('rag_role') === 'admin') return;
    if (sessionStorage.getItem('rag_model_prompted')) return;
    let cfg;
    try {
      cfg = await apiGet('/api/user/llm');
    } catch (e) { return; }
    if (cfg.has_custom) return;
    sessionStorage.setItem('rag_model_prompted', '1');

    const mask = document.createElement('div');
    mask.className = 'modal-mask model-prompt';
    mask.innerHTML = `
      <div class="modal small model-prompt-card" role="dialog" aria-modal="true">
        <div class="modal-head"><h3>🧩 先配置你的大模型</h3></div>
        <p class="mp-text">
          问渠 AskFlow 不内置默认模型——对话学习、模拟面试、知识清洗等 AI 能力都使用
          <b>你自己的模型</b>（按你的 API 用量计费，账号隔离）。<br>
          你还没有配置，现在就去填一下吧：<b>API 地址 / Key / 模型名</b>，配置一次全站通用。
        </p>
        <div class="mp-actions">
          <button class="mp-btn mp-later">暂不</button>
          <button class="mp-btn mp-go">去配置</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    mask.querySelector('.mp-later').addEventListener('click', () => mask.remove());
    mask.querySelector('.mp-go').addEventListener('click', () => {
      mask.remove();
      // 在对话页就直接弹配置窗，否则跳过去并自动打开
      if (location.pathname.endsWith('chat.html') && window.openLlmSettings) {
        window.openLlmSettings();
      } else {
        location.href = 'chat.html?open=settings';
      }
    });
  }

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
