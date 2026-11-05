// 命题智能体 · 主逻辑：输入框、对话（一个对话里可以连着出好几道题）、题目、历史对话与回放

import {
  $, DIFFICULTY_LABEL, FORCE_THINKING_FROM, GRADES, MODES, OFFLINE_MESSAGE, PURPOSES, THINKING, TYPE_LABEL,
  agentConfig, describeConfig, el, mathText, setBlueprints, setModelCatalog, thinkingFor,
  formatWhen, greeting, legacyText, normalize, readStorage, renderMath, request,
  roleOfStage, segmented, svg, toast, writeStorage,
} from './shared.js';
import { buildSheet, draftAsText } from './sheet.js';
import { Turn } from './turn.js';
import { BatchItemSource, BatchTurn, shownLane } from './batch.js';
import { ChatTurn, SummaryTurn, kilo } from './chat.js';
import { PaperTurn } from './assembly.js';
import { ImportTurn, importRecord } from './importcard.js';
import { setGeometryBusy, startGeometry } from './geometry.js';
import { isDark } from './theme.js';
import { language, startI18n } from './i18n.js';
import { initSplitters } from './split.js';
import { Library, asDraft } from './bank.js';
import { Papers, downloadPaper } from './paper.js';
import {
  initSettings, loadProfile, onProfileChange, openSettings, profile, refreshAccount, updateAgent,
} from './settings.js';

