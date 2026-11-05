// 对话里的聊天：你说的一句话，加上助手的回答（像一般的大模型助手那样，可以问候、答疑、讲题）。
// 回答是 Markdown 加 LaTeX 公式：这里只排常用的几种（段落、列表、加粗、标题、代码、引用、独立公式），
// 全部用 DOM 节点拼出来，不用 innerHTML，模型写出的尖括号不会被当成网页标签。公式交给 KaTeX。

import { AVATAR, el, renderMath, spin, svg, toast, typesetTopic } from './shared.js';

// 你那句话上面的附件：图片显示缩略图，PDF、文本文件显示图标、名字和页数
export function attachmentStrip(list) {
  const strip = el('div', 'attach-strip');
  for (const item of list || []) {
    const card = el('span', `attach-card ${item.kind || ''}`);
    card.title = item.name || '';
    if (item.thumb) {
      const img = el('img');
      img.src = item.thumb;
      img.alt = item.name || '';
      card.append(img);
    } else {
      card.insertAdjacentHTML('afterbegin', svg('file'));
    }
    const words = el('span', 'attach-words');
    words.append(el('span', 'attach-name', item.name || '文件'));
    if (item.kind === 'pdf') words.append(el('small', '', `PDF，${item.pages || 1} 页`));
    else if (item.kind === 'text') words.append(el('small', '', '文本'));
    if (!item.thumb || item.kind !== 'image') card.append(words);
    strip.append(card);
  }
  return strip;
}

// 公式：$$…$$、\[…\]、\(…\)、$…$（里面的 Markdown 记号不处理）
const MATH = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$(?:\\.|[^$\\\n])+?\$)/;

