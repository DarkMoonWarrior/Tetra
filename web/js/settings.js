// 个人设置：左下角的账号按钮、设置菜单和设置窗口。
// 参考原系统 math-teacher-agent 的"教师偏好"：只存结构化的偏好；偏好学习默认关闭，
// 建议要你点"采用"才生效；可以导出，也可以一次删掉全部偏好（出题记录不受影响）。

import {
  $, DETAIL, DIFFICULTY_LABEL, MODES, PURPOSES, THINKING, TYPE_LABEL, el, formatWhen, modelName, request, runtime, segmented,
  strongModel, svg, toast,
} from './shared.js';
import { applyTheme } from './theme.js';
import { setLanguage } from './i18n.js';

const SECTIONS = [
  { id: 'profile', name: '个人资料', icon: 'user' },
  { id: 'appearance', name: '外观与语言', icon: 'palette' },
  { id: 'defaults', name: '出题偏好', icon: 'sliders' },
  { id: 'models', name: '模型服务', icon: 'server' },
  { id: 'agent', name: '出题方式与模型', icon: 'layers' },
  { id: 'learning', name: '偏好学习', icon: 'bulb' },
  { id: 'data', name: '数据与隐私', icon: 'shield' },
];

// 设置里的各页：桌面版窗口里多一页"桌面版"（开机自启、快捷方式、导出位置、数据文件夹）
function sections() {
  return runtime.desktop ? [...SECTIONS, { id: 'desktop', name: '桌面版', icon: 'monitor' }] : SECTIONS;
}

// 读不到服务端设置时按这个来，和后端 Profile 的默认值一致
const FALLBACK = {
  display_name: '',
  avatar: '',
  grade: '',
  theme: 'light',
  language: 'zh',
  defaults: { question_type: 'single_choice', difficulty: 3, purpose: '课堂练习', solution_detail: 'standard', standing_request: '' },
  agent: { mode: 'fast', thinking: 'medium', model: null, lanes: 1, verify: false, check_model: null },
  learning: { enabled: false, feedback: [] },
  updated_at: null,
};

const store = { profile: null, proposals: [], resetPhrase: '删除全部偏好', listeners: new Set() };
let ctx = { models: () => [], defaultModel: () => null, health: () => ({ ok: false, text: '' }) };
let section = 'profile';
let menuOpen = false;
let pending = null;

export const profile = () => store.profile || FALLBACK;

export function onProfileChange(listener) {
  store.listeners.add(listener);
}

function apply(view, changed) {
  store.profile = view.profile;
  store.proposals = view.proposals || [];
  store.resetPhrase = view.reset_phrase || store.resetPhrase;
  // 主题按个人设置来（本机存的那份只用来在页面刚打开时上色）
  applyTheme(view.profile.theme || 'light');
  // 语言：个人设置和本机存的不一样（比如在别的浏览器里改过），按个人设置刷新一次
  if (setLanguage(view.profile.language || 'zh')) window.location.reload();
  for (const listener of store.listeners) listener(store.profile, changed);
  refreshAccount();
  renderTabs();
}

export async function loadProfile() {
  try {
    apply(await request('/api/profile'), ['all']);
  } catch (error) {
    toast(`个人设置没读出来：${error.message}`);
  }
}

function setStatus(text, bad = false) {
  const node = $('#settingsStatus');
  node.textContent = text;
  node.classList.toggle('bad', bad);
}

// 输入框里切换模型时调用：把选的模型记成主模型，下次打开还是它
export function updateAgent(changes) {
  return save({ agent: { ...profile().agent, ...changes } }, ['agent'], '已切换模型');
}

async function save(patch, changed, message = '已保存') {
  setStatus('正在保存…');
  try {
    apply(await request('/api/profile', { method: 'PATCH', body: JSON.stringify(patch) }), changed);
    setStatus(message);
    return true;
  } catch (error) {
    setStatus(`没保存上：${error.message}`, true);
    return false;
  }
}

// 打字的输入框停一会儿再存；关窗口时立刻存
function saveLater(patch, changed) {
  clearTimeout(pending?.timer);
  pending = { patch, changed, timer: setTimeout(flush, 600) };
  setStatus('正在输入…');
}

function flush() {
  if (!pending) return;
  clearTimeout(pending.timer);
  const { patch, changed } = pending;
  pending = null;
  save(patch, changed);
}

function valueText(key, value) {
  if (key === 'question_type') return TYPE_LABEL[value] || value;
  if (key === 'difficulty') return `${value} ${DIFFICULTY_LABEL[value] || ''}`.trim();
  return String(value);
}

// 头像：上传过图片就显示图片，没有就显示称呼的第一个字（没有称呼显示一个人形图标）
function avatarFill(node, name, image = '') {
  node.classList.toggle('has-image', Boolean(image));
  if (image) {
    const img = el('img');
    img.src = image;
    img.alt = '';
    node.replaceChildren(img);
    return;
  }
  const first = [...String(name || '').trim()][0];
  if (first) node.textContent = first;
  else node.innerHTML = svg('user');
}

// 上传的图片 → 居中裁成正方形、缩到 256×256 → data URL（支持透明的用 WebP，不支持就用 PNG）
const AVATAR_SIZE = 256;
function avatarFromFile(file) {
  return new Promise((resolve, reject) => {
    if (!/^image\//.test(file.type)) {
      reject(new Error('请选一张图片（PNG、JPG、WebP 或 GIF）'));
      return;
    }
    if (file.size > 15 * 1024 * 1024) {
      reject(new Error('图片太大了，选一张 15 MB 以内的'));
      return;
    }
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      const side = Math.min(image.naturalWidth, image.naturalHeight);
      const canvas = document.createElement('canvas');
      canvas.width = AVATAR_SIZE;
      canvas.height = AVATAR_SIZE;
      const draw = canvas.getContext('2d');
      draw.imageSmoothingQuality = 'high';
      draw.drawImage(image, (image.naturalWidth - side) / 2, (image.naturalHeight - side) / 2, side, side, 0, 0, AVATAR_SIZE, AVATAR_SIZE);
      URL.revokeObjectURL(url);
      const webp = canvas.toDataURL('image/webp', 0.88);
      resolve(webp.startsWith('data:image/webp') ? webp : canvas.toDataURL('image/png'));
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('这张图片打不开，换一张试试'));
    };
    image.src = url;
  });
}

