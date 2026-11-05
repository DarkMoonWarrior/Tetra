// 公共常量与小工具

import { autoMath } from './automath.js';

export { autoMath };

export const TYPE_LABEL = {
  single_choice: '单选题',
  multiple_choice: '多选题',
  fill_blank: '填空题',
  open_response: '解答题',
};
export const DIFFICULTY_LABEL = { 1: '基础', 2: '较易', 3: '中档', 4: '较难', 5: '压轴' };
export const PURPOSES = ['课堂练习', '课后作业', '单元测验', '高考复习'];
export const GRADES = ['高一', '高二', '高三'];

// 角色：界面上的名字、进度清单里没开始时的说明
export const ROLES = {
  composer: { name: '命题员', todo: '构思题干和选项' },
  reviewer: { name: '审题员', todo: '检查条件是否充分、有没有超纲、表述是否严谨' },
  solver: { name: '解题员', todo: '规划思路，写出完整解析' },
  auditor: { name: '复核员', todo: '从题面重新求解，核对解析和答案' },
  verifier: { name: '验算员', todo: '只看题面独立算一遍，再和解析的答案核对' },
  editor: { name: '整理员', todo: '把核对过的推导整理成标准答案的写法' },
  illustrator: { name: '配图员', todo: '按题干画配图，再核对图和题干是否一致' },
  check: { name: '检查', todo: '' },
};

// 解析详略：题目通过检查后，整理员按这个把解题员的完整推导改写；"详细"不改写
export const DETAIL = {
  concise: { name: '简洁', note: '像试卷后面的参考答案：两三步关键式子，最后"故选／故答案为"' },
  standard: { name: '标准', note: '像教辅书上的解析：三到六步，每步带半句依据' },
  detailed: { name: '详细', note: '保留解题员写的完整推导，每一步都写清依据，适合学生自学' },
};

// 出题方式：三个预设，加一个按个人设置走的"自定义"
export const MODES = {
  fast: { name: '快速出题', note: '一个方案，所有角色都用主模型；简单题通常半分钟内出好' },
  careful: { name: '多一道把关', note: '审题员和复核员换成推理更强的模型，题目更稳，会慢一些' },
  strict: { name: '最严格', note: '再请验算员只看题面独立解一遍，答案对不上就整题重出' },
  custom: { name: '自定义', note: '按个人设置里的方案数、验算和把关模型' },
};
const CHECKERS = ['reviewer', 'auditor', 'verifier'];

// 思考强度，从低到高四档（滑块按这个顺序排）：
// 高 = 构造题目、审题、规划解法、复核这几步先让模型想清楚再答；极高 = 同样这几步，用模型最深的一档思考；
// 中 = 只在构造题目和规划解法时想，审题、复核直接答；低 = 都不开，最快。
// 4、5 星（较难、压轴）的题至少用高：低、中选不了，可以在高和极高之间切换，服务端也会把低、中改成高
export const THINKING = {
  low: { name: '低', note: '不开思考模式，出题最快、最省 Token，适合基础题' },
  medium: { name: '中', note: '构造题目和规划解法时先想清楚，审题、复核直接答；比高快，比低稳' },
  high: { name: '高', note: '构造题目、审题、规划解法和复核时都先想清楚再答，算得更准，会慢一些' },
  max: { name: '极高', note: '最高的思考强度，但是会更快地消耗 Token' },
};
export const FORCE_THINKING_FROM = 4;
const THINKING_ORDER = Object.keys(THINKING);
export const thinkingFor = (difficulty, chosen) => {
  const level = THINKING[chosen] ? chosen : 'medium';
  return difficulty >= FORCE_THINKING_FROM && THINKING_ORDER.indexOf(level) < THINKING_ORDER.indexOf('high') ? 'high' : level;
};

// 把关用的"增强"模型：显示名称里有"增强"，或型号像各家的推理／旗舰型号；
// 优先和主模型同一家服务，那家没有再看别家，都没有就是 null（把关也用主模型）
const isStrong = (m) => /增强/.test(m.label || '') || /pro|reasoner|opus|astra/i.test(m.model || m.id);
export function strongModel(models, mainId = null) {
  const service = (id) => String(id || '').split(':')[0];
  const sameService = mainId ? models.filter((m) => service(m.id) === service(mainId)) : [];
  return sameService.find(isStrong) || models.find(isStrong) || null;
}