const FORM_KEY = 'mingti.form.v4'; // v4：出题选项默认不选（null），旧版存的选择不再沿用
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
// 输入框：可以写出题要求、要一套试卷，也可以直接问问题；助手先判断是哪一种（见后端 assistant.py）
const PLACEHOLDER = {
  get idle() { return `说说想出什么题，比如：${example}；也可以让我组一套卷子，或者直接问我问题`; },
  chat: '接着说：要出题、组卷，或者问问题都行；空着发送就按下面的设置出题',
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
  // 输入框下方的出题选项（考点、题型、难度、题量、年级、用途）默认都不选（null，题量是 1）：
  // 不选就按个人设置里的默认出题；选了只管下一次出题，题出完（不管成没成）恢复成不选。
  // 模型和思考强度不算在内，一直按你选的（聊天、出题都用）
  form: { topic: null, qtype: null, difficulty: null, grade: null, purpose: null, mode: 'fast', thinking: 'medium', count: 1 },
  blueprint: null,
  mode: 'idle',
  chat: null, // 当前打开的对话 { id, turns }
  focus: null, // 右栏正在显示哪一轮
  job: null, // 正在出的题 { id, chat, batch, paper }，同一时间只出一道（或一批、一份试卷）
  pending: null, // 正在等助手回答的那句话 { turn, chat }
  touched: false, // 上次发送以后，在输入框下方调过题型、难度这些出题选项
  context: null, // 当前对话带给模型的前文有多长（/api/sessions/{id}/context）
  attachments: [], // 输入框里准备发的附件 { key, name, kind, status, id, thumb, pages }
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
  attachTray: $('#attachTray'),
  attachInput: $('#attachInput'),
  attachBtn: $('#attachBtn'),
  taskLine: $('#taskLine'),
  chips: {
    kp: $('#kpChip'),
    type: $('#typeChip'),
    level: $('#levelChip'),
    count: $('#countChip'),
    use: $('#useChip'),
    context: $('#ctxChip'),
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

// 这一次出题实际用的设置：选了的按选的，没选的按个人设置里的默认（年级：选的 → 考点所在的年级 → 任教年级）
function effective() {
  const f = state.form;
  const p = profile();
  return {
    qtype: f.qtype || p.defaults.question_type || 'single_choice',
    difficulty: f.difficulty || p.defaults.difficulty || 3,
    count: f.count || 1,
    purpose: f.purpose || p.defaults.purpose || '课堂练习',
    grade: f.grade || f.topic?.grade || p.grade || null,
  };
}

// 出题选项的当前状态（出完题时看用户有没有在出题期间改过）
function chipState() {
  const f = state.form;
  return JSON.stringify([f.topic?.id || null, f.qtype, f.difficulty, f.count, f.grade, f.purpose]);
}

// 出完一道（或一批）题：选项恢复成不选。sent 是发出时的状态，出题期间改过的就留着
function clearChips(sent = null) {
  if (sent !== null && sent !== chipState()) return;
  Object.assign(state.form, { topic: null, qtype: null, difficulty: null, count: 1, grade: null, purpose: null });
  state.blueprint = null;
  afterFormChange();
}

// extra：追问时附加的要求（比如"别和上一道重复"），不放进输入框。
// note：用户写的要求；不给就用输入框里的（助手判断是出题以后，输入框已经清空了，就把那句话传进来）。
// 只选了题型、难度、年级和用途也能出题：没选考点也没写要求时，由模型在这个年级的范围里挑考点。
function composeRequest({ extra = '', followUp = '', note: given, said = '' } = {}) {
  const f = state.form;
  const p = profile();
  const eff = effective();
  const topic = f.topic;
  const { grade, qtype, difficulty, purpose } = eff;
  let text = `请出一道${grade || '高中'}数学${TYPE_LABEL[qtype]}`;
  if (topic) text += `，考查「${topic.name}」`;
  text += `，难度 ${difficulty}/5（${DIFFICULTY_LABEL[difficulty]}），用于${purpose}。`;
  const note = (given ?? ui.prompt.value).trim();
  const openTopic = !topic && !note;
  const constraints = [note, ...settingsConstraints(), openTopic ? topicHint(grade, qtype) : '', extra].filter(Boolean).join('\n');
  return {
    ready: true,
    openTopic,
    brief: {
      request_text: text,
      grade,
      curriculum_node_id: topic?.kind === 'kp' ? topic.id : null,
      knowledge_point_query: topic?.name || null,
      question_type: qtype,
      target_difficulty: difficulty,
      teaching_purpose: purpose,
      additional_constraints: constraints.slice(0, 2000),
    },
    meta: {
      knowledge_label: topic?.label || '',
      topic_id: topic?.id || null,
      open_topic: openTopic,
      note,
      said: said || null, // 用户说的原话（助手判断是出题时）；note 是助手连同前文整理成的出题要求
      follow_up: followUp || null,
      detail: p.defaults.solution_detail,
      source: state.blueprint ? `blueprint:${state.blueprint.id}` : followUp ? 'follow-up' : 'manual',
      edition: profile().textbook || 'pep_a', // 教材版本：题目的知识点路径按这一版的册、章、节写
    },
    config: {
      ...agentConfig(f.mode, { ...p.agent, model: mainModel()?.id }, state.models, state.defaultModel),
      // 出 4、5 星的题至少用高（选了极高就用极高）；聊天不受这一条限制
      thinking: thinkingFor(difficulty, f.thinking),
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

// 按钮上的文字包在 .chip-label 里：输入框窄的时候，选项可以只留图标（见下面的 fitComposer）
function setChip(chip, parts, isSet = false) {
  chip.innerHTML = '';
  for (const part of parts) chip.append(typeof part === 'string' ? el('span', 'chip-label', part) : part);
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
  const p = profile();
  const eff = effective();
  // 出题选项：没选时只写是哪一项（考点不限、题型、难度、题量、用途），选了才写选的值并加深。
  // 考点名太长时由样式截断（加省略号），不在这里截：英文界面要拿完整的名字去查译名
  setChip(ui.chips.kp, [f.topic ? `考查 ${f.topic.name}` : '考点不限'], Boolean(f.topic));
  ui.chips.kp.title = f.topic?.label || '可以不选：不选时按年级挑一个考点；想指定就点这里选';
  setChip(ui.chips.type, [f.qtype ? TYPE_LABEL[f.qtype] : '题型'], Boolean(f.qtype));
  ui.chips.type.title = f.qtype ? '这次出题用的题型，出完恢复不选' : `不选就按个人设置：${TYPE_LABEL[eff.qtype]}；也可以直接在输入框里说`;
  setChip(ui.chips.level, [dots(f.difficulty || 0), f.difficulty ? DIFFICULTY_LABEL[f.difficulty] : '难度'], Boolean(f.difficulty));
  ui.chips.level.title = f.difficulty ? '这次出题用的难度，出完恢复不选' : `不选就按个人设置：${DIFFICULTY_LABEL[eff.difficulty]}`;
  setChip(ui.chips.count, [f.count > 1 ? `出 ${f.count} 道` : '题量'], f.count > 1);
  ui.chips.count.title = '一次出几道：同样的要求一次出好几道，每道都单独审核；不选是 1 道';
  const use = [f.grade, f.purpose].filter(Boolean).join('，');
  setChip(ui.chips.use, [use || '用途'], Boolean(use));
  ui.chips.use.title = use ? '这次出题的年级和用途，出完恢复不选'
    : `年级和用途：不选就按考点所在的年级（或任教年级${p.grade ? ` ${p.grade}` : ''}），用途按个人设置：${eff.purpose}`;
  // 模型和思考强度合在一个按钮上："deepseek-flash · 思考 中"；"思考"和档位分开写、颜色深浅不同，
  // 免得"思考中"读成"正在思考"。显示的就是你选的那一档，聊天按它回答；出 4、5 星的题时至少用高（在提示里说明）
  const model = mainModel();
  const thinking = THINKING[f.thinking] ? f.thinking : 'medium';
  const raised = thinkingFor(eff.difficulty, thinking) !== thinking;
  const think = el('span', thinking === 'max' ? 'chip-think max' : 'chip-think'); // 极高：档位名用和滑块一样的渐变色
  think.append(el('span', '', '思考'), el('b', '', THINKING[thinking].name));
  setChip(ui.chips.model, model ? [modelTitle(model), think] : [modelTitle(model)]);
  ui.chips.model.title = !model ? '还没有可用的模型'
    : raised ? `切换模型和思考强度。聊天按${THINKING[thinking].name}；这次出${DIFFICULTY_LABEL[eff.difficulty]}题会用高`
      : '切换模型和思考强度（聊天和出题都按这一档）';

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
  // 出题时也能接着聊天；只有在等助手回答上一句时不能发
  ui.send.disabled = !state.models.length || Boolean(state.pending);
  // 要发的内容不再单独占一行，鼠标停在发送按钮上能看到
  const typed = ui.prompt.value.trim();
  ui.send.title = !state.models.length ? '还没有可用的模型'
    : state.pending ? '上一句还在回答'
      : state.attachments.length ? '发送：看附件里的内容回答（比如解题），或者把里面的题存进题库'
        : typed ? '发送：要出题就出题，要组卷就组卷，其他的直接回答'
        : state.job ? '上一道题还在出' : `按下面的设置出题：${ui.taskLine.textContent.replace(/^将发送：/, '')}`;
  scheduleFit();
}

// ---------- 输入框下方那一排：放不下一行时逐级收紧 ----------
// 中文、英文，选了哪些选项，开没开右栏，宽度都不一样，所以量着来：先照常排，换行了就收一级再量。
// 每一级收掉什么见 app.css 的 .fit-1～.fit-4。收到底还放不下（很窄的屏幕）才换行。
const FIT_LEVELS = ['fit-1', 'fit-2', 'fit-3', 'fit-4'];
let fitFrame = 0;
let fitWidth = 0;

function fitsOneRow(row) {
  const tops = [...row.querySelectorAll('.chips > .chip, .composer-tools > :not([hidden])')]
    .map((node) => node.getBoundingClientRect())
    .filter((box) => box.width)
    .map((box) => box.top);
  return !tops.length || Math.max(...tops) - Math.min(...tops) < 8;
}

function fitComposer() {
  const row = ui.composer.querySelector('.composer-row');
  if (!row || !ui.composer.isConnected || !ui.composer.offsetWidth) return;
  ui.composer.classList.remove(...FIT_LEVELS);
  for (const level of FIT_LEVELS) {
    if (fitsOneRow(row)) return;
    ui.composer.classList.add(level);
  }
}

// 英文界面的文字是在 MutationObserver 里换的（微任务），等到下一帧再量，量的就是换好以后的宽度
function scheduleFit() {
  cancelAnimationFrame(fitFrame);
  fitFrame = requestAnimationFrame(fitComposer);
}

function watchComposerWidth() {
  if (!('ResizeObserver' in window)) return;
  new ResizeObserver(([entry]) => {
    const width = Math.round(entry.contentRect.width);
    if (width === fitWidth) return; // 只看宽度：收紧以后高度变了不用再量
    fitWidth = width;
    scheduleFit();
  }).observe(ui.composer);
  document.fonts?.ready.then(scheduleFit);
}

// 输入框里的选择只在这次打开期间记住；下次打开按个人设置里的偏好重新预选
function saveForm() {
  const f = state.form;
  writeStorage(sessionStorage, FORM_KEY, { ...f, topic: f.topic?.id || null, prompt: ui.prompt.value });
}

// before：改动前的个人设置；给了就只同步真正变了的几项，没给就全部按设置来。
// 出题选项不从个人设置预选（默认不选，出题时按个人设置补）；这里只管出题方式、模型和思考强度
function formFromProfile(before = null) {
  const p = profile();
  const f = state.form;
  const changed = (pick) => !before || pick(p) !== pick(before);
  if (changed((x) => x.agent.mode)) f.mode = p.agent.mode;
  if (changed((x) => x.agent.model)) f.model = p.agent.model || null;
  if (changed((x) => x.agent.thinking)) f.thinking = THINKING[p.agent.thinking] ? p.agent.thinking : 'medium';
}

function restoreForm() {
  formFromProfile();
  const saved = readStorage(sessionStorage, FORM_KEY);
  if (!saved) return;
  const level = Number(saved.difficulty);
  const count = Number(saved.count);
  Object.assign(state.form, {
    qtype: TYPE_LABEL[saved.qtype] ? saved.qtype : null,
    difficulty: level >= 1 && level <= 5 ? level : null,
    count: count >= 1 && count <= 20 ? count : 1,
    grade: GRADES.includes(saved.grade) ? saved.grade : null,
    purpose: PURPOSES.includes(saved.purpose) ? saved.purpose : null,
    mode: MODES[saved.mode] ? saved.mode : state.form.mode,
    model: saved.model || state.form.model,
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
    context: contextPopover,
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

// 每个出题选项的菜单最上面都有"不指定"：回到不选的状态，出题时按个人设置里的默认
function unsetItem(note, checked, onPick) {
  return menuItem({ name: '不指定', note, checked, onPick: () => { onPick(); afterFormChange(); closePopover(); } });
}

function typePopover() {
  const wrap = el('div');
  wrap.append(el('p', 'pop-title', '题型'));
  wrap.append(unsetItem(`按个人设置：${TYPE_LABEL[profile().defaults.question_type] || '单选题'}；也可以在输入框里说`, !state.form.qtype, () => { state.form.qtype = null; }));
  for (const [value, label] of Object.entries(TYPE_LABEL)) {
    wrap.append(menuItem({
      name: label,
      note: TYPE_NOTE[value],
      checked: state.form.qtype === value,
      onPick: () => { state.form.qtype = value; state.touched = true; afterFormChange(); closePopover(); },
    }));
  }
  return wrap;
}

function levelPopover() {
  const wrap = el('div');
  wrap.append(el('p', 'pop-title', '难度'));
  wrap.append(unsetItem(`按个人设置：${DIFFICULTY_LABEL[profile().defaults.difficulty] || '中档'}`, !state.form.difficulty, () => { state.form.difficulty = null; }));
  for (let level = 1; level <= 5; level += 1) {
    wrap.append(menuItem({
      name: `${level} ${DIFFICULTY_LABEL[level]}`,
      note: LEVEL_NOTE[level],
      checked: state.form.difficulty === level,
      lead: dots(level),
      onPick: () => { state.form.difficulty = level; state.touched = true; afterFormChange(); closePopover(); },
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
      onPick: () => { state.form.count = count; state.touched = true; afterFormChange(); closePopover(); },
    }));
  }
  return wrap;
}

// 年级和用途：左右两列勾选，和题型、难度的菜单一个样子。"不指定"后面浅色写出按什么来（不指定的年级：
// 先按考点所在的年级，没选考点按个人设置里的任教年级）。选了一项菜单不关，可以接着选另一列
function usePopover() {
  const wrap = el('div', 'use-menu');
  const p = profile();
  const auto = state.form.topic?.grade || p.grade || '';
  const column = (title, options, current, pick) => {
    const col = el('div', 'use-col');
    col.setAttribute('role', 'group');
    col.setAttribute('aria-label', title);
    col.append(el('p', 'pop-title', title));
    for (const option of options) {
      const item = menuItem({
        name: option.label,
        checked: option.value === current,
        onPick: () => {
          pick(option.value);
          if (option.value) state.touched = true;
          afterFormChange();
          renderPopover();
          ui.popover.querySelector(`[aria-label="${title}"] [aria-checked="true"]`)?.focus({ preventScroll: true });
        },
      });
      if (option.hint) item.querySelector('.name').append(el('small', 'use-default', option.hint));
      col.append(item);
    }
    return col;
  };
  wrap.append(
    column('年级', [{ value: null, label: '不指定', hint: auto }, ...GRADES.map((g) => ({ value: g, label: g }))],
      state.form.grade, (value) => { state.form.grade = value; }),
    column('用途', [{ value: null, label: '不指定', hint: p.defaults.purpose }, ...PURPOSES.map((x) => ({ value: x, label: x }))],
      state.form.purpose, (value) => { state.form.purpose = value; }),
  );
  return wrap;
}

// 分档滑块：几档等距排开，可以拖动圆钮、点轨道或下面的字，也能用方向键；
// 拖动时圆钮跟着手走，松开后吸到最近的一档。min 以下的档选不了：字和圆点变淡，拖过去会停在 min。
// 拉满到最后一档时加上 is-max：填充变成流动的渐变色，闪着光点（样式在 app.css）。
// onPick(i) 在档位变了的时候调用（拖动途中也会）
const SPARKS = 16;
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
  // 拉满时在填充里闪的光点：位置、大小、节奏各不相同，平时看不见
  for (let k = 0; k < SPARKS; k += 1) {
    const spark = el('span', 'spark');
    const size = 1.4 + Math.random() * 1.8;
    spark.style.cssText = `--x:${(k / SPARKS) * 96 + Math.random() * 4}%;--y:${3 + Math.random() * 13}px;`
      + `--s:${size.toFixed(1)}px;--t:${(1.6 + Math.random() * 1.6).toFixed(2)}s;--d:${(-Math.random() * 3).toFixed(2)}s`;
    fill.append(spark);
  }
  const ticks = names.map((_, i) => {
    const tick = el('i', i < min ? 'off' : '');
    tick.style.setProperty('--p', String(i / last));
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
    wrap.classList.toggle('is-max', i === last);
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
    button.style.setProperty('--p', String(i / last));
    button.addEventListener('click', () => {
      pick(i);
      rail.focus({ preventScroll: true }); // 点了字以后，方向键接着能调
    });
    stops.append(button);
    return button;
  });
  show(current / last, current);
  // 圆钮在轨道里移动，两头各留一个圆钮半径
  const ratio = (event) => {
    const box = rail.getBoundingClientRect();
    const inset = thumb.offsetWidth / 2;
    return Math.max(min / last, Math.min(1, (event.clientX - box.left - inset) / Math.max(1, box.width - 2 * inset)));
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

// 思考强度：低、中、高、极高四档，在模型菜单里用滑块调，随时都能选任意一档，聊天就按它回答。
// 出 4、5 星的题时至少用高：选低、中也按高出，选极高就用极高（下面有一行提示）
function thinkingSection() {
  const section = el('div', 'think-section');
  const levels = Object.keys(THINKING);
  const difficulty = effective().difficulty;
  const hard = difficulty >= FORCE_THINKING_FROM;
  const current = THINKING[state.form.thinking] ? state.form.thinking : 'medium';
  // 各档的说明叠在同一格里、只显示当前这档：换档时菜单高度不变，滑块不会跟着上下跳
  const notes = el('div', 'think-notes');
  const noteNodes = levels.map((level) => notes.appendChild(el('p', '', THINKING[level].note)));
  const showNote = (level) => noteNodes.forEach((node, i) => node.classList.toggle('on', levels[i] === level));
  showNote(current);
  const hint = el('p', 'pop-hint');
  const showHint = (level) => {
    hint.hidden = !(hard && thinkingFor(difficulty, level) !== level);
    hint.textContent = `聊天按${THINKING[level].name}回答；这次要出的是${DIFFICULTY_LABEL[difficulty]}题（${difficulty} 星），出题时会用高：这类题最容易算错、反复返工。`;
  };
  showHint(current);
  const slider = levelSlider({
    names: levels.map((level) => THINKING[level].name),
    index: levels.indexOf(current),
    label: '思考强度',
    onPick: (i) => {
      state.form.thinking = levels[i];
      showNote(levels[i]);
      showHint(levels[i]);
      afterFormChange();
    },
  });
  section.append(el('p', 'pop-title', '思考强度'), slider, notes, hint);
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

// ---------- 上下文：这个对话带给模型的前文有多长，可以压缩成摘要 ----------

// 输入框右下角的小圆环：前文占预算的几成；对话还没有内容时不显示
function renderContext() {
  const chip = ui.chips.context;
  const stats = state.context;
  const show = state.mode === 'chat' && Boolean(state.chat) && Boolean(stats) && stats.turns + stats.summarized > 0;
  chip.hidden = !show;
  if (!show) return;
  const ratio = Math.min(1, stats.tokens / Math.max(1, stats.limit));
  const percent = Math.round(ratio * 100);
  const round = 2 * Math.PI * 6;
  chip.innerHTML = `<svg class="ctx-ring" viewBox="0 0 16 16" aria-hidden="true"><circle class="track" cx="8" cy="8" r="6"/>`
    + `<circle class="used" cx="8" cy="8" r="6" stroke-dasharray="${(round * Math.max(ratio, 0.02)).toFixed(2)} ${round.toFixed(2)}"/></svg>`;
  chip.append(el('span', '', `${percent}%`));
  chip.classList.toggle('full', ratio >= stats.auto);
  chip.title = `上下文：约 ${kilo(stats.tokens)} / ${kilo(stats.limit)} Token（${percent}%）。点开可以压缩`;
  chip.setAttribute('aria-label', `上下文用了 ${percent}%`);
}

function setContext(stats, chatId) {
  if (stats && state.chat?.id === chatId) state.context = stats;
  renderContext();
  // 上下文超额了：马上在后台压缩，不等下一句
  if (stats?.needs && state.chat?.id === chatId) setTimeout(() => autoCompress(chatId), 300);
}

// 自动压缩：只在超额时压（服务端再确认一次）；压完对话里排进"已压缩"的分界线
let autoCompressing = null;
async function autoCompress(chatId) {
  if (!chatId || autoCompressing === chatId || state.pending?.chat === chatId || !state.models.length) return;
  autoCompressing = chatId;
  try {
    const data = await request(`/api/sessions/${encodeURIComponent(chatId)}/compress`, {
      method: 'POST',
      body: JSON.stringify({ model: mainModel()?.id || null, auto: true }),
    });
    if (!data.summary) {
      setContext(data.context, chatId);
      return;
    }
    // 这个对话里正在出题时不重排（重新打开会打断进度的显示），只更新用量；下次打开就能看到分界线
    if (state.chat?.id === chatId && state.job?.chat !== chatId && !state.pending) await openChat(chatId, { quiet: true });
    else if (state.chat?.id === chatId) state.context = data.context;
    renderContext();
    toast(`上下文超额，已自动把前面 ${data.summary.covers_count} 轮压缩成摘要`);
  } catch {
    // 没压缩成：发下一句之前服务端还会再试
  } finally {
    autoCompressing = null;
  }
}

async function loadContext(chatId = state.chat?.id) {
  if (!chatId) {
    state.context = null;
    renderContext();
    return;
  }
  try {
    setContext(await request(`/api/sessions/${encodeURIComponent(chatId)}/context`), chatId);
  } catch {
    // 读不到就先不显示
  }
}

function contextPopover() {
  const stats = state.context || { tokens: 0, limit: 24000, auto: 0.85, keep: 4, turns: 0, summarized: 0, dropped: 0, compressible: false };
  const percent = Math.round(Math.min(1, stats.tokens / Math.max(1, stats.limit)) * 100);
  const wrap = el('div', 'ctx-menu');
  const bar = el('div', 'ctx-bar');
  const fill = el('i');
  fill.style.width = `${Math.max(percent, 1)}%`;
  bar.append(fill);
  const parts = [`带上了最近 ${stats.turns} 轮的原文`];
  if (stats.summarized) parts.push(`更早的 ${stats.summarized} 轮用摘要代替`);
  if (stats.dropped) parts.push(`还有 ${stats.dropped} 轮放不下、没带上`);
  wrap.append(
    el('p', 'pop-title', '上下文'),
    el('p', 'ctx-numbers', `约 ${kilo(stats.tokens)} / ${kilo(stats.limit)} Token · ${percent}%`),
    bar,
    el('p', 'ctx-note', `${parts.join('，')}。助手回答问题、判断要不要出题、定组卷方案时都会带上这些前文；出题时，它会把前文里相关的要求一起写给命题员。`),
    el('p', 'ctx-note', `为了速度和花费，前文最多带约 ${kilo(stats.limit)} Token。超过 ${Math.round(stats.auto * 100)}% 时，发下一句之前会自动把前面的内容压缩成摘要，最近 ${stats.keep} 轮保留原文。`),
  );
  const foot = el('div', 'pop-foot');
  const compress = el('button', 'btn', '压缩上下文');
  compress.type = 'button';
  compress.disabled = !stats.compressible || !state.models.length;
  compress.addEventListener('click', () => compressContext(compress));
  foot.append(el('span', '', stats.compressible ? `把最近 ${stats.keep} 轮以前的内容写成一段摘要` : '对话还很短，不用压缩'), compress);
  wrap.append(el('div', 'pop-sep'), foot);
  return wrap;
}

async function compressContext(button) {
  const chatId = state.chat?.id;
  if (!chatId) return;
  button.disabled = true;
  button.textContent = '正在压缩…';
  try {
    const data = await request(`/api/sessions/${encodeURIComponent(chatId)}/compress`, {
      method: 'POST',
      body: JSON.stringify({ model: mainModel()?.id || null }),
    });
    closePopover();
    // 重新打开这个对话：分界线排在压缩和没压缩的内容之间
    if (state.chat?.id === chatId) await openChat(chatId, { quiet: true });
    toast(`已压缩：前面 ${data.summary.covers_count} 轮写成了一段摘要，约 ${kilo(data.summary.tokens_after)} Token`);
  } catch (error) {
    toast(`没压缩成：${error.message}`);
    button.disabled = false;
    button.textContent = '压缩上下文';
  }
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
  clear.addEventListener('click', () => { state.form.topic = null; state.touched = true; afterFormChange(); closePopover(); });
  foot.append(clear);
  wrap.append(foot);

  const pick = (id) => {
    state.form.topic = topicFrom(id);
    state.blueprint = null;
    state.finder.query = '';
    state.touched = true;
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

// 显示用排版写法，公式和题目里一样用 KaTeX 排（写完这一批调 renderMath）；这是题目内容，英文界面也不翻译
const specTopic = (spec) => Object.assign(mathText('span', 'topic', spec.topic_tex || spec.topic), { translate: false });

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
      specTopic(spec),
      el('span', 'use', '用这条'),
    );
    button.addEventListener('click', () => applyBlueprint(spec));
    const item = el('li');
    item.append(button);
    list.append(item);
  }
  renderMath(list);
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
  state.touched = true;
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
    button.append(el('span', 'kind', spec.type_label), specTopic(spec), dots(spec.target_difficulty));
    button.addEventListener('click', () => applyBlueprint(spec));
    const item = el('li');
    item.append(button);
    list.append(item);
  }
  renderMath(list);
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
  // 题库和试卷自己决定右栏放什么；对话里右栏放题目（或组好的试卷），只有聊天的对话不开右栏
  if (chat) {
    ui.sheetHeading.textContent = '题目';
    ui.panelActions.replaceChildren();
    ui.sheetScroll.classList.remove('framed');
    delete ui.sheetPanel.dataset.view;
    if (state.focus) {
      panel.open();
      state.focus.sheetKey = '';
      renderSheet(state.focus, { animate: false });
    } else {
      panel.close();
    }
  } else if (mode !== 'bank' && mode !== 'papers') {
    panel.close();
  }
  closeRail();
  renderContext();
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
  return appendTurn(new BatchTurn(batchActions));
}

function appendTurn(turn) {
  state.chat.turns.push(turn);
  ui.conversation.append(turn.root);
  ui.conversation.classList.toggle('multi', state.chat.turns.length > 1);
  return turn;
}

function removeTurn(turn) {
  turn.root.remove();
  if (state.chat) state.chat.turns = state.chat.turns.filter((other) => other !== turn);
  ui.conversation.classList.toggle('multi', (state.chat?.turns.length || 0) > 1);
}

// 聊天那一轮没回答成时：再问一次，或者把这句话直接当出题要求
const chatActions = {
  retry: (turn) => converse(turn.text, turn),
  asQuestion: (turn) => {
    removeTurn(turn);
    startGeneration({ note: turn.text });
  },
};

// 自动组卷那一轮的按钮
const paperActions = {
  stop: (turn) => stopJob(turn),
  showSlot: (turn, no) => showSlot(turn, no),
  showPaper: (turn) => focusTurn(turn, true),
  exportPaper: (turn, format, trigger) => turn.paper
    && downloadPaper(turn.paper.id, { format, answers: true, title: turn.paper.title, trigger }),
  openPaper: async (turn) => {
    if (!turn.paper) return;
    setMode('papers');
    await papers.show(turn.paper.id);
  },
  again: (turn) => startAssembly(turn.snapshot?.text || ''),
};

// 右栏显示试卷里的第 no 题：挑好、出好的按题库里的题显示，正在现场出的读那道题的实时进度
class SlotSource {
  constructor(paperTurn, no) {
    this.paperTurn = paperTurn;
    this.no = no;
    this.snapshot = null;
    this.sheetKey = '';
    this.focusLane = null;
    this.fromBank = false;
    this.resultNote = '';
  }

  get sheetPrefix() {
    return `第 ${this.no} 题，`;
  }

  shownLane() {
    return shownLane(this.snapshot, this.focusLane);
  }
}

// 题库里的一道题 → 右栏题目卡片要的快照形状
function bankSnapshot(item) {
  return {
    id: `bank-${item.id}`,
    status: 'succeeded',
    brief: { target_difficulty: item.difficulty, grade: item.grade || '' },
    meta: { knowledge_label: item.knowledge },
    config: {},
    elapsed: 0,
    lanes: [{ id: 'A', status: 'won', note: '', previews: [], calls: 0, tokens: 0 }],
    winner: 'A',
    events: [],
    calls: [],
    totals: { calls: 0, input_tokens: 0, output_tokens: 0 },
    result: asDraft(item),
    error: null,
  };
}

async function showSlot(turn, no, reveal = true) {
  const slot = turn.slots().find((one) => one.no === no);
  if (!slot) return;
  const source = new SlotSource(turn, no);
  try {
    if (slot.bank_id) {
      source.snapshot = bankSnapshot(await request(`/api/bank/${slot.bank_id}`));
      source.fromBank = true;
      if (slot.source !== 'generated') source.resultNote = '从题库里选的';
      else source.resultNote = source.snapshot.result.solution_issue ? '现场出的，待核对' : '现场出的，已通过全部检查';
    } else if (slot.job_id) {
      source.snapshot = await request(`/api/jobs/${slot.job_id}`);
    }
  } catch (error) {
    toast(error.message);
    return;
  }
  focusTurn(source, reveal);
}

// 正在组卷：右栏显示的那一道还在现场出，就跟着刷新；出好了换成题库里的那一道
async function refreshSlot(turn) {
  const source = state.focus;
  if (!source || source.paperTurn !== turn) return;
  const slot = turn.slots().find((one) => one.no === source.no);
  if (!slot) return;
  if (slot.bank_id && !source.fromBank) {
    showSlot(turn, slot.no, false);
    return;
  }
  if (slot.state !== 'generating' || !slot.job_id) return;
  try {
    const snap = await request(`/api/jobs/${slot.job_id}`);
    if (state.focus !== source) return;
    const before = source.snapshot?.status;
    source.snapshot = snap;
    renderSheet(source, { animate: before !== snap.status });
  } catch {
    // 下一轮再试
  }
}

const scoreText = (value) => (Number.isInteger(Number(value)) ? String(Number(value)) : String(Number(Number(value).toFixed(1))));

// 右栏显示组好的整份试卷（和"试卷"页的预览一样，由后端的打印页生成），上面带导出按钮
function renderPaperSheet(turn) {
  const snap = turn.snapshot;
  const paper = turn.paper;
  ui.sheetPanel.dataset.view = 'paper';
  ui.sheetHeading.textContent = '试卷';
  ui.sheetTabs.innerHTML = '';
  ui.copy.hidden = true;
  if (!paper) {
    ui.sheetNote.textContent = turn.running ? '正在组卷' : '没组成';
    ui.panelActions.replaceChildren();
    const key = `${snap?.id}:${snap?.status}:none`;
    if (turn.sheetKey === key) return;
    turn.sheetKey = key;
    ui.sheetScroll.classList.remove('framed');
    ui.sheetScroll.replaceChildren(turn.running
      ? sheetMessage('正在组卷', ['题目都挑好、出好以后，整份试卷会显示在这里。', '点左边列表里的一道题，可以先看那道题。'], true)
      : sheetMessage('没能组成试卷', [legacyText(snap?.error) || '']));
    return;
  }
  ui.sheetNote.textContent = `${paper.count} 道题，满分 ${scoreText(paper.total)} 分`;
  const key = `${snap.id}:paper:${paper.id}:${isDark()}`; // 换了主题要重新载入预览
  if (turn.sheetKey === key) return;
  turn.sheetKey = key;
  const exportButton = (format, label) => {
    const node = el('button', 'ghost-btn', label);
    node.type = 'button';
    node.addEventListener('click', () => downloadPaper(paper.id, { format, answers: true, title: paper.title, trigger: node }));
    return node;
  };
  ui.panelActions.replaceChildren(exportButton('docx', '导出 Word'), exportButton('pdf', '导出 PDF'));
  const frame = el('iframe', 'paper-frame');
  frame.title = '试卷预览';
  frame.src = `/api/papers/${paper.id}/print?answers=true&dark=${isDark()}&v=${Date.now()}`;
  panel.frame(frame);
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
  startGeneration({ extra, followUp: `再出 ${turn.count} 道` });
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
  const same = state.chat?.id === id;
  state.chat = id ? { id, turns: [] } : null;
  state.focus = null;
  if (!same) state.context = null; // 换了对话：上下文用量等读回来再显示
  ui.conversation.replaceChildren();
  ui.conversation.classList.remove('multi');
  writeStorage(sessionStorage, CHAT_KEY, id || null);
  renderContext();
}

function addTurn() {
  return appendTurn(new Turn(turnActions));
}

function addChatTurn(text, attachments = []) {
  return appendTurn(new ChatTurn(chatActions, text, attachments));
}

// 把附件里的题存进题库的那一轮（核对卡片）
const importActions = {
  stop: (turn) => stopJob(turn),
  openBank: () => openBank(),
  saved: (turn, data) => {
    setCounts(data.counts);
    loadSessions();
    loadContext(state.chat?.id);
  },
};

function addImportTurn() {
  return appendTurn(new ImportTurn(importActions));
}

function addPaperTurn() {
  return appendTurn(new PaperTurn(paperActions));
}

function liveTurn() {
  if (!state.job || !state.chat) return null;
  return state.chat.turns.find((turn) => turn.jobId === state.job.id) || null;
}

function renderTurn(turn, { animate = true, replaying = false } = {}) {
  turn.render({ live: turn === liveTurn(), replaying });
  if (state.focus === turn) renderSheet(turn, { animate });
}

// 右栏显示哪一道：默认跟着最新的一道；点某一轮的"看题目"就换成那一道。
// 组卷那一轮显示整份试卷；点它列表里的一道题，显示那一道。聊天没有要放在右栏的东西
function focusTurn(turn, reveal = false) {
  // 批量那一轮本身不能直接显示：显示它出好的第一道（还没有就第一道）
  if (turn.isBatch) {
    const first = turn.items().find((item) => item.status === 'succeeded') || turn.items()[0];
    if (first) showBatchItem(turn, first.index, reveal);
    return;
  }
  if (turn.isChat || turn.isSummary || turn.isImport) return;
  state.focus = turn;
  const owner = turn.batchTurn || turn.paperTurn || turn;
  if (turn.paperTurn) turn.paperTurn.focusedNo = turn.no;
  else if (turn.isPaper) turn.focusedNo = null;
  for (const other of state.chat?.turns || []) other.setFocused(other === owner);
  if (turn.batchTurn) turn.batchTurn.setFocusedItem(turn.index);
  if (turn.paperTurn) turn.paperTurn.setFocusedSlot(turn.no);
  turn.sheetKey = '';
  if (state.mode !== 'chat') return;
  panel.open(reveal);
  renderSheet(turn, { animate: false });
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
  if (turn.isPaper) {
    renderPaperSheet(turn);
    return;
  }
  // 刚才右栏放的是整份试卷：换回题目的样子
  if (ui.sheetPanel.dataset.view === 'paper') {
    delete ui.sheetPanel.dataset.view;
    ui.sheetHeading.textContent = '题目';
    ui.panelActions.replaceChildren();
    ui.sheetScroll.classList.remove('framed');
  }
  const snap = turn.snapshot;
  // 第几道：只数出题的那几轮，聊天、组卷和压缩的分界线不算
  const turns = (state.chat?.turns || []).filter((other) => !other.isChat && !other.isPaper && !other.isSummary && !other.isImport);
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
    const passed = snap.result.solution_issue ? '待核对' : '已通过全部检查';
    note = turn.resultNote || (several ? `方案 ${snap.winner}，${passed}` : passed);
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
    // 拼成几种完整的说法（英文界面按整句翻译）
    if (several) note = version > 1 ? `方案 ${lane.id} 的题干第 ${version} 版` : `方案 ${lane.id} 的题干`;
    else note = version > 1 ? `题干第 ${version} 版` : '题干';
    if (snap.status === 'failed') note += '，没出成';
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

// ---------- 附件：粘贴截图、拖进输入框，或者点回形针选图片、PDF、文本文件 ----------

const ATTACH_MAX = 10;
const ATTACH_BYTES = 25 * 1024 * 1024;
let attachSeq = 0;

function fileKind(file) {
  if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name)) return 'pdf';
  if (/^image\/(png|jpeg|webp|gif|bmp)$/.test(file.type)) return 'image';
  if (file.type.startsWith('text/') || /\.(txt|md|markdown)$/i.test(file.name)) return 'text';
  return '';
}

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('文件没读出来'));
    reader.readAsDataURL(file);
  });
}

// 图片的小缩略图（最长边 160 像素）：显示在输入框上方，也存进聊天记录
function thumbnail(dataUrl) {
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => {
      const scale = Math.min(1, 160 / Math.max(image.naturalWidth, image.naturalHeight, 1));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
      const webp = canvas.toDataURL('image/webp', 0.8);
      resolve(webp.startsWith('data:image/webp') ? webp : canvas.toDataURL('image/jpeg', 0.8));
    };
    image.onerror = () => resolve('');
    image.src = dataUrl;
  });
}

function addFiles(list) {
  for (const file of [...list]) {
    if (state.attachments.length >= ATTACH_MAX) {
      toast(`一次最多带 ${ATTACH_MAX} 个附件`);
      break;
    }
    const kind = fileKind(file);
    if (!kind) {
      toast(`${file.name} 的格式不支持：可以传图片（PNG、JPG、WebP）、PDF 或文本文件`);
      continue;
    }
    if (file.size > ATTACH_BYTES) {
      toast(`${file.name} 太大了：一个文件最多 25 MB`);
      continue;
    }
    attachSeq += 1;
    const item = { key: `a${attachSeq}`, name: file.name || `截图${attachSeq}.png`, kind, size: file.size, status: 'uploading', id: null, thumb: '', pages: 1 };
    state.attachments.push(item);
    uploadAttachment(item, file);
  }
  renderAttachments();
}

async function uploadAttachment(item, file) {
  try {
    const data = await readDataUrl(file);
    if (item.kind === 'image') item.thumb = await thumbnail(data);
    renderAttachments();
    const saved = await request('/api/uploads', { method: 'POST', body: JSON.stringify({ name: item.name, data, thumb: item.thumb }) });
    Object.assign(item, { id: saved.id, pages: saved.pages, status: 'ready' });
  } catch (error) {
    Object.assign(item, { status: 'error', error: error.message });
    toast(`${item.name} 没传上去：${error.message}`);
  }
  renderAttachments();
}

// 输入框上方的附件条：缩略图或文件图标、名字，右上角的 × 拿掉
function renderAttachments() {
  const tray = ui.attachTray;
  tray.hidden = !state.attachments.length;
  tray.replaceChildren(...state.attachments.map((item) => {
    const card = el('span', `attach-card ${item.kind} ${item.status}`);
    card.title = item.status === 'error' ? `${item.name}：${item.error}` : item.name;
    if (item.thumb) {
      const img = el('img');
      img.src = item.thumb;
      img.alt = item.name;
      card.append(img);
    } else {
      card.insertAdjacentHTML('afterbegin', svg('file'));
    }
    if (item.kind !== 'image' || item.status !== 'ready') {
      const words = el('span', 'attach-words');
      const note = item.status === 'uploading' ? '正在上传…' : item.status === 'error' ? '没传上去'
        : item.kind === 'pdf' ? `PDF，${item.pages} 页` : item.kind === 'text' ? '文本' : '';
      words.append(el('span', 'attach-name', item.name), el('small', '', note));
      card.append(words);
    }
    const remove = el('button', 'attach-remove');
    remove.type = 'button';
    remove.title = `拿掉 ${item.name}`;
    remove.setAttribute('aria-label', `拿掉 ${item.name}`);
    remove.textContent = '×';
    remove.addEventListener('click', () => {
      state.attachments = state.attachments.filter((other) => other !== item);
      renderAttachments();
      ui.prompt.focus();
    });
    card.append(remove);
    return card;
  }));
  refreshComposer();
}

// ---------- 发送：聊天、出题、组卷，还是把附件里的题存进题库 ----------

// 输入框空着、也没有附件就按下面的设置出题；写了字或者带了附件就先问助手想做什么
function submit() {
  const text = ui.prompt.value.trim();
  if (state.attachments.some((item) => item.status === 'uploading')) {
    toast('附件还在上传，稍等一下再发');
    return;
  }
  const files = state.attachments.filter((item) => item.status === 'ready');
  if (!text && !files.length) {
    startGeneration();
    return;
  }
  converse(text, null, files);
}

// 发出去的话没能变成出题、组卷（比如上一道还在出）：输入框空着就把这句话放回去，免得白写
function restorePrompt(text) {
  if (!text || ui.prompt.value.trim()) return;
  ui.prompt.value = text;
  autosize();
  saveForm();
  refreshComposer();
}

// 助手提取到的出题设置（题型、难度、几道、年级、知识点、用途）填到输入框下方的选项里
function applyGenerate(settings) {
  const f = state.form;
  if (TYPE_LABEL[settings.question_type]) f.qtype = settings.question_type;
  if (settings.difficulty >= 1 && settings.difficulty <= 5) f.difficulty = settings.difficulty;
  if (settings.count >= 1 && settings.count <= 20) f.count = settings.count;
  if (GRADES.includes(settings.grade)) f.grade = settings.grade;
  if (PURPOSES.includes(settings.purpose)) f.purpose = settings.purpose;
  // 说了知识点：在目录里找对应的考点（找不到就不限考点，由出题的模型按原话去对）
  if (settings.topic) f.topic = matchTopic(settings.topic);
  state.blueprint = null;
  afterFormChange();
}

// 助手说的知识点 → 目录里的考点：名字一样的；话里包含了某个考点名的（取最长的）；某一节；
// 只是"导数"这样的大范围、对得上好几个考点的，不硬选
function matchTopic(text) {
  const key = normalize(text);
  if (!key) return null;
  for (const [id, entry] of state.nodes) {
    if (entry.path.length >= 3 && normalize(entry.node.name) === key) return topicFrom(id);
  }
  const inside = state.kpList.filter((item) => item.key.length >= 2 && key.includes(item.key)).sort((a, b) => b.key.length - a.key.length)[0];
  if (inside) return topicFrom(inside.kp.id);
  const sections = [...state.nodes.entries()]
    .filter(([, entry]) => entry.path.length === 3 && normalize(entry.node.name).length >= 2 && key.includes(normalize(entry.node.name)))
    .sort(([, a], [, b]) => normalize(b.node.name).length - normalize(a.node.name).length);
  if (sections.length) return topicFrom(sections[0][0]);
  const around = state.kpList.filter((item) => key.length >= 2 && item.key.includes(key));
  return around.length === 1 ? topicFrom(around[0].kp.id) : null;
}

// 问助手：先在对话里显示这句话（和附件）和"正在想"，回答回来以后，聊天就显示回答；
// 出题、组卷、存题就换成出题（组卷、核对题目）的那一轮。files：带着的附件（重试时用那一轮原来的附件）
async function converse(text, retryTurn = null, files = null) {
  const attachments = files || retryTurn?.attachments || [];
  if (state.pending) {
    toast('上一句还在回答，等它答完再发');
    return;
  }
  const chatId = retryTurn && state.chat ? state.chat.id : state.mode === 'chat' && state.chat ? state.chat.id : newChatId();
  if (!state.chat || state.chat.id !== chatId) resetChat(chatId);
  writeStorage(sessionStorage, CHAT_KEY, chatId);
  const turn = retryTurn || addChatTurn(text, attachments);
  // 前文快满了：这一句发出去之前会先压缩，等得久一点，先说一声
  const ctx = state.context;
  turn.wait(ctx && ctx.compressible && ctx.tokens > ctx.limit * ctx.auto ? '前文太长，先压缩成摘要' : '正在想');
  if (!retryTurn) {
    ui.prompt.value = '';
    autosize();
    saveForm();
    // 带着的附件随这句话发出去，输入框上方的附件条清空
    state.attachments = state.attachments.filter((item) => !attachments.includes(item));
    renderAttachments();
  }
  state.pending = { turn, chat: chatId, text: text || attachments.map((item) => item.name).join('、') };
  setMode('chat');
  scrollToBottom();
  refreshComposer();
  const f = state.form;
  let answer;
  try {
    answer = await request('/api/assistant', {
      method: 'POST',
      body: JSON.stringify({
        text,
        attachments: attachments.map((item) => item.id),
        language: language(), // 界面是英文时，回答也用英文
        session_id: chatId,
        model: mainModel()?.id || null,
        // 按输入框右下角你选的那一档回答（出题选项里的难度不影响聊天）
        thinking: THINKING[f.thinking] ? f.thinking : 'medium',
        touched: state.touched,
        // 出题选项：选了的按选的，没选的是个人设置里的默认（"难一点"这类说法按它算）
        current: (() => {
          const eff = effective();
          return {
            question_type: eff.qtype,
            difficulty: eff.difficulty,
            count: eff.count,
            grade: eff.grade || '',
            topic: f.topic?.name || '',
            purpose: eff.purpose,
          };
        })(),
      }),
    });
  } catch (error) {
    state.pending = null;
    turn.fail(error.message);
    refreshComposer();
    return;
  }
  state.pending = null;
  refreshComposer();
  setContext(answer.context, chatId);
  if (answer.intent === 'chat') {
    turn.show(answer.message);
    if (state.chat?.id === chatId) scrollToBottom();
    loadSessions();
  } else {
    removeTurn(turn);
    if (answer.intent === 'paper') await startAssembly(text, chatId);
    else if (answer.intent === 'import') await startChatImport(text, attachments, chatId);
    else {
      const settings = answer.generate || {};
      applyGenerate(settings);
      // 交给命题员的是助手连同前文整理成的要求；任务卡片上同时显示你的原话
      await startGeneration({ note: settings.requirement || text, said: text, chatId });
    }
  }
  // 发这一句之前自动压缩过前文：重新打开对话，把"已压缩"的分界线排进去
  if (answer.compressed && state.chat?.id === chatId) {
    await openChat(chatId, { quiet: true });
    toast(`前文太长，已把前面 ${answer.compressed.covers_count} 轮压缩成摘要`);
  }
}

// 把附件里的题存进题库：识别题目，没有解析的补写解析，做成核对卡片，确认后才存（见后端 importer.py）
async function startChatImport(text, files, chatId) {
  if (state.job) {
    toast('上一道题还在出，等它出完（或先停止）再存题');
    return;
  }
  try {
    const snap = await request('/api/chat-imports', {
      method: 'POST',
      body: JSON.stringify({
        session_id: chatId,
        uploads: files.map((item) => item.id),
        text,
        model: mainModel()?.id || null,
        thinking: THINKING[state.form.thinking] ? state.form.thinking : 'medium',
        edition: profile().textbook || 'pep_a',
      }),
    });
    startLive(snap.id, snap.session_id || chatId, { import: true, snapshot: snap });
  } catch (error) {
    toast(`没能开始识别：${error.message}`);
  }
}

// 自动组卷：按这句话定方案、从题库挑题，不够的现场出题（见后端 assemble.py）
async function startAssembly(text, chatId = null) {
  if (!text) return;
  if (state.job) {
    toast('上一道题还在出，等它出完（或先停止）再组卷');
    restorePrompt(text);
    return;
  }
  const p = profile();
  const id = chatId || (state.mode === 'chat' && state.chat ? state.chat.id : newChatId());
  const config = {
    ...agentConfig(state.form.mode, { ...p.agent, model: mainModel()?.id }, state.models, state.defaultModel),
    thinking: THINKING[state.form.thinking] ? state.form.thinking : 'medium',
  };
  try {
    const snap = await request('/api/assemblies', {
      method: 'POST',
      body: JSON.stringify({ text, session_id: id, config, meta: { edition: p.textbook || 'pep_a', detail: p.defaults.solution_detail } }),
    });
    state.touched = false;
    startLive(snap.id, snap.session_id || id, { paper: true, snapshot: snap });
  } catch (error) {
    toast(`没能开始组卷：${error.message}`);
    restorePrompt(text);
  }
}

// ---------- 出题 ----------

// 在对话里发出的要求接在这个对话后面；在起始页发出的开一个新对话。
// note：助手判断是出题的那句话（输入框已经清空）；chatId：那句话所在的对话
async function startGeneration(options = {}) {
  const given = options.note !== undefined;
  if (state.job) {
    toast('上一道题还在出，等它结束或先停止');
    if (given) restorePrompt(options.said || options.note);
    return;
  }
  const payload = composeRequest(options);
  const chatId = options.chatId || (state.mode === 'chat' && state.chat ? state.chat.id : newChatId());
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
    if (!given) {
      ui.prompt.value = '';
      autosize();
      saveForm();
    }
    state.touched = false;
    startLive(job.id, job.session_id || chatId, count > 1 ? { count, request: body } : null);
    // 记下发出时的出题选项：题出完以后，没再动过就恢复成不选
    state.job.chips = chipState();
    writeStorage(sessionStorage, JOB_KEY, state.job);
  } catch (error) {
    toast(`没能开始：${error.message}`);
    if (given) restorePrompt(options.said || options.note);
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
    state.form.difficulty = Math.min(5, effective().difficulty + 1);
    label = '难一点';
    extra = stem ? `比上一道题难一档，换一道新题。${previous}` : '';
  } else if (kind === 'easier') {
    state.form.difficulty = Math.max(1, effective().difficulty - 1);
    label = stem ? '简单一点' : '简单一点再试';
    extra = stem ? `比上一道题简单一档，换一道新题。${previous}` : '';
  } else if (kind === 'context') {
    label = '换个情境';
    extra = `考查内容和难度不变，换一个和上一道不同的情境。${previous}`;
  }
  // 上一次没出成：再试时把没出成的原因和上一次的题面一起交给命题员，避开同样的问题，
  // 而不是原样再来一次（那和抽签差不多）
  if (snap.status === 'failed' && (kind === 'retry' || kind === 'easier')) extra = failureContext(snap) || extra;
  afterFormChange();
  startGeneration({ extra, followUp: label });
}