function withCode(text, parent) {
  const parts = text.split(/(`[^`\n]+`)/);
  parts.forEach((part, k) => {
    if (!part) return;
    if (k % 2) parent.append(el('code', '', part.slice(1, -1)));
    else parent.append(document.createTextNode(part));
  });
}

// 一行文字：公式原样保留；公式外面的 **加粗**（可以把公式包在里面）和 `代码`
function inline(text, parent) {
  let target = parent;
  text.split(MATH).forEach((part, k) => {
    if (!part) return;
    if (k % 2) {
      target.append(document.createTextNode(part));
      return;
    }
    part.split('**').forEach((piece, j) => {
      if (j > 0) {
        if (target === parent) {
          target = el('strong');
          parent.append(target);
        } else {
          target = parent;
        }
      }
      if (piece) withCode(piece, target);
    });
  });
}

export function renderMarkdown(source) {
  const root = document.createDocumentFragment();
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (!paragraph.length) return;
    const p = el('p');
    paragraph.forEach((line, k) => {
      if (k) p.append(el('br'));
      inline(line, p);
    });
    root.append(p);
    paragraph = [];
  };
  const block = (node) => {
    flush();
    list = null;
    root.append(node);
  };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const text = line.trim();
    if (/^```/.test(text)) {
      const code = [];
      for (i += 1; i < lines.length && !/^```/.test(lines[i].trim()); i += 1) code.push(lines[i]);
      const pre = el('pre');
      pre.append(el('code', '', code.join('\n')));
      block(pre);
      continue;
    }
    // 单独成行的公式：$$ 开头，可能跨好几行，到收尾的 $$ 为止
    if (/^\$\$/.test(text)) {
      const math = [line];
      if (!(text.length > 4 && /\$\$$/.test(text))) {
        for (i += 1; i < lines.length; i += 1) {
          math.push(lines[i]);
          if (/\$\$\s*$/.test(lines[i])) break;
        }
      }
      block(el('div', 'md-math', math.join('\n').trim()));
      continue;
    }
    if (!text) {
      flush();
      list = null;
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(text);
    if (heading) {
      const node = el(heading[1].length <= 2 ? 'h3' : 'h4', 'md-h');
      inline(heading[2], node);
      block(node);
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(text)) {
      block(el('hr'));
      continue;
    }
    const quote = /^>\s?(.*)$/.exec(text);
    if (quote) {
      const node = el('blockquote');
      inline(quote[1], node);
      block(node);
      continue;
    }
    const bullet = /^[-*•]\s+(.*)$/.exec(text);
    const ordered = /^(\d+)(?:[.)]\s+|、\s*)(.*)$/.exec(text);
    if (bullet || ordered) {
      flush();
      const tag = bullet ? 'UL' : 'OL';
      if (!list || list.tagName !== tag) {
        list = el(tag.toLowerCase());
        if (ordered && ordered[1] !== '1') list.start = Number(ordered[1]);
        root.append(list);
      }
      const item = el('li');
      inline(bullet ? bullet[1] : ordered[2], item);
      list.append(item);
      continue;
    }
    // 列表项下面缩进的一行：接在这一项里
    if (list && /^\s{2,}/.test(line)) {
      const item = list.lastElementChild;
      item.append(el('br'));
      inline(text, item);
      continue;
    }
    list = null;
    paragraph.push(text);
  }
  flush();
  return root;
}

// 上下文压缩的分界线：上面的内容已经压缩成摘要，之后助手带的是摘要 + 下面各轮的原文；点开能看摘要
export class SummaryTurn {
  constructor(record) {
    this.isSummary = true;
    this.record = record;
    this.root = el('article', 'turn summary-turn');
    const box = el('details', 'summary-box');
    const head = el('summary');
    const before = Number(record.tokens_before) || 0;
    const after = Number(record.tokens_after) || 0;
    head.append(
      el('span', 'summary-line'),
      el('span', 'summary-label', `以上 ${record.covers_count || ''} 轮已压缩成摘要${before ? `（约 ${kilo(before)} → ${kilo(after)} Token）` : ''}`),
      el('span', 'summary-line'),
    );
    const body = el('div', 'chat-body summary-body');
    body.append(renderMarkdown(record.text || ''));
    box.append(head, body);
    box.addEventListener('toggle', () => { if (box.open) renderMath(body); }, { once: true });
    this.root.append(box);
  }

  setFocused() {}
}

export const kilo = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n)));

export class ChatTurn {
  // actions：retry(turn)、asQuestion(turn)（没回答成时：再试一次，或者直接按出题处理）
  constructor(actions, text, attachments = []) {
    this.actions = actions;
    this.isChat = true;
    this.text = text;
    this.attachments = attachments; // 附件（发消息时的编号和缩略图；重新打开对话时只有缩略图）
    this.message = null;
    this.root = el('article', 'turn chat-turn');
    this.task = el('div', 'task-card');
    if (attachments.length) this.task.append(attachmentStrip(attachments));
    // 你自己写的话：不翻译（界面切成英文时也保持原样）
    if (text) this.task.append(Object.assign(el('p', 'ask', typesetTopic(text)), { translate: false }));
    else if (!attachments.length) this.task.append(el('p', 'ask', ''));
    renderMath(this.task);
    const reply = el('section', 'reply');
    reply.setAttribute('aria-live', 'polite');
    this.avatar = el('span', 'reply-avatar');
    this.avatar.setAttribute('aria-hidden', 'true');
    this.avatar.innerHTML = AVATAR;
    const main = el('div', 'reply-main');
    this.body = el('div', 'chat-body');
    this.body.translate = false; // 模型的回答：选了英文时模型直接用英文写，界面不再翻译
    this.tools = el('div', 'chat-tools');
    main.append(this.body, this.tools);
    reply.append(this.avatar, main);
    this.root.append(this.task, reply);
  }

  setFocused() {}

  // 等回答：头像转起来，显示"正在想"（前文太长要先压缩时说一声）；等得久了说一声在仔细推理
  wait(first = '正在想') {
    spin(this.avatar.firstElementChild, true);
    const line = el('p', 'chat-wait');
    const words = el('span', '', first);
    line.append(words, el('i'), el('i'), el('i'));
    this.body.replaceChildren(line);
    this.tools.replaceChildren();
    clearTimeout(this.slow);
    this.slow = setTimeout(() => { words.textContent = '在仔细推理，稍等一会儿'; }, 6000);
  }

  show(message) {
    clearTimeout(this.slow);
    this.message = message;
    spin(this.avatar.firstElementChild, false);
    this.body.classList.remove('bad');
    this.body.replaceChildren(renderMarkdown(message.reply));
    renderMath(this.body);
    const copy = el('button', 'chat-copy', '复制');
    copy.type = 'button';
    copy.title = '复制这条回答（公式保留 LaTeX 写法）';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(message.reply);
        toast('已复制回答');
      } catch {
        toast('复制没成功，可以手动选中文字复制');
      }
    });
    this.tools.replaceChildren(copy);
  }

  fail(error) {
    clearTimeout(this.slow);
    spin(this.avatar.firstElementChild, false);
    this.body.classList.add('bad');
    this.body.replaceChildren(el('p', '', `没回答成：${error}`));
    const retry = el('button', 'followup', '再试一次');
    retry.type = 'button';
    retry.addEventListener('click', () => this.actions.retry(this));
    const ask = el('button', 'followup', '按出题要求处理');
    ask.type = 'button';
    ask.title = '把这句话当作出题要求，按输入框下方的设置出题';
    ask.addEventListener('click', () => this.actions.asQuestion(this));
    const row = el('div', 'followups');
    row.append(retry, ask);
    this.tools.replaceChildren(row);
  }
}