// 界面上显示接入的模型本身的名字（比如 deepseek-flash、glm-5.3）；
// 编号是"服务:型号"，找不到（那个服务已经删掉了）就去掉前面的服务编号
let catalog = [];
export function setModelCatalog(models) {
  catalog = models || [];
}

export function modelName(models, id) {
  return models.find((m) => m.id === id)?.model || modelLabel(id);
}

// 调用明细里记的是原始型号，按型号或编号找回名字
export function modelLabel(raw) {
  const found = catalog.find((m) => m.id === raw || m.model === raw);
  return found?.model || String(raw || '').split(':').pop() || '模型';
}

// 出题方式 → 发给后端的配置：几个方案、要不要验算、每个角色用哪个模型
export function agentConfig(mode, agent, models, fallback) {
  const known = new Set(models.map((m) => m.id));
  const main = known.has(agent?.model) ? agent.model : fallback;
  const strong = strongModel(models, main)?.id || main;
  let lanes = 1;
  let verify = false;
  let check = main;
  if (mode === 'careful') check = strong;
  if (mode === 'strict') {
    check = strong;
    verify = true;
  }
  if (mode === 'custom') {
    lanes = Math.min(3, Math.max(1, Number(agent?.lanes) || 1));
    verify = Boolean(agent?.verify);
    check = known.has(agent?.check_model) ? agent.check_model : main;
  }
  const roles = { composer: main, solver: main };
  for (const role of CHECKERS) roles[role] = check;
  return { mode, lanes, verify, model: main, roles };
}

// 把一次出题的配置说成几个短标签，旧记录（没有 mode）也能用
export function describeConfig(config, models) {
  if (!config) return [];
  const tags = [];
  if (MODES[config.mode]) tags.push(MODES[config.mode].name);
  const roles = config.roles || {};
  const main = config.model || roles.composer;
  if (main) tags.push(modelName(models, main));
  const checks = [...new Set(CHECKERS.map((r) => roles[r]).filter((m) => m && m !== main))];
  if (checks.length === 1) tags.push(`把关用 ${modelName(models, checks[0])}`);
  if ((config.lanes || 1) > 1) tags.push(`同时试 ${config.lanes} 个方案`);
  if (config.verify) tags.push('独立验算');
  // 写成"思考强度中"：只写"思考中"像是在说模型正在思考
  if (THINKING[config.thinking]) tags.push(`思考强度${THINKING[config.thinking].name}`);
  return tags;
}

export function greeting(date = new Date()) {
  const h = date.getHours();
  if (h < 5) return '夜深了';
  if (h < 9) return '早上好';
  if (h < 12) return '上午好';
  if (h < 14) return '中午好';
  if (h < 18) return '下午好';
  return '晚上好';
}

// 早期记录里的说法统一成现在的
export function legacyText(text) {
  return String(text || '')
    .replace(/^命题组 ([A-C]) 率先交卷，其余组停止$/, '采用方案 $1，其余方案停下')
    .replace(/^命题组 ([A-C]) 没能交卷：/, '方案 $1 没出成：')
    .replace('这一版整题作废，重新命题。', '这一版不合格，重新出题。')
    .replace('独立验算和解析答案对不上，整题重出。', '独立验算的答案和解析对不上，重新出题。')
    .replace('所有命题组都没能交出通过审核的题。', '没能得到通过全部检查的题：')
    .replace(/命题组 ([A-C])：/g, '方案 $1：')
    // 早期的报错里写着厂商名（"DeepSeek 返回……"），界面上统一说"模型"
    .replace(/DEEPSEEK_API_KEY/g, '模型的 API Key')
    .replace(/DeepSeek\s*/g, '模型');
}

const ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  sliders: '<path d="M4 6h9"/><path d="M17 6h3"/><circle cx="15" cy="6" r="2"/><path d="M4 12h3"/><path d="M11 12h9"/><circle cx="9" cy="12" r="2"/><path d="M4 18h11"/><path d="M19 18h1"/><circle cx="17" cy="18" r="2"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
  bulb: '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7V16h8v-1.3A7 7 0 0 0 12 2Z"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
  pin: '<path d="M12 17v5"/><path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8a2 2 0 0 0-1.1-1.8l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  pencil: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  palette: '<circle cx="13.5" cy="6.5" r="1.5"/><circle cx="17.5" cy="10.5" r="1.5"/><circle cx="8.5" cy="7.5" r="1.5"/><circle cx="6.5" cy="12.5" r="1.5"/><path d="M12 2a10 10 0 0 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.3-.3-.4-.5-.8-.5-1.3 0-1.1.9-2 2-2h2.3A4.7 4.7 0 0 0 22 10.7C22 5.9 17.5 2 12 2Z"/>',
  paperclip: '<path d="m21.4 11.6-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6"/>',
};

export function svg(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}

// 回复头像：四面体的六条棱（虚线是看不见的那条棱）
export const AVATAR = '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
  + '<line x1="15.8" y1="3.5" x2="4" y2="22.8"/>'
  + '<line x1="15.8" y1="3.5" x2="13.2" y2="28.8"/>'
  + '<line x1="15.8" y1="3.5" x2="28.2" y2="21.6"/>'
  + '<line x1="4" y1="22.8" x2="13.2" y2="28.8"/>'
  + '<line x1="13.2" y1="28.8" x2="28.2" y2="21.6"/>'
  + '<line x1="4" y1="22.8" x2="28.2" y2="21.6" stroke-width="1.5" stroke-dasharray="2 2.4"/>'
  + '</svg>';

// 出题时头像一遍遍"化成光点—向外扩散—再聚回来"：棱线淡出，沿棱的一串小光点向四周散开、
// 越散越淡，中间荡出两圈光晕；然后光点流回原位，棱线重新亮起。
// 停的时候把这一轮做完（回到完整的四面体）再停，不会突然跳回原样。
const CYCLE_MS = 3000;
const EASE = 'cubic-bezier(0.45, 0, 0.25, 1)';
const BURST = 'cubic-bezier(0.15, 0.7, 0.3, 1)';
const SVG_NS = 'http://www.w3.org/2000/svg';
const CENTER = [15.3, 19.2]; // 四个顶点的重心，光点从这里往外散

// 沿每条棱取一串光点，各自定好往哪个方向、散多远、多亮
function makeSparks(svgEl) {
  const group = document.createElementNS(SVG_NS, 'g');
  group.setAttribute('class', 'sparks');
  const sparks = [];
  for (const line of svgEl.querySelectorAll('line')) {
    const [x1, y1, x2, y2] = ['x1', 'y1', 'x2', 'y2'].map((key) => Number(line.getAttribute(key)));
    const hidden = line.hasAttribute('stroke-dasharray');
    const count = Math.max(4, Math.round(Math.hypot(x2 - x1, y2 - y1) / (hidden ? 3 : 1.5)));
    for (let i = 0; i <= count; i += 1) {
      const x = x1 + ((x2 - x1) * i) / count;
      const y = y1 + ((y2 - y1) * i) / count;
      const dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('cx', x.toFixed(2));
      dot.setAttribute('cy', y.toFixed(2));
      dot.setAttribute('r', (hidden ? 0.45 : 0.45 + Math.random() * 0.4).toFixed(2));
      dot.setAttribute('opacity', '0');
      group.append(dot);
      const angle = Math.atan2(y - CENTER[1], x - CENTER[0]) + (Math.random() - 0.5) * 1.3;
      const reach = 4 + Math.random() * 7;
      sparks.push({ dot, dx: Math.cos(angle) * reach, dy: Math.sin(angle) * reach, glow: 0.6 + Math.random() * 0.4 });
    }
  }
  // 两圈向外荡开的光晕
  const rings = [0, 1].map(() => {
    const ring = document.createElementNS(SVG_NS, 'circle');
    ring.setAttribute('class', 'ring');
    ring.setAttribute('cx', String(CENTER[0]));
    ring.setAttribute('cy', String(CENTER[1] - 2));
    ring.setAttribute('r', '7');
    ring.setAttribute('opacity', '0');
    svgEl.insertBefore(ring, svgEl.firstChild);
    return ring;
  });
  svgEl.append(group);
  return { sparks, rings };
}