// 没出成的那一次：各角色退回的原因（取最后几条）、最后的报错，以及最后一版题面
function failureContext(snap) {
  const reasons = [];
  for (const event of (snap.events || []).filter((one) => one.kind === 'reject').slice(-4)) {
    const detail = String((event.details || [])[0] || '');
    reasons.push(`${legacyText(event.text)}${detail ? `（${detail}）` : ''}`.slice(0, 160));
  }
  if (snap.error) reasons.push(legacyText(snap.error).slice(0, 160));
  const unique = [...new Set(reasons)].filter(Boolean);
  if (!unique.length) return '';
  const previews = (snap.lanes || []).flatMap((lane) => lane.previews || []);
  const last = String(previews.at(-1)?.statement?.stem_plain || '').replace(/\s+/g, ' ').slice(0, 300);
  const tried = last ? `\n上一次的题面（不要再用）：${last}` : '';
  return `上一次按这个要求没出成，问题出在：${unique.join('；')}。这次换一种设计，避开这些问题。${tried}`.slice(0, 900);
}

function loadBrief(brief, meta, config) {
  const f = state.form;
  if (TYPE_LABEL[brief.question_type]) f.qtype = brief.question_type;
  if (brief.target_difficulty) f.difficulty = brief.target_difficulty;
  if (PURPOSES.includes(brief.teaching_purpose)) f.purpose = brief.teaching_purpose;
  f.grade = GRADES.includes(brief.grade) ? brief.grade : null;
  const topicId = meta.topic_id || brief.curriculum_node_id;
  f.topic = (topicId && topicFrom(topicId)) || (brief.knowledge_point_query ? topicByName(brief.knowledge_point_query) : null);
  if (MODES[config.mode]) f.mode = config.mode;
  state.blueprint = null;
  ui.prompt.value = meta.note ?? (meta.follow_up ? '' : brief.additional_constraints || '');
  autosize();
}

