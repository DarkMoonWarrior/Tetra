// 题库：出好的题自动收进来；可以修改、删除，也可以勾选几道组成试卷。
// 中间是题目列表，右栏显示选中的那道题，点"修改"在右栏里直接改。
// 文件夹：自己建分类，可以一层套一层。列表上面是文件夹条（所在位置 + 下一层的文件夹）；
// 勾选几道点"移到文件夹"，或者直接把题拖到文件夹上；文件夹也能拖进别的文件夹。删文件夹不删题。

import {
  $, DIFFICULTY_LABEL, GRADES, TYPE_LABEL, el, formatWhen, mathText, normalize, renderMath, request, segmented, svg, toast,
} from './shared.js';
import { buildSheet } from './sheet.js';
import { profile } from './settings.js';
import { openImporter } from './importer.js';
import { folderOptions } from './importcard.js';

const FOLDER_KEY = 'mingti.bank-folder'; // 上次看的是哪个文件夹（all 全部、none 未归类，或文件夹编号）
const DRAG_ITEMS = 'application/x-tetra-items';
const DRAG_FOLDER = 'application/x-tetra-folder';

const CHOICE = new Set(['single_choice', 'multiple_choice']);
const oneLine = (text) => String(text || '').replace(/\$\$/g, '$').replace(/\s*\n+\s*/g, ' ');

// 题库列表里每道题只显示两行。按大概的宽度在公式外面先截断：只靠 CSS 的行数限制时，
// 截在一个公式中间，省略号后面会露出半截下标（"…ₙ₊₁"）
function clipStem(text, budget = 60) {
  const source = oneLine(text);
  let used = 0;
  for (let i = 0; i < source.length; i += 1) {
    if (source[i] === '$') {
      const end = source.indexOf('$', i + 1);
      if (end < 0) break;
      const body = source.slice(i + 1, end).replace(/\\[a-zA-Z]+/g, 'x').replace(/[{}^_\s\\]/g, '');
      used += Math.max(1, body.length * 0.55);
      if (used > budget) return `${source.slice(0, i).trimEnd()}…`;
      i = end;
      continue;
    }
    used += source.charCodeAt(i) < 0x2e80 ? 0.55 : 1;
    if (used > budget) return `${source.slice(0, i).trimEnd()}…`;
  }
  return source;
}
const GROUP_KEY = 'mingti.bank-grouped';
const samePath = (a, b) => a.length === b.length && a.every((part, i) => part === b[i]);
const startsWith = (path, prefix) => prefix.every((part, i) => path[i] === part);

// 题库里的题 → 题目卡片要的格式（和出题结果同一个形状）
export function asDraft(item) {
  return {
    question_type: item.question_type,
    target_difficulty: item.difficulty,
    stem_latex: item.stem,
    options: item.options,
    answer_value: item.answer,
    solution_method: item.solution_method,
    solution_steps: item.solution_steps,
    authoring_intent: item.authoring_intent,
    assessed_methods: item.assessed_methods,
    common_mistakes: item.common_mistakes,
    full_solution_method: item.full_solution_method,
    full_solution_steps: item.full_solution_steps,
    figure_svg: item.figure_svg,
    imported: item.imported,
    solution_issue: item.solution_issue,
  };
}

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

// 带公式预览的输入框：边打边在下面显示排好的样子
function mathField(label, value, { multiline = true, rows = 3, hint = '' } = {}) {
  const wrap = el('label', 'field');
  wrap.append(el('span', 'field-label', label));
  if (hint) wrap.append(el('span', 'field-note', hint));
  const input = el(multiline ? 'textarea' : 'input', 'text-input');
  if (multiline) input.rows = rows;
  input.value = value || '';
  const preview = el('div', 'math-preview');
  preview.setAttribute('aria-hidden', 'true');
  let timer = null;
  const show = () => {
    preview.textContent = input.value;
    preview.hidden = !/\$|\\\(|\\\[/.test(input.value);
    renderMath(preview);
  };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(show, 250);
  });
  show();
  wrap.append(input, preview);
  return { wrap, input };
}

export class Library {
  constructor(shell) {
    this.shell = shell;
    this.root = $('#bankView');
    this.items = [];
    // kp：按知识点筛选，是知识点路径的前几段（册、章、节、考点），空数组表示不限
    this.filters = { q: '', type: '', difficulty: 0, kp: [] };
    this.grouped = (() => { try { return localStorage.getItem(GROUP_KEY) === '1'; } catch { return false; } })();
    this.treeOpen = false;
    this.expanded = new Set();
    this.order = new Map(); // 知识点名称 → 在教材目录里的先后
    this.pathOf = new Map(); // 考点名称 → [册, 章, 节, 考点]（旧记录只存了考点名时用）
    this.picked = new Set();
    this.current = null;
    this.editing = false;
    this.confirming = false;
    this.confirmingBulk = false;
    this.figuring = false;
    this.folders = [];
    this.folder = (() => { try { return localStorage.getItem(FOLDER_KEY) || 'all'; } catch { return 'all'; } })();
    this.naming = null; // 正在起名的文件夹：{ parent }（新建）或 { folder }（改名）
    this.menu = null; // 打开着的小菜单（文件夹的"更多"、移到文件夹）
    this.build();
  }