// ---------- 左下角按钮和菜单 ----------

export function refreshAccount() {
  const p = profile();
  const health = ctx.health();
  const avatar = $('#accountAvatar');
  avatarFill(avatar, p.display_name, p.avatar);
  avatar.classList.toggle('offline', !health.ok);
  $('#accountName').textContent = p.display_name || '个人设置';
  $('#accountNote').textContent = !health.ok
    ? health.text
    : p.display_name
      ? p.grade ? `${p.grade}数学` : '个人设置'
      : '称呼、出题偏好、模型';
}

function renderMenu() {
  const p = profile();
  const menu = $('#accountMenu');
  menu.innerHTML = '';
  const card = el('div', 'account-card');
  const avatar = el('span', 'avatar');
  avatar.setAttribute('aria-hidden', 'true');
  avatarFill(avatar, p.display_name, p.avatar);
  // 菜单只放名字，不放说明小字
  const who = el('div');
  who.append(el('b', '', p.display_name || '个人设置'));
  card.append(avatar, who);
  menu.append(card);
  for (const item of sections()) {
    const button = el('button', 'account-item');
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    button.innerHTML = svg(item.icon);
    const words = el('span');
    words.append(el('b', '', item.name));
    button.append(words);
    button.addEventListener('click', () => {
      closeMenu();
      openSettings(item.id);
    });
    menu.append(button);
  }
  // 桌面程序的窗口里：多一项"在浏览器中打开"（同一个服务，平时用的浏览器也能用，地址写在下面）
  const desktop = runtime.desktop;
  if (desktop) {
    const button = el('button', 'account-item');
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    button.innerHTML = svg('globe');
    const words = el('span');
    words.append(el('b', '', '在浏览器中打开'), el('small', '', desktop.address));
    button.append(words);
    button.addEventListener('click', async () => {
      closeMenu();
      try {
        await request('/api/open-browser', { method: 'POST' });
      } catch (error) {
        toast(error.message);
      }
    });
    menu.append(button);
  }
  // 连接正常时不显示；只有出了问题（没有可用的模型、连不上本机服务）才在菜单底部说一句
  const health = ctx.health();
  if (!health.ok) menu.append(el('p', 'account-status bad', health.text));
}

// 是不是桌面程序打开的（/api/health 的 desktop）：runtime.desktop = { address } 或 null；顺便记下数据文件夹
function checkDesktop() {
  request('/api/health').then((info) => {
    runtime.desktop = info.desktop ? { address: info.address } : null;
    runtime.dataDir = info.data_dir || '';
  }).catch(() => {});
}

function openMenu() {
  menuOpen = true;
  renderMenu();
  $('#accountMenu').hidden = false;
  $('#accountBtn').setAttribute('aria-expanded', 'true');
  $('#accountMenu .account-item')?.focus();
}

function closeMenu() {
  if (!menuOpen) return;
  menuOpen = false;
  $('#accountMenu').hidden = true;
  $('#accountBtn').setAttribute('aria-expanded', 'false');
}

// ---------- 设置窗口 ----------

export function openSettings(id = 'profile') {
  section = sections().some((item) => item.id === id) ? id : 'profile';
  renderTabs();
  renderSection();
  const updated = profile().updated_at;
  setStatus(updated ? `改动会自动保存。上次保存于 ${formatWhen(updated)}` : '改动会自动保存');
  const dialog = $('#settingsDialog');
  if (!dialog.open) dialog.showModal();
  $('#settingsTabs [aria-selected="true"]')?.focus();
}