// extra：批量出题时是 { count, request }，正在出的是一整批（state.job.batch 为 true）；
// 自动组卷时是 { paper: true, snapshot }（state.job.paper 为 true）
function startLive(jobId, chatId, extra = null) {
  const paper = Boolean(extra?.paper);
  const imports = Boolean(extra?.import);
  const batch = !paper && !imports && Boolean(extra?.count);
  state.job = { id: jobId, chat: chatId, batch, paper, import: imports, count: extra?.count || 1 };
  state.liveSnapshot = paper || imports ? extra.snapshot : null;
  state.snapshotAt = performance.now();
  writeStorage(sessionStorage, JOB_KEY, state.job);
  if (!state.chat || state.chat.id !== chatId) resetChat(chatId);
  writeStorage(sessionStorage, CHAT_KEY, chatId);
  const turn = imports ? addImportTurn() : paper ? addPaperTurn() : batch ? addBatchTurn() : addTurn();
  turn.jobId = jobId;
  if (imports) {
    turn.snapshot = extra.snapshot;
    turn.render({ live: true });
  }
  if (batch) {
    turn.batchId = jobId;
    turn.count = extra.count;
    turn.request = extra.request;
    turn.render({ live: true });
  }
  if (paper) {
    turn.snapshot = extra.snapshot;
    turn.render({ live: true });
  }
  setMode('chat');
  // 单独一道题马上在右栏显示进度；一批题等第一道写出题干；组卷等整份试卷组好；存题的核对卡片在对话里
  if (!batch && !paper && !imports) focusTurn(turn);
  scrollToBottom();
  setGeometryBusy(true);
  refreshComposer();
  renderSessions();
  poll();
}

