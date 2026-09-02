// 主页逻辑：拉当前卡片 + 概览统计，跳转对话页

let todayCard = null;

async function loadCard() {
  const stateBox = document.getElementById('card-state');
  try {
    const [card, overview] = await Promise.all([
      apiGet('/api/card/today'),
      apiGet('/api/card/overview').catch(() => null), // 统计失败不影响卡片
    ]);
    todayCard = card;
    renderCard(card);
    if (overview) renderOverview(overview);
  } catch (e) {
    stateBox.textContent = '卡片加载失败：' + e.message + '（后端是否已启动在 4399 端口？）';
  }
}

function renderCard(card) {
  document.getElementById('card-state').style.display = 'none';
  document.getElementById('daily-card').style.display = '';

  document.getElementById('card-term').textContent = card.term;
  document.getElementById('card-category').textContent = categoryLabel(card.category);
  document.getElementById('card-brief').textContent = card.brief || '（暂无简介）';

  // 没有别名时整行隐藏，避免空白
  const aliasEl = document.getElementById('card-alias');
  if (card.alias) {
    aliasEl.style.display = '';
    aliasEl.textContent = '别名：' + card.alias;
  } else {
    aliasEl.style.display = 'none';
  }

  // 详细讲解 / 示例：有才显示
  const detailWrap = document.getElementById('card-detail-wrap');
  if (card.detail) {
    detailWrap.style.display = '';
    document.getElementById('card-detail').textContent = card.detail;
  } else {
    detailWrap.style.display = 'none';
  }
  const exampleWrap = document.getElementById('card-example-wrap');
  if (card.example) {
    exampleWrap.style.display = '';
    const pre = document.getElementById('card-example');
    if (looksLikeCode(card.example)) {
      pre.innerHTML = highlightCode(card.example);  // 代码 → 语法高亮
    } else {
      pre.textContent = card.example;               // 场景描述 → 纯文本
    }
  } else {
    exampleWrap.style.display = 'none';
  }
}

function renderOverview(o) {
  document.getElementById('stat-row').style.display = '';
  document.getElementById('stat-knowledge').textContent = o.knowledge;
  document.getElementById('stat-terms').textContent = o.terms;
  document.getElementById('stat-evals').textContent = o.evals;
  document.getElementById('stat-avg').textContent =
    o.eval_avg_score == null ? '-' : o.eval_avg_score;
}

// 换一张卡片：手动刷新（排除当前这张随机换），失败不打断当前卡片
document.getElementById('btn-swap').addEventListener('click', async () => {
  const btn = document.getElementById('btn-swap');
  btn.disabled = true;
  try {
    const card = await apiPostJson('/api/card/refresh', {});
    todayCard = card;
    renderCard(card);
  } catch (e) {
    btn.textContent = '换卡失败';
    setTimeout(() => (btn.textContent = '🔄 换一个'), 1500);
  } finally {
    btn.disabled = false;
  }
});

// 跳转对话页：讲解模式带上问题自动开问；费曼模式带上主题自动开局
document.getElementById('btn-ask').addEventListener('click', () => {
  if (!todayCard) return;
  location.href = 'chat.html?mode=ask&q=' + encodeURIComponent('帮我讲讲 ' + todayCard.term);
});
document.getElementById('btn-teach').addEventListener('click', () => {
  if (!todayCard) return;
  location.href = 'chat.html?mode=teach&topic=' + encodeURIComponent(todayCard.term);
});

loadCard();