function renderTabs() {
  const tabs = $('#settingsTabs');
  const hadFocus = tabs.contains(document.activeElement);
  tabs.innerHTML = '';
  for (const item of sections()) {
    const tab = el('button', 'settings-tab');
    tab.type = 'button';
    tab.id = `settings-tab-${item.id}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(item.id === section));
    tab.setAttribute('aria-controls', 'settingsPanel');
    tab.innerHTML = svg(item.icon);
    tab.append(item.name);
    if (item.id === 'learning' && store.proposals.length) tab.append(el('span', 'count', String(store.proposals.length)));
    tab.addEventListener('click', () => {
      flush();
      section = item.id;
      renderTabs();
      renderSection();
    });
    tabs.append(tab);
  }
  $('#settingsPanel').setAttribute('aria-labelledby', `settings-tab-${section}`);
  if (hadFocus) tabs.querySelector('[aria-selected="true"]')?.focus();
}

function renderSection() {
  $('#settingsSectionTitle').textContent = sections().find((item) => item.id === section).name;
  const body = $('#settingsBody');
  body.innerHTML = '';
  body.append(BUILDERS[section]());
  body.scrollTop = 0;
}

function field(label, note, controls, forId) {
  const wrap = el('div', 'field');
  const title = el(forId ? 'label' : 'div', 'field-label', label);
  if (forId) title.htmlFor = forId;
  wrap.append(title);
  if (note) wrap.append(el('p', 'field-note', note));
  wrap.append(...[].concat(controls));
  return wrap;
}

function choices(options, current, onPick, label) {
  const list = el('div', 'choice-list');
  list.setAttribute('role', 'radiogroup');
  list.setAttribute('aria-label', label);
  for (const option of options) {
    const item = el('button', 'choice');
    item.type = 'button';
    item.setAttribute('role', 'radio');
    item.setAttribute('aria-checked', String(option.value === current));
    item.append(el('span', 'radio'), el('span', 'name', option.name), el('span', 'note', option.note));
    item.addEventListener('click', () => {
      list.querySelectorAll('.choice').forEach((c) => c.setAttribute('aria-checked', String(c === item)));
      onPick(option.value);
    });
    list.append(item);
  }
  return list;
}

function switchRow(label, note, on, onToggle) {
  const wrap = el('div', 'field');
  const row = el('div', 'field-row');
  const words = el('div');
  words.append(el('div', 'field-label', label), el('p', 'field-note', note));
  const toggle = el('button', 'switch');
  toggle.type = 'button';
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', String(on));
  toggle.setAttribute('aria-label', label);
  toggle.addEventListener('click', () => {
    const next = toggle.getAttribute('aria-checked') !== 'true';
    toggle.setAttribute('aria-checked', String(next));
    onToggle(next);
  });
  row.append(words, toggle);
  wrap.append(row);
  return wrap;
}

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function modelSelect(id, current, onPick) {
  const models = ctx.models();
  const select = el('select', 'select-input');
  select.id = id;
  // 接了好几家服务时，名字后面注明是哪家（用户自己起的服务名）
  const several = new Set(models.map((m) => m.provider)).size > 1;
  for (const model of models) {
    select.add(new Option(several && model.provider ? `${model.label}（${model.provider}）` : model.label, model.id));
  }
  select.value = current || models[0]?.id || '';
  select.disabled = !models.length;
  select.addEventListener('change', () => onPick(select.value));
  return select;
}

// 头像：预览 + 上传 + 去掉。图片只存在这台电脑的个人设置里
function avatarField() {
  const p = profile();
  const row = el('div', 'avatar-field');
  const preview = el('span', 'avatar avatar-preview');
  preview.setAttribute('aria-hidden', 'true');
  const input = el('input');
  input.type = 'file';
  input.accept = 'image/png,image/jpeg,image/webp,image/gif';
  input.hidden = true;
  const upload = button(p.avatar ? '换一张' : '上传头像', 'btn', () => input.click());
  const remove = button('去掉头像', 'link-btn', async () => {
    remove.disabled = true;
    if (await save({ avatar: '' }, ['avatar'], '已去掉头像')) show();
    remove.disabled = false;
  });
  const show = () => {
    const current = profile();
    avatarFill(preview, current.display_name, current.avatar);
    upload.textContent = current.avatar ? '换一张' : '上传头像';
    remove.hidden = !current.avatar;
  };
  input.addEventListener('change', async () => {
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    upload.disabled = true;
    try {
      const image = await avatarFromFile(file);
      if (await save({ avatar: image }, ['avatar'], '已换上新头像')) show();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      upload.disabled = false;
    }
  });
  show();
  row.append(preview, upload, remove, input);
  return field('头像', '显示在左下角和设置菜单里。图片会居中裁成正方形，只保存在这台电脑上。', row);
}

function profileSection() {
  const p = profile();
  const wrap = el('div');
  const name = el('input', 'text-input');
  name.id = 'settingName';
  name.type = 'text';
  name.maxLength = 20;
  name.placeholder = '比如：王老师';
  name.value = p.display_name;
  name.addEventListener('input', () => saveLater({ display_name: name.value }, ['display_name']));
  name.addEventListener('change', flush);
  wrap.append(
    avatarField(),
    field('称呼', '首页会用它跟你打招呼；没上传头像时，头像显示称呼的第一个字。', name, 'settingName'),
    field(
      '任教年级',
      '出题时默认按这个年级；选了别的年级的考点，也可以在输入框里临时改。',
      segmented(
        [{ value: '', label: '不固定' }, ...['高一', '高二', '高三'].map((g) => ({ value: g, label: g }))],
        p.grade,
        (value) => save({ grade: value }, ['grade']),
        '任教年级',
      ),
    ),
    field(
      '教材版本',
      '选考点、题库按知识点分类、出好的题标的知识点位置，都按这一版教材的册、章、节。',
      segmented(
        [{ value: 'pep_a', label: '人教 A 版（2019）' }, { value: 'pep_b', label: '人教 B 版（2019）' }],
        p.textbook || 'pep_a',
        (value) => save({ textbook: value }, ['textbook'], value === 'pep_b' ? '已换成人教 B 版' : '已换成人教 A 版'),
        '教材版本',
      ),
    ),
  );
  return wrap;
}

function defaultsSection() {
  const d = { ...profile().defaults };
  const push = (changes) => {
    Object.assign(d, changes);
    return save({ defaults: { ...d } }, ['defaults']);
  };
  const wrap = el('div');
  wrap.append(el('p', 'settings-intro', '输入框下方的题型、难度、用途没选时，出题就按这里的默认；在输入框下方选了的只管那一次，出完恢复成没选。'));
  const request_ = el('textarea', 'text-input');
  request_.id = 'settingRequest';
  request_.maxLength = 300;
  request_.placeholder = '比如：数据尽量简洁，不要出现繁琐的计算';
  request_.value = d.standing_request;
  request_.addEventListener('input', () => {
    d.standing_request = request_.value;
    saveLater({ defaults: { ...d } }, ['defaults']);
  });
  request_.addEventListener('change', flush);
  wrap.append(
    field('默认题型', '', segmented(Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label })), d.question_type, (value) => push({ question_type: value }), '默认题型')),
    field('默认难度', '', segmented([1, 2, 3, 4, 5].map((level) => ({ value: level, label: `${level} ${DIFFICULTY_LABEL[level]}` })), d.difficulty, (value) => push({ difficulty: value }), '默认难度')),
    field('默认用途', '', segmented(PURPOSES.map((p) => ({ value: p, label: p })), d.purpose, (value) => push({ purpose: value }), '默认用途')),
    field(
      '解析详略',
      '会作为一句要求附在每次的出题要求后面。',
      choices(Object.entries(DETAIL).map(([value, item]) => ({ value, name: item.name, note: item.note })), d.solution_detail, (value) => push({ solution_detail: value }), '解析详略'),
    ),
    field('每道题都带上的要求', '写在这里的话，会附在每次的出题要求后面。最多 300 字。', request_, 'settingRequest'),
  );
  return wrap;
}

function agentSection() {
  const a = { ...profile().agent };
  const models = ctx.models();
  const main = models.some((m) => m.id === a.model) ? a.model : ctx.defaultModel();
  const strong = strongModel(models, main);
  const custom = el('div', 'subfield');
  const push = (changes) => {
    Object.assign(a, changes);
    custom.setAttribute('aria-disabled', String(a.mode !== 'custom'));
    return save({ agent: { ...a } }, ['agent']);
  };
  const strongNote = !strong || strong.id === main
    ? '（现在没有比主模型更强的模型，把关仍用主模型）'
    : `（把关用 ${strong.label}）`;
  const options = Object.entries(MODES).map(([value, item]) => ({
    value,
    name: item.name,
    note: value === 'careful' || value === 'strict' ? `${item.note}${strongNote}` : item.note,
  }));

  custom.setAttribute('aria-disabled', String(a.mode !== 'custom'));
  custom.append(
    field(
      '同时试几个方案',
      '几个方案各自独立出题，用最先通过全部检查的那个，其余随即停下。方案越多，一般越快拿到题，Token 也成倍增加。',
      segmented([1, 2, 3].map((n) => ({ value: n, label: `${n} 个` })), a.lanes, (value) => push({ lanes: value }), '同时试几个方案'),
    ),
    switchRow('独立验算', '题干定稿后，验算员只看题面独立解一遍，和解析的答案对不上就整题重出。', a.verify, (value) => push({ verify: value })),
    field('把关模型', '审题员、复核员和验算员用它。', modelSelect('settingCheckModel', a.check_model || main, (value) => push({ check_model: value })), 'settingCheckModel'),
  );

  const health = ctx.health();
  const wrap = el('div');
  wrap.append(
    field('默认出题方式', '每道题都按这里选的方式出。', choices(options, a.mode, (value) => push({ mode: value }), '默认出题方式')),
    field(
      '默认思考强度',
      '出题和聊天回答都按这一档；点输入框右下角的模型名可以临时调。较难和压轴题（4、5 星）至少用高。',
      choices(Object.entries(THINKING).map(([value, item]) => ({ value, name: item.name, note: item.note })), a.thinking || 'medium', (value) => push({ thinking: value }), '默认思考强度'),
    ),
    field('主模型', '命题员和解题员用它；选「快速出题」时，所有角色都用它。', modelSelect('settingMainModel', main, (value) => push({ model: value })), 'settingMainModel'),
    field('自定义方式', '选了「自定义」，才会按下面这几项出题。', custom),
    field('模型连接', '要接入、修改或者再加一家模型服务，到「模型服务」里改，不用重启。', [
      el('p', 'readonly', health.text),
      button('打开模型服务', 'link-btn', () => { section = 'models'; renderTabs(); renderSection(); }),
    ]),
  );
  return wrap;
}

async function review(proposal, action) {
  setStatus('正在处理…');
  try {
    const view = await request(`/api/profile/proposals/${encodeURIComponent(proposal.id)}`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    });
    apply(view, action === 'accept' ? [proposal.key === 'grade' ? 'grade' : 'defaults', 'learning'] : ['learning']);
    setStatus(action === 'accept'
      ? `已采用：${proposal.label}改成「${valueText(proposal.key, proposal.value)}」`
      : '已记下。同样的选择再多出现 3 次之前，不会再提这条建议');
  } catch (error) {
    setStatus(error.message, true);
    await loadProfile();
  }
  renderSection();
}

function learningSection() {
  const p = profile();
  const wrap = el('div');
  wrap.append(switchRow(
    '根据出题记录给我提建议',
    '只看最近 20 次出题的年级、题型、难度和用途，不读题目内容。同一个选择出现 3 次以上、占到六成，才会提建议；你点「采用」之前，不会改动任何设置。',
    p.learning.enabled,
    async (on) => {
      await save({ learning_enabled: on }, ['learning'], on ? '已开启偏好学习' : '已关闭偏好学习');
      renderSection();
    },
  ));
  if (p.learning.enabled) {
    if (store.proposals.length) {
      const list = el('ul', 'proposal-list');
      for (const proposal of store.proposals) {
        const item = el('li', 'proposal');
        const actions = el('div', 'actions');
        actions.append(
          button('采用', 'btn primary', () => review(proposal, 'accept')),
          button('不采用', 'btn', () => review(proposal, 'reject')),
        );
        item.append(
          el('b', '', `${proposal.label}改成「${valueText(proposal.key, proposal.value)}」`),
          el('small', '', `最近 ${proposal.total} 次出题里有 ${proposal.count} 次是这样，把握 ${Math.round(proposal.confidence * 100)}%`),
          actions,
        );
        list.append(item);
      }
      wrap.append(field('建议', '', list));
    } else {
      wrap.append(field('建议', '', el('p', 'empty-note', '暂时没有建议。多出几道题之后再来看看。')));
    }
  }
  if (p.learning.feedback.length) {
    const list = el('ul', 'feedback-list');
    for (const item of p.learning.feedback.slice(0, 12)) {
      const row = el('li');
      row.append(
        el('span', '', `${item.action === 'accept' ? '采用' : '不采用'}：${item.label}改成「${valueText(item.key, item.value)}」`),
        el('span', '', formatWhen(item.at)),
      );
      list.append(row);
    }
    wrap.append(field('处理过的建议', '不采用的建议，要等同样的选择再多出现 3 次才会重新提出。', list));
  }
  return wrap;
}

// ---------- 模型服务：在这里接入、修改、删除，不用改后台文件、也不用重启 ----------

// 思考模式在请求里怎么开关：各家写法不一样，不确定就选"不支持"（兼容 OpenAI 接口的服务才要选）
const THINKING_STYLES = [
  { value: 'thinking', name: 'thinking 参数', note: '请求里写 "thinking": {"type": "enabled"}，关闭时写 "disabled"；极高再加 "reasoning_effort": "max"（DeepSeek 等）' },
  { value: 'reasoning_effort', name: 'reasoning_effort 参数', note: '一直在思考、只能调强度的模型：要思考的那几步写 "high"（极高写 "max"，不认就退到 "xhigh"、"high"），其余写 "low"（OpenAI、智谱 GLM-5 等）' },
  { value: 'enable_thinking', name: 'enable_thinking 参数', note: '请求里写 "enable_thinking": true，关闭时写 false；只能开或关，极高和高一样' },
  { value: 'none', name: '不支持', note: '这家没有思考模式或者不确定怎么开；思考强度选中、高时也按普通方式回答' },
];
const thinkingName = (service) => (service.api === 'anthropic'
  ? '自适应'
  : THINKING_STYLES.find((item) => item.value === service.thinking)?.name || '不支持');
// picking：正在选模板；editing：正在填的表单（从模板来的带 preset）
const services = { list: null, presets: [], picking: false, editing: null, tests: new Map() };

async function loadServices() {
  try {
    const [data, presets] = await Promise.all([
      request('/api/model-services'),
      request('/api/model-services/presets').catch(() => ({ presets: [] })),
    ]);
    services.list = data.services;
    services.presets = presets.presets || [];
  } catch (error) {
    services.list = [];
    setStatus(`模型服务没读出来：${error.message}`, true);
  }
}

const sameUrl = (a, b) => String(a || '').replace(/\/+$/, '') === String(b || '').replace(/\/+$/, '');

function modelsSection() {
  const wrap = el('div', 'services');
  const draw = () => {
    wrap.innerHTML = '';
    if (services.editing) {
      wrap.append(serviceForm(services.editing, draw));
      return;
    }
    if (services.picking) {
      wrap.append(presetPicker(draw));
      return;
    }
    wrap.append(el('p', 'field-note services-intro',
      '常用的几家有现成模板，选一个填上 API Key 就能用；其他兼容 OpenAI 接口的服务也能手动接入。可以接好几家，'
      + '主模型、把关模型在「出题方式与模型」里选。API Key 只保存在这台电脑上，这里只显示头尾几位。'));
    if (services.list === null) {
      wrap.append(el('p', 'field-note', '正在读取…'));
      return;
    }
    if (!services.list.length) wrap.append(el('div', 'service-empty', '还没有接入模型服务，接入一个才能出题。'));
    for (const service of services.list) wrap.append(serviceCard(service, draw));
    wrap.append(button('接入模型服务', 'btn primary', () => {
      services.picking = true;
      draw();
    }));
  };
  if (services.list === null) loadServices().then(draw);
  draw();
  return wrap;
}

// 选模板：常用的几家 + "其他服务"（自己填接口地址和型号）
function presetPicker(draw) {
  const wrap = el('div', 'preset-picker');
  wrap.append(
    el('h3', 'service-form-title', '接入模型服务'),
    el('p', 'field-note', '选一家，填上 API Key 就能用；接口地址、型号和思考参数都已经按这家的文档填好。'),
  );
  const grid = el('div', 'preset-grid');
  const card = (name, intro, onPick, tag = '') => {
    const item = button('', 'preset-card', onPick);
    const head = el('span', 'preset-head');
    head.append(el('b', '', name));
    if (tag) head.append(el('span', 'preset-tag', tag));
    item.append(head, el('span', 'preset-intro', intro));
    grid.append(item);
  };
  for (const preset of services.presets) {
    const added = (services.list || []).some((s) => s.preset === preset.id || sameUrl(s.base_url, preset.base_url));
    card(preset.name, preset.intro, () => {
      services.picking = false;
      services.editing = {
        preset: preset.id,
        name: preset.name,
        base_url: preset.base_url,
        api: preset.api,
        thinking: preset.thinking,
        models: structuredClone(preset.models),
      };
      draw();
    }, added ? '已接入' : '');
  }
  card('其他服务', '兼容 OpenAI 接口的服务（通义千问、Kimi、学校或公司内部的服务等），自己填接口地址和型号。', () => {
    services.picking = false;
    services.editing = { preset: '', api: 'openai', name: '', base_url: '', thinking: 'none', models: [{ model: '', label: '', note: '' }] };
    draw();
  });
  wrap.append(grid, button('取消', 'btn', () => { services.picking = false; draw(); }));
  return wrap;
}

// 逐个模型发一次很短的请求，看能不能用
async function testService(service) {
  for (const entry of service.models) {
    let result;
    try {
      result = await request('/api/model-services/test', {
        method: 'POST',
        body: JSON.stringify({
          service_id: service.id, base_url: service.base_url, model: entry.model, thinking: service.thinking, api: service.api || 'openai',
        }),
      });
    } catch (error) {
      result = { ok: false, error: error.message };
    }
    services.tests.set(`${service.id}:${entry.model}`, result);
  }
}

function serviceCard(service, draw) {
  const card = el('div', 'service-card');
  const head = el('div', 'service-head');
  // 卡片上只放名字：接口地址和原始型号只在"修改"里看到（那是用户自己填的配置）
  head.append(el('b', '', service.name), el('span', 'service-meta', `Key ${service.key_hint || '未填'} · 思考模式：${thinkingName(service)} · ${service.models.length} 个模型`));
  const list = el('ul', 'service-models');
  for (const entry of service.models) {
    const item = el('li');
    item.append(el('span', 'name', entry.label || entry.model));
    const result = services.tests.get(`${service.id}:${entry.model}`);
    if (result) item.append(el('span', `test ${result.ok ? 'ok' : 'bad'}`, result.ok ? `能用，${result.seconds} 秒` : result.error));
    list.append(item);
  }
  const actions = el('div', 'service-actions');
  const test = button('测试连接', 'btn', async () => {
    test.disabled = true;
    test.textContent = '正在测试…';
    await testService(service);
    draw();
  });
  // 删除要再点一次确认
  const remove = button('删除', 'link-btn danger-link', async () => {
    if (!remove.dataset.confirm) {
      remove.dataset.confirm = '1';
      remove.textContent = '确定删除？用到它的模型会换成第一个可用的';
      return;
    }
    try {
      const data = await request(`/api/model-services/${encodeURIComponent(service.id)}`, { method: 'DELETE' });
      await afterServicesChange(data, '已删除');
    } catch (error) {
      setStatus(error.message, true);
    }
    draw();
  });
  actions.append(test, button('修改', 'btn', () => { services.editing = structuredClone(service); draw(); }), remove);
  card.append(head, list, actions);
  return card;
}

// 保存、删除之后：更新列表，并让页面重新读一遍可用的模型（输入框、出题方式马上就能用上）
async function afterServicesChange(data, message) {
  services.list = data.services;
  services.editing = null;
  await ctx.reloadModels?.();
  setStatus(message);
}

function serviceForm(draft, draw) {
  const isNew = !draft.id;
  const form = el('form', 'service-form');
  const api = draft.api || 'openai';
  // 从模板接入：只要填 API Key，其余（接口地址、型号、思考参数）收在"其他设置"里，一般不用动
  const preset = services.presets.find((item) => item.id === draft.preset) || null;
  const simple = Boolean(isNew && preset);
  const input = (id, value, placeholder, type = 'text') => {
    const node = el('input', 'text-input');
    Object.assign(node, { id, type, value: value || '', placeholder, autocomplete: 'off', spellcheck: false });
    return node;
  };
  const name = input('serviceName', draft.name, '比如：学校的模型服务');
  name.maxLength = 30;
  const url = input('serviceUrl', draft.base_url, 'https://…/v1');
  const key = input('serviceKey', '', isNew ? (preset?.key_placeholder || '粘贴 API Key') : `已保存（${draft.key_hint || '…'}），不换就留空`, 'password');
  let thinking = draft.thinking || 'none';

  // 模型：每行一个型号 + 显示名称
  const rows = el('div', 'model-rows');
  const addRow = (entry = { model: '', label: '' }) => {
    const row = el('div', 'model-row');
    const model = input('', entry.model, '型号，比如 xxx-chat');
    model.setAttribute('aria-label', '型号');
    const label = input('', entry.label, '显示名称，比如 快速模型');
    label.setAttribute('aria-label', '显示名称');
    label.maxLength = 30;
    row.append(model, label, button('删除', 'link-btn', () => row.remove()));
    rows.append(row);
  };
  (draft.models.length ? draft.models : [{}]).forEach(addRow);
  const collect = () => [...rows.querySelectorAll('.model-row')]
    .map((row) => {
      const [model, label] = row.querySelectorAll('input');
      return { model: model.value.trim(), label: label.value.trim(), note: '' };
    })
    .filter((entry) => entry.model);

  const status = el('p', 'field-note form-status');
  const say = (text, bad = false) => {
    status.textContent = text;
    status.classList.toggle('bad', bad);
  };
  const found = el('div', 'discovered');
  const discover = button('从服务获取模型列表', 'btn', async () => {
    say('正在获取…');
    try {
      const data = await request('/api/model-services/discover', {
        method: 'POST',
        body: JSON.stringify({
          service_id: draft.id || null, base_url: url.value.trim(), api_key: key.value.trim(), api, preset: draft.preset || '',
        }),
      });
      const have = new Set(collect().map((entry) => entry.model));
      found.innerHTML = '';
      for (const id of data.models.filter((m) => !have.has(m))) {
        found.append(button(`+ ${id}`, 'chip', (event) => {
          [...rows.querySelectorAll('.model-row')].filter((row) => !row.querySelector('input').value.trim()).forEach((row) => row.remove());
          addRow({ model: id, label: '' });
          event.currentTarget.remove();
        }));
      }
      say(data.models.length ? '点一下要用的型号，加到上面的列表里；记得给它起个显示名称。' : '这家没有返回模型列表，请手动填写型号。');
    } catch (error) {
      say(error.message, true);
    }
  });

  const save = async () => {
    const payload = {
      name: name.value.trim(), base_url: url.value.trim(), api_key: key.value.trim(), api, thinking, preset: draft.preset || '', models: collect(),
    };
    if (isNew && !payload.api_key) {
      key.focus();
      return say('请填写 API Key', true);
    }
    if (!payload.name) return say('请给这个服务起个名字', true);
    if (!/^https?:\/\/\S+/.test(payload.base_url)) return say('接口地址要以 http:// 或 https:// 开头', true);
    if (!payload.models.length) return say('至少填一个型号', true);
    say('正在保存…');
    try {
      const data = await request(isNew ? '/api/model-services' : `/api/model-services/${encodeURIComponent(draft.id)}`, {
        method: isNew ? 'POST' : 'PUT',
        body: JSON.stringify(payload),
      });
      await afterServicesChange(data, isNew ? '已接入，正在测试连接…' : '已保存');
      draw();
      // 新接入的服务顺手测一遍
      const saved = isNew ? services.list[services.list.length - 1] : null;
      if (saved) {
        await testService(saved);
        setStatus('已接入。可以在「出题方式与模型」里选用');
        draw();
      }
    } catch (error) {
      say(error.message, true);
    }
  };
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    save();
  });

  const rowActions = el('div', 'service-actions');
  rowActions.append(button('加一个型号', 'btn', () => addRow()), discover);
  const footer = el('div', 'service-actions');
  footer.append(
    button(isNew ? '接入' : '保存', 'btn primary', save),
    // 新接入的"返回"回到选模板；修改时直接关掉
    button(isNew ? '返回' : '取消', 'btn', () => { services.editing = null; services.picking = isNew; draw(); }),
  );

  const keyNote = el('p', 'field-note');
  if (isNew && preset?.key_url) {
    const link = el('a', '', `${preset.name} 的网站`);
    Object.assign(link, { href: preset.key_url, target: '_blank', rel: 'noopener noreferrer' });
    keyNote.append('还没有 Key？到', link, '申请。Key 只保存在这台电脑上。');
  } else {
    keyNote.textContent = isNew ? '在模型服务的网站上申请。只保存在这台电脑上。' : '只显示头尾几位；要换 Key 才需要填。';
  }
  const keyField = field('API Key', '', [keyNote, key], 'serviceKey');
  const nameField = field('名称', '自己起的名字，接了好几家时用来区分。', name, 'serviceName');
  const urlField = field('接口地址', api === 'anthropic'
    ? 'Claude 官方接口的地址；用转发服务时换成它给的地址（要支持 Claude 的 Messages 接口）。'
    : '兼容 OpenAI 接口的地址，一般以 /v1 结尾，服务商的文档里能找到。', url, 'serviceUrl');
  const thinkingField = api === 'anthropic'
    ? field('思考模式', 'Claude 自己决定想多少：要思考的那几步想得深一些（effort: high，极高时 max），其余少想（low）。', [])
    : field('思考模式', '思考强度选中或高时，要思考的那几步用什么参数告诉这家服务。', choices(THINKING_STYLES, thinking, (value) => { thinking = value; }, '思考模式'));
  const modelsField = field('模型', '型号照服务商的写法填；显示名称是界面上看到的名字。', [rows, rowActions, found]);

  if (simple) {
    const summary = el('ul', 'preset-models');
    for (const entry of draft.models) {
      const item = el('li');
      item.append(el('b', '', entry.label || entry.model), el('span', '', entry.note || ''));
      summary.append(item);
    }
    const more = el('details', 'service-more');
    more.append(el('summary', '', '接口地址、型号等其他设置（一般不用改）'), nameField, urlField, thinkingField, modelsField);
    form.append(
      el('h3', 'service-form-title', `接入 ${preset.name}`),
      el('p', 'field-note preset-lead', preset.intro),
      keyField,
      field('会接入这几个模型', '接入后在「出题方式与模型」里选用；以后可以在"修改"里增删型号。', summary),
      more,
      footer,
      status,
    );
    setTimeout(() => key.focus(), 0);
  } else {
    form.append(
      el('h3', 'service-form-title', isNew ? '接入其他模型服务' : `修改「${draft.name}」`),
      nameField, urlField, keyField, thinkingField, modelsField,
      footer,
      status,
    );
  }
  return form;
}

function dataSection() {
  const wrap = el('div');
  const privacy = el('p', 'privacy');
  privacy.append(
    '个人设置、题库和出题记录都保存在这台电脑上（',
    el('code', '', runtime.dataDir || 'data/runtime'),
    ' 文件夹），不会上传。出题时，题目要求会发给你选的模型服务（你接入的大模型）来生成题目。',
  );
  const exportLink = el('a', 'btn', '导出 JSON');
  exportLink.href = '/api/profile/export';
  exportLink.setAttribute('download', '');

  const zone = el('div', 'danger-zone');
  const input = el('input', 'text-input');
  input.type = 'text';
  input.placeholder = `输入“${store.resetPhrase}”确认`;
  input.setAttribute('aria-label', `输入“${store.resetPhrase}”确认`);
  const remove = button('删除', 'btn danger', async () => {
    setStatus('正在删除…');
    try {
      apply(await request('/api/profile/reset', { method: 'POST', body: JSON.stringify({ confirmation: input.value.trim() }) }), ['all']);
      setStatus('已删除全部偏好，恢复成默认设置');
      renderSection();
    } catch (error) {
      setStatus(error.message, true);
    }
  });
  remove.disabled = true;
  input.addEventListener('input', () => { remove.disabled = input.value.trim() !== store.resetPhrase; });
  const row = el('div', 'confirm-row');
  row.append(input, remove);
  zone.append(
    el('b', '', '删除全部偏好'),
    el('p', '', '清除称呼、头像、出题偏好、出题方式和偏好学习的全部记录，恢复成默认设置。出题记录不受影响。删除后不能恢复。'),
    row,
  );

  wrap.append(
    field('数据存在哪里', '', privacy),
    field('导出', '把个人设置和全部出题记录导出成一个 JSON 文件，不含 API Key。', exportLink),
    field('删除', '', zone),
  );
  return wrap;
}

// 外观与语言：主题马上生效；语言要刷新一下页面（界面文字按新语言重新排）
function appearanceSection() {
  const p = profile();
  const wrap = el('div');
  wrap.append(
    field(
      '主题',
      '白天是浅色界面，夜晚是深色界面；选"跟随系统"就和这台电脑的深浅色设置一致。',
      segmented(
        [{ value: 'light', label: '白天' }, { value: 'dark', label: '夜晚' }, { value: 'system', label: '跟随系统' }],
        p.theme || 'light',
        (value) => {
          applyTheme(value);
          save({ theme: value }, ['theme'], '已切换主题');
        },
        '主题',
      ),
    ),
    field(
      '语言',
      '界面上的文字用哪种语言（Interface language）。题目、解析这些内容不翻译；切换后页面会刷新一下。',
      segmented(
        [{ value: 'zh', label: '中文' }, { value: 'en', label: 'English' }],
        p.language || 'zh',
        async (value) => {
          if (value === (profile().language || 'zh')) return;
          if (await save({ language: value }, ['language'], value === 'en' ? 'Switched to English' : '已切换成中文')) {
            setLanguage(value);
            window.location.reload();
          }
        },
        '语言',
      ),
    ),
  );
  return wrap;
}

// ---------- 桌面版：开机自启、快捷方式、导出的文件存到哪、数据文件夹（只在桌面版窗口里有这一页） ----------

async function desktopCall(path, body = null, method = 'POST') {
  return request(path, body === null ? { method } : { method, body: JSON.stringify(body) });
}

function pathLine(path) {
  const line = el('p', 'path-line');
  line.append(el('code', '', path));
  return line;
}

function desktopSection() {
  const wrap = el('div', 'desktop-settings');
  wrap.append(el('p', 'settings-intro', '正在读取…'));
  let view = null;
  const update = async (changes, message = '已保存') => {
    try {
      view = await desktopCall('/api/desktop', changes, 'PATCH');
      setStatus(message);
    } catch (error) {
      setStatus(error.message, true);
      view = await desktopCall('/api/desktop', null, 'GET').catch(() => view);
    }
    draw();
  };
  const open = (target) => desktopCall('/api/desktop/open', { target }).catch((error) => setStatus(error.message, true));
  const pick = async (purpose) => {
    try {
      const { path } = await desktopCall('/api/desktop/pick-folder', { purpose });
      if (!path) return;
      if (purpose === 'export') {
        await update({ export_dir: path }, '导出的文件以后存到新的文件夹');
        return;
      }
      view = await desktopCall('/api/desktop/data-dir', { path });
      setStatus('记下了：下次打开 Tetra 时把数据复制过去');
      draw();
    } catch (error) {
      setStatus(error.message, true);
    }
  };

  function draw() {
    wrap.replaceChildren();
    const locked = new Set(view.shortcuts.locked || []);
    const where = view.frozen
      ? `${view.installed ? '安装版' : '免安装版'}，程序在 ${view.app_dir}`
      : '从源码运行的桌面窗口：开机自启和快捷方式要用打包好的 Tetra.exe 才能设置';
    wrap.append(el('p', 'settings-intro', `这些设置只在桌面版里有。${where}。`));

    const auto = switchRow('开机时自动启动 Tetra', '登录 Windows 以后自动打开，出题、组卷随时能用。', view.autostart,
      (on) => update({ autostart: on }, on ? '已设为开机自动启动' : '已取消开机自动启动'));
    const autoMin = switchRow('开机启动时窗口最小化', '只在任务栏里出现，不挡住别的窗口；需要时再点开。', view.autostart_minimized,
      (on) => update({ autostart_minimized: on }));
    autoMin.hidden = !view.autostart;
    const desk = switchRow('桌面快捷方式', locked.has('desktop') ? '这是安装时为所有用户建的，要用管理员权限才能删。' : '在桌面上放一个 Tetra 图标。',
      view.shortcuts.desktop, (on) => update({ desktop_shortcut: on }, on ? '已在桌面创建快捷方式' : '已删掉桌面快捷方式'));
    const menu = switchRow('开始菜单快捷方式', locked.has('start_menu') ? '这是安装时为所有用户建的，要用管理员权限才能删。' : '在开始菜单里放一个 Tetra，也能在 Windows 搜索里找到。',
      view.shortcuts.start_menu, (on) => update({ start_menu_shortcut: on }, on ? '已添加到开始菜单' : '已从开始菜单移除'));
    for (const row of [auto, desk, menu]) {
      if (!view.frozen) row.querySelector('.switch').disabled = true;
    }

    const exportRow = el('div', 'path-actions');
    exportRow.append(button('选择文件夹…', 'btn', () => pick('export')), button('打开', 'btn', () => open('export')));
    if (!view.export_dir_default) exportRow.append(button('恢复默认（文档\\Tetra）', 'link-btn', () => update({ export_dir: '' }, '已恢复成默认的文件夹')));
    const ask = switchRow('每次导出都问存到哪', '打开时，每次导出都弹出“另存为”，自己选位置和文件名；关掉时直接存进上面的文件夹，存好后可以点“打开文件夹”。',
      view.export_ask, (on) => update({ export_ask: on }));

    const dataRow = el('div', 'path-actions');
    dataRow.append(button('打开', 'btn', () => open('data')), button('更改…', 'btn', () => pick('data')));
    const dataNotes = [];
    if (view.data_dir_next) {
      const pending = el('div', 'pending-move');
      pending.append(
        el('p', '', `下次打开 Tetra 时，数据会复制到：${view.data_dir_next}（原来的文件夹原样保留，确认没问题以后可以自己删）。`),
      );
      const row = el('div', 'path-actions');
      row.append(
        button('现在重新打开 Tetra', 'btn primary', async () => {
          try {
            await desktopCall('/api/desktop/restart');
            setStatus('正在重新打开 Tetra…');
          } catch (error) {
            setStatus(error.message, true);
          }
        }),
        button('不换了', 'link-btn', async () => {
          view = await desktopCall('/api/desktop/data-dir', { path: '' });
          setStatus('已取消更换数据文件夹');
          draw();
        }),
      );
      pending.append(row);
      dataNotes.push(pending);
    }

    const logRow = el('div', 'path-actions');
    logRow.append(button('打开日志文件夹', 'btn', () => open('logs')));
    if (view.frozen) logRow.append(button('打开程序所在的文件夹', 'btn', () => open('app')));

    wrap.append(
      el('h3', 'settings-group', '启动'),
      auto,
      autoMin,
      el('h3', 'settings-group', '快捷方式'),
      desk,
      menu,
      el('h3', 'settings-group', '文件'),
      field('导出的 Word、PDF 存到哪', '在试卷、组卷里点“导出 Word / PDF”时存到这里。', [pathLine(view.export_dir), exportRow]),
      ask,
      field('数据文件夹', '题库、试卷、对话、设置和 API Key 都在这里。换位置时先记下来，下次打开 Tetra 时把数据复制过去。', [pathLine(view.data_dir), dataRow, ...dataNotes]),
      field('出了问题', '日志里记着出错的详情。', logRow),
    );
  }

  desktopCall('/api/desktop', null, 'GET').then((data) => { view = data; draw(); }).catch((error) => {
    wrap.replaceChildren(el('p', 'settings-intro', `没读出桌面版的设置：${error.message}`));
  });
  return wrap;
}

const BUILDERS = {
  profile: profileSection,
  appearance: appearanceSection,
  defaults: defaultsSection,
  models: modelsSection,
  agent: agentSection,
  learning: learningSection,
  data: dataSection,
  desktop: desktopSection,
};

export function initSettings(context) {
  ctx = { ...ctx, ...context };
  checkDesktop();
  $('#accountBtn').addEventListener('click', (event) => {
    event.stopPropagation();
    if (menuOpen) closeMenu();
    else openMenu();
  });
  document.addEventListener('click', (event) => {
    if (menuOpen && !event.composedPath().includes($('#account'))) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menuOpen) {
      closeMenu();
      $('#accountBtn').focus();
    }
  });
  const dialog = $('#settingsDialog');
  $('#closeSettings').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', flush);
  refreshAccount();
}
