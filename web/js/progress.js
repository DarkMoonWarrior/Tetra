// 出题进度：每个角色一行，像任务清单一样往下走；同时试几个方案时可以切换看哪一个。
// 还有过程记录（审题意见、退回原因、验算结果）和每一次模型调用的明细。

import {
  ROLES, autoMath, describeStage, el, formatClock, formatTokens, legacyText, mathText, modelLabel, renderMath, svg,
} from './shared.js';

export const LANE_STATE = { waiting: '等待中', running: '进行中', won: '已采用', stopped: '已停下', failed: '没出成' };

// 各角色被退回、重来时的说法
const BACK_WORD = {
  composer: '重出', reviewer: '退回', solver: '退回', auditor: '退回', verifier: '对不上', editor: '重来', illustrator: '重画',
};

// 整理员：正在出的题按解析详略决定有没有这一步；已经出完的题看实际有没有调用。
// 配图员：要不要配图得等题目定稿才知道，所以真的开始画了才显示
export function stationsFor(snap) {
  const running = ['queued', 'running'].includes(snap.status);
  const tidy = snap.calls.some((call) => call.role === 'editor') || (running && snap.meta?.detail !== 'detailed');
  const figure = snap.calls.some((call) => call.role === 'illustrator');
  return [
    'composer', 'reviewer', 'solver', 'auditor',
    ...(snap.config?.verify ? ['verifier'] : []), ...(tidy ? ['editor'] : []), ...(figure ? ['illustrator'] : []),
  ];
}

const oneLine = (text) => String(text || '').replace(/\$\$/g, '$').replace(/\s*\n+\s*/g, ' ');

export class Steps {
  constructor(list, switcher, { onPickLane }) {
    this.list = list;
    this.switcher = switcher;
    this.onPickLane = onPickLane;
    this.key = '';
    this.rows = new Map();
    this.pills = new Map();
  }

  render(snap, selectedLane) {
    const stations = stationsFor(snap);
    const key = `${snap.id}:${stations.join(',')}:${snap.lanes.map((lane) => lane.id).join('')}`;
    if (key !== this.key) this.build(snap, stations, key);
    const finished = !['queued', 'running'].includes(snap.status);
    for (const lane of snap.lanes) {
      const pill = this.pills.get(lane.id);
      pill.className = `lane-pill ${lane.status}`;
      pill.setAttribute('aria-pressed', String(lane.id === selectedLane));
      pill.querySelector('.state').textContent = `方案 ${lane.id}，${LANE_STATE[lane.status] || ''}`;
    }
    const lane = snap.lanes.find((item) => item.id === selectedLane) || snap.lanes[0];
    if (lane) this.update(lane, snap, stations, finished);
  }

  build(snap, stations, key) {
    this.key = key;
    this.list.innerHTML = '';
    this.rows.clear();
    for (const role of stations) {
      const row = el('li', 'step todo');
      row.dataset.role = role;
      const icon = el('span', 'step-icon');
      icon.setAttribute('aria-hidden', 'true');
      const detail = el('span', 'step-detail');
      const meta = el('span', 'step-meta');
      row.append(icon, el('span', 'step-name', ROLES[role].name), detail, meta);
      this.list.append(row);
      this.rows.set(role, { row, icon, detail, meta, status: '', text: null, metaKey: '' });
    }
    this.switcher.innerHTML = '';
    this.pills.clear();
    this.switcher.hidden = snap.lanes.length < 2;
    for (const lane of snap.lanes) {
      const pill = el('button', 'lane-pill');
      pill.type = 'button';
      pill.title = `看方案 ${lane.id} 的进度和题干`;
      pill.append(el('span', 'id', lane.id), el('span', 'state'));
      pill.addEventListener('click', () => this.onPickLane(lane.id));
      this.switcher.append(pill);
      this.pills.set(lane.id, pill);
    }
  }