async function poll() {
  clearTimeout(state.pollTimer);
  if (!state.job) return;
  const jobId = state.job.id;
  const { batch, paper } = state.job;
  const imports = state.job.import;
  try {
    const url = imports ? `/api/imports/${jobId}` : paper ? `/api/assemblies/${jobId}` : batch ? `/api/batches/${jobId}` : `/api/jobs/${jobId}`;
    const snap = await request(url);
    if (state.job?.id !== jobId) return;
    state.liveSnapshot = snap;
    state.snapshotAt = performance.now();
    const turn = liveTurn();
    if (turn) {
      turn.snapshot = snap;
      if (imports) {
        turn.render({ live: true });
      } else if (paper) {
        turn.render({ live: true });
        refreshSlot(turn);
      } else if (batch) {
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
      state.pollTimer = setTimeout(poll, batch || paper || imports ? 1200 : 700);
    } else {
      finishLive(snap);
    }
  } catch (error) {
    if (/任务不存在/.test(error.message)) {
      // 服务重启过，进行中的题丢了：把这一轮拿掉
      const turn = liveTurn();
      if (turn && !turn.snapshot) {
        removeTurn(turn);
      } else if (turn?.isPaper) {
        // 组卷做到一半：已经出好的题都在题库里，这一轮说明一下没组完
        turn.jobId = null;
        turn.snapshot = { ...turn.snapshot, status: 'failed', error: '服务重启过，这次组卷没有完成。已经现场出好的题都在题库里，可以再组一次。' };
        turn.render();
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
  const sent = state.job.chips ?? null;
  state.job = null;
  writeStorage(sessionStorage, JOB_KEY, null);
  setGeometryBusy(false);
  clearChips(sent); // 这一批出完了：出题选项恢复成不选
  refreshComposer();
  // 每一道都存成了出题记录：重新打开这个对话，这一批就按记录显示（能看题、组卷、进题库）
  if (state.chat?.id === chatId) await openChat(chatId, { quiet: true, focusBatch: { id: snap.id, index: focused } });
  loadSessions();
  refreshCounts();
  const { done, failed } = snap.totals;
  if (snap.status === 'cancelled') toast(`已停下：出好的 ${done} 道已收进题库`);
  else toast(failed ? `出好 ${done} 道，${failed} 道没出成；出好的已收进题库` : `${done} 道题都出好了，已收进题库`);
}

// 组卷结束：这一轮按最后的结果显示；组成了试卷就在右栏打开整份试卷
function finishPaper(snap) {
  const turn = liveTurn();
  const chatId = state.job.chat;
  state.job = null;
  writeStorage(sessionStorage, JOB_KEY, null);
  setGeometryBusy(false);
  refreshComposer();
  if (turn) {
    turn.jobId = null;
    turn.snapshot = snap;
    turn.render();
    if (snap.paper && state.mode === 'chat') focusTurn(turn);
  }
  loadSessions();
  refreshCounts();
  loadContext(chatId);
  if (snap.paper) toast(snap.status === 'cancelled' ? '已停下：用已有的题组成了试卷' : `试卷组好了：${snap.paper.count} 道题，已存进“试卷”`);
  else if (snap.status === 'cancelled') toast('已停下');
}

// 识别完了：这一轮换成核对卡片（题目、答案、解析，勾选后存进题库）
function finishImport(snap) {
  const turn = liveTurn();
  const chatId = state.job.chat;
  state.job = null;
  writeStorage(sessionStorage, JOB_KEY, null);
  setGeometryBusy(false);
  refreshComposer();
  if (turn) {
    turn.jobId = null;
    turn.load(importRecord(snap));
    turn.render();
  }
  loadSessions();
  loadContext(chatId);
  if (snap.status === 'cancelled') toast('已停下');
  else toast(snap.questions.length ? `识别出 ${snap.questions.length} 道题，核对以后再存进题库` : '没有识别出题目');
}

function finishLive(snap) {
  if (state.job?.import) {
    finishImport(snap);
    return;
  }
  if (state.job?.paper) {
    finishPaper(snap);
    return;
  }
  if (state.job?.batch) {
    finishBatch(snap);
    return;
  }
  const turn = liveTurn();
  const chatId = state.job.chat;
  const sent = state.job.chips ?? null;
  state.job = null;
  writeStorage(sessionStorage, JOB_KEY, null);
  if (turn) {
    turn.jobId = null;
    turn.recordId = snap.id;
    renderTurn(turn);
  }
  setGeometryBusy(false);
  clearChips(sent); // 这道题出完了：出题选项恢复成不选
  refreshComposer();
  loadSessions();
  refreshCounts();
  loadContext(chatId);
  if (snap.status === 'succeeded') toast('题目出好了，已收进题库');
  else if (snap.status === 'cancelled') toast('已停下');
}

// 停止：服务端当场掐断正在进行的模型请求，等它收完尾才返回（一般零点几秒），这里马上刷新这一轮，不等下一次轮询
async function stopJob(turn) {
  if (!turn.jobId || turn.stopping) return;
  const kind = turn.isImport ? 'imports' : turn.isPaper ? 'assemblies' : turn.isBatch ? 'batches' : 'jobs';
  turn.stopping = true;
  if (turn.stopBtn) turn.stopBtn.disabled = true;
  try {
    await request(`/api/${kind}/${turn.jobId}/stop`, { method: 'POST' });
  } catch (error) {
    toast(error.message);
    return;
  } finally {
    turn.stopping = false;
    if (turn.stopBtn) turn.stopBtn.disabled = false;
  }
  if (state.job?.id === turn.jobId) poll();
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
  // 新对话里正在出第一道题（或者在等第一句的回答）：服务器上还没有这个对话，先补一条
  const now = new Date().toISOString();
  if (state.job && !items.some((session) => session.id === state.job.chat)) {
    items.unshift({ id: state.job.chat, count: 0, knowledge: '', updated_at: now, fresh: true, title: state.job.paper ? '组卷' : '' });
  }
  if (state.pending && !items.some((session) => session.id === state.pending.chat)) {
    items.unshift({ id: state.pending.chat, count: 0, knowledge: '', updated_at: now, fresh: true, title: state.pending.text.slice(0, 40), chats: 1 });
  }
  if (!items.length) {
    ui.historyList.append(el('li', 'history-empty', '对话会列在这里：出的题、组的卷、聊过的问题都在里面。'));
    return;
  }
  for (const session of items) {
    const live = state.job?.chat === session.id;
    const status = !live && !session.papers && session.last_status && session.last_status !== 'succeeded' ? ` ${session.last_status}` : '';
    const button = el('button', `history-item${live ? ' live' : ''}${status}`);
    button.type = 'button';
    button.dataset.session = session.id;
    button.setAttribute('aria-current', String(state.mode === 'chat' && state.chat?.id === session.id));
    const meta = el('span', 'meta');
    if (live) {
      meta.append(el('span', '', state.job.paper ? '正在组卷' : session.count ? `${session.count} 道题，正在出下一道` : '正在出题'));
    } else {
      // 有组卷的写"组卷"；出过题的写几道题（一道就写题型）；只聊过天的写"对话"
      const kind = session.papers ? '组卷'
        : session.count > 1 ? `${session.count} 道题`
          : session.count ? TYPE_LABEL[session.question_type] || '' : '对话';
      meta.append(el('span', '', kind), el('span', 'num', formatWhen(session.updated_at)));
      if (!session.papers && session.last_status === 'failed') meta.append(el('span', 'flag', '没出成'));
      else if (!session.papers && session.last_status === 'cancelled') meta.append(el('span', 'flag', '已停下'));
    }
    const title = el('span', 'title compact-math'); // 标题是第一句话时可能带公式
    // 置顶的对话标题前有一个小图钉
    if (session.pinned_at) title.insertAdjacentHTML('afterbegin', svg('pin').replace('<svg ', '<svg class="pin-mark" '));
    title.append(sessionTitle(session));
    button.append(title, meta);
    button.title = `${session.knowledge || ''}${session.knowledge ? '\n' : ''}右键：重命名、置顶、删除`;
    button.addEventListener('click', () => openChat(session.id));
    // 右键（或者键盘上的菜单键）：重命名、置顶、删除
    button.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      if (session.fresh) return;
      const box = button.getBoundingClientRect();
      const fromKeyboard = !event.clientX && !event.clientY;
      openRailMenu(session, fromKeyboard ? box.left + 24 : event.clientX, fromKeyboard ? box.bottom : event.clientY, button);
    });
    const item = el('li');
    item.append(button);
    ui.historyList.append(item);
  }
  renderMath(ui.historyList);
}

// 对话的名字：你改过的 → 第一句话或试卷名 → 第一道题考的知识点
function sessionTitle(session) {
  return session.custom_title || session.title || (session.knowledge || '').split(' › ').pop() || (session.fresh ? '新对话' : '考点不限');
}

// 重命名：在列表里把标题换成输入框，回车或点到别处保存，Esc 取消；清空保存就恢复成自动的名字
function renameSession(session) {
  closeRailMenu();
  const button = [...ui.historyList.querySelectorAll('.history-item')].find((node) => node.dataset.session === session.id);
  const title = button?.querySelector('.title');
  if (!title) return;
  const input = el('input', 'rename-input');
  input.type = 'text';
  input.maxLength = 40;
  input.value = sessionTitle(session);
  input.setAttribute('aria-label', '对话名称');
  const holder = el('div', 'history-item renaming');
  holder.append(input);
  button.replaceWith(holder);
  input.focus();
  input.select();
  let done = false;
  const finish = async (keep) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (keep && value !== sessionTitle(session)) {
      try {
        await request(`/api/sessions/${encodeURIComponent(session.id)}/rename`, { method: 'POST', body: JSON.stringify({ title: value }) });
        toast(value ? '已重命名' : '已恢复成自动的名字');
      } catch (error) {
        toast(error.message);
      }
    }
    await loadSessions();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}

// ---------- 对话列表的右键菜单：重命名、置顶、删除（删除前在菜单里确认一次） ----------

const railMenu = { node: null, anchor: null };

function closeRailMenu(restoreFocus = false) {
  if (!railMenu.node) return;
  railMenu.node.remove();
  if (restoreFocus) railMenu.anchor?.focus();
  railMenu.node = null;
  railMenu.anchor = null;
}

function openRailMenu(session, x, y, anchor) {
  closeRailMenu();
  closePopover();
  const menu = el('div', 'popover rail-menu');
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', `对话「${sessionTitle(session)}」`);
  railMenu.node = menu;
  railMenu.anchor = anchor;
  const place = () => {
    const box = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - box.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - box.height - 8))}px`;
  };
  const item = (label, icon, onClick, className = '') => {
    const button = el('button', `rail-menu-item${className ? ` ${className}` : ''}`);
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    button.innerHTML = svg(icon);
    button.append(el('span', '', label));
    button.addEventListener('click', onClick);
    return button;
  };
  const confirm = () => {
    const box = el('div', 'rail-confirm');
    const note = el('p', 'note', '这个对话里的出题记录、聊天和组卷过程会删掉，不能恢复。出好的题已经收进题库、组好的试卷也还在，不受影响。');
    box.append(el('p', 'ask', `删除「${sessionTitle(session)}」？`), note);
    // 这个对话出的题也在题库里：可以勾选一起删掉（题出得不好、删对话时多半也不想留着）；组进试卷的留着
    const withBank = el('input');
    withBank.type = 'checkbox';
    const option = el('label', 'rail-confirm-option');
    option.hidden = true;
    const optionText = el('span');
    option.append(withBank, optionText);
    box.append(option);
    request(`/api/sessions/${encodeURIComponent(session.id)}/bank`).then((info) => {
      const removable = info.count - info.in_papers;
      if (!option.isConnected) return;
      if (removable <= 0) {
        // 出的题都组进了试卷：说明为什么不能顺带删（要删先从试卷里移出去）
        if (info.count) note.textContent = `这个对话里的出题记录、聊天和组卷过程会删掉，不能恢复。这里出的 ${info.count} 道题组进了试卷，会留在题库里；要删，先从试卷里移出去。`;
        place();
        return;
      }
      note.textContent = '这个对话里的出题记录、聊天和组卷过程会删掉，不能恢复。组好的试卷还在，不受影响。';
      optionText.textContent = `同时从题库删掉这里出的 ${removable} 道题${info.in_papers ? `（另有 ${info.in_papers} 道组进了试卷，保留）` : ''}`;
      option.hidden = false;
      place();
    }).catch(() => {});
    const actions = el('div', 'rail-confirm-actions');
    const cancel = el('button', 'btn', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', main);
    const remove = el('button', 'btn danger', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => deleteSession(session, remove, !option.hidden && withBank.checked));
    actions.append(cancel, remove);
    box.append(actions);
    menu.replaceChildren(box);
    place();
    cancel.focus();
  };
  function main() {
    menu.replaceChildren(
      item('重命名', 'pencil', () => renameSession(session)),
      item(session.pinned_at ? '取消置顶' : '置顶', 'pin', () => pinSession(session, !session.pinned_at)),
      item('删除', 'trash', confirm, 'danger'),
    );
    place();
    menu.querySelector('button')?.focus({ preventScroll: true });
  }
  document.body.append(menu);
  main();
}

async function pinSession(session, pinned) {
  closeRailMenu(true);
  try {
    await request(`/api/sessions/${encodeURIComponent(session.id)}/pin`, { method: 'POST', body: JSON.stringify({ pinned }) });
    await loadSessions();
    toast(pinned ? '已置顶' : '已取消置顶');
  } catch (error) {
    toast(error.message);
  }
}

async function deleteSession(session, button, withBank = false) {
  if (state.job?.chat === session.id) {
    toast('这个对话里还有题在出，等它出完或先停止，再删除');
    return;
  }
  if (state.pending?.chat === session.id) {
    toast('这个对话里还有一句在回答，等它答完再删除');
    return;
  }
  button.disabled = true;
  let result;
  try {
    const query = withBank ? '?bank_items=true' : '';
    result = await request(`/api/sessions/${encodeURIComponent(session.id)}${query}`, { method: 'DELETE' });
  } catch (error) {
    button.disabled = false;
    toast(error.message);
    return;
  }
  setCounts(result?.counts);
  closeRailMenu();
  // 删的正是打开着的对话：回到起始页
  if (state.chat?.id === session.id) {
    resetChat(null);
    setMode('idle');
    renderGreeting();
    shuffleSuggestions();
  }
  await loadSessions();
  toast(result?.bank_deleted ? `已删除对话，题库里的 ${result.bank_deleted} 道题也删掉了` : '已删除对话');
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
  // 同一批出的题合成一轮（按第一道出现的位置）；聊天、组卷各是一轮；组卷时现场出的题列在组卷那一轮里
  const batches = new Map();
  const assemblies = new Map();
  for (const record of records) {
    if (record.kind === 'chat') {
      addChatTurn(record.text || '', record.attachments || []).show(record);
      continue;
    }
    if (record.kind === 'import') {
      const turn = addImportTurn();
      turn.load(record);
      turn.render();
      continue;
    }
    if (record.kind === 'summary') {
      appendTurn(new SummaryTurn(record));
      continue;
    }
    if (record.kind === 'paper') {
      const turn = addPaperTurn();
      turn.load(record);
      turn.render();
      assemblies.set(record.assembly_id, turn);
      continue;
    }
    if (record.request?.meta?.assembly_id) continue;
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
  // 正在识别附件里的题：做完才存记录，这里接上正在做的那一轮
  if (hasJob && state.job.import && !records.some((record) => record.import_id === state.job.id)) {
    const turn = addImportTurn();
    turn.jobId = state.job.id;
    if (state.liveSnapshot?.id === state.job.id) {
      turn.snapshot = state.liveSnapshot;
      turn.render({ live: true });
    }
  }
  // 正在组卷：组完才存记录，这里接上正在组的那一轮
  if (hasJob && state.job.paper && !assemblies.has(state.job.id)) {
    const turn = addPaperTurn();
    turn.jobId = state.job.id;
    if (state.liveSnapshot?.id === state.job.id) {
      turn.snapshot = state.liveSnapshot;
      turn.render({ live: true });
    }
  }
  if (hasJob && !state.job.batch && !state.job.paper && !state.job.import && !records.some((record) => record.id === state.job.id)) {
    const turn = addTurn();
    turn.jobId = state.job.id;
    if (state.liveSnapshot) {
      turn.snapshot = state.liveSnapshot;
      turn.render({ live: true });
    }
  }
  // 这个对话里还有一句在等回答：接回到最后（回答回来以后就显示在这一轮里）
  if (state.pending?.chat === id && !state.chat.turns.includes(state.pending.turn)) appendTurn(state.pending.turn);
  setMode('chat');
  const target = focusBatch && batches.get(focusBatch.id);
  if (target && focusBatch.index) showBatchItem(target, focusBatch.index, false);
  else {
    // 右栏放最近的一道题（或组好的试卷）；只聊过天的对话不开右栏
    const last = target || [...state.chat.turns].reverse()
      .find((turn) => !turn.isChat && !turn.isSummary && !turn.isImport && !(turn.isPaper && !turn.paper));
    if (last) focusTurn(last);
  }
  scrollToBottom();
  loadContext(id);
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
  // 附件：粘贴截图或文件、拖进输入框、点回形针
  ui.prompt.addEventListener('paste', (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    addFiles(files);
  });
  ui.composer.addEventListener('dragover', (event) => {
    if (![...(event.dataTransfer?.types || [])].includes('Files')) return;
    event.preventDefault();
    ui.composer.classList.add('dropping');
  });
  ui.composer.addEventListener('dragleave', (event) => {
    if (!ui.composer.contains(event.relatedTarget)) ui.composer.classList.remove('dropping');
  });
  ui.composer.addEventListener('drop', (event) => {
    if (!event.dataTransfer?.files?.length) return;
    event.preventDefault();
    ui.composer.classList.remove('dropping');
    addFiles(event.dataTransfer.files);
  });
  ui.attachBtn.addEventListener('click', () => ui.attachInput.click());
  ui.attachInput.addEventListener('change', () => {
    addFiles(ui.attachInput.files);
    ui.attachInput.value = '';
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
  // 换了主题：右栏正在显示的整份试卷按新主题重新载入（预览页四周的底色跟着变）
  window.addEventListener('tetra:theme', () => {
    if (state.focus?.isPaper) {
      state.focus.sheetKey = '';
      renderSheet(state.focus, { animate: false });
    }
  });
  // 对话列表的右键菜单：点在菜单外面、按 Esc、窗口大小变了或者列表滚动了，就收起来
  document.addEventListener('pointerdown', (event) => {
    if (railMenu.node && !event.composedPath().includes(railMenu.node)) closeRailMenu();
  }, true);
  window.addEventListener('resize', () => closeRailMenu());
  ui.historyList.addEventListener('scroll', () => closeRailMenu());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (railMenu.node) {
      closeRailMenu(true);
      return;
    }
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
  startI18n(); // 选了英文：把界面文字换成英文（题目内容不换）
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
  watchComposerWidth();
  renderGreeting();
  await checkHealth();
  await loadProfile();
  try {
    const [tree, blueprints] = await Promise.all([request('/api/curriculum'), request('/api/blueprints')]);
    state.tree = tree;
    state.blueprints = blueprints;
    setBlueprints(blueprints);
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