export function spin(node, on) {
  if (!node || typeof node.animate !== 'function') return;
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  const current = node.partAnimations || [];
  const endless = current.length > 0 && current[0].effect.getTiming().iterations === Infinity && current[0].playState !== 'finished';
  if (on) {
    if (endless) return;
    current.forEach((animation) => animation.cancel());
    node.effects = node.effects || makeSparks(node);
    const timing = { duration: CYCLE_MS, iterations: Infinity };
    // 棱线：先亮着，化开时淡出，光点聚回来时再亮起
    const animations = [...node.querySelectorAll('line')].map((line) => line.animate([
      { opacity: 1, offset: 0 },
      { opacity: 1, offset: 0.1, easing: EASE },
      { opacity: 0, offset: 0.26 },
      { opacity: 0, offset: 0.76, easing: EASE },
      { opacity: 1, offset: 0.9 },
      { opacity: 1, offset: 1 },
    ], timing));
    const home = 'translate(0px, 0px)';
    for (const { dot, dx, dy, glow } of node.effects.sparks) {
      const out = `translate(${dx.toFixed(2)}px, ${dy.toFixed(2)}px)`;
      const drift = `translate(${(dx * 1.35).toFixed(2)}px, ${(dy * 1.35).toFixed(2)}px)`;
      animations.push(dot.animate([
        { transform: home, opacity: 0, offset: 0 },
        { transform: home, opacity: 0, offset: 0.1, easing: EASE },
        { transform: home, opacity: 1, offset: 0.2, easing: BURST },
        { transform: out, opacity: glow, offset: 0.44, easing: 'linear' },
        { transform: drift, opacity: glow * 0.35, offset: 0.6, easing: EASE },
        { transform: home, opacity: 1, offset: 0.84, easing: EASE },
        { transform: home, opacity: 0, offset: 0.93 },
        { transform: home, opacity: 0, offset: 1 },
      ], timing));
    }
    node.effects.rings.forEach((ring, k) => {
      const start = 0.18 + k * 0.12;
      animations.push(ring.animate([
        { transform: 'scale(0.35)', opacity: 0, offset: 0 },
        { transform: 'scale(0.35)', opacity: 0, offset: start },
        { transform: 'scale(0.55)', opacity: 0.55, offset: start + 0.04, easing: 'cubic-bezier(0.2, 0.6, 0.35, 1)' },
        { transform: 'scale(2.1)', opacity: 0, offset: start + 0.36 },
        { transform: 'scale(2.1)', opacity: 0, offset: 1 },
      ], timing));
    });
    node.partAnimations = animations;
  } else if (endless) {
    const rounds = Math.max(1, Math.ceil((current[0].currentTime || 0) / CYCLE_MS));
    current.forEach((animation) => animation.effect.updateTiming({ iterations: rounds }));
  }
}

export const $ = (selector, root = document) => root.querySelector(selector);

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

export function mathText(tag, className, text) {
  const node = el(tag, className);
  node.style.whiteSpace = 'pre-line';
  node.textContent = text || '';
  return node;
}

// 模拟卷的命题要求有两种写法：topic 是纯文本（填进输入框、搜索、发给模型），topic_tex 是排版写法（公式在 $…$ 里）。
// 任务卡片上的"题目内容：……"来自输入框，是纯文本，显示时换回排版写法
let topicTex = new Map();
export function setBlueprints(specs) {
  topicTex = new Map(specs.filter((spec) => spec.topic_tex).flatMap((spec) => [
    [spec.topic, spec.topic_tex],
    [spec.topic.replaceAll('e^x', 'eˣ'), spec.topic_tex], // 早期的记录里写的是 eˣ
  ]));
}
export function typesetTopic(text) {
  return String(text ?? '').replace(/(题目内容：)(.+)/g, (all, lead, topic) => lead + (topicTex.get(topic) || autoMath(topic)));
}

