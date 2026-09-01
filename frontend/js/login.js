// 登录页逻辑：登录 / 注册（邮箱验证码）/ 忘记密码 三合一

const msg = document.getElementById('auth-msg');
const tabs = document.querySelectorAll('.auth-tab');
const forms = {
  login: document.getElementById('form-login'),
  register: document.getElementById('form-register'),
  reset: document.getElementById('form-reset'),
};

// 已登录则直接进首页
if (getToken()) location.replace('index.html');

function showMsg(text, ok = false) {
  msg.textContent = text || '';
  msg.className = 'auth-msg ' + (text ? (ok ? 'ok' : 'err') : '');
}

function switchTab(name) {
  tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  for (const [k, f] of Object.entries(forms)) f.style.display = k === name ? '' : 'none';
  showMsg('');
}
tabs.forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));

// 登录成功后回跳：优先 URL 的 next 参数
function redirectAfterAuth() {
  const params = new URLSearchParams(location.search);
  const next = params.get('next');
  location.replace(next && next.startsWith('/') ? '.' + next : 'index.html');
}

function storeToken(data) {
  localStorage.setItem('rag_token', data.token);
}

// ---------- 验证码倒计时（两个发送按钮共用逻辑）----------
function startCountdown(btn) {
  let sec = 60;
  btn.disabled = true;
  btn.textContent = `${sec}s 后重发`;
  const timer = setInterval(() => {
    sec -= 1;
    if (sec <= 0) {
      clearInterval(timer);
      btn.disabled = false;
      btn.textContent = '发送验证码';
    } else {
      btn.textContent = `${sec}s 后重发`;
    }
  }, 1000);
}

async function sendCode(emailInput, btn, purpose) {
  const email = emailInput.value.trim();
  if (!email) { showMsg('请先填写邮箱'); return; }
  showMsg('');
  btn.disabled = true;
  try {
    const r = await apiPostJson('/api/auth/send-code', { email, purpose });
    showMsg(r.message || '验证码已发送', true);
    startCountdown(btn);
  } catch (e) {
    showMsg(e.message);
    btn.disabled = false;
  }
}

document.getElementById('rg-send').addEventListener('click', (e) =>
  sendCode(document.getElementById('rg-email'), e.currentTarget, 'register'));
document.getElementById('rs-send').addEventListener('click', (e) =>
  sendCode(document.getElementById('rs-email'), e.currentTarget, 'reset'));

// ---------- 登录 ----------
forms.login.addEventListener('submit', async (e) => {
  e.preventDefault();
  showMsg('');
  try {
    const data = await apiPostJson('/api/auth/login', {
      email: document.getElementById('li-email').value.trim(),
      password: document.getElementById('li-password').value,
    });
    storeToken(data);
    redirectAfterAuth();
  } catch (err) {
    showMsg(err.message);
  }
});

// ---------- 注册 ----------
forms.register.addEventListener('submit', async (e) => {
  e.preventDefault();
  const pw = document.getElementById('rg-password').value;
  const pw2 = document.getElementById('rg-password2').value;
  if (pw.length < 6) { showMsg('密码至少 6 位'); return; }
  if (pw !== pw2) { showMsg('两次输入的密码不一致'); return; }
  showMsg('');
  try {
    const data = await apiPostJson('/api/auth/register', {
      email: document.getElementById('rg-email').value.trim(),
      code: document.getElementById('rg-code').value.trim(),
      password: pw,
    });
    storeToken(data);
    redirectAfterAuth();
  } catch (err) {
    showMsg(err.message);
  }
});

// ---------- 忘记密码 ----------
forms.reset.addEventListener('submit', async (e) => {
  e.preventDefault();
  showMsg('');
  try {
    const r = await apiPostJson('/api/auth/reset', {
      email: document.getElementById('rs-email').value.trim(),
      code: document.getElementById('rs-code').value.trim(),
      new_password: document.getElementById('rs-password').value,
    });
    showMsg(r.message + '，正在跳转登录…', true);
    setTimeout(() => switchTab('login'), 1200);
  } catch (err) {
    showMsg(err.message);
  }
});
