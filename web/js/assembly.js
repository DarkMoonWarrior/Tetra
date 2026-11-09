// 自动组卷：在对话里是一轮。先显示组卷方案（试卷名、考查范围、结构），再按大题列出每一道题从哪来：
// 题库里挑的、现场出的（出题中显示正在做哪一步）、没出成的。点一行在右侧看那道题；
// 组好以后可以直接导出 Word、PDF，右侧显示整份试卷。
// 正在组时读 /api/assemblies/{id}；组完（或重新打开对话）时按存下来的记录显示，两者形状一样。

import { AVATAR, DIFFICULTY_LABEL, ROLES, describeStage, el, formatClock, formatTokens, legacyText, renderMath, spin } from './shared.js';

const ORDER = ['single_choice', 'multiple_choice', 'fill_blank', 'open_response'];
const SECTION = { single_choice: '单项选择题', multiple_choice: '多项选择题', fill_blank: '填空题', open_response: '解答题' };
const CN = '一二三四五六七八九十';
// 每道题的状态 → 列表里的样式（和批量出题的圆点一样）和文字
const STATE = {
  planned: ['waiting', '待定'],
  picked: ['succeeded', '题库'],
  queued: ['queued', '排队'],
  generating: ['running', '出题中'],
  generated: ['succeeded', '现场出'],
  failed: ['failed', '没出成'],
};
const STAGES = [['planning', '定组卷方案'], ['selecting', '从题库挑题'], ['generating', '现场出题'], ['assembling', '组成试卷']];
// 题目从哪来（方案里的 source）
const SOURCE_NOTE = {
  original: '题目全部现场出新题，不用题库里的题',
  bank_first: '先从题库里挑合适的题，不够的现场出',
  bank_only: '只用题库里的题，不现场出',
};
const oneLine = (text) => String(text || '').replace(/\$\$/g, '$').replace(/\s*\n+\s*/g, ' ');
const scoreText = (value) => (Number.isInteger(Number(value)) ? String(Number(value)) : String(Number(Number(value).toFixed(1))));

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

export class PaperTurn {
  // actions：stop(turn)、showSlot(turn, slot)、exportPaper(turn, format, button)、openPaper(turn)、showPaper(turn)、again(turn)
  constructor(actions) {
    this.actions = actions;
    this.isPaper = true;
    this.jobId = null; // 正在组卷时是组卷任务的编号
    this.snapshot = null;
    this.sheetKey = '';
    this.focusedNo = null;
    this.rowKeys = new Map();
    this.outcomeKey = '';

    this.root = el('article', 'turn paper-turn');
    this.task = el('div', 'task-card');
    const reply = el('section', 'reply');
    reply.setAttribute('aria-live', 'polite');
    this.avatar = el('span', 'reply-avatar');
    this.avatar.setAttribute('aria-hidden', 'true');
    this.avatar.innerHTML = AVATAR;
    const main = el('div', 'reply-main');
    const head = el('header', 'reply-head');
    this.title = el('h2', 'reply-title');
    this.time = el('b');
    this.calls = el('b');
    this.tokens = el('b');
    const stats = el('div', 'reply-stats');
    for (const [value, label] of [[this.time, '用时'], [this.calls, '次调用'], [this.tokens, 'Token']]) {
      const span = el('span');
      span.append(value, label);
      stats.append(span);
    }
    const tools = el('div', 'reply-tools');
    this.stopBtn = button('停止', 'stop-btn', () => actions.stop(this));
    this.sheetBtn = button('看试卷', 'ghost-btn turn-sheet-btn', () => actions.showPaper(this));
    tools.append(this.stopBtn, this.sheetBtn);
    head.append(this.title, stats, tools);
    this.plan = el('div', 'paper-plan');
    this.steps = el('ol', 'paper-steps');
    this.steps.setAttribute('aria-label', '组卷进度');
    this.list = el('ol', 'slot-list');
    this.list.setAttribute('aria-label', '试卷里的每一道题');
    this.outcome = el('div', 'outcome');
    this.outcome.hidden = true;
    main.append(head, this.plan, this.steps, this.list, this.outcome);
    reply.append(this.avatar, main);
    this.root.append(this.task, reply);
  }

