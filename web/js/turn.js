// 对话里的一轮：你的要求，加上助手的回复（进度、结果、过程记录、调用明细）。
// 一个对话可以连着出好几道题，每道题是一轮，从上往下排。

import { AVATAR, DETAIL, describeConfig, el, formatClock, formatTokens, legacyText, renderMath, spin } from './shared.js';
import { Steps, WorkLog, renderCalls } from './progress.js';


function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function stat(value, label) {
  const span = el('span');
  span.append(value, label);
  return span;
}

export class Turn {
  // actions：models() 可选模型；pickLane、showSheet、followUp、openBank、stop、replay 都以这一轮为第一个参数
  constructor(actions) {
    this.actions = actions;
    this.snapshot = null;
    this.record = null; // 从服务器读回来的出题记录（回放要用）
    this.recordId = null;
    this.jobId = null; // 正在出的题
    this.focusLane = null;
    this.outcomeKey = '';
    this.sheetKey = '';

    this.root = el('article', 'turn');
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
    stats.append(stat(this.time, '用时'), stat(this.calls, '次调用'), stat(this.tokens, 'Token'));
    const tools = el('div', 'reply-tools');
    this.stopBtn = button('停止', 'stop-btn', () => actions.stop(this));
    this.replayBtn = button('回放过程', 'ghost-btn', () => actions.replay(this));
    this.sheetBtn = button('看题目', 'ghost-btn turn-sheet-btn', () => actions.showSheet(this));
    tools.append(this.stopBtn, this.replayBtn, this.sheetBtn);
    head.append(this.title, stats, tools);

    this.note = el('p', 'run-note');
    const switcher = el('div', 'lane-switch');
    switcher.setAttribute('role', 'group');
    switcher.setAttribute('aria-label', '切换方案');
    switcher.hidden = true;
    const steps = el('ol', 'steps');
    steps.setAttribute('aria-label', '出题进度');
    this.outcome = el('div', 'outcome');
    this.outcome.hidden = true;
    const worklog = el('details', 'worklog');
    const logSummary = el('summary');
    const log = el('ol', 'log');
    worklog.append(logSummary, log);
    this.callsBox = el('details', 'calls');
    this.callsSummary = el('summary');
    this.callsTable = el('div');
    this.callsBox.append(this.callsSummary, this.callsTable);
    this.callsBox.addEventListener('toggle', () => {
      if (this.callsBox.open && this.snapshot) renderCalls(this.callsTable, this.snapshot);
    });

    main.append(head, this.note, switcher, steps, this.outcome, worklog, this.callsBox);
    reply.append(avatar, main);
    this.root.append(this.task, reply);
    this.steps = new Steps(steps, switcher, { onPickLane: (lane) => actions.pickLane(this, lane) });
    this.log = new WorkLog(log, logSummary);
  }

  // 出题记录编号：去题库找这道题、回放时用
  get sourceId() {
    return this.recordId || this.jobId;
  }

  get running() {
    return ['queued', 'running'].includes(this.snapshot?.status);
  }

  shownLane() {
    const snap = this.snapshot;
    const lanes = snap.lanes || [];
    if (this.focusLane && lanes.some((lane) => lane.id === this.focusLane)) return this.focusLane;
    if (snap.winner) return snap.winner;
    let latest = null;
    for (const lane of lanes) {
      const preview = lane.previews[lane.previews.length - 1];
      if (preview && (!latest || preview.at > latest.at)) latest = { lane: lane.id, at: preview.at };
    }
    return latest?.lane || lanes[0]?.id || null;
  }

  setFocused(focused) {
    this.root.classList.toggle('focused', focused);
    this.sheetBtn.setAttribute('aria-pressed', String(focused));
    this.sheetBtn.title = focused ? '右侧显示的就是这一道' : '在右侧显示这一道';
  }

  renderTask() {
    const { brief = {}, meta = {}, config } = this.snapshot;
    this.task.innerHTML = '';
    // said：用户在输入框里说的原话（助手判断是出题时）；下面是助手整理成的出题要求
    if (meta.said) this.task.append(el('p', 'said', meta.said));
    else if (meta.follow_up) this.task.append(el('p', 'lead', meta.follow_up));
    this.task.append(el('p', 'ask', brief.request_text || ''));
    const note = meta.note ?? (meta.follow_up ? '' : brief.additional_constraints);
    if (note && note !== meta.said) this.task.append(el('p', 'extra', note));
    const tags = el('div', 'config');
    if (meta.knowledge_label) tags.append(el('span', '', meta.knowledge_label));
    else if (meta.open_topic) tags.append(el('span', '', '考点不限'));
    if (meta.detail && meta.detail !== 'standard' && DETAIL[meta.detail]) tags.append(el('span', '', `解析${DETAIL[meta.detail].name}`));
    for (const tag of describeConfig(config, this.actions.models())) tags.append(el('span', '', tag));
    if (tags.childElementCount) this.task.append(tags);
  }

