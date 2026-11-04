// 命题智能体 · 主逻辑：输入框、对话（一个对话里可以连着出好几道题）、题目、历史对话与回放

import {
  $, DIFFICULTY_LABEL, FORCE_THINKING_FROM, GRADES, MODES, OFFLINE_MESSAGE, PURPOSES, THINKING, TYPE_LABEL,
  agentConfig, describeConfig, el, setModelCatalog, thinkingFor,
  formatWhen, greeting, legacyText, normalize, readStorage, renderMath, request,
  roleOfStage, segmented, svg, toast, writeStorage,
} from './shared.js';
import { buildSheet, draftAsText } from './sheet.js';
import { Turn } from './turn.js';
import { BatchItemSource, BatchTurn } from './batch.js';
import { setGeometryBusy, startGeometry } from './geometry.js';
import { initSplitters } from './split.js';
import { Library } from './bank.js';
import { Papers } from './paper.js';
import {
  initSettings, loadProfile, onProfileChange, openSettings, profile, refreshAccount, updateAgent,
} from './settings.js';

const FORM_KEY = 'mingti.form.v3';
const JOB_KEY = 'mingti.job.v3';
const CHAT_KEY = 'mingti.chat';
const RAIL_KEY = 'mingti.rail-collapsed';
// 起始页输入框里的示例：每次打开起始页随机换一条，不和上次一样
const EXAMPLES = [
  '以二十四节气为情境、考查分类讨论、数据别太繁琐',
  '以校园运动会为背景，考查概率与统计，分两小问',
  '结合新能源汽车的续航数据，考查函数模型的选择',
  '考查数形结合，图象上的关键点要一眼能看出来',
  '用错位相减法求和，第二问加一个不等式证明',
  '以古建筑的斗拱为情境出一道立体几何题，要配图',
  '错误选项里放一个常见易错点，比如忽略定义域',
  '外卖配送路线为情境，考查正弦定理和余弦定理',
  '第三问考查存在性问题，难度比前两问高一档',
  '以空间站的运行轨道为背景，考查椭圆的几何性质',
  '计算量控制在三四步以内，不要用超纲的方法',
  '结合体育测试成绩，考查频率分布直方图和中位数',
  '考查导数与函数零点个数，参数要分情况讨论',
  '以《九章算术》里的问题为背景，考查等差数列',
  '题干简洁，不超过两行，适合课堂上口头提问',
  '数据取整数，答案是整数或简单分数，方便口算核对',
];
const EXAMPLE_KEY = 'mingti.last-example';
let example = '';
function pickExample() {
  const last = readStorage(localStorage, EXAMPLE_KEY);
  const pool = EXAMPLES.filter((text) => text !== last);
  example = pool[Math.floor(Math.random() * pool.length)];
  writeStorage(localStorage, EXAMPLE_KEY, example);
}
pickExample(); // 每次打开页面先挑一条
const PLACEHOLDER = {
  get idle() { return `补充要求写在这里，比如：${example}`; },
  chat: '接着出下一道：可以先改题型、难度，再写新的要求；不写也行',
};
const TYPE_NOTE = {
  single_choice: '四个选项，只有一个正确',
  multiple_choice: '两个或以上正确选项',
  fill_blank: '只写结果，可以多空',
  open_response: '写出完整过程，可以分小问',
};
const LEVEL_NOTE = {
  1: '概念直接应用，一两步做完',
  2: '常规方法，计算量不大',
  3: '需要转化或分类，计算中等',
  4: '综合几个知识点，有思维量',
  5: '压轴难度，需要构造或深入讨论',
};

const state = {
  tree: [],
  kpList: [],
  nodes: new Map(),
  blueprints: [],
  models: [],
  defaultModel: null,
  health: { ok: false, text: '正在连接模型' },
  form: { topic: null, qtype: 'single_choice', difficulty: 3, grade: '', purpose: '课堂练习', mode: 'fast', thinking: 'high', count: 1 },
  blueprint: null,
  mode: 'idle',
  chat: null, // 当前打开的对话 { id, turns }
  focus: null, // 右栏正在显示哪一轮
  job: null, // 正在出的题 { id, chat }，同一时间只出一道
  liveSnapshot: null,
  snapshotAt: 0,
  pollTimer: null,
  replay: null, // { turn, timer }
  revealed: new Set(),
  sessions: [],
  history: [],
  finder: { volume: null, chapter: null, query: '' },
  ready: false,
};

const ui = {
  body: document.body,
  composer: $('#composer'),
  prompt: $('#prompt'),
  heroSlot: $('#heroSlot'),
  heroHello: $('#heroHello'),
  heroAlert: $('#heroAlert'),
  dock: $('#dock'),
  hero: $('#hero'),
  conversation: $('#conversation'),
  experiment: $('#experiment'),
  threadScroll: $('#threadScroll'),
  sheetPanel: $('#sheetPanel'),
  sheetScroll: $('#sheetScroll'),
  sheetTabs: $('#sheetTabs'),
  sheetHeading: $('#sheetHeading'),
  sheetNote: $('#sheetNote'),
  panelActions: $('#panelActions'),
  copy: $('#copyBtn'),
  bankView: $('#bankView'),
  paperView: $('#paperView'),
  bankCount: $('#bankCount'),
  paperCount: $('#paperCount'),
  historyList: $('#historyList'),
  popover: $('#popover'),
  send: $('#sendBtn'),
  taskLine: $('#taskLine'),
  chips: {
    kp: $('#kpChip'),
    type: $('#typeChip'),
    level: $('#levelChip'),
    count: $('#countChip'),
    use: $('#useChip'),
    model: $('#modelChip'),
  },
};

// 右栏：出题时放题目，题库里放选中的题，组卷时放试卷预览
const panel = {
  body: ui.sheetScroll,
  open(reveal = false) {
    ui.body.classList.add('has-sheet');
    if (reveal) ui.body.classList.add('sheet-open');
    ui.sheetPanel.hidden = false;
  },
  close() {
    ui.body.classList.remove('has-sheet', 'sheet-open');
    ui.sheetPanel.hidden = true;
  },
  header({ heading, note = '', actions = [] }) {
    ui.sheetHeading.textContent = heading;
    ui.sheetNote.textContent = note;
    ui.sheetTabs.innerHTML = '';
    ui.copy.hidden = true;
    ui.panelActions.replaceChildren(...actions);
    ui.sheetScroll.classList.remove('framed');
    if (state.focus) state.focus.sheetKey = '';
  },
  message(title, lines) {
    ui.sheetScroll.replaceChildren(sheetMessage(title, lines));
  },
  frame(node) {
    ui.sheetScroll.classList.add('framed');
    if (node.parentNode !== ui.sheetScroll) ui.sheetScroll.replaceChildren(node);
  },
};

const shell = {
  panel,
  setMode: (mode) => setMode(mode),
  setCounts,
  refreshCounts,
  bankItem: (id) => library.item(id),
  openPaper: () => papers.open(),
  newPaper: (ids) => papers.create(ids),
  addToPaper: (ids) => papers.add(ids),
  paperChanged: () => papers.refresh(),
};
const library = new Library(shell);
const papers = new Papers(shell);

function setCounts(counts) {
  if (!counts) return;
  ui.bankCount.textContent = counts.bank ? String(counts.bank) : '';
  ui.paperCount.textContent = counts.papers ? String(counts.papers) : '';
}

async function refreshCounts() {
  try {
    setCounts(await request('/api/counts'));
  } catch {
    // 数字只是提示，读不到就先不显示
  }
}

// ---------- 知识树 ----------

function indexTree(tree) {
  state.nodes = new Map();
  state.kpList = [];
  for (const volume of tree) {
    state.nodes.set(volume.id, { node: volume, path: [volume] });
    for (const chapter of volume.children) {
      state.nodes.set(chapter.id, { node: chapter, path: [volume, chapter] });
      for (const section of chapter.children) {
        state.nodes.set(section.id, { node: section, path: [volume, chapter, section] });
        for (const kp of section.children) {
          state.nodes.set(kp.id, { node: kp, path: [volume, chapter, section, kp] });
          state.kpList.push({ kp, volume, chapter, section, key: normalize(kp.name), sectionKey: normalize(section.name) });
        }
      }
    }
  }
}

function topicFrom(id) {
  const entry = state.nodes.get(id);
  if (!entry) return null;
  const { node, path } = entry;
  return {
    kind: node.type === 'knowledge_point' ? 'kp' : 'section',
    id: node.id,
    name: node.name,
    label: path.map((part) => part.name).join(' › '),
    grade: path[0].grade || '',
  };
}

function topicByName(name) {
  for (const [id, entry] of state.nodes) {
    if (entry.path.length >= 3 && entry.node.name === name) return topicFrom(id);
  }
  return null;
}

// ---------- 出题要求 ----------

// 解析详略不再写进出题要求：题目通过检查后由整理员按它改写解析（见后端 answer_key.py）
function settingsConstraints() {
  const standing = profile().defaults.standing_request?.trim();
  return standing ? [standing] : [];
}

const GRADE_SCOPE = { 高一: '高一（必修第一、二册）', 高二: '高二（选择性必修）', 高三: '高三复习（高中全部内容）' };