  build() {
    const head = el('header', 'view-head');
    this.lead = el('p', 'view-lead');
    // 标题旁边：导入题目（上传 PDF、图片，识别出题目收进题库）、新建文件夹
    const title = el('div', 'view-title');
    title.append(
      el('h1', '', '题库'),
      button('导入题目', 'btn', () => openImporter({ onSaved: () => this.show() })),
      button('新建文件夹', 'btn', () => this.startNaming({ parent: this.folderId() })),
    );
    head.append(title, this.lead);
    this.folderBar = el('div', 'folder-bar');

    const tools = el('div', 'bank-tools');
    const search = el('input', 'text-input');
    search.type = 'search';
    search.placeholder = '搜索题干或知识点，比如：椭圆、交集';
    search.setAttribute('aria-label', '搜索题库');
    search.addEventListener('input', () => { this.filters.q = search.value; this.renderList(); });
    const type = el('select', 'select-input');
    type.setAttribute('aria-label', '按题型筛选');
    type.add(new Option('全部题型', ''));
    for (const [value, label] of Object.entries(TYPE_LABEL)) type.add(new Option(label, value));
    type.addEventListener('change', () => { this.filters.type = type.value; this.renderList(); });
    const level = el('select', 'select-input');
    level.setAttribute('aria-label', '按难度筛选');
    level.add(new Option('全部难度', '0'));
    for (let i = 1; i <= 5; i += 1) level.add(new Option(`${i} ${DIFFICULTY_LABEL[i]}`, String(i)));
    level.addEventListener('change', () => { this.filters.difficulty = Number(level.value); this.renderList(); });
    const all = el('label', 'select-all');
    this.allBox = el('input');
    this.allBox.type = 'checkbox';
    this.allBox.addEventListener('change', () => {
      for (const item of this.visible()) {
        if (this.allBox.checked) this.picked.add(item.id);
        else this.picked.delete(item.id);
      }
      this.confirmingBulk = false;
      this.renderList();
    });
    all.append(this.allBox, el('span', '', '全选'));
    // 按知识点：点开是教材目录（册 › 章 › 节 › 考点），每一级都标着题库里有几道
    this.kpBtn = button('', 'select-input kp-btn', () => { this.treeOpen = !this.treeOpen; this.renderTree(); });
    this.kpBtn.setAttribute('aria-expanded', 'false');
    const arrange = segmented(
      [{ value: false, label: '最新在前' }, { value: true, label: '按知识点' }],
      this.grouped,
      (value) => {
        this.grouped = value;
        try { localStorage.setItem(GROUP_KEY, value ? '1' : '0'); } catch { /* 存不了就只在这次有效 */ }
        this.renderList();
      },
      '题目怎么排列',
    );
    tools.append(search, this.kpBtn, type, level, arrange, all);
    this.treePanel = el('div', 'kp-panel');
    this.treePanel.hidden = true;

    this.list = el('ul', 'bank-list');
    this.bar = el('div', 'pick-bar');
    this.bar.hidden = true;
    this.root.append(head, this.folderBar, tools, this.treePanel, this.list, this.bar);
    // 小菜单：点在外面、按 Esc、滚动列表时收起
    document.addEventListener('pointerdown', (event) => {
      if (this.menu && !event.composedPath().includes(this.menu)) this.closeMenu();
    }, true);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.menu) {
        event.stopPropagation();
        this.closeMenu(true);
      }
    }, true);
    window.addEventListener('resize', () => this.closeMenu());
  }

  // ---------- 文件夹 ----------

  // 当前看的是哪个文件夹：文件夹编号；看"全部题目""未归类"时是 null
  folderId() {
    return this.folder !== 'all' && this.folder !== 'none' ? this.folder : null;
  }

  folderById(id) {
    return this.folders.find((folder) => folder.id === id) || null;
  }

  // 从最外层到这个文件夹的一串文件夹
  folderPath(id) {
    const path = [];
    const seen = new Set();
    let node = this.folderById(id);
    while (node && !seen.has(node.id)) {
      seen.add(node.id);
      path.unshift(node);
      node = this.folderById(node.parent_id);
    }
    return path;
  }

  childrenOf(parentId) {
    return this.folders.filter((folder) => (folder.parent_id || null) === (parentId || null))
      .sort((a, b) => a.name.localeCompare(b.name, 'zh'));
  }

  openFolder(view) {
    this.folder = view || 'all';
    try { localStorage.setItem(FOLDER_KEY, this.folder); } catch { /* 存不了就只在这次有效 */ }
    this.naming = null;
    this.closeMenu();
    const items = this.visible();
    if (!items.some((item) => item.id === this.current)) {
      this.current = items[0]?.id || null;
      this.editing = false;
      this.confirming = false;
      this.figuring = false;
      this.renderPanel();
    }
    this.renderList();
  }

  // 文件夹条：第一行是所在位置（全部题目 › 期中复习 › 函数），第二行是下一层的文件夹和"新建文件夹"。
  // 每个文件夹（和位置上的每一级）都能把题拖上去；文件夹也能拖到别的文件夹上
  renderFolders() {
    const bar = this.folderBar;
    bar.replaceChildren();
    const here = this.folderId();
    const path = here ? this.folderPath(here) : [];
    const unfiled = this.items.filter((item) => !item.folder_id).length;

    const trail = el('nav', 'folder-path');
    trail.setAttribute('aria-label', '文件夹位置');
    const crumb = (label, view, target) => {
      const current = this.folder === view;
      const node = button(label, `folder-crumb${current ? ' on' : ''}`, () => this.openFolder(view));
      node.setAttribute('aria-current', String(current));
      if (target !== undefined) this.dropTarget(node, target);
      return node;
    };
    trail.append(crumb('全部题目', 'all', null));
    if (this.folder === 'none') trail.append(el('span', 'folder-sep', '›'), crumb('未归类', 'none'));
    for (const folder of path) trail.append(el('span', 'folder-sep', '›'), crumb(folder.name, folder.id, folder.id));
    if (here) {
      const more = el('button', 'folder-more');
      more.type = 'button';
      more.innerHTML = svg('more');
      more.title = '这个文件夹：重命名、新建子文件夹、删除';
      more.setAttribute('aria-label', '文件夹操作');
      more.addEventListener('click', () => this.folderMenu(this.folderById(here), more));
      trail.append(more);
    }
    bar.append(trail);

    if (this.folder === 'none') return;
    const chips = el('div', 'folder-chips');
    if (this.folder === 'all') {
      const loose = button('', `folder-chip loose${unfiled ? '' : ' empty'}`, () => this.openFolder('none'));
      loose.append(el('span', 'folder-name', '未归类'), el('span', 'folder-count', String(unfiled)));
      loose.title = '不在任何文件夹里的题；把题拖到这里就移出文件夹';
      this.dropTarget(loose, null);
      chips.append(loose);
    }
    for (const folder of this.childrenOf(here)) {
      if (this.naming?.folder?.id === folder.id) {
        chips.append(this.nameInput(folder.name));
        continue;
      }
      const chip = button('', 'folder-chip', () => this.openFolder(folder.id));
      chip.insertAdjacentHTML('afterbegin', svg('folder'));
      chip.append(el('span', 'folder-name', folder.name), el('span', 'folder-count', String(folder.total)));
      chip.title = folder.total === folder.count ? `${folder.name}：${folder.count} 道题。右键可以重命名、删除`
        : `${folder.name}：${folder.count} 道题，连同子文件夹一共 ${folder.total} 道。右键可以重命名、删除`;
      chip.draggable = true;
      chip.addEventListener('dragstart', (event) => {
        event.dataTransfer.setData(DRAG_FOLDER, folder.id);
        event.dataTransfer.effectAllowed = 'move';
      });
      chip.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        this.folderMenu(folder, chip, event);
      });
      this.dropTarget(chip, folder.id);
      chips.append(chip);
    }
    if (this.naming && !this.naming.folder && (this.naming.parent || null) === here) chips.append(this.nameInput(''));
    else {
      const add = button('', 'folder-chip add', () => this.startNaming({ parent: here }));
      add.insertAdjacentHTML('afterbegin', svg('folder-plus'));
      add.append(el('span', 'folder-name', here ? '新建子文件夹' : '新建文件夹'));
      chips.append(add);
    }
    bar.append(chips);
  }

  // 起名的输入框（新建或改名）：回车保存，Esc 或点到别处取消（点到别处时写了名字就保存）
  nameInput(value) {
    const wrap = el('span', 'folder-chip naming');
    const input = el('input', 'folder-input');
    input.type = 'text';
    input.maxLength = 30;
    input.value = value;
    input.placeholder = '文件夹名';
    input.setAttribute('aria-label', '文件夹名');
    wrap.append(input);
    let done = false;
    const finish = (keep) => {
      if (done) return;
      done = true;
      const naming = this.naming;
      this.naming = null;
      const name = input.value.trim();
      if (!keep || !name || (naming?.folder && name === naming.folder.name)) {
        this.renderFolders();
        return;
      }
      if (naming?.folder) this.renameFolder(naming.folder, name);
      else this.createFolder(name, naming?.parent || null);
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
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return wrap;
  }

  startNaming(naming) {
    this.closeMenu();
    // 在"未归类"里点新建：建在最外层，回到"全部题目"才看得到
    if (this.folder === 'none') this.folder = 'all';
    this.naming = naming;
    this.renderFolders();
  }

  // 把题（或文件夹）拖到这里：target 是文件夹编号，null 表示移出文件夹（未归类）/ 挪到最外层
  dropTarget(node, target) {
    node.addEventListener('dragover', (event) => {
      const types = [...(event.dataTransfer?.types || [])];
      if (!types.includes(DRAG_ITEMS) && !types.includes(DRAG_FOLDER)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      node.classList.add('drop');
    });
    node.addEventListener('dragleave', () => node.classList.remove('drop'));
    node.addEventListener('drop', (event) => {
      node.classList.remove('drop');
      const items = event.dataTransfer.getData(DRAG_ITEMS);
      const folder = event.dataTransfer.getData(DRAG_FOLDER);
      if (!items && !folder) return;
      event.preventDefault();
      if (items) this.moveItems(JSON.parse(items), target);
      else if (folder && folder !== target) this.moveFolder(folder, target);
    });
  }

  // 小菜单（文件夹的操作、移到文件夹）：放在 anchor 下面，或者鼠标右键的位置
  openMenu(anchor, build, event = null) {
    this.closeMenu();
    const menu = el('div', 'popover rail-menu bank-menu');
    menu.setAttribute('role', 'menu');
    this.menu = menu;
    this.menuAnchor = anchor;
    document.body.append(menu);
    const place = () => {
      const box = menu.getBoundingClientRect();
      const at = anchor.getBoundingClientRect();
      const x = event?.clientX || at.left;
      // 下面放不下（比如从底部的选择栏打开）就往上弹，不盖住按钮
      let y = event?.clientY || at.bottom + 4;
      if (!event && y + box.height > window.innerHeight - 8 && at.top - box.height - 4 >= 8) y = at.top - box.height - 4;
      menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - box.width - 8))}px`;
      menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - box.height - 8))}px`;
    };
    build(menu, place);
    place();
    menu.querySelector('button')?.focus({ preventScroll: true });
  }

  closeMenu(restoreFocus = false) {
    if (!this.menu) return;
    this.menu.remove();
    this.menu = null;
    if (restoreFocus) this.menuAnchor?.focus();
    this.menuAnchor = null;
  }

  menuItem(label, icon, onClick, className = '') {
    const node = el('button', `rail-menu-item${className ? ` ${className}` : ''}`);
    node.type = 'button';
    node.setAttribute('role', 'menuitem');
    node.innerHTML = svg(icon);
    node.append(el('span', '', label));
    node.addEventListener('click', onClick);
    return node;
  }

  // 一个文件夹的操作：打开、重命名、新建子文件夹、删除（删除先在菜单里确认：题不会删，挪到上一级）
  folderMenu(folder, anchor, event = null) {
    if (!folder) return;
    this.openMenu(anchor, (menu, place) => {
      const main = () => {
        menu.replaceChildren(
          this.menuItem('打开', 'folder', () => { this.closeMenu(); this.openFolder(folder.id); }),
          this.menuItem('重命名', 'pencil', () => {
            this.closeMenu();
            // 改名的输入框放在下一层的文件夹那一行：当前打开的就是它时，先回到它的上一级
            if (this.folder === folder.id) this.folder = folder.parent_id || 'all';
            this.naming = { folder };
            this.renderList();
          }),
          this.menuItem('新建子文件夹', 'folder-plus', () => {
            this.closeMenu();
            this.folder = folder.id;
            this.naming = { parent: folder.id };
            this.renderList();
          }),
          this.menuItem('删除文件夹', 'trash', confirm, 'danger'),
        );
        place();
      };
      const confirm = () => {
        const box = el('div', 'rail-confirm');
        const where = folder.parent_id ? `「${this.folderById(folder.parent_id)?.name || '上一级'}」` : '未归类';
        const inside = folder.total ? `里面的 ${folder.total} 道题会挪到${where}，题不会删。` : '这是个空文件夹。';
        const subs = this.childrenOf(folder.id).length ? '子文件夹也挪到上一级。' : '';
        box.append(el('p', 'ask', `删除文件夹「${folder.name}」？`), el('p', 'note', `${inside}${subs}`));
        const actions = el('div', 'rail-confirm-actions');
        const cancel = button('取消', 'btn', main);
        actions.append(cancel, button('删除', 'btn danger', () => this.deleteFolder(folder)));
        box.append(actions);
        menu.replaceChildren(box);
        place();
        cancel.focus();
      };
      main();
    }, event);
  }

  // 移到文件夹：未归类、各个文件夹（子文件夹缩进），以及新建一个再移进去
  moveMenu(anchor, ids) {
    if (!ids.length) return;
    const inOne = new Set(ids.map((id) => this.item(id)?.folder_id || null));
    const only = inOne.size === 1 ? [...inOne][0] : undefined; // 这些题现在都在同一个地方
    this.openMenu(anchor, (menu, place) => {
      menu.classList.add('move-menu');
      menu.append(el('p', 'pop-title', ids.length > 1 ? `把 ${ids.length} 道题移到` : '移到'));
      const list = el('div', 'move-list');
      const unfiled = this.menuItem('未归类（不放在文件夹里）', 'file', () => { this.closeMenu(); this.moveItems(ids, null); });
      unfiled.disabled = only === null;
      list.append(unfiled);
      for (const { folder, depth } of folderOptions(this.folders)) {
        const item = this.menuItem(folder.name, 'folder', () => { this.closeMenu(); this.moveItems(ids, folder.id); });
        item.style.paddingLeft = `${10 + depth * 16}px`;
        item.disabled = only === folder.id;
        list.append(item);
      }
      menu.append(list);
      // 新建一个文件夹再移进去（建在当前打开的文件夹里）
      const form = el('form', 'move-new');
      const input = el('input', 'folder-input');
      input.type = 'text';
      input.maxLength = 30;
      input.placeholder = this.folderId() ? `在「${this.folderById(this.folderId())?.name}」里新建…` : '新建文件夹…';
      input.setAttribute('aria-label', '新建文件夹再移进去');
      const go = el('button', 'btn', '新建并移入');
      go.type = 'submit';
      form.append(input, go);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const name = input.value.trim();
        if (!name) {
          input.focus();
          return;
        }
        this.closeMenu();
        const folder = await this.createFolder(name, this.folderId(), { quiet: true });
        if (folder) this.moveItems(ids, folder.id);
      });
      menu.append(form);
      place();
    });
  }

  async folderRequest(url, options, success) {
    try {
      const data = await request(url, options);
      if (data.folders) this.folders = data.folders;
      if (success) toast(success);
      return data;
    } catch (error) {
      toast(error.message);
      return null;
    }
  }

  async createFolder(name, parent, { quiet = false } = {}) {
    const data = await this.folderRequest('/api/bank/folders', { method: 'POST', body: JSON.stringify({ name, parent_id: parent }) },
      quiet ? null : `已新建文件夹「${name}」`);
    this.renderList();
    return data?.folder || null;
  }

  async renameFolder(folder, name) {
    await this.folderRequest(`/api/bank/folders/${folder.id}`, { method: 'PATCH', body: JSON.stringify({ name }) }, '已重命名');
    this.renderList();
  }

  async moveFolder(id, parent) {
    const folder = this.folderById(id);
    const into = parent ? `「${this.folderById(parent)?.name || ''}」里` : '最外层';
    await this.folderRequest(`/api/bank/folders/${id}`, { method: 'PATCH', body: JSON.stringify({ move: true, parent_id: parent }) },
      `文件夹「${folder?.name || ''}」挪到了${into}`);
    this.renderList();
  }

  async deleteFolder(folder) {
    this.closeMenu();
    const data = await this.folderRequest(`/api/bank/folders/${folder.id}`, { method: 'DELETE' });
    if (!data) return;
    // 题挪到了上一级：重新读一遍题库（题上记着在哪个文件夹）
    await this.load().catch(() => {});
    if (this.folderId() && !this.folderById(this.folderId())) this.folder = folder.parent_id || 'all';
    this.openFolder(this.folder);
    const parent = folder.parent_id ? this.folderById(folder.parent_id) : null;
    const where = parent ? `「${parent.name}」` : '未归类';
    toast(data.moved_items ? `已删除文件夹「${folder.name}」，里面的 ${data.moved_items} 道题挪到了${where}` : `已删除文件夹「${folder.name}」`);
  }

  async moveItems(ids, folderId) {
    const data = await this.folderRequest('/api/bank/move', { method: 'POST', body: JSON.stringify({ ids, folder_id: folderId }) });
    if (!data) return;
    for (const item of this.items) if (ids.includes(item.id)) item.folder_id = folderId;
    const name = folderId ? `「${this.folderById(folderId)?.name || ''}」` : '未归类';
    this.picked.clear();
    this.confirmingBulk = false;
    this.renderList();
    this.renderPanel();
    toast(data.moved ? `${data.moved} 道题移到了${name}` : `这些题本来就在${name}`);
  }

  // 教材目录（按个人设置里的教材版本）：每个知识点名称的先后顺序，以及考点名称对应的完整路径
  async loadTree() {
    const edition = profile().textbook || 'pep_a';
    if (this.treeEdition === edition && this.order.size) return;
    let tree = [];
    try {
      tree = await request(`/api/curriculum?edition=${edition}`);
    } catch {
      return; // 读不到目录也能用：按名称排序
    }
    this.order.clear();
    this.pathOf.clear();
    this.treeEdition = edition;
    let position = 0;
    const walk = (node, path) => {
      const here = [...path, node.name];
      if (!this.order.has(node.name)) this.order.set(node.name, position);
      position += 1;
      if (!node.children?.length && here.length === 4) this.pathOf.set(node.name, here);
      for (const child of node.children || []) walk(child, here);
    };
    for (const volume of tree) walk(volume, []);
  }

  // 一道题的知识点路径：按考点名称在当前教材版本的目录里找（换了教材版本，旧题也按新版本归类）；
  // 目录里没有这个考点，就用题里存的"册 › 章 › 节 › 考点"
  pathFor(item) {
    const parts = String(item.knowledge || '').split(' › ').map((part) => part.trim()).filter(Boolean);
    const leaf = parts[parts.length - 1];
    if (leaf && this.pathOf.has(leaf)) return this.pathOf.get(leaf);
    if (parts.length >= 2) return parts;
    return ['未归类', leaf || '没写知识点'];
  }

  sortedChildren(children) {
    return [...children.values()].sort((a, b) => (this.order.get(a.name) ?? 1e9) - (this.order.get(b.name) ?? 1e9) || a.name.localeCompare(b.name, 'zh'));
  }

  pickKp(path) {
    this.filters.kp = path;
    for (let i = 1; i <= path.length; i += 1) this.expanded.add(path.slice(0, i).join('›'));
    this.renderList();
    this.renderTree();
  }

  renderKpButton() {
    const kp = this.filters.kp;
    this.kpBtn.textContent = kp.length ? `知识点：${kp[kp.length - 1]}` : '全部知识点';
    this.kpBtn.title = kp.length ? kp.join(' › ') : '按教材目录找题';
    this.kpBtn.classList.toggle('set', kp.length > 0);
    this.kpBtn.setAttribute('aria-expanded', String(this.treeOpen));
  }

  renderTree() {
    this.renderKpButton();
    this.treePanel.hidden = !this.treeOpen;
    if (!this.treeOpen) return;
    // 数量按其余筛选条件算（题型、难度、搜索），不受当前选的知识点影响
    const root = { children: new Map(), count: 0 };
    for (const item of this.visible({ ignoreKp: true })) {
      let node = root;
      node.count += 1;
      for (const part of this.pathFor(item)) {
        if (!node.children.has(part)) node.children.set(part, { name: part, children: new Map(), count: 0 });
        node = node.children.get(part);
        node.count += 1;
      }
    }
    const head = el('div', 'kp-head');
    head.append(
      el('b', '', '按知识点找题'),
      button(`全部（${root.count}）`, `kp-name${this.filters.kp.length ? '' : ' on'}`, () => this.pickKp([])),
      button('收起', 'link-btn', () => { this.treeOpen = false; this.renderTree(); }),
    );
    const walk = (node, path) => {
      const list = el('ul', 'kp-list');
      for (const child of this.sortedChildren(node.children)) {
        const here = [...path, child.name];
        const key = here.join('›');
        const open = this.expanded.has(key);
        const leaf = !child.children.size;
        const row = el('div', 'kp-row');
        const caret = button('', `kp-caret${open ? ' open' : ''}`, () => {
          if (open) this.expanded.delete(key);
          else this.expanded.add(key);
          this.renderTree();
        });
        caret.disabled = leaf;
        caret.setAttribute('aria-label', leaf ? '' : open ? '收起' : '展开');
        row.append(
          caret,
          button(child.name, `kp-name${samePath(here, this.filters.kp) ? ' on' : ''}`, () => this.pickKp(here)),
          el('span', 'kp-count', `${child.count} 道`),
        );
        const li = el('li');
        li.append(row);
        if (!leaf && open) li.append(walk(child, here));
        list.append(li);
      }
      return list;
    };
    this.treePanel.replaceChildren(head, root.count ? walk(root, []) : el('p', 'field-note', '没有符合条件的题。'));
  }

  async load() {
    const [data] = await Promise.all([request('/api/bank'), this.loadTree()]);
    this.items = data.items;
    this.folders = data.folders || [];
    this.shell.setCounts(data.counts);
    const alive = new Set(this.items.map((item) => item.id));
    for (const id of this.picked) if (!alive.has(id)) this.picked.delete(id);
    // 上次看的文件夹已经删掉了：回到全部题目
    if (this.folderId() && !this.folderById(this.folderId())) this.folder = 'all';
  }

  item(id) {
    return this.items.find((item) => item.id === id) || null;
  }

  // focus：要打开的题（题库编号或出题记录编号）；folder：要打开的文件夹（对话里整理完、存完题点"在题库里查看"时）
  async show(focus = null, { folder = null } = {}) {
    try {
      await this.load();
    } catch (error) {
      // 读不到就明说没加载出来，不能显示成"题库还是空的"
      this.lead.textContent = '';
      this.bar.hidden = true;
      this.folderBar.replaceChildren();
      this.list.replaceChildren(el('li', 'bank-empty', `题库没加载出来。${error.message}`));
      return;
    }
    this.naming = null;
    if (folder && this.folderById(folder)) this.folder = folder;
    const target = focus && this.items.find((item) => item.id === focus || item.source_id === focus);
    // 要看的那道题不在当前打开的文件夹里：回到全部题目
    if (target && !this.visible().some((item) => item.id === target.id)) this.folder = 'all';
    if (target) this.current = target.id;
    else if (!this.item(this.current)) this.current = this.visible()[0]?.id || null;
    this.editing = false;
    this.confirming = false;
    this.figuring = false;
    this.renderList();
    this.renderPanel(Boolean(target));
    if (target) this.root.querySelector(`[data-id="${target.id}"]`)?.scrollIntoView({ block: 'center' });
  }

  // ignoreKp：不按知识点筛（算知识点目录里的数量时用）。文件夹：只看直接放在这个文件夹里的题
  visible({ ignoreKp = false } = {}) {
    const { q, type, difficulty, kp } = this.filters;
    const key = normalize(q);
    const view = this.folder;
    return this.items.filter((item) => (view === 'all' || (view === 'none' ? !item.folder_id : item.folder_id === view))
      && (!type || item.question_type === type)
      && (!difficulty || item.difficulty === difficulty)
      && (ignoreKp || !kp.length || startsWith(this.pathFor(item), kp))
      && (!key || normalize(item.stem).includes(key) || normalize(item.knowledge).includes(key)));
  }

  renderList() {
    const items = this.visible();
    const here = this.folderId();
    const inView = this.folder === 'all' ? this.items.length
      : this.items.filter((item) => (this.folder === 'none' ? !item.folder_id : item.folder_id === here)).length;
    const filtered = items.length !== inView;
    const folder = here ? this.folderById(here) : null;
    const nested = folder ? folder.total - folder.count : 0;
    if (!this.items.length) this.lead.textContent = '';
    else if (folder) this.lead.textContent = `「${folder.name}」里有 ${folder.count} 道题${nested ? `，子文件夹里还有 ${nested} 道` : ''}${filtered ? `，符合条件的 ${items.length} 道` : ''}。把题拖到上面的文件夹上，或者勾选后点"移到文件夹"，就能整理。`;
    else if (this.folder === 'none') this.lead.textContent = `不在任何文件夹里的题有 ${inView} 道${filtered ? `，符合条件的 ${items.length} 道` : ''}。`;
    else this.lead.textContent = `共 ${this.items.length} 道题${filtered ? `，符合条件的 ${items.length} 道` : ''}。出好的题会自动收进来；点一道题查看或修改，勾选几道可以组成试卷，也可以建文件夹分类整理。`;
    this.list.innerHTML = '';
    if (!this.items.length) {
      const empty = el('li', 'bank-empty');
      empty.append(el('p', '', '题库还是空的。出好的题会自动收进这里。'), button('去出题', 'btn primary', () => this.shell.setMode('idle')));
      this.list.append(empty);
    } else if (!items.length && inView) {
      this.list.append(el('li', 'bank-empty', '没有符合条件的题，换个关键词或筛选条件试试。'));
    } else if (!items.length) {
      this.list.append(el('li', 'bank-empty', folder
        ? (nested ? '这个文件夹里的题都在子文件夹里，点上面的子文件夹看。' : '这个文件夹还是空的：回到全部题目，把题拖到这个文件夹上，或者勾选后点"移到文件夹"。')
        : '题都放进文件夹了。'));
    }
    // 按知识点排：同一个考点的题放在一起，考点按教材目录的先后，组里新出的在前
    let ordered = items.map((item) => ({ item, group: null }));
    if (this.grouped) {
      const groups = new Map();
      for (const item of items) {
        const path = this.pathFor(item);
        const key = path.join(' › ');
        if (!groups.has(key)) groups.set(key, { path, items: [] });
        groups.get(key).items.push(item);
      }
      const rank = (path) => path.map((part) => this.order.get(part) ?? 1e9);
      const sorted = [...groups.values()].sort((a, b) => {
        const ra = rank(a.path);
        const rb = rank(b.path);
        for (let i = 0; i < Math.max(ra.length, rb.length); i += 1) if ((ra[i] ?? 0) !== (rb[i] ?? 0)) return (ra[i] ?? 0) - (rb[i] ?? 0);
        return a.path.join().localeCompare(b.path.join(), 'zh');
      });
      ordered = sorted.flatMap((group) => group.items.map((item, i) => ({ item, group: i === 0 ? group : null })));
    }
    for (const { item, group } of ordered) {
      if (group) {
        const header = el('li', 'bank-group');
        const [volume, chapter, ...rest] = group.path;
        header.append(
          el('span', 'where', [volume, chapter].filter(Boolean).join(' · ')),
          el('b', '', rest.length ? rest.join(' › ') : chapter || volume),
          el('span', 'n', `${group.items.length} 道`),
        );
        header.addEventListener('click', () => this.pickKp(group.path));
        header.title = '只看这个知识点的题';
        this.list.append(header);
      }
      const row = el('li', 'bank-row');
      row.dataset.id = item.id;
      row.setAttribute('aria-current', String(item.id === this.current));
      const pick = el('input');
      pick.type = 'checkbox';
      pick.checked = this.picked.has(item.id);
      pick.setAttribute('aria-label', '选这道题来组卷');
      pick.addEventListener('change', () => {
        if (pick.checked) this.picked.add(item.id);
        else this.picked.delete(item.id);
        this.confirmingBulk = false;
        this.renderBar();
      });
      const open = el('button', 'bank-open');
      open.type = 'button';
      const meta = el('span', 'bank-meta');
      meta.append(el('span', 'kind', TYPE_LABEL[item.question_type] || ''), el('span', '', DIFFICULTY_LABEL[item.difficulty] || ''));
      if (item.knowledge) meta.append(el('span', 'where', item.knowledge.split(' › ').pop()));
      if (item.solution_issue) meta.append(el('span', 'edited flagged', '待核对'));
      if (item.imported) meta.append(el('span', 'edited', '导入'));
      if (item.edited) meta.append(el('span', 'edited', '改过'));
      // 看全部题目时标出这道题在哪个文件夹
      const home = this.folder === 'all' && item.folder_id ? this.folderById(item.folder_id) : null;
      if (home) {
        const tag = el('span', 'folder-tag');
        tag.insertAdjacentHTML('afterbegin', svg('folder'));
        tag.append(home.name);
        tag.title = this.folderPath(home.id).map((folder) => folder.name).join(' › ');
        meta.append(tag);
      }
      meta.append(el('span', 'when', formatWhen(item.created_at)));
      open.append(meta, mathText('span', 'bank-stem', clipStem(item.stem)));
      open.addEventListener('click', () => this.select(item.id));
      // 拖到文件夹上就移进去：勾选了几道、拖的是其中一道时，几道一起移
      row.draggable = true;
      row.addEventListener('dragstart', (event) => {
        const ids = this.picked.has(item.id) ? [...this.picked] : [item.id];
        event.dataTransfer.setData(DRAG_ITEMS, JSON.stringify(ids));
        event.dataTransfer.effectAllowed = 'move';
        row.classList.add('dragging');
      });
      row.addEventListener('dragend', () => row.classList.remove('dragging'));
      row.append(pick, open);
      this.list.append(row);
    }
    renderMath(this.list);
    this.renderFolders();
    this.renderBar();
    this.renderTree();
  }

  renderBar() {
    const count = this.picked.size;
    const visible = this.visible();
    this.allBox.checked = visible.length > 0 && visible.every((item) => this.picked.has(item.id));
    this.bar.hidden = !count;
    this.bar.innerHTML = '';
    if (!count) return;
    if (this.tidying) {
      this.bar.append(el('span', 'pick-count', this.tidying));
      return;
    }
    if (this.confirmingBulk) {
      this.bar.append(
        el('span', 'pick-count', `从题库删除选中的 ${count} 道题？删除后不能恢复。`),
        button('删除', 'btn danger', () => this.removeMany([...this.picked])),
        button('取消', 'btn', () => { this.confirmingBulk = false; this.renderBar(); }),
      );
      return;
    }
    this.bar.append(el('span', 'pick-count', `已选 ${count} 道`));
    const paper = this.shell.openPaper();
    if (paper) this.bar.append(button(`加入《${paper.title}》`, 'btn primary', () => this.compose(true)));
    const move = button('移到文件夹', 'btn', () => this.moveMenu(move, [...this.picked]));
    this.bar.append(
      button('用选中的题组卷', paper ? 'btn' : 'btn primary', () => this.compose(false)),
      move,
      button('精简解析', 'btn', () => this.tidyMany([...this.picked])),
      button('删除', 'btn', () => { this.confirmingBulk = true; this.renderBar(); }),
      button('取消选择', 'link-btn', () => { this.picked.clear(); this.renderList(); }),
    );
  }

  // 一道一道地整理，每道几秒；中途出错的跳过，最后告诉用户结果
  async tidyMany(ids) {
    let done = 0;
    const failed = [];
    for (const [index, id] of ids.entries()) {
      this.tidying = `正在精简解析：第 ${index + 1} 道，共 ${ids.length} 道…`;
      this.renderBar();
      try {
        const updated = await request(`/api/bank/${id}/tidy`, { method: 'POST', body: JSON.stringify({ detail: 'concise' }) });
        this.items = this.items.map((existing) => (existing.id === updated.id ? updated : existing));
        done += 1;
      } catch {
        failed.push(id);
      }
    }
    this.tidying = null;
    this.picked = new Set(failed);
    this.renderList();
    this.renderPanel();
    this.shell.paperChanged();
    toast(failed.length ? `精简了 ${done} 道，${failed.length} 道没成功（仍然勾选着，可以再试）` : `已精简 ${done} 道题的解析，完整推导都保留着`);
  }

  async compose(intoOpenPaper) {
    const ids = this.items.filter((item) => this.picked.has(item.id)).map((item) => item.id);
    const done = intoOpenPaper ? await this.shell.addToPaper(ids) : await this.shell.newPaper(ids);
    if (done) this.picked.clear();
  }

  select(id) {
    this.current = id;
    this.editing = false;
    this.confirming = false;
    this.figuring = false;
    this.root.querySelectorAll('.bank-row').forEach((row) => row.setAttribute('aria-current', String(row.dataset.id === id)));
    this.renderPanel(true);
  }

  renderPanel(reveal = false) {
    const panel = this.shell.panel;
    const item = this.item(this.current);
    panel.open(reveal);
    if (!item) {
      panel.header({ heading: '题目' });
      panel.message('选一道题', ['点左边列表里的题，可以在这里查看、修改或删除。']);
      return;
    }
    if (this.editing) {
      this.renderEditor(item);
      return;
    }
    const actions = this.confirming
      ? [
        el('span', 'confirm-text', '从题库删除这道题？'),
        button('删除', 'btn danger', () => this.removeMany([item.id])),
        button('取消', 'btn', () => { this.confirming = false; this.renderPanel(); }),
      ]
      : [
        button(item.full_solution_steps.length ? '重新精简解析' : '精简解析', 'ghost-btn', (event) => this.tidy(item, event.currentTarget)),
        button(item.figure_svg ? '改配图' : '配图', 'ghost-btn', () => { this.figuring = !this.figuring; this.renderPanel(); }),
        button('移到', 'ghost-btn', (event) => this.moveMenu(event.currentTarget, [item.id])),
        button('修改', 'ghost-btn', () => { this.editing = true; this.renderPanel(); }),
        button('删除', 'ghost-btn', () => { this.confirming = true; this.renderPanel(); }),
      ];
    const home = item.folder_id ? this.folderById(item.folder_id) : null;
    const when = item.edited ? `改于 ${formatWhen(item.updated_at)}` : `收进题库 ${formatWhen(item.created_at)}`;
    panel.header({
      heading: '题目',
      note: home ? `${when} · 在「${home.name}」` : when,
      actions,
    });
    panel.body.innerHTML = '';
    if (this.figuring) panel.body.append(this.figureForm(item));
    panel.body.append(buildSheet(asDraft(item), { brief: { grade: item.grade }, meta: { knowledge_label: item.knowledge } }));
    renderMath(panel.body);
    panel.body.scrollTop = 0;
  }

  renderEditor(item) {
    const panel = this.shell.panel;
    const draft = structuredClone(item);
    const form = el('form', 'editor');
    form.addEventListener('submit', (event) => { event.preventDefault(); save(); });

    const optionsBox = el('div', 'editor-options');
    const optionInputs = [];
    const renderOptions = () => {
      optionsBox.innerHTML = '';
      optionInputs.length = 0;
      optionsBox.hidden = !CHOICE.has(draft.question_type);
      if (optionsBox.hidden) return;
      const keys = ['A', 'B', 'C', 'D'];
      for (const option of draft.options) if (!keys.includes(option.key)) keys.push(option.key);
      for (const key of keys) {
        const found = draft.options.find((option) => option.key === key);
        const field = mathField(`选项 ${key}`, found?.text || '', { multiline: false });
        optionInputs.push({ key, input: field.input });
        optionsBox.append(field.wrap);
      }
    };

    const meta = el('div', 'editor-meta');
    const typeField = el('div', 'field');
    typeField.append(el('span', 'field-label', '题型'), segmented(
      Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label })),
      draft.question_type,
      (value) => {
        draft.options = optionInputs.map(({ key, input }) => ({ key, text: input.value }));
        draft.question_type = value;
        renderOptions();
      },
      '题型',
    ));
    const levelField = el('div', 'field');
    levelField.append(el('span', 'field-label', '难度'), segmented(
      [1, 2, 3, 4, 5].map((level) => ({ value: level, label: `${level} ${DIFFICULTY_LABEL[level]}` })),
      draft.difficulty,
      (value) => { draft.difficulty = value; },
      '难度',
    ));
    const gradeField = el('div', 'field');
    gradeField.append(el('span', 'field-label', '年级'), segmented(
      [{ value: '', label: '不写' }, ...GRADES.map((g) => ({ value: g, label: g }))],
      draft.grade,
      (value) => { draft.grade = value; },
      '年级',
    ));
    const knowledge = el('label', 'field');
    const knowledgeInput = el('input', 'text-input');
    knowledgeInput.value = draft.knowledge;
    knowledge.append(el('span', 'field-label', '知识点'), knowledgeInput);
    meta.append(typeField, levelField, gradeField, knowledge);

    const stem = mathField('题干', draft.stem, { rows: 5, hint: '公式用 $…$ 包起来，比如 $x^2+1$。' });
    const answer = mathField('答案', draft.answer, { multiline: false });
    const method = mathField('思路', draft.solution_method, { rows: 3 });
    const steps = mathField('解析步骤', draft.solution_steps.join('\n\n'), { rows: 8, hint: '每一步之间空一行。' });
    const mistakes = mathField('易错点', draft.common_mistakes.join('\n'), { rows: 3, hint: '一行一条。' });
    renderOptions();
    form.append(meta, stem.wrap, optionsBox, answer.wrap, method.wrap, steps.wrap, mistakes.wrap);

    const save = async () => {
      const payload = {
        question_type: draft.question_type,
        difficulty: draft.difficulty,
        grade: draft.grade,
        knowledge: knowledgeInput.value.trim(),
        stem: stem.input.value.trim(),
        options: CHOICE.has(draft.question_type)
          ? optionInputs.map(({ key, input }) => ({ key, text: input.value.trim() })).filter((option) => option.text)
          : [],
        answer: answer.input.value.trim(),
        solution_method: method.input.value.trim(),
        solution_steps: steps.input.value.split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean),
        authoring_intent: item.authoring_intent,
        assessed_methods: item.assessed_methods,
        common_mistakes: mistakes.input.value.split('\n').map((s) => s.trim()).filter(Boolean),
        full_solution_method: item.full_solution_method,
        full_solution_steps: item.full_solution_steps,
        figure: item.figure,
        figure_svg: item.figure_svg,
      };
      if (!payload.stem) {
        toast('题干不能是空的');
        stem.input.focus();
        return;
      }
      try {
        const updated = await request(`/api/bank/${item.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        this.items = this.items.map((existing) => (existing.id === updated.id ? updated : existing));
        this.editing = false;
        this.renderList();
        this.renderPanel();
        this.shell.paperChanged();
        toast('已保存修改');
      } catch (error) {
        toast(`没保存上：${error.message}`);
      }
    };

    panel.header({
      heading: '修改题目',
      note: '改完点保存',
      actions: [
        button('保存', 'btn primary', save),
        button('取消', 'btn', () => { this.editing = false; this.renderPanel(); }),
      ],
    });
    panel.body.innerHTML = '';
    panel.body.append(form);
    panel.body.scrollTop = 0;
    stem.input.focus();
  }

  // 配图：可以写一句要求（比如"把 E 标在 PC 的中点"），配图员按题干和要求重新画；也可以去掉配图
  figureForm(item) {
    const form = el('form', 'figure-form');
    const input = el('input', 'text-input');
    input.maxLength = 200;
    input.placeholder = item.figure_svg ? '想怎么改，比如：把 E 标在 PC 的中点；不写就重新画一张' : '有特别要求可以写在这里，不写也行';
    input.setAttribute('aria-label', '配图要求');
    const row = el('div', 'figure-form-actions');
    const draw = button(item.figure_svg ? '重新画' : '画图', 'btn primary', () => this.figure(item, 'draw', input.value, draw));
    row.append(draw);
    if (item.figure_svg) row.append(button('去掉配图', 'btn', (event) => this.figure(item, 'remove', '', event.currentTarget)));
    row.append(button('取消', 'link-btn', () => { this.figuring = false; this.renderPanel(); }));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!draw.disabled) this.figure(item, 'draw', input.value, draw);
    });
    form.append(el('div', 'field-label', item.figure_svg ? '改配图' : '给这道题配图'), input, row);
    setTimeout(() => input.focus(), 0);
    return form;
  }

  async figure(item, action, requestText, trigger) {
    const label = trigger.textContent;
    trigger.disabled = true;
    trigger.textContent = action === 'draw' ? '正在画图…' : '正在去掉…';
    try {
      const data = await request(`/api/bank/${item.id}/figure`, { method: 'POST', body: JSON.stringify({ action, request: requestText.trim() }) });
      this.items = this.items.map((existing) => (existing.id === data.item.id ? data.item : existing));
      this.figuring = false;
      this.renderList();
      this.renderPanel();
      this.shell.paperChanged();
      if (action === 'remove') toast('已去掉配图');
      else toast(data.problems.length ? `画好了，但和题干可能还有出入：${data.problems[0]}` : '配图画好了，和题干核对一致');
    } catch (error) {
      toast(error.message);
      trigger.disabled = false;
      trigger.textContent = label;
    }
  }

  // 把解析整理成标准答案的写法；总是从完整推导出发，完整推导保留在下面可以展开
  async tidy(item, trigger) {
    const label = trigger.textContent;
    trigger.disabled = true;
    trigger.textContent = '正在精简…';
    try {
      const updated = await request(`/api/bank/${item.id}/tidy`, { method: 'POST', body: JSON.stringify({ detail: 'concise' }) });
      this.items = this.items.map((existing) => (existing.id === updated.id ? updated : existing));
      this.renderList();
      this.renderPanel();
      this.shell.paperChanged();
      toast(`解析精简成 ${updated.solution_steps.length} 步，完整推导在下面可以展开`);
    } catch (error) {
      toast(error.message);
      trigger.disabled = false;
      trigger.textContent = label;
    }
  }

  async removeMany(ids) {
    try {
      const data = await request('/api/bank/delete', { method: 'POST', body: JSON.stringify({ ids }) });
      // 文件夹里的题数跟着变
      this.folders = (await request('/api/bank/folders').catch(() => null))?.folders || this.folders;
      const gone = new Set(ids);
      const order = this.visible().map((item) => item.id);
      const index = order.indexOf(this.current);
      this.items = this.items.filter((item) => !gone.has(item.id));
      for (const id of ids) this.picked.delete(id);
      if (gone.has(this.current)) this.current = order.slice(index + 1).find((id) => !gone.has(id)) || this.visible()[0]?.id || null;
      this.confirming = false;
      this.confirmingBulk = false;
      this.shell.setCounts(data.counts);
      this.renderList();
      this.renderPanel();
      this.shell.paperChanged();
      toast(ids.length > 1 ? `已从题库删除 ${data.deleted} 道题` : '已从题库删除');
    } catch (error) {
      toast(`没删掉：${error.message}`);
    }
  }
}
