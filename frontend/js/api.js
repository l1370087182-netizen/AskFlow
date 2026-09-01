// API 封装：后端基址 + 通用请求/SSE 流读取 + 登录态（Bearer token）
// 本地开发：前端 10001、后端独立 4399，用当前访问的 host 拼基址，
// 本机（127.0.0.1）和局域网（192.168.x.x）访问都能通。
// 部署环境：Nginx 同源反代 /api 到后端，基址留空走同源。

const API_BASE = location.port === '10001' ? `http://${location.hostname}:4399` : '';

// ---------- 登录态 ----------

function getToken() {
  return localStorage.getItem('rag_token') || '';
}

// 所有请求统一携带的鉴权头（FormData 请求勿手工设 Content-Type）
function authHeaders() {
  const t = getToken();
  return t ? { Authorization: 'Bearer ' + t } : {};
}

// 401 → 登录失效：清 token 并跳登录页（登录页本身不跳，防死循环）
function handleAuthError(resp) {
  if (resp.status === 401 && !location.pathname.endsWith('login.html')) {
    localStorage.removeItem('rag_token');
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = 'login.html?next=' + next;
    return true;
  }
  return false;
}

async function _errDetail(resp) {
  let detail = resp.statusText;
  try { detail = (await resp.json()).detail || detail; } catch (e) {}
  return detail;
}

async function apiGet(path) {
  const resp = await fetch(API_BASE + path, { headers: authHeaders() });
  if (!resp.ok) {
    handleAuthError(resp);
    throw new Error(await _errDetail(resp));
  }
  return resp.json();
}

async function apiPostJson(path, body) {
  const resp = await fetch(API_BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    handleAuthError(resp);
    throw new Error(await _errDetail(resp));
  }
  return resp.json();
}

async function apiPutJson(path, body) {
  const resp = await fetch(API_BASE + path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    handleAuthError(resp);
    throw new Error(await _errDetail(resp));
  }
  return resp.json();
}

async function apiDelete(path) {
  const resp = await fetch(API_BASE + path, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  if (!resp.ok) {
    handleAuthError(resp);
    throw new Error(await _errDetail(resp));
  }
  return resp.json();
}

/**
 * POST /api/chat 的 SSE 流式读取。
 * EventSource 只支持 GET，这里用 fetch + ReadableStream 手动解析 `data: {...}` 行。
 * @param {object} payload ChatRequest
 * @param {(event: object) => void} onEvent 每收到一个事件回调（{type, ...}）
 */
async function streamChat(payload, onEvent) {
  const resp = await fetch(API_BASE + '/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    handleAuthError(resp);
    throw new Error(await _errDetail(resp));
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // 事件之间以空行（\n\n）分隔
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue;
        try {
          onEvent(JSON.parse(line.slice(5).trim()));
        } catch (e) { /* 半个事件或脏行，跳过 */ }
      }
    }
  }
}

// 分类英文 → 中文展示名（数据层存的是英文分类标识）
const CATEGORY_ZH = {
  fastapi: 'FastAPI 框架',
  python: 'Python 语言',
  ai: '人工智能',
  general: '通用知识',
};
function categoryLabel(cat) {
  return CATEGORY_ZH[cat] || (cat ? cat[0].toUpperCase() + cat.slice(1) : '未分类');
}