// 最近出过的几个考点，没选考点时提醒模型避开，免得连着出同一类题
function recentTopics() {
  const names = [];
  for (const record of state.history) {
    const name = (record.knowledge || '').split(' › ').pop();
    if (name && !names.includes(name)) names.push(name);
    if (names.length >= 6) break;
  }
  return names;
}

function topicHint(grade, qtype) {
  const recent = recentTopics();
  return `考点不限：请在${GRADE_SCOPE[grade] || '高中'}的范围里选一个适合出${TYPE_LABEL[qtype]}的考点`
    + `${recent.length ? `，尽量避开最近出过的：${recent.join('、')}` : ''}。`;
}

// extra：追问时附加的要求（比如"别和上一道重复"），不放进输入框。
// 只选了题型、难度、年级和用途也能出题：没选考点也没写要求时，由模型在这个年级的范围里挑考点。
function composeRequest({ extra = '', followUp = '' } = {}) {
  const f = state.form;
  const p = profile();
  const topic = f.topic;
  const grade = f.grade || topic?.grade || null;
  let text = `请出一道${grade || '高中'}数学${TYPE_LABEL[f.qtype]}`;
  if (topic) text += `，考查「${topic.name}」`;
  text += `，难度 ${f.difficulty}/5（${DIFFICULTY_LABEL[f.difficulty]}），用于${f.purpose}。`;
  const note = ui.prompt.value.trim();
  const openTopic = !topic && !note;
  const constraints = [note, ...settingsConstraints(), openTopic ? topicHint(grade, f.qtype) : '', extra].filter(Boolean).join('\n');
  return {
    ready: true,
    openTopic,
    brief: {
      request_text: text,
      grade,
      curriculum_node_id: topic?.kind === 'kp' ? topic.id : null,
      knowledge_point_query: topic?.name || null,
      question_type: f.qtype,
      target_difficulty: f.difficulty,
      teaching_purpose: f.purpose,
      additional_constraints: constraints.slice(0, 2000),
    },
    meta: {
      knowledge_label: topic?.label || '',
      topic_id: topic?.id || null,
      open_topic: openTopic,
      note,
      follow_up: followUp || null,
      detail: p.defaults.solution_detail,
      source: state.blueprint ? `blueprint:${state.blueprint.id}` : followUp ? 'follow-up' : 'manual',
      edition: profile().textbook || 'pep_a', // 教材版本：题目的知识点路径按这一版的册、章、节写
    },
    config: {
      ...agentConfig(f.mode, { ...p.agent, model: mainModel()?.id }, state.models, state.defaultModel),
      thinking: thinkingFor(f.difficulty, f.thinking),
    },
  };
}

// 主模型：输入框里选的 → 个人设置里的 → 第一个可用的
function mainModel() {
  const find = (id) => state.models.find((m) => m.id === id);
  return find(state.form.model) || find(profile().agent.model) || find(state.defaultModel) || state.models[0] || null;
}

// 显示接入的模型本身的名字（比如 deepseek-flash、glm-5.3），不加说明
function modelTitle(model) {
  return model ? model.model : '没有可用的模型';
}

function setChip(chip, parts, isSet = false) {
  chip.innerHTML = '';
  for (const part of parts) chip.append(typeof part === 'string' ? document.createTextNode(part) : part);
  chip.insertAdjacentHTML('beforeend', svg('chevron').replace('<svg ', '<svg class="caret" '));
  chip.classList.toggle('set', isSet);
}

function dots(level) {
  const wrap = el('span', 'pip-dots');
  for (let i = 1; i <= 5; i += 1) wrap.append(el('i', i <= level ? 'on' : ''));
  return wrap;
}

function refreshComposer() {
  const f = state.form;
  const topicName = f.topic && f.topic.name.length > 10 ? `${f.topic.name.slice(0, 10)}…` : f.topic?.name;
  setChip(ui.chips.kp, [f.topic ? `考查 ${topicName}` : '考点不限'], Boolean(f.topic));
  ui.chips.kp.title = f.topic?.label || '可以不选：不选时按年级挑一个考点；想指定就点这里选';
  setChip(ui.chips.type, [TYPE_LABEL[f.qtype]]);
  setChip(ui.chips.level, [dots(f.difficulty), DIFFICULTY_LABEL[f.difficulty]]);
  setChip(ui.chips.count, [`出 ${f.count} 道`], f.count > 1);
  ui.chips.count.title = '一次出几道：同样的要求一次出好几道，每道都单独审核';
  const grade = f.grade || f.topic?.grade || '';
  setChip(ui.chips.use, [`${grade ? `${grade}，` : ''}${f.purpose}`]);
  // 模型和思考强度合在一个按钮上："deepseek-flash · 思考 高"；"思考"和档位分开写、颜色深浅不同，
  // 免得"思考中"读成"正在思考"。4、5 星的题至少用高：停在高这一档时前面加一把小锁，表示不能再低
  const model = mainModel();
  const thinking = thinkingFor(f.difficulty, f.thinking);
  const hard = f.difficulty >= FORCE_THINKING_FROM;
  const think = el('span', 'chip-think');
  if (hard && thinking === 'high') think.innerHTML = svg('lock').replace('<svg ', '<svg class="lock" ');
  think.append(el('span', '', '思考'), el('b', '', THINKING[thinking].name));
  setChip(ui.chips.model, model ? [modelTitle(model), think] : [modelTitle(model)]);
  ui.chips.model.title = !model ? '还没有可用的模型'
    : hard ? `切换模型和思考强度；${DIFFICULTY_LABEL[f.difficulty]}题至少用高思考强度` : '切换模型和思考强度';

  const { brief, openTopic } = composeRequest();
  ui.taskLine.innerHTML = '';
  const many = f.count > 1;
  ui.taskLine.append(
    document.createTextNode('将发送：'),
    el('b', '', many ? brief.request_text.replace(/^请出一道/, `请出 ${f.count} 道`) : brief.request_text),
  );
  if (many) ui.taskLine.append(document.createTextNode('每道题单独出、单独审核，互不重复。'));
  if (openTopic) ui.taskLine.append(document.createTextNode('考点不限，会在这个年级的范围里挑一个。'));
  if (settingsConstraints().length) ui.taskLine.append(document.createTextNode('另附个人设置里的要求。'));
  const busy = Boolean(state.job);
  ui.send.disabled = busy || !state.models.length;
  // 要发的内容不再单独占一行，鼠标停在发送按钮上能看到
  ui.send.title = !state.models.length ? '还没有可用的模型' : busy ? '上一道题还在出' : `出题：${ui.taskLine.textContent.replace(/^将发送：/, '')}`;
}

// 输入框里的选择只在这次打开期间记住；下次打开按个人设置里的偏好重新预选
function saveForm() {
  const f = state.form;
  writeStorage(sessionStorage, FORM_KEY, { ...f, topic: f.topic?.id || null, prompt: ui.prompt.value });
}

// before：改动前的个人设置；给了就只同步真正变了的几项，没给就全部按设置来
function formFromProfile(before = null) {
  const p = profile();
  const f = state.form;
  const changed = (pick) => !before || pick(p) !== pick(before);
  if (changed((x) => x.defaults.question_type)) f.qtype = p.defaults.question_type;
  if (changed((x) => x.defaults.difficulty)) f.difficulty = p.defaults.difficulty;
  if (changed((x) => x.defaults.purpose)) f.purpose = p.defaults.purpose;
  if (changed((x) => x.grade)) f.grade = p.grade || '';
  if (changed((x) => x.agent.mode)) f.mode = p.agent.mode;
  if (changed((x) => x.agent.model)) f.model = p.agent.model || null;
  if (changed((x) => x.agent.thinking)) f.thinking = THINKING[p.agent.thinking] ? p.agent.thinking : 'high';
}

function restoreForm() {
  formFromProfile();
  const saved = readStorage(sessionStorage, FORM_KEY);
  if (!saved) return;
  Object.assign(state.form, {
    qtype: TYPE_LABEL[saved.qtype] ? saved.qtype : state.form.qtype,
    difficulty: Number(saved.difficulty) || state.form.difficulty,
    grade: GRADES.includes(saved.grade) || saved.grade === '' ? saved.grade : state.form.grade,
    purpose: PURPOSES.includes(saved.purpose) ? saved.purpose : state.form.purpose,
    mode: MODES[saved.mode] ? saved.mode : state.form.mode,
    thinking: THINKING[saved.thinking] ? saved.thinking : state.form.thinking,
    topic: saved.topic ? topicFrom(saved.topic) : null,
  });
  ui.prompt.value = saved.prompt || '';
}

function autosize() {
  ui.prompt.style.height = 'auto';
  ui.prompt.style.height = `${Math.min(160, ui.prompt.scrollHeight)}px`;
}

function afterFormChange() {
  refreshComposer();
  saveForm();
}

// ---------- 弹出菜单 ----------

const pop = { kind: null, anchor: null };

function openPopover(kind, anchor) {
  if (pop.kind === kind) {
    closePopover();
    return;
  }
  closePopover();
  pop.kind = kind;
  pop.anchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  renderPopover();
  ui.popover.hidden = false;
  positionPopover();
  const focusable = ui.popover.querySelector('input, [aria-checked="true"], button');
  focusable?.focus({ preventScroll: true });
}

