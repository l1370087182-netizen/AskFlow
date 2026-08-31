// 轻量代码高亮器（零依赖，面向 Python 风格代码）
// 思路：一条组合正则按「注释/字符串/关键字/数字/函数名/装饰器」顺序匹配，
// 逐段 HTML 转义后包上 token span，未匹配部分原样转义输出。

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const HL_KEYWORDS =
  'def|class|import|from|return|if|elif|else|for|while|in|not|and|or|with|as|' +
  'try|except|finally|raise|yield|lambda|pass|break|continue|global|nonlocal|' +
  'assert|del|async|await|True|False|None|self|print';

const HL_REGEX = new RegExp(
  '(#[^\\n]*)' +                                                  // 1 注释
  '|("(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\')' +     // 2 字符串
  '|(\\b(?:' + HL_KEYWORDS + ')\\b)' +                            // 3 关键字
  '|(\\b\\d+(?:\\.\\d+)?\\b)' +                                   // 4 数字
  '|(\\b[A-Za-z_][\\w]*(?=\\s*\\())' +                            // 5 函数调用名
  '|(@[\\w.]+)',                                                  // 6 装饰器
  'g'
);

// 判断一段示例是否像代码（否则按纯文本渲染，不做高亮）
function looksLikeCode(s) {
  return /\b(import|from|def|class|return|await|async|print)\b/.test(s)
    || /[=(]/.test(s) && /\n/.test(s);
}

// 返回高亮后的 HTML 字符串
function highlightCode(src) {
  let out = '';
  let last = 0;
  let m;
  HL_REGEX.lastIndex = 0;
  while ((m = HL_REGEX.exec(src))) {
    out += escHtml(src.slice(last, m.index));
    const cls = m[1] ? 'cmt' : m[2] ? 'str' : m[3] ? 'kw' : m[4] ? 'num' : m[5] ? 'fn' : 'dec';
    out += '<span class="tok-' + cls + '">' + escHtml(m[0]) + '</span>';
    last = m.index + m[0].length;
  }
  out += escHtml(src.slice(last));
  return out;
}

// ---------- 富文本渲染：markdown 段落 + ``` 代码块高亮 ----------

// markdown 轻量解析：标题/有序无序列表/加粗/行内代码/表格/段落
function mdBlocks(md) {
  const inline = s => s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, (m, c) => '<code class="inline-code">' + escHtml(c) + '</code>');
  let html = '', inUl = false, inOl = false, para = [];
  let tableRows = null; // 表格行缓冲：二维数组

  const flushPara = () => {
    if (para.length) { html += '<p>' + inline(escHtml(para.join(' '))) + '</p>'; para = []; }
  };
  const closeLists = () => {
    if (inUl) { html += '</ul>'; inUl = false; }
    if (inOl) { html += '</ol>'; inOl = false; }
  };
  const flushTable = () => {
    if (!tableRows) return;
    const rows = tableRows;
    tableRows = null;
    if (!rows.length) return;
    const cell = (c, tag) => `<${tag}>` + inline(escHtml(c)) + `</${tag}>`;
    let t = '<div class="table-wrap"><table>';
    t += '<thead><tr>' + rows[0].map(c => cell(c, 'th')).join('') + '</tr></thead>';
    if (rows.length > 1) {
      t += '<tbody>' + rows.slice(1)
        .map(r => '<tr>' + r.map(c => cell(c, 'td')).join('') + '</tr>').join('')
        + '</tbody>';
    }
    t += '</table></div>';
    html += t;
  };

  const isTableRow = l => l.trim().startsWith('|');
  const isTableSep = l => /^[\s|:-]+$/.test(l) && l.includes('-'); // |---|---| 分隔行
  const parseCells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(s => s.trim());

  for (const raw of md.split('\n')) {
    const line = raw.trimEnd();
    if (!line.trim()) { flushPara(); closeLists(); flushTable(); continue; }

    // 表格行
    if (isTableRow(line)) {
      if (isTableSep(line)) continue;          // 分隔行跳过（首行自然当表头）
      flushPara(); closeLists();
      if (!tableRows) tableRows = [];
      tableRows.push(parseCells(line));
      continue;
    }
    flushTable();

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); closeLists();
      // # / ## → h2，### → h3，#### → h4（模型回答常用 ###，落在 h3 更合适）
      const lv = h[1].length <= 2 ? 2 : Math.min(h[1].length, 4);
      html += `<h${lv}>` + inline(escHtml(h[2])) + `</h${lv}>`;
      continue;
    }
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (ul) {
      flushPara();
      if (inOl) { html += '</ol>'; inOl = false; }
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += '<li>' + inline(escHtml(ul[1])) + '</li>';
      continue;
    }
    const ol = line.match(/^\s*\d+[.、)]\s*(.*)$/);
    if (ol) {
      flushPara();
      if (inUl) { html += '</ul>'; inUl = false; }
      if (!inOl) { html += '<ol>'; inOl = true; }
      html += '<li>' + inline(escHtml(ol[1])) + '</li>';
      continue;
    }
    para.push(line.trim());
  }
  flushPara(); closeLists(); flushTable();
  return html;
}

// 整段回答渲染：``` 包裹的部分做代码高亮，其余走 markdown
function renderRich(text) {
  const parts = String(text).split('```');
  let html = '';
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 0) {
      html += mdBlocks(parts[i]);
    } else {
      let code = parts[i];
      // 去掉首行语言标识（如 python / bash）
      const nl = code.indexOf('\n');
      if (nl > -1 && /^[a-zA-Z0-9_+-]{0,15}$/.test(code.slice(0, nl))) {
        code = code.slice(nl + 1);
      }
      html += '<pre class="code-block"><code>' + highlightCode(code.replace(/\n$/, '')) + '</code></pre>';
    }
  }
  return html;
}
