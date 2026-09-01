// 公共顶栏用户区：登录门禁 + 注入「邮箱 + 退出」
// 约定：所有业务页面在 api.js 之后引入本脚本（依赖 getToken / apiGet）。

(function () {
  // 未登录 → 直接去登录页（带回访地址）
  if (!getToken()) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.replace('login.html?next=' + next);
    return;
  }

  // 注入用户信息；顺带探活：token 失效时 apiGet 会触发 401 跳转
  apiGet('/api/auth/me')
    .then((me) => {
      const inner = document.querySelector('.topbar-inner');
      if (!inner) return;
      const box = document.createElement('div');
      box.className = 'topbar-user';

      const mail = document.createElement('span');
      mail.className = 'tu-email';
      mail.title = '当前登录账号';
      mail.textContent = '📧 ' + me.email;

      const out = document.createElement('button');
      out.className = 'tu-logout';
      out.textContent = '退出';
      out.addEventListener('click', logout);

      box.appendChild(mail);
      box.appendChild(out);
      inner.appendChild(box);
    })
    .catch(() => { /* 401 已由 apiGet 跳转；其他网络错误不打扰 */ });

  function logout() {
    // 清登录态与会话本地状态，回登录页
    ['rag_token', 'rag_cur_ask', 'rag_cur_teach', 'rag_current_session', 'rag_mode']
      .forEach((k) => localStorage.removeItem(k));
    location.replace('login.html');
  }
})();
