// 管理后台：注册用户总览（邮箱 / 注册时间 / 最近登录时间）
// 仅管理员 token（role=admin）可访问；401/403 一律回登录页。

if (!getToken() || localStorage.getItem('rag_role') !== 'admin') {
  location.replace('login.html');
}

function fmtTime(s) {
  if (!s) return '—';
  return s.replace('T', ' ').slice(0, 16);
}

async function loadUsers() {
  const stateBox = document.getElementById('admin-state');
  try {
    const data = await apiGet('/api/auth/admin/users');
    document.getElementById('admin-sub').textContent =
      `共 ${data.total} 位注册用户`;
    if (!data.users.length) {
      stateBox.style.display = '';
      stateBox.textContent = '还没有注册用户';
      return;
    }
    const tbody = document.getElementById('admin-rows');
    tbody.innerHTML = '';
    for (const u of data.users) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="cell-dim">${u.id}</td>
        <td class="cell-email"></td>
        <td class="cell-dim">${fmtTime(u.created_at)}</td>
        <td class="cell-dim">${fmtTime(u.last_login_at)}</td>`;
      tr.querySelector('.cell-email').textContent = u.email;
      tbody.appendChild(tr);
    }
    document.getElementById('admin-table').style.display = '';
  } catch (e) {
    // 401/403 由 apiGet 统一跳登录；其余错误就地提示
    stateBox.style.display = '';
    stateBox.textContent = '加载失败：' + e.message;
  }
}

document.getElementById('admin-logout').addEventListener('click', () => {
  localStorage.removeItem('rag_token');
  localStorage.removeItem('rag_role');
  location.replace('login.html');
});

loadUsers();