function closePopover() {
  if (!pop.kind) return;
  pop.anchor?.setAttribute('aria-expanded', 'false');
  ui.popover.hidden = true;
  ui.popover.innerHTML = '';
  pop.kind = null;
  pop.anchor = null;
}

function positionPopover() {
  if (!pop.anchor) return;
  const anchor = pop.anchor.getBoundingClientRect();
  const box = ui.popover.getBoundingClientRect();
  let left = Math.min(anchor.left, window.innerWidth - box.width - 12);
  left = Math.max(12, left);
  let top = anchor.bottom + 8;
  if (top + box.height > window.innerHeight - 12) top = Math.max(12, anchor.top - box.height - 8);
  ui.popover.style.left = `${left}px`;
  ui.popover.style.top = `${top}px`;
}

function renderPopover() {
  ui.popover.innerHTML = '';
  const builders = {
    kp: finderPopover, type: typePopover, level: levelPopover, count: countPopover, use: usePopover, model: modelPopover,
  };
  ui.popover.append(builders[pop.kind]());
}

function menuItem({ name, note, checked, onPick, lead, disabled = false }) {
  const item = el('button', 'menu-item');
  item.type = 'button';
  item.disabled = disabled;
  item.setAttribute('role', 'menuitemradio');
  item.setAttribute('aria-checked', String(Boolean(checked)));
  const check = el('span', 'check');
  if (checked) check.innerHTML = svg('check');
  item.append(check);
  const label = el('span', 'name');
  if (lead) label.append(lead, document.createTextNode(' '));
  label.append(document.createTextNode(name));
  item.append(label);
  if (note) item.append(el('span', 'note', note));
  item.addEventListener('click', onPick);
  return item;
}

function typePopover() {
  const wrap = el('div');
  wrap.append(el('p', 'pop-title', '题型'));
  for (const [value, label] of Object.entries(TYPE_LABEL)) {
    wrap.append(menuItem({
      name: label,
      note: TYPE_NOTE[value],
      checked: state.form.qtype === value,
      onPick: () => { state.form.qtype = value; afterFormChange(); closePopover(); },
    }));
  }
  return wrap;
}

function levelPopover() {
  const wrap = el('div');
  wrap.append(el('p', 'pop-title', '难度'));
  for (let level = 1; level <= 5; level += 1) {
    wrap.append(menuItem({
      name: `${level} ${DIFFICULTY_LABEL[level]}`,
      note: LEVEL_NOTE[level],
      checked: state.form.difficulty === level,
      lead: dots(level),
      onPick: () => { state.form.difficulty = level; afterFormChange(); closePopover(); },
    }));
  }
  return wrap;
}

// 一次出几道：多于 1 道时走批量出题，同样的要求一道道出，后面的题避开前面已经写出的
const COUNT_NOTE = { 1: '一道题，出题过程逐步显示', 3: '一组小练习', 5: '一节课的随堂练习', 10: '一份专题练习', 20: '一个单元的题量；用时较长' };
function countPopover() {
  const wrap = el('div');
  wrap.append(el('p', 'pop-title', '一次出几道'));
  for (const count of [1, 3, 5, 10, 20]) {
    wrap.append(menuItem({
      name: `${count} 道`,
      note: COUNT_NOTE[count],
      checked: state.form.count === count,
      onPick: () => { state.form.count = count; afterFormChange(); closePopover(); },
    }));
  }
  return wrap;
}

function usePopover() {
  const wrap = el('div', 'pop-section');
  wrap.style.minWidth = '320px';
  const auto = state.form.topic?.grade ? `按考点（${state.form.topic.grade}）` : '按考点';
  wrap.append(
    el('p', 'pop-title', '年级'),
    segmented([{ value: '', label: auto }, ...GRADES.map((g) => ({ value: g, label: g }))], state.form.grade, (value) => {
      state.form.grade = value;
      afterFormChange();
    }, '年级'),
    el('p', 'pop-title', '用途'),
    segmented(PURPOSES.map((p) => ({ value: p, label: p })), state.form.purpose, (value) => {
      state.form.purpose = value;
      afterFormChange();
    }, '用途'),
  );
  return wrap;
}

// 分档滑块：几档等距排开，可以拖动圆钮、点轨道或下面的字，也能用方向键；
// 拖动时圆钮跟着手走，松开后吸到最近的一档。min 以下的档选不了：字和圆点变淡，拖过去会停在 min。
// onPick(i) 在档位变了的时候调用（拖动途中也会）
function levelSlider({ names, index, label, min = 0, onPick }) {
  const last = names.length - 1;
  const clamp = (i) => Math.max(min, Math.min(last, i));
  const wrap = el('div', 'level-slider');
  const rail = el('div', 'level-rail');
  rail.tabIndex = 0;
  rail.setAttribute('role', 'slider');
  rail.setAttribute('aria-label', label);
  rail.setAttribute('aria-valuemin', String(min));
  rail.setAttribute('aria-valuemax', String(last));
  const fill = el('span', 'level-fill');
  const ticks = names.map((_, i) => {
    const tick = el('i', i < min ? 'off' : '');
    tick.style.left = `${(i / last) * 100}%`;
    return tick;
  });
  const thumb = el('span', 'level-thumb');
  rail.append(fill, ...ticks, thumb);
  const stops = el('div', 'level-stops');
  let current = index;
  // at：圆钮的位置（0 到 1）；i：对应的档
  const show = (at, i) => {
    rail.style.setProperty('--at', String(at));
    ticks.forEach((tick, k) => tick.classList.toggle('on', k / last <= at + 0.001));
    buttons.forEach((button, k) => button.classList.toggle('on', k === i));
    rail.setAttribute('aria-valuenow', String(i));
    rail.setAttribute('aria-valuetext', names[i]);
  };
  const choose = (i) => {
    if (i === current) return;
    current = i;
    onPick(i);
  };
  const pick = (i) => {
    const target = clamp(i);
    show(target / last, target);
    choose(target);
  };
  const buttons = names.map((name, i) => {
    const button = el('button', '', name);
    button.type = 'button';
    button.tabIndex = -1;
    button.disabled = i < min;
    button.style.left = `${(i / last) * 100}%`;
    button.addEventListener('click', () => {
      pick(i);
      rail.focus({ preventScroll: true }); // 点了字以后，方向键接着能调
    });
    stops.append(button);
    return button;
  });
  show(current / last, current);
  const ratio = (event) => {
    const box = rail.getBoundingClientRect();
    return Math.max(min / last, Math.min(1, (event.clientX - box.left) / box.width));
  };
  rail.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    rail.focus({ preventScroll: true });
    rail.setPointerCapture(event.pointerId);
    pick(Math.round(ratio(event) * last)); // 按下：直接跳到最近的一档（带过渡）
    const move = (e) => {
      wrap.classList.add('dragging'); // 拖动：圆钮跟手，不要过渡
      const at = ratio(e);
      const i = clamp(Math.round(at * last));
      show(at, i);
      choose(i);
    };
    const end = (e) => {
      rail.removeEventListener('pointermove', move);
      rail.removeEventListener('pointerup', end);
      rail.removeEventListener('pointercancel', end);
      wrap.classList.remove('dragging');
      pick(e.type === 'pointercancel' ? current : Math.round(ratio(e) * last)); // 松开：吸到最近的一档
    };
    rail.addEventListener('pointermove', move);
    rail.addEventListener('pointerup', end);
    rail.addEventListener('pointercancel', end);
  });
  rail.addEventListener('keydown', (event) => {
    const step = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[event.key];
    const to = step !== undefined ? current + step : event.key === 'Home' ? min : event.key === 'End' ? last : null;
    if (to === null) return;
    event.preventDefault();
    pick(to);
  });
  wrap.append(rail, stops);
  return wrap;
}

// 思考强度：低、中、高、极高四档，在模型菜单里用滑块调。4、5 星的题至少用高：低、中选不了，
// 可以在高和极高之间切换；改回 3 星及以下，又能选低、中
function thinkingSection() {
  const section = el('div', 'think-section');
  const levels = Object.keys(THINKING);
  const difficulty = state.form.difficulty;
  const hard = difficulty >= FORCE_THINKING_FROM;
  const current = thinkingFor(difficulty, state.form.thinking);
  // 各档的说明叠在同一格里、只显示当前这档：换档时菜单高度不变，滑块不会跟着上下跳
  const notes = el('div', 'think-notes');
  const noteNodes = levels.map((level) => notes.appendChild(el('p', '', THINKING[level].note)));
  const showNote = (level) => noteNodes.forEach((node, i) => node.classList.toggle('on', levels[i] === level));
  showNote(current);
  const slider = levelSlider({
    names: levels.map((level) => THINKING[level].name),
    index: levels.indexOf(current),
    label: '思考强度',
    min: hard ? levels.indexOf('high') : 0,
    onPick: (i) => {
      state.form.thinking = levels[i];
      showNote(levels[i]);
      afterFormChange();
    },
  });
  section.append(el('p', 'pop-title', '思考强度'), slider, notes);
  if (hard) {
    const hint = el('p', 'pop-hint think-lock');
    hint.innerHTML = svg('lock');
    hint.append(`难度为${DIFFICULTY_LABEL[difficulty]}（${difficulty} 星）时至少用高，可以选高或极高：这类题最容易算错、反复返工。`);
    section.append(hint);
  }
  return section;
}

