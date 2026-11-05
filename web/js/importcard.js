// 聊天里把附件中的题存进题库：先识别题目，原题没有解析的由助手补写，做成一张核对卡片；
// 你勾选、点"存入题库"之后才存。正在做时读 /api/imports/{id}；做完（或重新打开对话）时按存进对话的记录显示。

import { AVATAR, DIFFICULTY_LABEL, TYPE_LABEL, el, mathText, renderMath, request, spin, toast } from './shared.js';
import { attachmentStrip } from './chat.js';

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

// 正在识别时的快照（/api/imports/{id}）→ 和存下来的记录一样的形状
export function importRecord(snap) {
  const questions = snap.questions || [];
  return {
    id: snap.message_id,
    kind: 'import',
    text: snap.text || '',
    attachments: snap.attachments || [],
    import_id: snap.id,
    status: questions.length ? 'review' : snap.status === 'cancelled' ? 'cancelled' : 'empty',
    questions,
    errors: snap.errors || [],
    pages: snap.pages,
    saved: 0,
    saved_indexes: [],
  };
}

export class ImportTurn {
  // actions：stop(turn)、openBank(turn)、saved(turn, data)
  constructor(actions) {
    this.actions = actions;
    this.isImport = true;
    this.jobId = null; // 正在识别时是导入任务的编号
    this.snapshot = null; // 正在识别：/api/imports/{id} 的快照
    this.record = null; // 做完以后：存在对话里的核对卡片
    this.picked = new Set();
    this.drawnKey = '';

    this.root = el('article', 'turn import-turn');
    this.task = el('div', 'task-card');
    const reply = el('section', 'reply');
    reply.setAttribute('aria-live', 'polite');
    this.avatar = el('span', 'reply-avatar');
    this.avatar.setAttribute('aria-hidden', 'true');
    this.avatar.innerHTML = AVATAR;
    const main = el('div', 'reply-main');
    const head = el('header', 'reply-head');
    this.title = el('h2', 'reply-title');
    const tools = el('div', 'reply-tools');
    this.stopBtn = button('停止', 'stop-btn', () => actions.stop(this));
    tools.append(this.stopBtn);
    head.append(this.title, tools);
    this.note = el('p', 'run-note');
    this.bar = el('div', 'batch-bar');
    this.fill = el('i');
    this.bar.append(this.fill);
    this.list = el('ol', 'import-list chat-import-list');
    this.foot = el('div', 'followups');
    main.append(head, this.note, this.bar, this.list, this.foot);
    reply.append(this.avatar, main);
    this.root.append(this.task, reply);
  }

  get running() {
    return Boolean(this.snapshot) && !this.record && this.snapshot.status === 'running';
  }

  setFocused() {}

  tick() {}

  load(record) {
    this.record = record;
    this.snapshot = null;
    this.picked = new Set(record.status === 'saved' ? record.saved_indexes || [] : (record.questions || []).map((q) => q.index));
    this.drawnKey = '';
  }

  renderTask(source) {
    if (this.task.childElementCount) return;
    if (source.attachments?.length) this.task.append(attachmentStrip(source.attachments));
    this.task.append(Object.assign(el('p', 'ask', source.text || '把附件里的题存进题库'), { translate: !source.text }));
  }

  render({ live = false } = {}) {
    // 快照显示已经做完、还没换成核对记录：先按快照换成记录（和存进对话的那张卡片一样）
    if (!this.record && this.snapshot && this.snapshot.status !== 'running') this.load(importRecord(this.snapshot));
    const source = this.record || this.snapshot;
    if (!source) return;
    this.renderTask(source);
    const running = this.running;
    spin(this.avatar.firstElementChild, running);
    this.stopBtn.hidden = !(live && running);
    this.bar.hidden = !running;
    if (running) {
      this.renderProgress(this.snapshot);
      return;
    }
    this.renderRecord(this.record);
  }

  renderProgress(snap) {
    const solving = snap.stage === 'solving';
    this.title.textContent = solving ? '正在补写解析' : '正在识别题目';
    this.note.textContent = solving
      ? `识别出 ${snap.questions.length} 道题，其中 ${snap.solving} 道原题没有解析，正在请模型解出来、按标准答案的格式写好（${snap.solved} / ${snap.solving}）。写好以后先给你核对，确认了才存进题库。`
      : `正在看第 ${snap.pages_done} / ${snap.pages} 页，已经找到 ${snap.questions.length} 道题。识别完以后，没有解析的题会补写解析，再给你核对。`;
    const done = solving ? snap.solved / Math.max(1, snap.solving) : snap.pages_done / Math.max(1, snap.pages);
    this.fill.style.width = `${Math.round(done * 100)}%`;
    this.list.replaceChildren();
    this.foot.replaceChildren();
  }