  // live：正在出的这一道（显示"停止"）；replaying：回放中
  render({ live = false, replaying = false } = {}) {
    const snap = this.snapshot;
    if (!snap) return;
    if (!this.task.childElementCount) this.renderTask();
    const running = this.running;
    spin(this.avatar.firstElementChild, running); // 出题时头像一直转，出完转完这一圈停下
    this.time.textContent = formatClock(snap.elapsed);
    this.calls.textContent = snap.totals.calls;
    this.tokens.textContent = formatTokens(snap.totals.input_tokens + snap.totals.output_tokens);
    if (running) this.title.textContent = snap.lanes.length > 1 ? `同时试 ${snap.lanes.length} 个方案` : '正在出题';
    else if (snap.status === 'succeeded') this.title.textContent = '题目出好了';
    else if (snap.status === 'cancelled') this.title.textContent = '已停下';
    else this.title.textContent = '这次没出成';
    this.note.textContent = running ? this.runNote() : '';
    this.stopBtn.hidden = !(live && running);
    this.replayBtn.hidden = live || replaying || !this.recordId;
    this.steps.render(snap, this.shownLane());
    this.log.render(snap);
    this.renderOutcome(running);
    this.callsSummary.innerHTML = '';
    this.callsSummary.append('模型调用明细', el('small', '', `${snap.totals.calls} 次`));
    if (this.callsBox.open) renderCalls(this.callsTable, snap);
  }

  tick(seconds) {
    this.time.textContent = formatClock(seconds);
  }

  runNote() {
    const snap = this.snapshot;
    const verify = snap.config?.verify;
    const flow = `命题员出题，审题员、解题员、复核员${verify ? '、验算员' : ''}逐一把关，全部通过后，题目会显示在右侧。`;
    return snap.lanes.length > 1 ? `${snap.lanes.length} 个方案各自出题，用最先通过全部检查的那个。${flow}` : flow;
  }

  followup(label, kind, disabled = false) {
    const node = button(label, 'followup', () => this.actions.followUp(this, kind));
    node.disabled = disabled;
    return node;
  }

  renderOutcome(running) {
    const snap = this.snapshot;
    const key = `${snap.id}:${snap.status}`;
    if (running) {
      this.outcome.hidden = true;
      this.outcomeKey = '';
      return;
    }
    if (this.outcomeKey === key) return;
    this.outcomeKey = key;
    this.outcome.innerHTML = '';
    this.outcome.hidden = false;
    const lane = snap.winner || snap.lanes[0]?.id;
    const events = snap.events.filter((event) => event.lane === lane);
    const count = (role, kind) => events.filter((event) => event.role === role && event.kind === kind).length;
    const followups = el('div', 'followups');
    const level = snap.brief?.target_difficulty || 3;

    if (snap.status === 'succeeded') {
      this.outcome.className = 'outcome ok';
      const verified = count('verifier', 'pass') > 0;
      const fixes = [
        ['reviewer', '审题员退回'], ['solver', '解题员退回'], ['auditor', '复核员退回'], ['verifier', '验算对不上'], ['composer', '整题重出'],
      ].map(([role, word]) => (count(role, 'reject') ? `${word} ${count(role, 'reject')} 次` : '')).filter(Boolean);
      this.outcome.append(
        el('p', '', `审题、复核都通过了${verified ? '，独立验算的答案也一致' : ''}。用时 ${Math.max(1, Math.round(snap.elapsed))} 秒。`),
        el('p', '', `${fixes.length ? `过程中${fixes.join('、')}，都已改好。` : ''}题目已经收进题库。想接着出，点下面的按钮，或者直接在输入框里说。`),
      );
      followups.append(
        this.followup('再出一道同类题', 'again'),
        this.followup('难一点', 'harder', level >= 5),
        this.followup('简单一点', 'easier', level <= 1),
        this.followup('换个情境', 'context'),
        button('在题库里查看', 'followup', () => this.actions.openBank(this)),
      );
    } else if (snap.status === 'failed') {
      this.outcome.className = 'outcome bad';
      this.outcome.append(
        el('p', '', legacyText(snap.error) || '没能得到通过全部检查的题。'),
        el('p', '', '可以换一个考点，或者降一档难度再试。'),
      );
      followups.append(this.followup('再试一次', 'retry'), this.followup('简单一点再试', 'easier', level <= 1));
    } else {
      this.outcome.className = 'outcome';
      this.outcome.append(el('p', '', '已经停下，这次没有生成题目。'));
      followups.append(this.followup('重新出这道题', 'retry'));
    }
    this.outcome.append(followups);
    renderMath(this.outcome);
  }
}