// 模型菜单：上面选模型（接了几家就列出所有模型，选中的记成主模型），下面用滑块调思考强度；
// 选好的模型名和思考强度一起显示在输入框右下角
function modelPopover() {
  const wrap = el('div', 'model-menu');
  wrap.append(el('p', 'pop-title', '模型'));
  // 只列模型的名字（接入的是什么就显示什么），不加说明
  const current = mainModel();
  for (const m of state.models) {
    wrap.append(menuItem({
      name: m.model,
      checked: m.id === current?.id,
      onPick: () => {
        state.form.model = m.id;
        afterFormChange();
        closePopover();
        updateAgent({ model: m.id });
      },
    }));
  }
  if (!state.models.length) wrap.append(el('p', 'pop-empty', '还没有可用的模型'));
  wrap.append(el('div', 'pop-sep'), thinkingSection(), el('div', 'pop-sep'));
  const foot = el('div', 'pop-foot');
  const add = el('button', 'link-btn', '接入更多模型');
  add.type = 'button';
  add.addEventListener('click', () => { closePopover(); openSettings('models'); });
  foot.append(add);
  wrap.append(foot);
  return wrap;
}

function finderPopover() {
  const wrap = el('div', 'finder');
  const search = el('input', 'finder-search');
  search.type = 'search';
  search.placeholder = '搜索知识点，比如：椭圆、导数、条件概率';
  search.setAttribute('aria-label', '搜索知识点');
  search.value = state.finder.query;
  wrap.append(search);
  const body = el('div');
  wrap.append(body);
  const foot = el('div', 'finder-foot');
  foot.append(el('span', '', state.form.topic ? `当前：${state.form.topic.label}` : '不选也可以：会在这个年级的范围里挑一个考点'));
  const clear = el('button', 'link-btn', '不限考点');
  clear.type = 'button';
  clear.addEventListener('click', () => { state.form.topic = null; afterFormChange(); closePopover(); });
  foot.append(clear);
  wrap.append(foot);

  const pick = (id) => {
    state.form.topic = topicFrom(id);
    state.blueprint = null;
    state.finder.query = '';
    afterFormChange();
    closePopover();
  };

  const renderBody = () => {
    body.innerHTML = '';
    const query = normalize(search.value);
    state.finder.query = search.value;
    if (query) {
      const hits = state.kpList.filter((item) => item.key.includes(query) || item.sectionKey.includes(query)).slice(0, 40);
      const list = el('ul', 'finder-results');
      if (!hits.length) list.append(el('li', 'finder-empty', '没找到这个知识点。换个说法试试，或者直接写在输入框里。'));
      for (const hit of hits) {
        const button = el('button');
        button.type = 'button';
        button.append(el('span', '', hit.kp.name), el('small', '', `${hit.volume.name} › ${hit.chapter.name} › ${hit.section.name}`));
        button.addEventListener('click', () => pick(hit.kp.id));
        const item = el('li');
        item.append(button);
        list.append(item);
      }
      body.append(list);
      return;
    }
    const current = state.form.topic ? state.nodes.get(state.form.topic.id)?.path : null;
    const volume = state.tree.find((v) => v.id === state.finder.volume) || (current && current[0]) || state.tree[0];
    if (!volume) return;
    state.finder.volume = volume.id;
    const chapter = volume.children.find((c) => c.id === state.finder.chapter) || (current && current[0] === volume && current[1]) || volume.children[0];
    state.finder.chapter = chapter.id;

    const cols = el('div', 'finder-cols');
    const volCol = el('div', 'finder-col');
    for (const v of state.tree) {
      const b = el('button', 'finder-item', v.name);
      b.type = 'button';
      b.setAttribute('aria-current', String(v.id === volume.id));
      b.addEventListener('click', () => { state.finder.volume = v.id; state.finder.chapter = null; renderBody(); });
      volCol.append(b);
    }
    const chCol = el('div', 'finder-col');
    for (const c of volume.children) {
      const b = el('button', 'finder-item', c.name);
      b.type = 'button';
      b.setAttribute('aria-current', String(c.id === chapter.id));
      b.addEventListener('click', () => { state.finder.chapter = c.id; renderBody(); });
      chCol.append(b);
    }
    const secCol = el('div', 'finder-col');
    for (const section of chapter.children) {
      const block = el('div', 'finder-section');
      const title = el('h5');
      title.append(el('span', '', section.name));
      const whole = el('button', 'link-btn', '整节');
      whole.type = 'button';
      whole.title = `考查「${section.name}」整节内容`;
      whole.addEventListener('click', () => pick(section.id));
      title.append(whole);
      const chips = el('div', 'kp-chips');
      for (const kp of section.children) {
        const chip = el('button', 'kp-chip', kp.name);
        chip.type = 'button';
        chip.setAttribute('aria-pressed', String(state.form.topic?.id === kp.id));
        chip.addEventListener('click', () => pick(kp.id));
        chips.append(chip);
      }
      block.append(title, chips);
      secCol.append(block);
    }
    cols.append(volCol, chCol, secCol);
    body.append(cols);
  };
  search.addEventListener('input', () => { renderBody(); positionPopover(); });
  renderBody();
  return wrap;
}

// ---------- 模拟卷里的命题要求 ----------