  get running() {
    return this.snapshot?.status === 'running';
  }

  get paper() {
    return this.snapshot?.paper || null;
  }

  // 存下来的组卷记录（对话重新打开时）
  load(record) {
    this.snapshot = { ...record, elapsed: record.elapsed ?? record.seconds ?? 0 };
  }

  slots() {
    return this.snapshot?.slots || [];
  }

  // focusedNo：右栏显示的是第几题（null 表示整份试卷），由 focusTurn 先设好
  setFocused(focused) {
    this.root.classList.toggle('focused', focused);
    this.sheetBtn.setAttribute('aria-pressed', String(focused && this.focusedNo === null));
    this.setFocusedSlot(focused ? this.focusedNo : null);
  }

  setFocusedSlot(no) {
    this.focusedNo = no;
    for (const row of this.list.querySelectorAll('.slot-item')) row.setAttribute('aria-current', String(Number(row.dataset.no) === no));
  }

  tick(seconds) {
    this.time.textContent = formatClock(seconds);
  }

  render({ live = false } = {}) {
    const snap = this.snapshot;
    if (!snap) return;
    if (!this.task.childElementCount) {
      this.task.append(el('p', 'ask', snap.text || ''));
      renderMath(this.task);
    }
    const running = this.running;
    spin(this.avatar.firstElementChild, running);
    const totals = snap.totals || {};
    this.time.textContent = formatClock(snap.elapsed);
    this.calls.textContent = totals.calls || 0;
    this.tokens.textContent = formatTokens(totals.tokens || 0);
    const slots = this.slots();
    const waiting = slots.filter((slot) => ['queued', 'generating'].includes(slot.state)).length;
    const made = slots.filter((slot) => slot.state === 'generated').length;
    if (running) {
      this.title.textContent = {
        planning: '正在定组卷方案',
        selecting: '正在从题库挑题',
        generating: `正在现场出题（出好 ${made} 道，还有 ${waiting} 道）`,
        assembling: '正在组成试卷',
      }[snap.stage] || '正在组卷';
    } else if (snap.status === 'succeeded') {
      this.title.textContent = '试卷组好了';
    } else if (snap.status === 'cancelled') {
      this.title.textContent = this.paper ? '已停下，用已有的题组成了试卷' : '已停下';
    } else {
      this.title.textContent = '组卷没完成';
    }
    this.stopBtn.hidden = !(live && running);
    this.sheetBtn.hidden = !this.paper;
    this.renderPlan(snap);
    this.renderSteps(snap);
    this.renderRows(slots);
    this.renderOutcome(snap);
  }

  renderPlan(snap) {
    const plan = snap.plan;
    const key = plan ? `${plan.title}|${plan.summary}|${this.slots().length}` : 'none';
    if (this.planKey === key) return;
    this.planKey = key;
    this.plan.replaceChildren();
    if (!plan) {
      this.plan.append(el('p', 'run-note', '先按你的要求定组卷方案：考查范围、题型结构，以及每道题考什么知识点、多难、几分；再按你的说法准备题目：要原创就全部现场出新题，否则先从题库里挑，题库里没有合适的再现场出。'));
      return;
    }
    const count = this.slots().length;
    this.plan.append(el('p', 'plan-title', plan.title));
    const scope = String(plan.scope || '').replace(/[。．.；;，,\s]+$/, '');
    const facts = [scope, `${count} 道题，满分 ${scoreText(plan.total)} 分`, plan.minutes ? `考试时间 ${plan.minutes} 分钟` : ''];
    this.plan.append(el('p', 'plan-facts', `${facts.filter(Boolean).join('；')}。`));
    if (SOURCE_NOTE[plan.source]) this.plan.append(el('p', 'plan-facts', `${SOURCE_NOTE[plan.source]}。`));
    if (plan.summary) this.plan.append(el('p', 'plan-summary', plan.summary));
  }