  renderRecord(record) {
    const questions = record.questions || [];
    const generated = questions.filter((q) => q.generated_solution).length;
    const status = record.status;
    this.title.textContent = {
      review: `识别出 ${questions.length} 道题，请核对`,
      saved: `已存入题库 ${record.saved} 道`,
      declined: '没有存进题库',
      empty: '没有识别出题目',
      cancelled: '已停下',
    }[status] || '识别完了';
    this.note.textContent = status === 'review'
      ? `核对题目、答案和解析，勾选要存的，再点"存入题库"。${generated ? `其中 ${generated} 道原题没有解析，是助手补写的（标着"助手补写"），请特别看一下。` : '题目都带着原来的解析。'}`
      : status === 'empty' ? '换一张更清楚的图片、PDF，或者换一个能看图的模型再试。' : '';
    const key = `${status}|${record.saved}|${questions.length}`;
    if (this.drawnKey !== key) {
      this.drawnKey = key;
      this.list.replaceChildren(...questions.map((q) => this.card(q, status === 'review')));
      for (const error of record.errors || []) this.list.append(el('li', 'import-q-warn', `没识别成：${error}`));
      renderMath(this.list);
    }
    this.renderFoot(record);
  }

  card(q, editable) {
    const card = el('li', 'import-q');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = this.picked.has(q.index);
    box.disabled = !editable;
    box.setAttribute('aria-label', '存进题库');
    box.addEventListener('change', () => {
      if (box.checked) this.picked.add(q.index);
      else this.picked.delete(q.index);
      this.renderFoot(this.record);
    });
    const main = el('div', 'import-q-main');
    const meta = el('div', 'import-q-meta');
    meta.append(
      el('b', '', `${q.number ? `${q.number}．` : ''}${TYPE_LABEL[q.question_type] || ''}`),
      el('span', '', q.knowledge ? q.knowledge.split(' › ').pop() : '未对上考点'),
      el('span', '', DIFFICULTY_LABEL[q.difficulty] || ''),
    );
    if (q.generated_solution) meta.append(el('span', 'made-by', '助手补写'));
    const content = el('div', 'import-q-content');
    content.translate = false; // 题目和解析是内容，不翻译
    content.append(mathText('div', 'import-q-stem', q.stem));
    if (q.figure_svg) {
      const figure = el('div', 'import-q-figure');
      figure.innerHTML = q.figure_svg;
      content.append(figure);
    }
    if (q.options?.length) {
      const options = el('ol', 'import-q-options');
      for (const option of q.options) options.append(mathText('li', '', `${option.key}．${option.text}`));
      content.append(options);
    }
    main.append(meta, content);
    if (q.answer) {
      const answer = el('p', 'import-q-answer');
      answer.append(el('b', '', '答案　'), Object.assign(mathText('span', '', q.answer), { translate: false }));
      main.append(answer);
    }
    // 解析：助手补写的默认展开（要你核对），原题自带的默认收起
    if (q.solution_steps?.length) {
      const steps = el('details', 'import-q-steps');
      steps.open = Boolean(q.generated_solution);
      steps.append(el('summary', '', q.generated_solution ? `解析（助手补写，${q.solution_steps.length} 行）` : `解析（${q.solution_steps.length} 行）`));
      const body = el('div', '');
      body.translate = false;
      if (q.solution_method) body.append(mathText('p', 'import-q-method', `【分析】${q.solution_method}`));
      for (const step of q.solution_steps) body.append(mathText('p', '', step));
      steps.append(body);
      main.append(steps);
    }
    if (q.solve_error) main.append(el('p', 'import-q-warn', `没补写成解析：${q.solve_error}`));
    for (const note of q.warnings || []) main.append(el('p', 'import-q-warn', note));
    card.append(box, main);
    return card;
  }

  renderFoot(record) {
    this.foot.replaceChildren();
    if (record.status === 'review') {
      const count = (record.questions || []).filter((q) => this.picked.has(q.index)).length;
      const save = button(`存入题库（${count} 道）`, 'followup primary', () => this.decide(false, save));
      save.disabled = !count;
      const all = button('全选', 'followup', () => { record.questions.forEach((q) => this.picked.add(q.index)); this.drawnKey = ''; this.render(); });
      const none = button('全不选', 'followup', () => { this.picked.clear(); this.drawnKey = ''; this.render(); });
      this.foot.append(save, button('不存了', 'followup', () => this.decide(true)), all, none);
    } else if (record.status === 'saved') {
      this.foot.append(button('在题库里查看', 'followup', () => this.actions.openBank(this)));
    }
  }

  async decide(decline, trigger = null) {
    const record = this.record;
    if (!record?.id) return;
    if (trigger) trigger.disabled = true;
    try {
      const indexes = decline ? [] : record.questions.filter((q) => this.picked.has(q.index)).map((q) => q.index);
      const data = await request(`/api/messages/${encodeURIComponent(record.id)}/import`, {
        method: 'POST',
        body: JSON.stringify({ indexes, decline }),
      });
      this.load(data.message);
      this.render();
      toast(decline ? '好的，这些题没有存' : `已存入题库 ${data.saved} 道`);
      this.actions.saved(this, data);
    } catch (error) {
      toast(error.message);
      if (trigger) trigger.disabled = false;
    }
  }
}