function shuffleSuggestions() {
  const pool = [...state.blueprints];
  for (let i = pool.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  const list = $('#suggestList');
  list.innerHTML = '';
  for (const spec of pool.slice(0, 4)) {
    const button = el('button', 'suggestion');
    button.type = 'button';
    button.append(
      el('span', 'where', spec.type_label),
      el('span', 'topic', spec.topic),
      el('span', 'use', '用这条'),
    );
    button.addEventListener('click', () => applyBlueprint(spec));
    const item = el('li');
    item.append(button);
    list.append(item);
  }
}

function applyBlueprint(spec) {
  state.blueprint = spec;
  state.form.qtype = spec.question_type;
  state.form.difficulty = spec.target_difficulty;
  const topicKey = normalize(spec.topic);
  const match = state.kpList
    .filter((item) => item.key.length >= 2 && topicKey.includes(item.key))
    .sort((a, b) => b.key.length - a.key.length)[0];
  state.form.topic = match ? topicFrom(match.kp.id) : null;
  ui.prompt.value = `题目内容：${spec.topic}`;
  autosize();
  $('#blueprintDialog').close();
  afterFormChange();
  ui.prompt.focus();
  toast('已填入这条命题要求');
}

function renderBlueprintList() {
  const query = normalize($('#bpSearch').value);
  const type = $('#bpType').value;
  const list = $('#bpList');
  list.innerHTML = '';
  const items = state.blueprints.filter((s) => (!type || s.question_type === type) && (!query || normalize(s.topic).includes(query)));
  if (!items.length) list.append(el('li', 'bp-empty', '没有符合条件的要求，换个关键词试试。'));
  for (const spec of items) {
    const button = el('button');
    button.type = 'button';
    button.append(el('span', 'kind', spec.type_label), el('span', 'topic', spec.topic), dots(spec.target_difficulty));
    button.addEventListener('click', () => applyBlueprint(spec));
    const item = el('li');
    item.append(button);
    list.append(item);
  }
}

// ---------- 视图切换 ----------

function setMode(mode) {
  const previous = state.mode;
  state.mode = mode;
  closePopover();
  const idle = mode === 'idle';
  const chat = mode === 'chat';
  if (idle && previous !== 'idle') pickExample(); // 回到起始页时换一条示例
  ui.body.classList.toggle('is-idle', idle);
  ui.hero.hidden = !idle;
  ui.conversation.hidden = !chat;
  ui.experiment.hidden = mode !== 'experiment';
  ui.bankView.hidden = mode !== 'bank';
  ui.paperView.hidden = mode !== 'papers';
  ui.dock.hidden = !(idle || chat);
  (idle ? ui.heroSlot : ui.dock).append(ui.composer);
  ui.prompt.placeholder = chat ? PLACEHOLDER.chat : PLACEHOLDER.idle;
  // 题库和试卷自己决定右栏放什么；对话里右栏放题目
  if (chat) {
    panel.open();
    ui.sheetHeading.textContent = '题目';
    ui.panelActions.replaceChildren();
    ui.sheetScroll.classList.remove('framed');
    if (state.focus) {
      state.focus.sheetKey = '';
      renderSheet(state.focus, { animate: false });
    }
  } else if (mode !== 'bank' && mode !== 'papers') {
    panel.close();
  }
  closeRail();
  $('#experimentLink').setAttribute('aria-current', mode === 'experiment' ? 'page' : 'false');
  $('#bankLink').setAttribute('aria-current', mode === 'bank' ? 'page' : 'false');
  $('#papersLink').setAttribute('aria-current', mode === 'papers' ? 'page' : 'false');
  if (!chat) stopReplay();
  renderSessions();
}

function renderGreeting() {
  const name = profile().display_name;
  ui.heroHello.innerHTML = '';
  ui.heroHello.append(`${greeting()}${name ? `，${name}` : ''}`);
  if (!name) {
    const link = el('button', 'link-btn', '告诉我怎么称呼你');
    link.type = 'button';
    link.style.marginLeft = '12px';
    link.addEventListener('click', () => openSettings('profile'));
    ui.heroHello.append(link);
  }
}

// ---------- 侧栏：宽屏可以收起；窄屏是从左边滑出来的抽屉，点空白处收回 ----------

const narrow = window.matchMedia('(max-width: 1180px)');

function openRail() {
  if (narrow.matches) {
    ui.body.classList.add('rail-open');
  } else {
    ui.body.classList.remove('rail-collapsed');
    writeStorage(localStorage, RAIL_KEY, null);
  }
}

function closeRail() {
  ui.body.classList.remove('rail-open');
}

function hideRail() {
  if (narrow.matches) {
    closeRail();
  } else {
    ui.body.classList.add('rail-collapsed');
    writeStorage(localStorage, RAIL_KEY, true);
  }
}

// ---------- 对话：一个对话里可以连着出好几道题，每道题是一轮 ----------

const turnActions = {
  models: () => state.models,
  pickLane: (turn, lane) => {
    turn.focusLane = lane;
    turn.sheetKey = '';
    if (state.focus !== turn) focusTurn(turn);
    renderTurn(turn, { animate: false });
  },
  showSheet: (turn) => focusTurn(turn, true),
  followUp: (turn, kind) => followUp(turn, kind),
  openBank: (turn) => openBank(turn.sourceId),
  stop: (turn) => stopJob(turn),
  replay: (turn) => replay(turn),
};

// 批量出题那一轮的按钮
const batchActions = {
  models: () => state.models,
  stop: (turn) => stopJob(turn),
  showItem: (turn, index) => showBatchItem(turn, index),
  followUp: (turn) => batchAgain(turn),
  makePaper: (turn) => batchPaper(turn),
  openBank: (turn) => openBank([...turn.records.values()].find((r) => r.status === 'succeeded')?.id || null),
};

function addBatchTurn() {
  const turn = new BatchTurn(batchActions);
  state.chat.turns.push(turn);
  ui.conversation.append(turn.root);
  ui.conversation.classList.toggle('multi', state.chat.turns.length > 1);
  return turn;
}

// 右栏显示批量里的第 index 道：出完的按出题记录，还在出的读这道题的实时进度
async function showBatchItem(turn, index, reveal = true) {
  const source = new BatchItemSource(turn, index);
  const record = turn.records.get(index);
  const item = turn.items().find((one) => one.index === index);
  if (record) source.snapshot = recordSnapshot(record);
  else if (item?.job_id) {
    try {
      source.snapshot = await request(`/api/jobs/${item.job_id}`);
    } catch {
      // 还没开始或已经结束又丢了：右栏先显示"正在构思"
    }
  }
  focusTurn(source, reveal);
}

// 正在出的批量：右栏显示的那一道还在出，就跟着刷新它的进度
async function refreshBatchItem(turn) {
  const source = state.focus;
  if (!source || source.batchTurn !== turn) return;
  const item = turn.items().find((one) => one.index === source.index);
  if (!item?.job_id) return;
  const before = source.snapshot?.status;
  if (!['queued', 'running'].includes(item.status) && before === item.status) return;
  try {
    const snap = await request(`/api/jobs/${item.job_id}`);
    if (state.focus !== source) return;
    source.snapshot = snap;
    renderSheet(source, { animate: before !== snap.status });
  } catch {
    // 下一轮再试
  }
}

const oneLine = (text) => String(text || '').replace(/\$\$/g, '$').replace(/\s*\n+\s*/g, ' ');

// 再出一批：同样的要求，避开这一批已经出好的题
function batchAgain(turn) {
  if (state.job) {
    toast('上一批题还在出，等它结束或先停止');
    return;
  }
  const { brief = {}, meta = {}, config = {} } = turn.request || {};
  loadBrief(brief, meta, config);
  state.form.count = turn.count;
  const stems = turn.items().map((item) => oneLine(item.stem).slice(0, 100)).filter(Boolean).slice(-8);
  const extra = stems.length ? `再出一批新题，不要和下面这些已经出过的题重复：\n${stems.map((s) => `- ${s}`).join('\n')}` : '';
  afterFormChange();
  submit({ extra, followUp: `再出 ${turn.count} 道` });
}

// 把这一批出好的题组成一份试卷
async function batchPaper(turn) {
  const ids = new Set([...turn.records.values()].filter((r) => r.status === 'succeeded').map((r) => r.id));
  try {
    await library.load();
  } catch (error) {
    toast(`题库没读出来：${error.message}`);
    return;
  }
  const bankIds = library.items.filter((item) => ids.has(item.source_id)).map((item) => item.id);
  if (!bankIds.length) {
    toast('题库里没找到这一批的题（可能已经删掉了）');
    return;
  }
  await papers.create(bankIds);
}

function newChatId() {
  return `c_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}

function resetChat(id) {
  stopReplay();
  state.chat = id ? { id, turns: [] } : null;
  state.focus = null;
  ui.conversation.replaceChildren();
  ui.conversation.classList.remove('multi');
  writeStorage(sessionStorage, CHAT_KEY, id || null);
}

function addTurn() {
  const turn = new Turn(turnActions);
  state.chat.turns.push(turn);
  ui.conversation.append(turn.root);
  ui.conversation.classList.toggle('multi', state.chat.turns.length > 1);
  return turn;
}

function liveTurn() {
  if (!state.job || !state.chat) return null;
  return state.chat.turns.find((turn) => turn.jobId === state.job.id) || null;
}

function renderTurn(turn, { animate = true, replaying = false } = {}) {
  turn.render({ live: turn === liveTurn(), replaying });
  if (state.focus === turn) renderSheet(turn, { animate });
}

// 右栏显示哪一道：默认跟着最新的一道；点某一轮的"看题目"就换成那一道
function focusTurn(turn, reveal = false) {
  // 批量那一轮本身不能直接显示：显示它出好的第一道（还没有就第一道）
  if (turn.isBatch) {
    const first = turn.items().find((item) => item.status === 'succeeded') || turn.items()[0];
    if (first) showBatchItem(turn, first.index, reveal);
    return;
  }
  state.focus = turn;
  const owner = turn.batchTurn || turn;
  for (const other of state.chat?.turns || []) other.setFocused(other === owner);
  if (turn.batchTurn) turn.batchTurn.setFocusedItem(turn.index);
  turn.sheetKey = '';
  if (state.mode !== 'chat') return;
  renderSheet(turn, { animate: false });
  if (reveal) panel.open(true);
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    ui.threadScroll.scrollTop = ui.threadScroll.scrollHeight;
  });
}

function renderSheetTabs(turn, shown) {
  ui.sheetTabs.innerHTML = '';
  const lanes = turn.snapshot.lanes || [];
  if (lanes.length < 2) return;
  for (const lane of lanes) {
    const button = el('button', lane.status === 'won' ? 'won' : '', `方案 ${lane.id}`);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(lane.id === shown));
    button.addEventListener('click', () => turnActions.pickLane(turn, lane.id));
    ui.sheetTabs.append(button);
  }
}

function sheetMessage(title, lines, busy = false) {
  const box = el('div', 'sheet-empty');
  box.append(el('h3', '', title));
  for (const line of lines) box.append(el('p', '', line));
  if (busy) box.append(el('div', 'writing'));
  return box;
}

function renderSheet(turn, { animate = true } = {}) {
  if (!turn || state.mode !== 'chat' || state.focus !== turn) return;
  const snap = turn.snapshot;
  const turns = state.chat?.turns || [];
  const prefix = turn.sheetPrefix ?? (turns.length > 1 ? `第 ${turns.indexOf(turn) + 1} 道，` : '');
  const waiting = () => sheetMessage('正在构思题干', ['题干通过审题后会先显示在这里；答案和解析要等解题员写完、复核员核对后再补上。'], true);
  if (!snap) {
    ui.sheetTabs.innerHTML = '';
    ui.copy.hidden = true;
    ui.sheetNote.textContent = `${prefix}正在构思`;
    if (turn.sheetKey !== 'waiting') {
      turn.sheetKey = 'waiting';
      ui.sheetScroll.replaceChildren(waiting());
    }
    return;
  }
  const shown = turn.shownLane();
  renderSheetTabs(turn, shown);
  const several = snap.lanes.length > 1;
  const lane = (snap.lanes || []).find((l) => l.id === shown);
  const showResult = Boolean(snap.result) && (!several || shown === snap.winner);
  let key;
  let build;
  let note = '';
  if (showResult) {
    key = `${snap.id}:result`;
    note = several ? `方案 ${snap.winner}，已通过全部检查` : '已通过全部检查';
    build = () => {
      const reveal = animate && !state.revealed.has(snap.id);
      state.revealed.add(snap.id);
      return buildSheet(snap.result, { brief: snap.brief, meta: snap.meta, reveal });
    };
  } else if (lane && lane.previews.length) {
    const version = lane.previews.length;
    // 这道题（或这个方案）已经不会再往下写了，就说清楚为什么只有题干
    let ended = '';
    if (snap.status === 'failed') ended = `这次没出成：${(legacyText(snap.error) || '没能得到通过全部检查的题').replace(/[。.]$/, '')}。上面是审过的题干，没有答案和解析。`;
    else if (snap.status === 'cancelled') ended = '已经停下，只留下了审过的题干。';
    else if (['stopped', 'failed'].includes(lane.status)) ended = `方案 ${lane.id} 已经停下，没有接着写答案和解析。`;
    key = `${snap.id}:${lane.id}:${version}:${ended ? 'ended' : 'live'}`;
    note = `${several ? `方案 ${lane.id} 的` : ''}题干${version > 1 ? `第 ${version} 版` : ''}${snap.status === 'failed' ? '，没出成' : ''}`;
    build = () => buildSheet(lane.previews[version - 1].statement, { brief: snap.brief, meta: snap.meta, preview: true, ended });
  } else if (snap.status === 'failed' || snap.status === 'cancelled') {
    key = `${snap.id}:${snap.status}`;
    note = snap.status === 'cancelled' ? '已停下' : '没出成';
    build = () => (snap.status === 'cancelled'
      ? sheetMessage('已经停下', ['这次没有生成题目。'])
      : sheetMessage('这次没出成', [legacyText(snap.error) || '没能得到通过全部检查的题。', '可以换一个考点，或者降一档难度再试。']));
  } else {
    key = `${snap.id}:waiting`;
    note = '正在构思';
    build = waiting;
  }
  ui.sheetNote.textContent = `${prefix}${note}`;
  ui.copy.hidden = !showResult;
  if (key === turn.sheetKey) return;
  turn.sheetKey = key;
  ui.sheetScroll.replaceChildren(build());
  renderMath(ui.sheetScroll);
  ui.sheetScroll.scrollTop = 0;
}

// ---------- 出题 ----------

// 在对话里发出的要求接在这个对话后面；在起始页发出的开一个新对话
async function submit(options = {}) {
  if (state.job) {
    toast('上一道题还在出，等它结束或先停止');
    return;
  }
  const payload = composeRequest(options);
  const chatId = state.mode === 'chat' && state.chat ? state.chat.id : newChatId();
  payload.meta.session_id = chatId;
  const count = state.form.count || 1;
  saveForm();
  ui.send.disabled = true;
  try {
    const body = { brief: payload.brief, meta: payload.meta, config: payload.config };
    const job = count > 1
      ? await request('/api/batches', { method: 'POST', body: JSON.stringify({ ...body, count }) })
      : await request('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
    // 像聊天一样，发出去以后输入框清空；题型、难度这些选择留着，接着出下一道
    ui.prompt.value = '';
    autosize();
    saveForm();
    startLive(job.id, job.session_id || chatId, count > 1 ? { count, request: body } : null);
  } catch (error) {
    toast(`没能开始：${error.message}`);
    refreshComposer();
  }
}

// 接着某一道题再出：输入框回到那道题的要求，再按按钮的意思改一处，新题接在这个对话后面
function followUp(turn, kind) {
  const snap = turn.snapshot;
  if (!snap) return;
  if (state.job) {
    toast('上一道题还在出，等它结束或先停止');
    return;
  }
  loadBrief(snap.brief || {}, snap.meta || {}, snap.config || {});
  // 那道题是"考点不限"出的：接着出时沿用它实际考的知识点，不然"难一点""换个情境"会换成别的考点
  if (!state.form.topic && !ui.prompt.value.trim() && snap.result?.curriculum?.node_id) {
    state.form.topic = topicFrom(snap.result.curriculum.node_id);
  }
  const stem = String(snap.result?.stem_plain || snap.result?.stem_latex || '').replace(/\s+/g, ' ').slice(0, 300);
  const previous = stem ? `\n上一道题：${stem}` : '';
  let extra = '';
  let label = '再试一次';
  if (kind === 'again') {
    label = '再出一道同类题';
    extra = stem ? `出一道新题，题目和情境都不要和上一道重复。${previous}` : '';
  } else if (kind === 'harder') {
    state.form.difficulty = Math.min(5, state.form.difficulty + 1);
    label = '难一点';
    extra = stem ? `比上一道题难一档，换一道新题。${previous}` : '';
  } else if (kind === 'easier') {
    state.form.difficulty = Math.max(1, state.form.difficulty - 1);
    label = stem ? '简单一点' : '简单一点再试';
    extra = stem ? `比上一道题简单一档，换一道新题。${previous}` : '';
  } else if (kind === 'context') {
    label = '换个情境';
    extra = `考查内容和难度不变，换一个和上一道不同的情境。${previous}`;
  }
  afterFormChange();
  submit({ extra, followUp: label });
}

function loadBrief(brief, meta, config) {
  const f = state.form;
  if (TYPE_LABEL[brief.question_type]) f.qtype = brief.question_type;
  if (brief.target_difficulty) f.difficulty = brief.target_difficulty;
  if (PURPOSES.includes(brief.teaching_purpose)) f.purpose = brief.teaching_purpose;
  f.grade = GRADES.includes(brief.grade) ? brief.grade : '';
  const topicId = meta.topic_id || brief.curriculum_node_id;
  f.topic = (topicId && topicFrom(topicId)) || (brief.knowledge_point_query ? topicByName(brief.knowledge_point_query) : null);
  if (MODES[config.mode]) f.mode = config.mode;
  state.blueprint = null;
  ui.prompt.value = meta.note ?? (meta.follow_up ? '' : brief.additional_constraints || '');
  autosize();
}

// batch：批量出题时是 { count, request }，正在出的是一整批（state.job.batch 为 true）
function startLive(jobId, chatId, batch = null) {
  state.job = { id: jobId, chat: chatId, batch: Boolean(batch), count: batch?.count || 1 };
  state.liveSnapshot = null;
  writeStorage(sessionStorage, JOB_KEY, state.job);
  if (!state.chat || state.chat.id !== chatId) resetChat(chatId);
  writeStorage(sessionStorage, CHAT_KEY, chatId);
  const turn = batch ? addBatchTurn() : addTurn();
  turn.jobId = jobId;
  if (batch) {
    turn.batchId = jobId;
    turn.count = batch.count;
    turn.request = batch.request;
    turn.render({ live: true });
  }
  setMode('chat');
  if (!batch) focusTurn(turn);
  scrollToBottom();
  setGeometryBusy(true);
  refreshComposer();
  poll();
}

async function poll() {
  clearTimeout(state.pollTimer);
  if (!state.job) return;
  const jobId = state.job.id;
  const batch = state.job.batch;
  try {
    const snap = await request(batch ? `/api/batches/${jobId}` : `/api/jobs/${jobId}`);
    if (state.job?.id !== jobId) return;
    state.liveSnapshot = snap;
    state.snapshotAt = performance.now();
    const turn = liveTurn();
    if (turn) {
      turn.snapshot = snap;
      if (batch) {
        turn.count = snap.count;
        turn.request = turn.request || { brief: snap.brief, meta: snap.meta, config: snap.config };
        turn.render({ live: true });
        // 右栏还没显示这一批的题：第一道写出题干后就显示它
        if (!state.focus?.batchTurn && snap.items.some((item) => item.job_id)) showBatchItem(turn, 1, false);
        else refreshBatchItem(turn);
      } else {
        renderTurn(turn);
      }
    }
    if (snap.status === 'queued' || snap.status === 'running') {
      state.pollTimer = setTimeout(poll, batch ? 1200 : 700);
    } else {
      finishLive(snap);
    }
  } catch (error) {
    if (/任务不存在/.test(error.message)) {
      // 服务重启过，进行中的题丢了：把这一轮拿掉
      const turn = liveTurn();
      if (turn && !turn.snapshot) {
        turn.root.remove();
        state.chat.turns = state.chat.turns.filter((other) => other !== turn);
      } else if (turn) {
        turn.jobId = null;
      }
      state.job = null;
      writeStorage(sessionStorage, JOB_KEY, null);
      setGeometryBusy(false);
      refreshComposer();
      renderSessions();
      return;
    }
    state.pollTimer = setTimeout(poll, 2000);
  }
}

async function finishBatch(snap) {
  const focused = state.focus?.batchTurn ? state.focus.index : null;
  const chatId = state.job.chat;
  state.job = null;
  writeStorage(sessionStorage, JOB_KEY, null);
  setGeometryBusy(false);
  refreshComposer();
  // 每一道都存成了出题记录：重新打开这个对话，这一批就按记录显示（能看题、组卷、进题库）
  if (state.chat?.id === chatId) await openChat(chatId, { quiet: true, focusBatch: { id: snap.id, index: focused } });
  loadSessions();
  refreshCounts();
  const { done, failed } = snap.totals;
  if (snap.status === 'cancelled') toast(`已停下：出好的 ${done} 道已收进题库`);
  else toast(failed ? `出好 ${done} 道，${failed} 道没出成；出好的已收进题库` : `${done} 道题都出好了，已收进题库`);
}

function finishLive(snap) {
  if (state.job?.batch) {
    finishBatch(snap);
    return;
  }
  const turn = liveTurn();
  state.job = null;
  writeStorage(sessionStorage, JOB_KEY, null);
  if (turn) {
    turn.jobId = null;
    turn.recordId = snap.id;
    renderTurn(turn);
  }
  setGeometryBusy(false);
  refreshComposer();
  loadSessions();
  refreshCounts();
  if (snap.status === 'succeeded') toast('题目出好了，已收进题库');
  else if (snap.status === 'cancelled') toast('已停下');
}

async function stopJob(turn) {
  if (!turn.jobId) return;
  try {
    await request(turn.isBatch ? `/api/batches/${turn.jobId}/stop` : `/api/jobs/${turn.jobId}/stop`, { method: 'POST' });
    toast('正在停下：手头这次模型调用结束后就停');
  } catch (error) {
    toast(error.message);
  }
}

function openBank(focus = null) {
  setMode('bank');
  library.show(focus);
}

function openPapers() {
  setMode('papers');
  papers.show();
}

setInterval(() => {
  const turn = liveTurn();
  const snap = state.liveSnapshot;
  if (!turn || !snap || !['queued', 'running'].includes(snap.status)) return;
  turn.tick(snap.elapsed + (performance.now() - state.snapshotAt) / 1000);
}, 250);

// ---------- 历史对话与回放 ----------

async function loadSessions() {
  try {
    [state.sessions, state.history] = await Promise.all([request('/api/sessions'), request('/api/questions')]);
  } catch {
    // 列表读不到就先留着旧的
  }
  renderSessions();
}

function renderSessions() {
  ui.historyList.innerHTML = '';
  const items = [...state.sessions];
  // 新对话里正在出第一道题：服务器上还没有这个对话，先补一条
  if (state.job && !items.some((session) => session.id === state.job.chat)) {
    items.unshift({ id: state.job.chat, count: 0, knowledge: '', updated_at: new Date().toISOString(), fresh: true });
  }
  if (!items.length) {
    ui.historyList.append(el('li', 'history-empty', '出过的题会按对话列在这里。'));
    return;
  }
  for (const session of items) {
    const live = state.job?.chat === session.id;
    const status = !live && session.last_status && session.last_status !== 'succeeded' ? ` ${session.last_status}` : '';
    const button = el('button', `history-item${live ? ' live' : ''}${status}`);
    button.type = 'button';
    button.setAttribute('aria-current', String(state.mode === 'chat' && state.chat?.id === session.id));
    const meta = el('span', 'meta');
    if (live) {
      meta.append(el('span', '', session.count ? `${session.count} 道题，正在出下一道` : '正在出题'));
    } else {
      meta.append(
        el('span', '', session.count > 1 ? `${session.count} 道题` : TYPE_LABEL[session.question_type] || ''),
        el('span', 'num', formatWhen(session.updated_at)),
      );
      if (session.last_status === 'failed') meta.append(el('span', 'flag', '没出成'));
      else if (session.last_status === 'cancelled') meta.append(el('span', 'flag', '已停下'));
    }
    const title = (session.knowledge || '').split(' › ').pop() || (session.fresh ? '新对话' : '考点不限');
    button.append(el('span', 'title', title), meta);
    button.title = session.knowledge || '';
    button.addEventListener('click', () => openChat(session.id));
    const item = el('li');
    item.append(button);
    ui.historyList.append(item);
  }
}

// 打开一个对话：按顺序把每一道题排出来；正在出的那道接在最后
// focusBatch：{ id, index } 打开后右栏显示这一批的第 index 道（批量刚出完时用）
async function openChat(id, { quiet = false, focusBatch = null } = {}) {
  const hasJob = state.job?.chat === id;
  let records = [];
  try {
    records = await request(`/api/sessions/${encodeURIComponent(id)}`);
  } catch (error) {
    if (!hasJob) {
      if (!quiet) toast(error.message);
      writeStorage(sessionStorage, CHAT_KEY, null);
      return false;
    }
  }
  resetChat(id);
  // 同一批出的题合成一轮（按第一道出现的位置）
  const batches = new Map();
  for (const record of records) {
    const batchId = record.request?.meta?.batch_id;
    if (batchId) {
      if (!batches.has(batchId)) batches.set(batchId, addBatchTurn());
      batches.get(batchId).addRecord(record);
      continue;
    }
    const turn = addTurn();
    turn.record = record;
    turn.recordId = record.id;
    turn.snapshot = recordSnapshot(record);
    turn.render();
  }
  const liveBatch = hasJob && state.job.batch;
  if (liveBatch && !batches.has(state.job.id)) {
    const turn = addBatchTurn();
    turn.batchId = state.job.id;
    turn.count = state.job.count || 0;
    batches.set(state.job.id, turn);
  }
  for (const [batchId, turn] of batches) {
    const live = liveBatch && batchId === state.job.id;
    if (live) {
      turn.jobId = batchId;
      if (state.liveSnapshot?.items) {
        turn.snapshot = state.liveSnapshot;
        turn.count = state.liveSnapshot.count;
        turn.request = turn.request || { brief: state.liveSnapshot.brief, meta: state.liveSnapshot.meta, config: state.liveSnapshot.config };
      }
    }
    if (turn.request || turn.snapshot) turn.render({ live });
  }
  if (hasJob && !state.job.batch && !records.some((record) => record.id === state.job.id)) {
    const turn = addTurn();
    turn.jobId = state.job.id;
    if (state.liveSnapshot) {
      turn.snapshot = state.liveSnapshot;
      turn.render({ live: true });
    }
  }
  setMode('chat');
  const target = focusBatch && batches.get(focusBatch.id);
  if (target && focusBatch.index) showBatchItem(target, focusBatch.index, false);
  else {
    const last = target || state.chat.turns[state.chat.turns.length - 1];
    if (last) focusTurn(last);
  }
  scrollToBottom();
  return true;
}

function recordSnapshot(record, t = Infinity) {
  const extra = record.extra || {};
  const req = record.request || {};
  const rawCalls = (record.call_log || []).map((c) => ({ lane: 'A', role: roleOfStage(c.stage), model: '', ...c }));
  const total = Math.max(record.seconds || 0, ...rawCalls.map((c) => c.at + c.seconds), 0);
  const done = t >= total;
  const laneInfo = extra.lanes && extra.lanes.length
    ? extra.lanes
    : [{ id: 'A', status: record.status === 'succeeded' ? 'won' : 'stopped', note: '', previews: record.previews || [] }];
  const calls = rawCalls
    .filter((c) => c.at <= t)
    .map((c) => (done || t >= c.at + c.seconds ? c : { ...c, status: 'running', seconds: t - c.at, input_tokens: 0, output_tokens: 0 }));
  const lanes = laneInfo.map((lane) => {
    const laneCalls = calls.filter((c) => c.lane === lane.id);
    return {
      id: lane.id,
      status: done ? lane.status : laneCalls.length ? 'running' : 'waiting',
      stage: null,
      note: lane.note || '',
      previews: (lane.previews || []).filter((p) => p.at <= t),
      calls: laneCalls.length,
      tokens: laneCalls.reduce((sum, c) => sum + c.input_tokens + c.output_tokens, 0),
    };
  });
  return {
    id: `record-${record.id}`,
    status: done ? record.status : 'running',
    brief: req.brief || {},
    meta: req.meta || {},
    config: req.config || { lanes: lanes.length, verify: false },
    elapsed: Math.min(t, total),
    lanes,
    winner: done ? extra.winner || (record.status === 'succeeded' ? 'A' : null) : null,
    events: (extra.events || []).filter((e) => e.at <= t),
    calls,
    totals: {
      calls: calls.length,
      input_tokens: calls.reduce((sum, c) => sum + c.input_tokens, 0),
      output_tokens: calls.reduce((sum, c) => sum + c.output_tokens, 0),
    },
    result: done ? record.result : null,
    error: done ? record.error : null,
  };
}

function stopReplay() {
  if (!state.replay) return;
  const { turn, timer } = state.replay;
  clearInterval(timer);
  state.replay = null;
  // 中途停下的回放，恢复成完整的样子
  if (turn.running && turn.record) {
    turn.snapshot = recordSnapshot(turn.record);
    turn.outcomeKey = '';
  }
  renderTurn(turn, { animate: false });
  setGeometryBusy(Boolean(state.job));
}

async function replay(turn) {
  if (!turn.recordId) return;
  if (!turn.record) {
    try {
      turn.record = await request(`/api/questions/${turn.recordId}`);
    } catch (error) {
      toast(error.message);
      return;
    }
  }
  stopReplay();
  const record = turn.record;
  const calls = record.call_log || [];
  const total = Math.max(record.seconds || 0, ...calls.map((c) => c.at + c.seconds), 0);
  // 不管原来花了多久，回放都压在 20 秒左右；本来就很快的题按原速放
  const speed = Math.min(20, Math.max(1, total / 20));
  const replayId = `replay-${Date.now()}`;
  turn.focusLane = null;
  turn.outcomeKey = '';
  focusTurn(turn);
  setGeometryBusy(true);
  const started = performance.now();
  const frame = () => {
    const t = ((performance.now() - started) / 1000) * speed;
    turn.snapshot = { ...recordSnapshot(record, t), id: replayId };
    renderTurn(turn, { animate: true, replaying: true });
    if (t >= total) stopReplay();
  };
  state.replay = { turn, timer: setInterval(frame, 120) };
  frame();
}

// ---------- 启动 ----------

function bindEvents() {
  ui.composer.addEventListener('submit', (event) => {
    event.preventDefault();
    submit();
  });
  ui.prompt.addEventListener('input', () => {
    state.blueprint = null;
    autosize();
    refreshComposer();
    saveForm();
  });
  ui.prompt.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  for (const chip of Object.values(ui.chips)) {
    chip.addEventListener('click', (event) => {
      event.stopPropagation();
      openPopover(chip.dataset.pop, chip);
    });
  }
  // 用点击发生时的路径判断点没点在菜单里：知识点查找器里换一册、换一章会重画列表，
  // 被点的按钮这时已经不在页面上了，只看 event.target 会误以为点在外面而把菜单关掉
  // 在菜单里按下、拖到外面才松开（拖思考强度的滑块时常见），不算点在外面
  let pressedInPopover = false;
  document.addEventListener('pointerdown', (event) => {
    pressedInPopover = Boolean(pop.kind) && event.composedPath().includes(ui.popover);
  }, true);
  document.addEventListener('click', (event) => {
    if (pop.kind && !pressedInPopover && !event.composedPath().includes(ui.popover)) closePopover();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (pop.kind) {
      const anchor = pop.anchor;
      closePopover();
      anchor?.focus();
    } else {
      closeRail();
      ui.body.classList.remove('sheet-open');
    }
  });
  window.addEventListener('resize', positionPopover);
  ui.threadScroll.addEventListener('scroll', positionPopover);

  $('#newTask').addEventListener('click', () => {
    resetChat(null);
    setMode('idle');
    renderGreeting();
    shuffleSuggestions();
    ui.prompt.focus();
    if (state.job) toast('上一道题还在出，出好以后才能开始新的一道');
  });
  $('#experimentLink').addEventListener('click', () => setMode('experiment'));
  $('#bankLink').addEventListener('click', () => openBank());
  $('#papersLink').addEventListener('click', () => openPapers());
  $('#menuToggle').addEventListener('click', () => {
    if (narrow.matches && ui.body.classList.contains('rail-open')) closeRail();
    else openRail();
  });
  $('#railHide').addEventListener('click', hideRail);
  $('#scrim').addEventListener('click', closeRail);
  $('#closeSheet').addEventListener('click', () => {
    ui.body.classList.remove('has-sheet', 'sheet-open');
    ui.sheetPanel.hidden = true;
  });
  ui.copy.addEventListener('click', async () => {
    const result = state.focus?.snapshot?.result;
    if (!result) return;
    try {
      await navigator.clipboard.writeText(draftAsText(result));
      toast('已复制题目和解析（LaTeX）');
    } catch {
      toast('复制没成功，可以手动选中文字复制');
    }
  });
  $('#shuffleSuggest').addEventListener('click', shuffleSuggestions);
  $('#openBlueprints').addEventListener('click', () => {
    renderBlueprintList();
    $('#blueprintDialog').showModal();
    $('#bpSearch').focus();
  });
  $('#closeBlueprints').addEventListener('click', () => $('#blueprintDialog').close());
  $('#blueprintDialog').addEventListener('click', (event) => {
    if (event.target.id === 'blueprintDialog') event.target.close();
  });
  $('#bpSearch').addEventListener('input', renderBlueprintList);
  $('#bpType').addEventListener('change', renderBlueprintList);
}

async function checkHealth() {
  try {
    const data = await request('/api/models');
    state.models = data.models;
    setModelCatalog(data.models);
    state.defaultModel = data.default;
    state.health = data.models.length
      ? { ok: true, text: `已连接 ${[...new Set(data.models.map((m) => m.label))].join('、')}` }
      : { ok: false, text: '没有可用的模型' };
  } catch {
    state.health = { ok: false, text: '连不上本机服务' };
  }
  ui.heroAlert.hidden = state.health.ok;
  ui.heroAlert.replaceChildren();
  if (state.health.text === '没有可用的模型') {
    const open = el('button', 'link-btn', '接入模型服务');
    open.type = 'button';
    open.addEventListener('click', () => openSettings('models'));
    ui.heroAlert.append('还没有可用的模型：先在个人设置里接入一个模型服务。', open);
  } else {
    ui.heroAlert.textContent = '连不上本机服务：在终端输入 tetra（或双击“启动命题智能体.bat”）启动，再刷新这个页面。';
  }
  refreshAccount();
}

// 页面打开以后界面文件又改过（比如更新了代码），提示刷新，免得一部分是新代码、一部分是旧代码
async function uiVersion() {
  try {
    return (await request('/api/health')).ui_version || null;
  } catch {
    return null;
  }
}

async function watchForUpdates() {
  const loaded = await uiVersion();
  if (!loaded) return;
  const check = async () => {
    const now = await uiVersion();
    if (!now || now === loaded || $('#updateNotice')) return;
    const bar = el('div', 'update-notice');
    bar.id = 'updateNotice';
    bar.setAttribute('role', 'status');
    const reload = el('button', 'btn primary', '刷新');
    reload.type = 'button';
    reload.addEventListener('click', () => window.location.reload());
    bar.append(el('span', '', '界面有更新，刷新一下才能用上新的功能。'), reload);
    document.body.append(bar);
  };
  window.addEventListener('focus', check);
  setInterval(check, 60000);
}

// 服务断开（比如运行 tetra 的窗口被关掉了）：顶部常驻提示，每 3 秒试着重连；连上以后把当前页面的数据重新读一遍
function watchConnection() {
  let bar = null;
  let timer = null;
  const recover = async () => {
    try {
      if (!(await fetch('/api/health', { cache: 'no-store' })).ok) return;
    } catch {
      return;
    }
    clearInterval(timer);
    timer = null;
    bar?.remove();
    bar = null;
    await checkHealth();
    refreshCounts();
    await loadSessions();
    if (state.mode === 'bank') library.show();
    else if (state.mode === 'papers') papers.show();
    else if (state.mode === 'chat' && state.chat) openChat(state.chat.id, { quiet: true });
    toast('已经重新连上服务');
  };
  window.addEventListener('tetra:offline', () => {
    if (bar) return;
    bar = el('div', 'update-notice offline-notice');
    bar.setAttribute('role', 'alert');
    const retry = el('button', 'btn', '重试');
    retry.type = 'button';
    retry.addEventListener('click', recover);
    bar.append(el('span', '', OFFLINE_MESSAGE), retry);
    document.body.append(bar);
    timer = setInterval(recover, 3000);
  });
}

// 在个人设置里改了默认值，输入框里对应的那一项跟着变，其余保持你这次的选择
let lastProfile = null;
onProfileChange((p, changed) => {
  const before = lastProfile;
  lastProfile = structuredClone(p);
  renderGreeting();
  if (!state.ready) return;
  formFromProfile(changed.includes('all') ? null : before);
  afterFormChange();
  // 换了教材版本：重新读知识点目录；知识点编号两版相同，已选的考点换成新版本里的位置
  if (before && before.textbook !== p.textbook) reloadTree();
});

async function reloadTree() {
  try {
    state.tree = await request(`/api/curriculum?edition=${profile().textbook}`);
    indexTree(state.tree);
    if (state.form.topic) state.form.topic = topicFrom(state.form.topic.id) || null;
    state.finder = { volume: null, chapter: null, query: '' };
    afterFormChange();
  } catch (error) {
    toast(`知识点目录没加载出来：${error.message}`);
  }
}

async function init() {
  ui.prompt.placeholder = PLACEHOLDER.idle;
  if (readStorage(localStorage, RAIL_KEY)) ui.body.classList.add('rail-collapsed');
  startGeometry($('#geometry'));
  initSplitters();
  bindEvents();
  refreshCounts();
  watchForUpdates();
  watchConnection();
  initSettings({
    models: () => state.models,
    defaultModel: () => state.defaultModel,
    health: () => state.health,
    // 在设置里接入、修改模型服务以后，重新读可用的模型，输入框和出题方式马上用上
    reloadModels: async () => {
      await checkHealth();
      refreshComposer();
    },
  });
  ui.heroSlot.append(ui.composer);
  renderGreeting();
  await checkHealth();
  await loadProfile();
  try {
    const [tree, blueprints] = await Promise.all([request('/api/curriculum'), request('/api/blueprints')]);
    state.tree = tree;
    state.blueprints = blueprints;
    indexTree(tree);
  } catch (error) {
    toast(`知识点目录没加载出来：${error.message}`);
  }
  restoreForm();
  autosize();
  refreshComposer();
  shuffleSuggestions();
  await loadSessions();
  state.ready = true;

  // 刷新页面后回到刚才的对话；有题正在出就接着看进度
  const pending = readStorage(sessionStorage, JOB_KEY);
  const chat = readStorage(sessionStorage, CHAT_KEY);
  if (pending?.id && pending?.chat) {
    state.job = pending;
    setGeometryBusy(true);
    await openChat(pending.chat, { quiet: true });
    refreshComposer();
    poll();
  } else if (chat) {
    await openChat(chat, { quiet: true });
  }
}

init();