  // 四步：定方案、挑题、现场出题、组成试卷。题库里的题够用时，现场出题这一步写"不用现场出题"；
  // 全部原创时挑题这一步写"不用题库"，只用题库时现场出题这一步写"不现场出题"
  renderSteps(snap) {
    const generated = this.slots().some((slot) => slot.source === 'generated' || ['queued', 'generating', 'failed'].includes(slot.state));
    const source = snap.plan?.source || 'bank_first';
    const key = `${snap.stage}|${snap.status}|${generated}|${Boolean(snap.plan)}|${Boolean(snap.paper)}|${source}`;
    if (this.stepsKey === key) return;
    this.stepsKey = key;
    let states;
    if (snap.status === 'running') {
      const current = Math.max(0, STAGES.findIndex(([stage]) => stage === snap.stage));
      states = STAGES.map((_, i) => (i < current ? 'done' : i === current ? 'active' : 'todo'));
    } else if (snap.paper) {
      states = STAGES.map(() => 'done');
    } else if (snap.plan) {
      states = ['done', 'done', generated ? 'done' : 'todo', 'stopped'];
    } else {
      states = ['stopped', 'todo', 'todo', 'todo'];
    }
    this.steps.replaceChildren();
    STAGES.forEach(([stage, name], i) => {
      let label = name;
      if (stage === 'selecting' && source === 'original' && snap.plan) label = '不用题库';
      else if (stage === 'generating' && source === 'bank_only' && snap.plan) label = '不现场出题';
      else if (stage === 'generating' && states[i] === 'done' && !generated) label = '不用现场出题';
      const item = el('li', `paper-step ${states[i]}`);
      item.append(el('i'), el('span', '', label));
      this.steps.append(item);
    });
  }

  renderRows(slots) {
    const groups = ORDER.map((type) => ({ type, slots: slots.filter((slot) => slot.type === type) })).filter((group) => group.slots.length);
    const shape = groups.map((group) => `${group.type}:${group.slots.length}`).join('|');
    if (this.shape !== shape) {
      this.shape = shape;
      this.rowKeys.clear();
      this.list.replaceChildren();
      groups.forEach((group, index) => {
        const total = group.slots.reduce((sum, slot) => sum + Number(slot.score || 0), 0);
        this.list.append(el('li', 'slot-group', `${CN[index]}、${SECTION[group.type]}（${group.slots.length} 道，${scoreText(total)} 分）`));
        for (const slot of group.slots) {
          const row = el('li', 'slot-item batch-item');
          row.dataset.no = slot.no;
          this.list.append(row);
        }
      });
    }
    let changed = false;
    for (const slot of slots) {
      const stage = slot.state === 'generating' && slot.stage ? describeStage(slot.stage).text : '';
      const key = `${slot.state}|${slot.stem}|${stage}|${slot.error || ''}|${slot.topic}|${slot.difficulty}`;
      if (this.rowKeys.get(slot.no) === key) continue;
      this.rowKeys.set(slot.no, key);
      changed = true;
      const row = this.list.querySelector(`.slot-item[data-no="${slot.no}"]`);
      if (!row) continue;
      const [look, word] = STATE[slot.state] || ['waiting', slot.state];
      row.className = `slot-item batch-item ${look}${slot.source === 'generated' ? ' made' : ''}`;
      row.setAttribute('aria-current', String(this.focusedNo === slot.no));
      const open = button('', 'slot-row', () => this.actions.showSlot(this, slot.no));
      open.disabled = !slot.bank_id && !slot.job_id;
      const status = el('span', 'batch-state');
      status.append(el('i', 'dot'), document.createTextNode(slot.state === 'picked' && slot.match === 'spare' ? '题库补' : word));
      const meta = el('span', 'slot-meta');
      meta.append(el('b', '', slot.topic || '考点不限'), document.createTextNode(` · ${DIFFICULTY_LABEL[slot.difficulty] || ''} · ${scoreText(slot.score)} 分`));
      meta.title = (slot.path || []).join(' › ');
      let text;
      if (slot.state === 'failed') text = el('span', 'batch-text bad', legacyText(slot.error) || '没出成');
      else if (slot.stem) text = el('span', 'batch-text stem compact-math', oneLine(slot.stem));
      else if (slot.state === 'generating') {
        const who = ROLES[slot.role]?.name;
        text = el('span', 'batch-text', stage ? `${who ? `${who}：` : ''}${stage}…` : '正在开始…');
      } else if (slot.state === 'queued') {
        text = el('span', 'batch-text', this.snapshot?.plan?.source === 'original' ? '等着现场出题' : '题库里没有合适的，等着现场出题');
      }
      else text = el('span', 'batch-text', '等挑题');
      open.append(el('span', 'batch-no', String(slot.no)), status, meta, text);
      row.replaceChildren(open);
    }
    if (changed) renderMath(this.list);
  }

