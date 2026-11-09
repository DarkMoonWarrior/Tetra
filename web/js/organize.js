// 对话里整理题库的一轮：你说的话（"把导数的题都放进导数专题"），助手的回答，做了哪些改动（新建、改名、挪动文件夹，
// 把几道题放进哪个文件夹），以及"在题库里查看""撤销"。整理是当场做完的，撤销会把这次的改动全部恢复原样。

import { AVATAR, el, renderMath, request, toast, typesetTopic } from './shared.js';
import { renderMarkdown } from './chat.js';

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

// 一条改动 → 一句话（和后端 organize.describe 说法一致）
export function describeChange(change) {
  const into = (name) => (name ? `「${name}」` : '未归类');
  switch (change.kind) {
    case 'create':
      return change.parent ? `新建文件夹「${change.name}」（在「${change.parent}」里）` : `新建文件夹「${change.name}」`;
    case 'rename':
      return `文件夹「${change.old}」改名为「${change.name}」`;
    case 'move_folder':
      return change.to ? `文件夹「${change.name}」挪到「${change.to}」里` : `文件夹「${change.name}」挪到最外层`;
    case 'delete':
      return `删掉文件夹「${change.name}」，里面的 ${change.count || 0} 道题挪到${into(change.to)}`;
    case 'move_items':
      return change.to ? `${change.count} 道题放进「${change.to}」` : `${change.count} 道题移出文件夹，放回未归类`;
    default:
      return '';
  }
}

export class OrganizeTurn {
  // actions：openFolder(turn, folderId)、undone(turn, data)
  constructor(actions, record) {
    this.actions = actions;
    this.isChat = true; // 和聊天一样不占右栏
    this.record = record;
    this.root = el('article', 'turn chat-turn organize-turn');
    this.task = el('div', 'task-card');
    this.task.append(Object.assign(el('p', 'ask', typesetTopic(record.text || '')), { translate: false }));
    renderMath(this.task);
    const reply = el('section', 'reply');
    const avatar = el('span', 'reply-avatar');
    avatar.setAttribute('aria-hidden', 'true');
    avatar.innerHTML = AVATAR;
    this.main = el('div', 'reply-main');
    reply.append(avatar, this.main);
    this.root.append(this.task, reply);
    this.render();
  }

  setFocused() {}

  render() {
    const record = this.record;
    const changes = record.changes || [];
    const undone = record.status === 'undone';
    const head = el('header', 'reply-head');
    head.append(el('h2', 'reply-title', undone ? '这次整理已撤销' : changes.length ? '题库整理好了' : '这次没有改动题库'));
    const body = el('div', 'chat-body');
    body.translate = false; // 模型的回答：选了英文时模型直接用英文写
    body.append(renderMarkdown(record.reply || ''));
    const nodes = [head, body];
    if (changes.length) {
      const list = el('ul', `organize-changes${undone ? ' undone' : ''}`);
      for (const change of changes) list.append(el('li', '', describeChange(change)));
      nodes.push(list);
    }
    for (const problem of record.problems || []) nodes.push(el('p', 'import-q-warn', problem));
    const row = el('div', 'followups');
    if (!undone) {
      row.append(button('在题库里查看', 'followup', () => this.actions.openFolder(this, record.focus || null)));
      if (changes.length) this.undoBtn = row.appendChild(button('撤销', 'followup', () => this.undo()));
    }
    if (row.childElementCount) nodes.push(row);
    this.main.replaceChildren(...nodes);
    renderMath(this.main);
  }

  async undo() {
    if (!this.record?.id || this.undoBtn?.disabled) return;
    this.undoBtn.disabled = true;
    try {
      const data = await request(`/api/messages/${encodeURIComponent(this.record.id)}/undo`, { method: 'POST' });
      this.record = data.message;
      this.render();
      this.actions.undone(this, data);
    } catch (error) {
      toast(error.message);
      this.undoBtn.disabled = false;
    }
  }
}