// 行内公式也按试卷的排法用"显示"样式：分数和正文一样大（相当于 \dfrac），
// 不像 LaTeX 默认那样缩成小一号；指数、下标里的分数仍按上下标的大小排
export const displayStyle = (math) => `\\displaystyle ${math}`;

// 浏览器在公式和紧跟着的字之间总可以折行，"$…$，"里的"，"就会单独跑到下一行开头。
// 渲染前把紧挨着行内公式的中文标点挪进公式（\text{，}），和公式排在一起；
// 字体、字号在 app.css 里调回和正文一样（.cjk_fallback）
const INLINE_MATH = /([（《「『【]*)(?<![$\\])\$(?!\$)((?:\\.|[^$\\])+)\$(?!\$)([，。；：、！？）》」』】]*)/g;
export function gluePunctuation(text) {
  return text.replace(INLINE_MATH, (all, open, body, close) => (open || close
    ? `$${open ? `\\text{${open}}` : ''}${body}${close ? `\\text{${close}}` : ''}$`
    : all));
}

const NO_MATH = '.katex, script, noscript, style, textarea, pre, code, option';

function glueInElement(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => (node.data.includes('$') && !node.parentElement?.closest(NO_MATH)
      ? NodeFilter.FILTER_ACCEPT
      : NodeFilter.FILTER_REJECT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const glued = gluePunctuation(node.data);
    if (glued !== node.data) node.data = glued;
  }
}

const MATH_OPTIONS = {
  delimiters: [
    { left: '$$', right: '$$', display: true },
    { left: '\\[', right: '\\]', display: true },
    { left: '$', right: '$', display: false },
    { left: '\\(', right: '\\)', display: false },
  ],
  throwOnError: false,
};

export function renderMath(root) {
  if (typeof window.renderMathInElement !== 'function') return;
  glueInElement(root);
  // 只排一行、放不下整号分数的地方（试卷里的题目列表）标了 .compact-math：按 LaTeX 默认的行内样式排，
  // 题目里写的 \dfrac 也当 \frac，免得藏在省略号后面的分数把这一行撑高
  const compact = root.matches('.compact-math') ? [root] : root.querySelectorAll('.compact-math');
  for (const node of compact) window.renderMathInElement(node, { ...MATH_OPTIONS, macros: { '\\dfrac': '\\frac' } });
  window.renderMathInElement(root, { ...MATH_OPTIONS, preProcess: displayStyle });
}

// 服务关掉了（或者还没启动）时，fetch 只会抛一句英文的 "Failed to fetch"；换成说得清的提示，
// 并通知页面在顶部常驻"连不上服务"、自动重连
export const OFFLINE_MESSAGE = '连不上服务：可能已经关掉了。在终端输入 tetra 重新启动，连上后页面会自动恢复。';

export async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  } catch {
    window.dispatchEvent(new Event('tetra:offline'));
    throw new Error(OFFLINE_MESSAGE);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // 非 JSON 响应，按状态码报错
  }
  if (!response.ok) {
    const detail = data && data.detail;
    // 表单校验没通过时 detail 是一个列表，取第一条的说明（去掉英文前缀）
    const first = Array.isArray(detail) ? String(detail[0]?.msg || '').replace(/^Value error,\s*/, '') : '';
    throw new Error(typeof detail === 'string' ? detail : first || `请求失败（${response.status}）`);
  }
  return data;
}

export const normalize = (value) => String(value || '').replace(/\s+/g, '').toLowerCase();

