// 批量出题：一次出好几道同类题，在对话里是一轮，列出每一道的进度；点一行在右栏看那道题。
// 正在出时读 /api/batches/{id} 的概况；出完（或者重新打开对话）时按出题记录显示。

import { AVATAR, ROLES, autoMath, describeConfig, describeStage, el, formatClock, formatTokens, legacyText, renderMath, spin, typesetTopic } from './shared.js';

const STATE_TEXT = { waiting: '排队中', queued: '排队中', running: '出题中', succeeded: '出好了', failed: '没出成', cancelled: '已停下' };
const oneLine = (text) => String(text || '').replace(/\$\$/g, '$').replace(/\s*\n+\s*/g, ' ');

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

// 和 Turn 一样的选方案逻辑：右栏显示哪个方案的题干
export function shownLane(snap, focusLane = null) {
  const lanes = snap?.lanes || [];
  if (focusLane && lanes.some((lane) => lane.id === focusLane)) return focusLane;
  if (snap?.winner) return snap.winner;
  let latest = null;
  for (const lane of lanes) {
    const preview = lane.previews[lane.previews.length - 1];
    if (preview && (!latest || preview.at > latest.at)) latest = { lane: lane.id, at: preview.at };
  }
  return latest?.lane || lanes[0]?.id || null;
}

// 右栏显示批量里的某一道：给 renderSheet 用的"一轮"
export class BatchItemSource {
  constructor(batchTurn, index) {
    this.batchTurn = batchTurn;
    this.index = index;
    this.snapshot = null;
    this.sheetKey = '';
    this.focusLane = null;
  }

  get sheetPrefix() {
    return `第 ${this.index} 道（共 ${this.batchTurn.count} 道），`;
  }

  shownLane() {
    return shownLane(this.snapshot, this.focusLane);
  }
}

export class BatchTurn {
  // actions：models()、stop(turn)、showItem(turn, index)、followUp(turn, kind)、makePaper(turn)、openBank(turn)
  constructor(actions) {
    this.actions = actions;
    this.isBatch = true;
    this.jobId = null; // 正在出时是批量任务的编号
    this.batchId = null;
    this.snapshot = null; // /api/batches/{id} 的概况
    this.records = new Map(); // 第几道 → 出题记录（出完或重新打开对话时有）
    this.request = null; // { brief, meta, config }
    this.count = 0;
    this.focusedIndex = null;
    this.rowKeys = new Map();
    this.outcomeKey = '';

    this.root = el('article', 'turn batch-turn');
    this.task = el('div', 'task-card');
    const reply = el('section', 'reply');
    reply.setAttribute('aria-live', 'polite');
    const avatar = el('span', 'reply-avatar');
    avatar.setAttribute('aria-hidden', 'true');
    avatar.innerHTML = AVATAR;
    this.avatar = avatar;
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
    tools.append(this.stopBtn);
    head.append(this.title, stats, tools);
    this.note = el('p', 'run-note');
    this.bar = el('div', 'batch-bar');
    this.barFill = el('i');
    this.bar.append(this.barFill);
    this.list = el('ol', 'batch-list');
    this.list.setAttribute('aria-label', '这一批的每一道题');
    this.outcome = el('div', 'outcome');
    this.outcome.hidden = true;
    main.append(head, this.note, this.bar, this.list, this.outcome);
    reply.append(avatar, main);
    this.root.append(this.task, reply);
  }

  get running() {
    return this.snapshot?.status === 'running';
  }

  // 每一道的概况：正在出时用批量任务的快照，否则按出题记录
  items() {
    if (this.snapshot) return this.snapshot.items;
    const items = [];
    for (let index = 1; index <= this.count; index += 1) {
      const record = this.records.get(index);
      items.push(record
        ? {
          index,
          job_id: record.id,
          status: record.status,
          elapsed: record.seconds || 0,
          calls: record.call_count || (record.call_log || []).length,
          tokens: record.tokens || 0,
          stage: null,
          role: null,
          stem: record.result?.stem_latex || record.result?.stem_plain || '',
          error: record.error,
        }
        : { index, job_id: null, status: 'cancelled', elapsed: 0, calls: 0, tokens: 0, stem: '', error: null });
    }
    return items;
  }

  addRecord(record) {
    const meta = record.request?.meta || {};
    this.batchId = meta.batch_id;
    this.count = Math.max(this.count, meta.batch_count || 0, meta.batch_index || 0);
    this.records.set(meta.batch_index, record);
    if (!this.request) this.request = record.request || {};
  }

  renderTask() {
    const { brief = {}, meta = {}, config } = this.request || {};
    this.task.innerHTML = '';
    if (meta.said) this.task.append(el('p', 'said', typesetTopic(meta.said)));
    else if (meta.follow_up) this.task.append(el('p', 'lead', meta.follow_up));
    const text = String(brief.request_text || '').replace(/^请出一道/, `请出 ${this.count} 道`);
    this.task.append(el('p', 'ask', text));
    if (meta.note && meta.note !== meta.said) this.task.append(el('p', 'extra', autoMath(typesetTopic(meta.note))));
    const tags = el('div', 'config');
    tags.append(el('span', '', `一次出 ${this.count} 道`));
    if (meta.knowledge_label) tags.append(el('span', '', meta.knowledge_label));
    else if (meta.open_topic) tags.append(el('span', '', '考点不限'));
    for (const tag of describeConfig(config, this.actions.models())) tags.append(el('span', '', tag));
    this.task.append(tags);
    renderMath(this.task);
  }

