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

// ---------- 知识正文渲染：纯文本中启发式识别代码块 ----------
// 知识库正文是网页提取的纯文本（无 markdown 围栏），代码与文字混排。
// 思路：逐行打「代码似度」分 → 连续代码行成块 → 代码块高亮、其余走 markdown。

// 单行代码似度：3=强信号（可独立成块），2=较弱（需成组），0=散文
function kbCodeLineScore(line) {
  const t = line.trim();
  if (!t) return 0;
  // Python REPL 提示符
  if (/^(>>>|\.\.\.)(\s|$)/.test(t)) return 3;
  // shell 命令（$ 前缀或常见包管理/工具命令）
  if (/^\$\s+\S/.test(t)) return 3;
  // 注：白名单只收「后随参数也不像英文句子」的命令，避免把 "make sure..." 这类散文判成代码
  if (/^(pip3?|python3?|uv|uvicorn|gunicorn|git|curl|wget|npm|yarn|docker|kubectl|apt|apt-get|yum|poetry|pipx|pytest|flask)\s+[\w./:=@-]+/.test(t)) return 3;
  // Python 结构性行
  if (/^(async\s+def|def|class)\s+[A-Za-z_][\w.]*/.test(t)) return 3;
  if (/^(import|from)\s+[A-Za-z_][\w.]*/.test(t)) return 3;
  if (/^@[A-Za-z_][\w.]*/.test(t)) return 3;
  // 字典/列表字面量行（JSON 示例等）
  if (/^[{[]/.test(t) && /[}\]]\s*,?\s*$/.test(t)) return 2;
  // 关键字起始行
  if (/^(if|elif|else|for|while|try|except|finally|with|return|raise|yield|assert|pass|break|continue|global|nonlocal|del|lambda)\b/.test(t)) return 2;
  // 赋值：identifier = value（排除 URL 行）
  if (/^[A-Za-z_][\w.\[\]'"-]*\s*[+\-*/%&|^]?=\s*\S/.test(t) && !/^https?:\/\//.test(t)) return 2;
  // 行尾花括号/分号（C/JS/Java 风格）
  if (/[{\[]\s*$/.test(t) || /^\s*[}\])][;,]?\s*$/.test(t)) return 2;
  return 0;
}

// 零分但形似上一行的延续（多行调用/参数列表的后半段）
function kbIsContinuation(prevLine, cur) {
  if (!prevLine || !cur) return false;
  const p = prevLine.trim();
  if (/[(\[,\\]$/.test(p) || /,\s*$/.test(p)) {
    return /^[+\-*/%)]/.test(cur) || /^[\w).}'"\]]/.test(cur);
  }
  return /^[).,\]}]/.test(cur);
}

// 把纯文本切成 {code, text} 段
function kbSplitBlocks(text) {
  const lines = String(text).split('\n');
  const n = lines.length;
  const scores = lines.map(kbCodeLineScore);
  const isCode = new Array(n).fill(false);

  let i = 0;
  while (i < n) {
    const s = scores[i];
    // 块起点：强信号行；或较弱行且下一非空行也有代码似度
    let startable = s >= 3;
    if (!startable && s === 2) {
      let k = i + 1;
      while (k < n && !lines[k].trim()) k++;
      startable = k < n && (scores[k] >= 2 || kbIsContinuation(lines[i], lines[k].trim()));
    }
    if (!startable) { i++; continue; }

    // 向后扩展块边界
    let j = i + 1;
    while (j < n) {
      const t = lines[j].trim();
      if (scores[j] >= 2) { j++; continue; }
      if (!t) {
        // 空行：后面两行内还有代码行才算块内空行，否则块结束
        let k = j + 1;
        while (k < n && !lines[k].trim()) k++;
        if (k < n && scores[k] >= 2) { j = k; continue; }
        break;
      }
      if (kbIsContinuation(lines[j - 1], t)) { j++; continue; }
      break;
    }
    // 去掉块尾空行
    while (j > i && !lines[j - 1].trim()) j--;

    // 块有效性：弱起点需 ≥2 个非空行，且至少一行带代码符号（防把「return 语句」这类散文误判）
    const blockLines = lines.slice(i, j).filter(l => l.trim());
    const hasGlyph = blockLines.some(l => /[=(){}[\]:;]|^\s*[@$]/.test(l.trim()));
    const valid = s >= 3 || (blockLines.length >= 2 && hasGlyph);
    if (valid) {
      for (let k = i; k < j; k++) isCode[k] = true;
    }
    i = valid ? j : i + 1;
  }

  // 按 isCode 归段
  const segs = [];
  let buf = [], inCode = false;
  const flush = () => {
    if (buf.length) segs.push({ code: inCode, text: buf.join('\n') });
    buf = [];
  };
  for (let k = 0; k < n; k++) {
    if (isCode[k] !== inCode) { flush(); inCode = isCode[k]; }
    buf.push(lines[k]);
  }
  flush();
  return segs;
}

// 知识详情正文渲染：带围栏的直接走 markdown；纯文本先做代码块识别
function renderKnowledge(text) {
  const src = String(text);
  if (src.includes('```')) return renderRich(src);
  return kbSplitBlocks(src)
    .map(seg => seg.code
      ? '<pre class="code-block"><code>' + highlightCode(seg.text.replace(/^\n+|\n+$/g, '')) + '</code></pre>'
      : mdBlocks(seg.text))
    .join('');
}