export function formatClock(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export function formatTokens(n) {
  if (!n) return '0';
  if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 0 : 1)} 万`;
  return String(n);
}

export function formatWhen(iso) {
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function readStorage(storage, key) {
  try {
    return JSON.parse(storage.getItem(key) || 'null');
  } catch {
    return null;
  }
}

export function writeStorage(storage, key, value) {
  try {
    if (value === null) storage.removeItem(key);
    else storage.setItem(key, JSON.stringify(value));
  } catch {
    // 隐私模式等情况下存储不可用，忽略
  }
}

// 和后端 team.role_of 一致：根据阶段名判断角色（旧记录里没有存角色时用）
export function roleOfStage(stage) {
  const s = String(stage || '').replace(/-retry-\d+$/, '');
  if (s.startsWith('independent-solve') || s.startsWith('answer-equivalence')) return 'verifier';
  if (s.startsWith('solution-condense')) return 'editor';
  if (s.startsWith('figure-')) return 'illustrator';
  if (s === 'question-review' || s === 'solution-regeneration-question-review' || /-review-check(?:-\d+)?$/.test(s)) return 'reviewer';
  if (s.startsWith('completed-solution-review') || s.startsWith('solution-regeneration-verification')) return 'auditor';
  if (s.startsWith('solution-')) return 'solver';
  return 'composer';
}

// 把阶段名翻译成"这个角色正在做什么"
export function describeStage(stage) {
  const retry = stage.match(/-retry-(\d+)$/);
  const s = stage.replace(/-retry-\d+$/, '');
  let m;
  let text;
  let repair = false;
  if (s === 'core' || s === 'single') text = '构思题干与选项';
  else if (s === 'core-high-school-repair') { text = '改掉超出高中范围的内容'; repair = true; }
  else if (s === 'question-review') text = '审查题干的条件、范围与表述';
  else if (/-review-check(?:-\d+)?$/.test(s)) text = '复查修改后的题干';
  else if (s.startsWith('core-question-repair')) { text = '按审题意见修改题干'; repair = true; }
  else if (s.startsWith('answer-gate-repair')) { text = '按答案核对结果修改题干'; repair = true; }
  else if (s === 'answer-gate-equivalence') text = '核对命题构造和审题员的答案';
  else if (s.startsWith('post-solution-question-repair')) { text = '根据解题中发现的问题修改题干'; repair = true; }
  else if (s === 'solution-plan') text = '规划解题思路';
  else if ((m = s.match(/^solution-section-(\d+)-subplan$/))) text = `细分第 ${m[1]} 部分`;
  else if ((m = s.match(/^solution-section-(\d+)-part-(\d+)$/))) text = `写第 ${m[1]} 部分第 ${m[2]} 段`;
  else if ((m = s.match(/^solution-section-(\d+)-cleanup$/))) text = `润色第 ${m[1]} 部分`;
  else if ((m = s.match(/^solution-section-(\d+)$/))) text = `写第 ${m[1]} 部分解析`;
  else if (s === 'solution-conclusion') text = '得出最终答案';
  else if (s === 'solution-condense') text = '整理成标准答案的写法';
  else if (s === 'figure-draw') text = '按题干画配图';
  else if (s === 'figure-redraw') { text = '按核对意见重画配图'; repair = true; }
  else if (s === 'completed-solution-review') text = '复核完整解析与答案';
  else if (s === 'completed-solution-review-equivalence') text = '核对两份答案是否等价';
  else if (s.startsWith('solution-regeneration')) { text = '重写解析'; repair = true; }
  else if (s === 'independent-solve') text = '只看题面独立解题';
  else if (s === 'answer-equivalence') text = '核对两份答案是否等价';
  else text = s;
  // 主流程里 -retry-N 表示第 N 次尝试；重写解析从 -retry-1 起表示第 2 次
  const attempt = retry ? Number(retry[1]) + (s.startsWith('solution-regeneration') ? 1 : 0) : 0;
  return { text: attempt ? `${text}（第 ${attempt} 次）` : text, repair };
}

// 分段按钮：一组里只能选一个
export function segmented(options, current, onPick, label) {
  const group = el('div', 'segmented');
  group.setAttribute('role', 'group');
  if (label) group.setAttribute('aria-label', label);
  for (const option of options) {
    const button = el('button', '', option.label);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(option.value === current));
    button.addEventListener('click', () => {
      group.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
      onPick(option.value);
    });
    group.append(button);
  }
  return group;
}

export function pips(level) {
  const wrap = el('span', 'pips');
  wrap.setAttribute('aria-label', `难度 ${level}/5`);
  for (let i = 1; i <= 5; i += 1) wrap.append(el('i', i <= level ? 'on' : ''));
  return wrap;
}

let toastTimer = null;
export function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.hidden = false;
  node.classList.remove('show');
  void node.offsetWidth;
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 2800);
}