  renderOutcome(snap) {
    const running = this.running;
    const totals = snap.totals || {};
    const key = `${snap.status}|${this.paper?.id || ''}|${totals.picked}|${totals.generated}|${totals.failed}`;
    if (running) {
      this.outcome.hidden = true;
      this.outcomeKey = '';
      return;
    }
    if (this.outcomeKey === key) return;
    this.outcomeKey = key;
    this.outcome.hidden = false;
    this.outcome.replaceChildren();
    const paper = this.paper;
    const actions = el('div', 'followups');
    if (paper) {
      this.outcome.className = 'outcome ok';
      const from = totals.picked || snap.plan?.source !== 'original' ? [`从题库里选了 ${totals.picked || 0} 道`] : [];
      const flagged = (snap.slots || []).filter((slot) => slot.flagged).length;
      if (totals.generated) from.push(flagged ? `现场出了 ${totals.generated} 道（其中 ${flagged} 道复核还有意见，用之前请核对）` : `现场出了 ${totals.generated} 道（都审核过，已收进题库）`);
      const lacking = !totals.failed ? ''
        : snap.plan?.source === 'bank_only' ? `；有 ${totals.failed} 道题库里没有合适的，可以在试卷里补上`
          : `；有 ${totals.failed} 道没出成，可以在试卷里从题库补上`;
      this.outcome.append(
        el('p', '', `${snap.status === 'cancelled' ? '已停下：' : ''}《${paper.title}》，${paper.count} 道题，满分 ${scoreText(paper.total)} 分。`),
        el('p', '', `${from.join('，')}${lacking}。右侧是整份试卷的预览，答案和解析另起一份附在后面，可以直接导出。`),
      );
      const word = button('导出 Word', 'followup primary', () => this.actions.exportPaper(this, 'docx', word));
      const pdf = button('导出 PDF', 'followup primary', () => this.actions.exportPaper(this, 'pdf', pdf));
      actions.append(
        word,
        pdf,
        button('看整份试卷', 'followup', () => this.actions.showPaper(this)),
        button('在试卷里调整', 'followup', () => this.actions.openPaper(this)),
      );
      // 用了题库里的题、其实想要全新的：同样的要求再组一次，题目全部现场出
      if (totals.picked) {
        const fresh = button('全部换成新题', 'followup', () => this.actions.again(this, 'original'));
        fresh.title = '按同样的要求再组一份，题目全部现场出新题，不用题库里的题';
        actions.append(fresh);
      }
    } else {
      this.outcome.className = snap.status === 'cancelled' ? 'outcome' : 'outcome bad';
      this.outcome.append(el('p', '', legacyText(snap.error) || '没能组成试卷。'));
      if (snap.status !== 'cancelled') this.outcome.append(el('p', '', '可以把要求说得具体一些（比如年级、考试类型、考到哪一章）再试。'));
      actions.append(button('再试一次', 'followup', () => this.actions.again(this)));
    }
    this.outcome.append(actions);
  }
}