  render({ live = false } = {}) {
    if (!this.task.childElementCount && this.request) this.renderTask();
    const items = this.items();
    const done = items.filter((item) => item.status === 'succeeded').length;
    const failed = items.filter((item) => ['failed', 'cancelled'].includes(item.status)).length;
    const stopped = items.some((item) => item.status === 'cancelled'); // 是你点了停止，不算"没出成"
    const running = this.running;
    spin(this.avatar.firstElementChild, running); // 这一批还在出时头像一直转
    const totals = this.snapshot?.totals || {
      calls: items.reduce((sum, item) => sum + item.calls, 0),
      tokens: items.reduce((sum, item) => sum + item.tokens, 0),
    };
    const elapsed = this.snapshot?.elapsed ?? items.reduce((sum, item) => Math.max(sum, item.elapsed), 0);
    this.time.textContent = formatClock(elapsed);
    this.calls.textContent = totals.calls;
    this.tokens.textContent = formatTokens(totals.tokens);
    this.title.textContent = running
      ? `正在出 ${this.count} 道题（出好 ${done} 道）`
      : stopped ? (done ? `已停下，出好 ${done} 道` : '已停下')
        : failed && done ? `出好 ${done} 道，${failed} 道没出成` : done ? `${done} 道题都出好了` : '这一批没出成';
    this.note.textContent = running
      ? `同时出 ${this.snapshot.parallel || 2} 道；每道题都单独审题、解题、复核。后开始的题会避开前面已经写出的题干，不会出成一样的题。点一行可以在右侧看那道题。`
      : '';
    this.stopBtn.hidden = !(live && running);
    this.bar.hidden = !running;
    this.barFill.style.width = `${Math.round(((done + failed) / Math.max(1, this.count)) * 100)}%`;
    this.renderRows(items);
    this.renderOutcome(running, done, failed, stopped);
  }

  renderRows(items) {
    let changed = false;
    for (const item of items) {
      const stage = item.stage ? describeStage(item.stage).text : '';
      const key = `${item.status}|${item.stem}|${stage}|${item.error || ''}|${this.focusedIndex === item.index}`;
      if (this.rowKeys.get(item.index) === key) continue;
      this.rowKeys.set(item.index, key);
      changed = true;
      let row = this.list.querySelector(`[data-index="${item.index}"]`);
      if (!row) {
        row = el('li', 'batch-item');
        row.dataset.index = item.index;
        this.list.append(row);
      }
      row.className = `batch-item ${item.status}`;
      row.setAttribute('aria-current', String(this.focusedIndex === item.index));
      const open = button('', 'batch-row', () => this.actions.showItem(this, item.index));
      open.disabled = !item.job_id;
      const state = el('span', 'batch-state');
      state.append(el('i', 'dot'), document.createTextNode(STATE_TEXT[item.status] || item.status));
      let text;
      if (item.status === 'failed') text = el('span', 'batch-text bad', legacyText(item.error) || '没能得到通过全部检查的题');
      else if (item.stem) text = el('span', 'batch-text stem compact-math', oneLine(item.stem));
      else if (item.status === 'running' || item.status === 'queued') {
        const who = ROLES[item.role]?.name;
        text = el('span', 'batch-text', stage ? `${who ? `${who}：` : ''}${stage}…` : '正在开始…');
      } else text = el('span', 'batch-text', item.status === 'cancelled' ? '没有出这一道' : '等前面的题写出题干后开始');
      open.append(el('span', 'batch-no', String(item.index)), state, text);
      row.replaceChildren(open);
    }
    if (changed) renderMath(this.list);
  }

  setFocusedItem(index) {
    this.focusedIndex = index;
    this.root.classList.toggle('focused', index !== null);
    for (const row of this.list.children) row.setAttribute('aria-current', String(Number(row.dataset.index) === index));
  }

  setFocused(focused) {
    if (!focused) this.setFocusedItem(null);
  }

  tick(seconds) {
    this.time.textContent = formatClock(seconds);
  }

  renderOutcome(running, done, failed, stopped) {
    const key = `${running}:${done}:${failed}:${stopped}`;
    if (running) {
      this.outcome.hidden = true;
      this.outcomeKey = '';
      return;
    }
    if (this.outcomeKey === key) return;
    this.outcomeKey = key;
    this.outcome.className = `outcome${done ? ' ok' : stopped ? '' : ' bad'}`;
    this.outcome.hidden = false;
    this.outcome.innerHTML = '';
    let summary = '这一批一道也没出成。可以换一个考点，或者降一档难度再试。';
    if (done) {
      const rest = stopped ? `另外 ${failed} 道没有出。` : failed ? `另外 ${failed} 道没出成，可以再出一批补上。` : '';
      summary = `出好的 ${done} 道都审题、复核过，已经收进题库。${rest}`;
    } else if (stopped) summary = '已经停下，这次没有生成题目。';
    this.outcome.append(el('p', '', summary));
    const actions = el('div', 'followups');
    if (done) actions.append(button(`把这 ${done} 道组成试卷`, 'followup', () => this.actions.makePaper(this)));
    actions.append(button(`再出 ${this.count} 道`, 'followup', () => this.actions.followUp(this, 'again')));
    if (done) actions.append(button('在题库里查看', 'followup', () => this.actions.openBank(this)));
    this.outcome.append(actions);
  }
}
