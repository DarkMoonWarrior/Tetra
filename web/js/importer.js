// 导入题目：上传试卷、讲义的 PDF 或照片，模型逐页识别出题目；老师核对、勾选后收进题库，之后就能用来组卷。

import { DIFFICULTY_LABEL, TYPE_LABEL, el, mathText, renderMath, request, toast } from './shared.js';
import { profile } from './settings.js';

const ACCEPT = '.pdf,image/png,image/jpeg,image/webp';
const MAX_BYTES = 30 * 1024 * 1024;

function button(text, className, onClick) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, size: file.size, data: reader.result });
    reader.onerror = () => reject(new Error(`${file.name} 没读出来`));
    reader.readAsDataURL(file);
  });
}

const sizeText = (bytes) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

export function openImporter({ onSaved } = {}) {
  // opened：展开了解析的题（识别过程中列表会重画，展开的保持展开）
  const state = { files: [], models: [], model: null, job: null, picked: new Set(), opened: new Set(), timer: null, busy: false };
  const dialog = el('dialog', 'import-dialog');
  dialog.setAttribute('aria-label', '导入题目');
  const head = el('header', 'import-head');
  head.append(el('h2', '', '导入题目'), button('关闭', 'link-btn', () => close()));
  const body = el('div', 'import-body');
  const foot = el('footer', 'import-foot');
  dialog.append(head, body, foot);
  document.body.append(dialog);
  dialog.addEventListener('close', () => { clearTimeout(state.timer); dialog.remove(); });
  dialog.showModal();

  function close() {
    if (state.job?.status === 'running') request(`/api/imports/${state.job.id}/stop`, { method: 'POST' }).catch(() => {});
    dialog.close();
  }

  // ---------- 第一步：选文件和模型 ----------
  const picker = el('input');
  Object.assign(picker, { type: 'file', accept: ACCEPT, multiple: true, hidden: true });
  picker.addEventListener('change', () => addFiles(picker.files));

  async function addFiles(list) {
    for (const file of list) {
      if (file.size > MAX_BYTES) {
        toast(`${file.name} 超过 30 MB，先拆小一点再传`);
        continue;
      }
      try {
        state.files.push(await readFile(file));
      } catch (error) {
        toast(error.message);
      }
    }
    picker.value = '';
    draw();
  }

  async function loadModels() {
    try {
      state.models = (await request('/api/imports/models')).models;
    } catch {
      state.models = [];
    }
    state.model = (state.models.find((m) => m.vision) || state.models[0])?.id || null;
    draw();
  }

  function drawSetup() {
    const zone = el('div', 'import-drop');
    zone.append(
      el('b', '', '把 PDF 或图片拖到这里'),
      el('span', '', '试卷、讲义、拍的照片都行；PDF 每页单独识别，最多 40 页'),
      button('选择文件', 'btn', () => picker.click()),
    );
    zone.addEventListener('dragover', (event) => { event.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('over'));
    zone.addEventListener('drop', (event) => { event.preventDefault(); zone.classList.remove('over'); addFiles(event.dataTransfer.files); });
    const list = el('ul', 'import-files');
    state.files.forEach((file, index) => {
      const item = el('li');
      item.append(
        el('span', 'name', file.name),
        el('span', 'size', sizeText(file.size)),
        button('移除', 'link-btn', () => { state.files.splice(index, 1); draw(); }),
      );
      list.append(item);
    });
    const choose = el('select', 'select-input');
    choose.setAttribute('aria-label', '识别用的模型');
    for (const m of state.models) {
      const option = new Option(m.vision ? m.model : `${m.model}（可能看不了图）`, m.id);
      choose.add(option);
    }
    choose.value = state.model || '';
    choose.addEventListener('change', () => { state.model = choose.value; });
    const modelRow = el('label', 'import-model');
    modelRow.append(el('span', '', '识别用的模型'), choose);
    body.replaceChildren(
      el('p', 'field-note', '上传试卷或讲义，自动识别出题目（题干、选项、答案和解析、考点），核对后收进题库，就能用来组卷。'),
      zone, list, modelRow, picker,
    );
    const start = button(state.busy ? '正在上传…' : '开始识别', 'btn primary', begin);
    start.disabled = !state.files.length || state.busy || !state.models.length;
    foot.replaceChildren(start, button('取消', 'btn', () => close()));
  }

  async function begin() {
    state.busy = true;
    draw();
    try {
      state.job = await request('/api/imports', {
        method: 'POST',
        body: JSON.stringify({ files: state.files.map(({ name, data }) => ({ name, data })), model: state.model, edition: profile().textbook }),
      });
      state.files = [];
      poll();
    } catch (error) {
      toast(`没能开始识别：${error.message}`);
    } finally {
      state.busy = false;
      draw();
    }
  }

  async function poll() {
    clearTimeout(state.timer);
    if (!state.job || !dialog.isConnected) return;
    try {
      const before = state.job.questions.length;
      state.job = await request(`/api/imports/${state.job.id}`);
      // 新识别出来的题默认勾上（有题干就算）
      state.job.questions.slice(before).forEach((q) => state.picked.add(q.index));
      draw();
    } catch (error) {
      toast(error.message);
      return;
    }
    if (state.job.status === 'running') state.timer = setTimeout(poll, 1500);
  }

  // ---------- 第二步：看识别结果，勾选后收进题库 ----------
  function questionCard(q) {
    const card = el('li', 'import-q');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = state.picked.has(q.index);
    box.setAttribute('aria-label', '收进题库');
    box.addEventListener('change', () => {
      if (box.checked) state.picked.add(q.index);
      else state.picked.delete(q.index);
      drawFoot();
    });
    const main = el('div', 'import-q-main');
    const meta = el('div', 'import-q-meta');
    meta.append(
      el('b', '', `${q.number ? `${q.number}．` : ''}${TYPE_LABEL[q.question_type] || ''}`),
      el('span', '', q.knowledge ? q.knowledge.split(' › ').pop() : '未对上考点'),
      el('span', '', DIFFICULTY_LABEL[q.difficulty] || ''),
      el('span', 'src', q.source),
    );
    main.append(meta, mathText('div', 'import-q-stem', q.stem));
    // 从原卷上截下来的配图（服务端截的图片，包在 SVG 里）
    if (q.figure_svg) {
      const figure = el('div', 'import-q-figure');
      figure.innerHTML = q.figure_svg;
      main.append(figure);
    }
    if (q.options.length) {
      const options = el('ol', 'import-q-options');
      for (const option of q.options) options.append(mathText('li', '', `${option.key}．${option.text}`));
      main.append(options);
    }
    if (q.answer) main.append(mathText('p', 'import-q-answer', `答案　${q.answer}`));
    // 解答过程默认收起，点开核对是否完整
    if (q.solution_steps.length) {
      const steps = el('details', 'import-q-steps');
      steps.open = state.opened.has(q.index);
      steps.addEventListener('toggle', () => {
        if (steps.open) state.opened.add(q.index);
        else state.opened.delete(q.index);
      });
      steps.append(el('summary', '', `解析（${q.solution_steps.length} 行）`));
      for (const step of q.solution_steps) steps.append(mathText('p', '', step));
      main.append(steps);
    }
    for (const note of q.warnings) main.append(el('p', 'import-q-warn', note));
    card.append(box, main);
    return card;
  }

  function drawResult() {
    const job = state.job;
    const running = job.status === 'running';
    const progress = el('div', 'import-progress');
    const bar = el('div', 'batch-bar');
    const fill = el('i');
    fill.style.width = `${Math.round((job.pages_done / Math.max(1, job.pages)) * 100)}%`;
    bar.append(fill);
    progress.append(
      el('p', '', running
        ? `正在识别：${job.pages_done} / ${job.pages} 页，已经找到 ${job.questions.length} 道题`
        : `识别完了：${job.pages} 页，找到 ${job.questions.length} 道题${job.status === 'cancelled' ? '（中途停下了）' : ''}。勾选的题会收进题库。`),
      bar,
    );
    if (!running) bar.hidden = true;
    const nodes = [progress];
    for (const error of job.errors) nodes.push(el('p', 'import-q-warn', `没识别成：${error}`));
    if (job.questions.length) {
      const tools = el('div', 'import-tools');
      tools.append(
        button('全选', 'link-btn', () => { job.questions.forEach((q) => state.picked.add(q.index)); draw(); }),
        button('全不选', 'link-btn', () => { state.picked.clear(); draw(); }),
      );
      const list = el('ol', 'import-list');
      job.questions.forEach((q) => list.append(questionCard(q)));
      nodes.push(tools, list);
    } else if (!running) {
      nodes.push(el('p', 'field-note', '没有识别出题目。换一个更清楚的文件，或者换一个能看图的模型再试。'));
    }
    const scroll = body.scrollTop;
    body.replaceChildren(...nodes);
    renderMath(body);
    body.scrollTop = scroll;
    drawFoot();
  }

  function drawFoot() {
    const job = state.job;
    const running = job.status === 'running';
    const count = job.questions.filter((q) => state.picked.has(q.index)).length;
    const save = button(`收进题库（${count} 道）`, 'btn primary', async () => {
      save.disabled = true;
      try {
        const picked = job.questions.filter((q) => state.picked.has(q.index));
        const data = await request(`/api/imports/${job.id}/save`, { method: 'POST', body: JSON.stringify({ questions: picked }) });
        toast(`已收进题库 ${data.saved} 道`);
        dialog.close();
        onSaved?.(data);
      } catch (error) {
        toast(`没收进去：${error.message}`);
        save.disabled = false;
      }
    });
    save.disabled = running || !count;
    const nodes = [save];
    if (running) nodes.push(button('停止识别', 'btn', () => request(`/api/imports/${job.id}/stop`, { method: 'POST' }).catch(() => {})));
    nodes.push(button('关闭', 'btn', () => close()));
    foot.replaceChildren(...nodes);
  }

  function draw() {
    if (state.job) drawResult();
    else drawSetup();
  }

  draw();
  loadModels();
}