  update(lane, snap, stations, finished) {
    const calls = snap.calls.filter((call) => call.lane === lane.id);
    const events = snap.events.filter((event) => event.lane === lane.id);
    for (const role of stations) {
      const parts = this.rows.get(role);
      const mine = calls.filter((call) => call.role === role);
      const running = finished ? [] : mine.filter((call) => call.status === 'running');
      const said = events.filter((event) => event.role === role);
      const backs = said.filter((event) => event.kind === 'reject').length;

      // 定出题目后整理员还在改写解析，所以"正在调用"优先于"已采用"
      let status = running.length ? 'active' : mine.length || lane.status === 'won' ? 'done' : 'todo';
      if (parts.status !== status) {
        parts.status = status;
        parts.row.className = `step ${status}`;
        parts.icon.innerHTML = status === 'done' ? svg('check') : '';
      }

      let text;
      if (running.length) text = `${describeStage(running[running.length - 1].stage).text}…`;
      else if (said.length) text = autoMath(legacyText(said[said.length - 1].text));
      else if (mine.length) text = describeStage(mine[mine.length - 1].stage).text;
      else text = ROLES[role].todo;
      if (parts.text !== text) {
        parts.text = text;
        parts.detail.textContent = oneLine(text);
        renderMath(parts.detail);
      }

      const metaKey = `${backs}:${mine.length}`;
      if (parts.metaKey !== metaKey) {
        parts.metaKey = metaKey;
        parts.meta.innerHTML = '';
        if (backs) parts.meta.append(el('span', 'step-back', `${BACK_WORD[role]} ${backs} 次`));
        if (mine.length) parts.meta.append(el('span', '', `${mine.length} 次调用`));
      }
    }
  }
}

export class WorkLog {
  constructor(root, summary) {
    this.root = root;
    this.summary = summary;
    this.key = '';
    this.count = 0;
  }

  render(snap) {
    const multiLane = snap.lanes.length > 1;
    if (snap.id !== this.key || snap.events.length < this.count) {
      this.key = snap.id;
      this.count = 0;
      this.root.innerHTML = '';
    }
    if (snap.events.length > this.count) this.root.querySelector('.log-empty')?.remove();
    for (let i = this.count; i < snap.events.length; i += 1) {
      const item = this.item(snap.events[i], multiLane);
      this.root.append(item);
      renderMath(item);
    }
    this.count = snap.events.length;
    if (!snap.events.length && !this.root.querySelector('.log-empty')) {
      this.root.append(el('li', 'log-empty', '审题意见、退回原因和验算结果会记在这里。'));
    }
    const backs = snap.events.filter((event) => event.kind === 'reject').length;
    this.summary.innerHTML = '';
    this.summary.append('过程记录');
    if (snap.events.length) {
      this.summary.append(el('small', '', backs ? `${snap.events.length} 条，其中退回或重来 ${backs} 次` : `${snap.events.length} 条`));
    }
  }

  item(event, multiLane) {
    const item = el('li', `log-item ${event.kind}`);
    item.dataset.role = event.role;
    const head = el('div', 'log-head');
    head.append(el('time', 'log-time', formatClock(event.at)));
    if (multiLane) head.append(el('span', 'log-lane', `方案 ${event.lane}`));
    head.append(el('span', 'log-role', ROLES[event.role]?.name || ''));
    item.append(head, mathText('p', 'log-text', autoMath(legacyText(event.text))));
    if (event.details && event.details.length) {
      const list = el('ul', 'log-details');
      for (const detail of event.details) list.append(mathText('li', '', autoMath(detail)));
      item.append(list);
    }
    return item;
  }
}

export function renderCalls(root, snap) {
  root.innerHTML = '';
  const multiLane = snap.lanes.length > 1;
  const table = el('table', 'calls-table');
  const head = el('tr');
  const titles = ['时间', ...(multiLane ? ['方案'] : []), '角色', '在做什么', '模型', '用时', 'Token'];
  for (const title of titles) head.append(el('th', '', title));
  const thead = el('thead');
  thead.append(head);
  const body = el('tbody');
  for (const call of snap.calls) {
    const row = el('tr', call.status);
    row.dataset.role = call.role;
    row.append(el('td', 'num', formatClock(call.at)));
    if (multiLane) row.append(el('td', 'num', call.lane));
    row.append(
      el('td', 'role', ROLES[call.role]?.name || ''),
      el('td', '', describeStage(call.stage).text),
      el('td', 'model', modelLabel(call.model)),
      el('td', 'num', call.status === 'running' ? '进行中' : `${Number(call.seconds).toFixed(1)} 秒`),
      el('td', 'num', formatTokens(call.input_tokens + call.output_tokens)),
    );
    body.append(row);
  }
  table.append(thead, body);
  root.append(table);
}
