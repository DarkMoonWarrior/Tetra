// 试卷：从题库里挑题组卷，调整顺序和分值，导出 Word 或 PDF。
// 右栏是试卷预览，和导出的 PDF、打印出来的样子一致（都由后端的打印页生成）。

import {
  $, DIFFICULTY_LABEL, TYPE_LABEL, el, formatWhen, mathText, renderMath, request, runtime, segmented, toast,
} from './shared.js';
import { isDark } from './theme.js';

const TYPE_ORDER = ['single_choice', 'multiple_choice', 'fill_blank', 'open_response'];
const SECTION_TITLE = { single_choice: '单项选择题', multiple_choice: '多项选择题', fill_blank: '填空题', open_response: '解答题' };
const DEFAULT_SCORE = { single_choice: 5, multiple_choice: 6, fill_blank: 5, open_response: 12 };
const CN_NUMBERS = '一二三四五六七八九十';
const oneLine = (text) => String(text || '').replace(/\$\$/g, '$').replace(/\s*\n+\s*/g, ' ');
const scoreText = (value) => (Number.isInteger(value) ? String(value) : String(Number(value.toFixed(1))));

function button(text, className, onClick, title = '') {
  const node = el('button', className, text);
  node.type = 'button';
  if (title) {
    node.title = title;
    node.setAttribute('aria-label', title);
  }
  node.addEventListener('click', onClick);
  return node;
}

function filenameFrom(header) {
  const match = /filename\*=UTF-8''([^;]+)/i.exec(header || '');
  return match ? decodeURIComponent(match[1]) : null;
}

export function defaultScore(type) {
  return DEFAULT_SCORE[type] || 5;
}

// 桌面版：刚存好的文件，打开它或者在资源管理器里打开所在的文件夹
async function openSaved(target, path) {
  try {
    await request('/api/desktop/open', { method: 'POST', body: JSON.stringify({ target, path }) });
  } catch (error) {
    toast(error.message);
  }
}

// 导出一份试卷（Word 或 PDF）；trigger 是点的那个按钮，生成期间改成"正在生成…"。
// 网页版（浏览器里打开的）：浏览器下载。桌面版的窗口：直接存进设置里的文件夹（默认"文档\Tetra"，或者每次弹另存为），存好后可以打开
export async function downloadPaper(paperId, { format, answers = true, title = '试卷', trigger = null }) {
  const label = trigger?.textContent;
  if (trigger) {
    trigger.disabled = true;
    trigger.textContent = format === 'pdf' ? '正在生成 PDF…' : '正在生成 Word…';
  }
  try {
    if (runtime.desktop) {
      const saved = await request(`/api/papers/${paperId}/save`, { method: 'POST', body: JSON.stringify({ format, answers }) });
      if (saved.cancelled) return;
      toast(`已保存：${saved.name}`, [
        { label: '打开', onClick: () => openSaved('file', saved.path) },
        { label: '打开文件夹', onClick: () => openSaved('folder', saved.path) },
      ]);
      return;
    }
    const response = await fetch(`/api/papers/${paperId}/export?format=${format}&answers=${answers}`);
    if (!response.ok) {
      let detail = '';
      try {
        detail = (await response.json()).detail;
      } catch {
        // 不是 JSON，按状态码提示
      }
      throw new Error(detail || `导出失败（${response.status}）`);
    }
    const blob = await response.blob();
    const name = filenameFrom(response.headers.get('Content-Disposition')) || `${title}.${format}`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast(`已导出：${name}`);
  } catch (error) {
    toast(error.message);
  } finally {
    if (trigger) {
      trigger.disabled = false;
      trigger.textContent = label;
    }
  }
}

export class Papers {
  constructor(shell) {
    this.shell = shell;
    this.root = $('#paperView');
    this.paper = null;
    this.saveTimer = null;
    this.saving = null;
    this.confirmingDelete = false;
    this.frame = null;
    // 换了主题：正在看的试卷预览按新主题重新载入
    window.addEventListener('tetra:theme', () => {
      if (this.paper && this.frame?.isConnected) this.renderPreview();
    });
  }

  open() {
    return this.paper ? { id: this.paper.id, title: this.paper.title } : null;
  }

  async show(id = null) {
    if (id) await this.load(id);
    if (this.paper) {
      this.renderEditor();
      this.renderPreview();
    } else {
      await this.renderList();
    }
  }

  async load(id) {
    try {
      this.paper = await request(`/api/papers/${id}`);
      this.confirmingDelete = false;
    } catch (error) {
      this.paper = null;
      toast(error.message);
    }
  }

  async renderList() {
    this.paper = null;
    this.shell.panel.close();
    this.root.innerHTML = '';
    let papers = [];
    let failed = '';
    try {
      const data = await request('/api/papers');
      papers = data.papers;
      this.shell.setCounts(data.counts);
    } catch (error) {
      failed = `试卷列表没加载出来。${error.message}`;
    }
    const head = el('header', 'view-head');
    head.append(
      el('h1', '', '试卷'),
      el('p', 'view-lead', '在题库里勾选题目，点「用选中的题组卷」；也可以先建一份空白试卷，再从题库添加。组好后可以导出 Word 或 PDF。'),
    );
    const tools = el('div', 'view-tools');
    tools.append(
      button('新建空白试卷', 'btn primary', () => this.create([])),
      button('去题库挑题', 'btn', () => this.shell.setMode('bank')),
    );
    const list = el('ul', 'paper-list');
    if (!papers.length) list.append(el('li', 'bank-empty', failed || '还没有试卷。'));
    for (const paper of papers) {
      const row = el('li');
      const open = el('button', 'paper-row');
      open.type = 'button';
      open.append(
        el('span', 'title', paper.title),
        el('span', 'meta', `${paper.count} 道题，满分 ${scoreText(paper.total)} 分，改于 ${formatWhen(paper.updated_at)}`),
      );
      open.addEventListener('click', () => this.show(paper.id));
      row.append(open);
      list.append(row);
    }
    this.root.append(head, tools, list);
  }

  async create(bankIds) {
    const items = bankIds.map((id) => ({ bank_id: id, score: defaultScore(this.shell.bankItem(id)?.question_type) }));
    const now = new Date();
    try {
      const paper = await request('/api/papers', {
        method: 'POST',
        body: JSON.stringify({ title: `数学练习（${now.getMonth() + 1} 月 ${now.getDate()} 日）`, items }),
      });
      this.paper = paper;
      this.shell.setMode('papers');
      await this.show();
      this.shell.refreshCounts();
      toast(items.length ? `已用 ${items.length} 道题组成试卷` : '已新建空白试卷');
      return true;
    } catch (error) {
      toast(`试卷没建成：${error.message}`);
      return false;
    }
  }

  async add(bankIds) {
    if (!this.paper) return this.create(bankIds);
    const have = new Set(this.paper.items.map((item) => item.bank_id));
    const fresh = bankIds.filter((id) => !have.has(id));
    for (const id of fresh) {
      const question = this.shell.bankItem(id);
      this.paper.items.push({ bank_id: id, score: defaultScore(question?.question_type), question });
    }
    await this.save();
    this.shell.setMode('papers');
    await this.show();
    toast(fresh.length ? `已加入 ${fresh.length} 道题` : '这些题已经在试卷里了');
    return true;
  }

  // 题库里改了或删了题，重新读一遍正在编辑的试卷
  async refresh() {
    if (!this.paper) return;
    await this.load(this.paper.id);
  }

  sections() {
    const groups = Object.fromEntries(TYPE_ORDER.map((type) => [type, []]));
    const missing = [];
    for (const item of this.paper.items) {
      if (item.question) groups[item.question.question_type].push(item);
      else missing.push(item);
    }
    let number = 0;
    const sections = TYPE_ORDER.filter((type) => groups[type].length).map((type, index) => {
      const rows = groups[type].map((item) => ({ item, number: (number += 1) }));
      const total = rows.reduce((sum, row) => sum + Number(row.item.score), 0);
      return { type, rows, total, heading: `${CN_NUMBERS[index]}、${SECTION_TITLE[type]}（共 ${rows.length} 小题，共 ${scoreText(total)} 分）` };
    });
    return { sections, missing };
  }

  // 在同一大题里和相邻的题交换位置
  move(item, step) {
    const items = this.paper.items;
    const same = items.filter((other) => other.question && other.question.question_type === item.question.question_type);
    const neighbor = same[same.indexOf(item) + step];
    if (!neighbor) return;
    const a = items.indexOf(item);
    const b = items.indexOf(neighbor);
    [items[a], items[b]] = [items[b], items[a]];
    this.changed(true);
  }

  remove(item) {
    this.paper.items = this.paper.items.filter((other) => other !== item);
    this.changed(true);
  }

  changed(rerender = false) {
    if (rerender) this.renderEditor();
    else this.renderSummary();
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.save(), 500);
  }

  payload() {
    const { title, subtitle, minutes, show_answers: showAnswers, items } = this.paper;
    return {
      title: title.trim() || '数学练习',
      subtitle,
      minutes: minutes || null,
      show_answers: showAnswers,
      style: this.paper.style === 'standard' ? 'standard' : 'practice',
      exam_name: (this.paper.exam_name || '').trim(),
      tag: (this.paper.tag || '').trim(),
      items: items.map((item) => ({ bank_id: item.bank_id, score: Number(item.score) || 0 })),
    };
  }

  async save() {
    clearTimeout(this.saveTimer);
    if (!this.paper) return;
    const id = this.paper.id;
    this.saving = request(`/api/papers/${id}`, { method: 'PUT', body: JSON.stringify(this.payload()) });
    try {
      const saved = await this.saving;
      if (this.paper?.id === id) {
        this.paper.updated_at = saved.updated_at;
        this.renderPreview();
      }
    } catch (error) {
      toast(`试卷没保存上：${error.message}`);
    } finally {
      this.saving = null;
    }
  }

  async flush() {
    if (this.saveTimer) await this.save();
    else if (this.saving) await this.saving.catch(() => {});
  }

  renderEditor() {
    const paper = this.paper;
    this.root.innerHTML = '';
    const back = button('全部试卷', 'link-btn back-link', async () => { await this.flush(); this.renderList(); });

    const title = el('input', 'paper-title-input');
    title.value = paper.title;
    title.maxLength = 60;
    title.setAttribute('aria-label', '试卷标题');
    title.addEventListener('input', () => { paper.title = title.value; this.changed(); });

    const fields = el('div', 'paper-fields');
    const subtitle = el('input', 'text-input');
    subtitle.placeholder = '副标题，比如：高三一轮复习 集合与常用逻辑用语';
    subtitle.maxLength = 120;
    subtitle.value = paper.subtitle || '';
    subtitle.setAttribute('aria-label', '副标题');
    subtitle.addEventListener('input', () => { paper.subtitle = subtitle.value; this.changed(); });
    const minutesWrap = el('label', 'minutes');
    const minutes = el('input', 'text-input');
    minutes.type = 'number';
    minutes.min = '1';
    minutes.max = '300';
    minutes.placeholder = '不写';
    minutes.value = paper.minutes || '';
    minutes.addEventListener('input', () => { paper.minutes = Number(minutes.value) || null; this.changed(); });
    minutesWrap.append(el('span', '', '考试时间'), minutes, el('span', '', '分钟'));
    const answers = el('label', 'answers-toggle');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = paper.show_answers;
    box.addEventListener('change', () => { paper.show_answers = box.checked; this.changed(); });
    answers.append(box, el('span', '', '附答案与解析'));

    // 版式：练习卷（原来的排法）或标准试卷（照高考数学卷：绝密★启用前、注意事项、新高考的大题说明）
    const standard = paper.style === 'standard';
    const style = segmented(
      [{ value: 'practice', label: '练习卷' }, { value: 'standard', label: '标准试卷（高考格式）' }],
      standard ? 'standard' : 'practice',
      (value) => { paper.style = value; this.changed(true); },
      '试卷版式',
    );
    const styleRow = el('div', 'paper-style');
    styleRow.append(el('span', 'label', '版式'), style, el('span', 'note', standard
      ? '照高考数学卷排版：版头、注意事项、大题说明、字体和页码都按模拟卷的标准；答案单独成册，页码各自编。'
      : '题号、大题按题型自动编；适合课堂练习和作业。'));
    if (standard) {
      const now = new Date();
      const examName = el('input', 'text-input');
      examName.maxLength = 60;
      examName.placeholder = `${now.getFullYear() + (now.getMonth() >= 6 ? 1 : 0)}年普通高等学校招生全国统一考试`;
      examName.value = paper.exam_name || '';
      examName.setAttribute('aria-label', '考试名称');
      examName.title = '印在试卷最上面的考试名称；空着用默认的写法';
      examName.addEventListener('input', () => { paper.exam_name = examName.value; this.changed(); });
      const tag = el('input', 'text-input tag-input');
      tag.maxLength = 30;
      tag.placeholder = '右上角标记，比如：模拟卷（一）';
      tag.value = paper.tag || '';
      tag.setAttribute('aria-label', '右上角标记');
      tag.addEventListener('input', () => { paper.tag = tag.value; this.changed(); });
      const gaokao = button('按高考分值', 'btn', () => {
        // 单选 5 分、多选 6 分、填空 5 分；解答题依次 13、15、15、17、17 分（超出的按 17 分）
        const open = [13, 15, 15, 17, 17];
        let k = 0;
        for (const { rows, type } of this.sections().sections) {
          for (const { item } of rows) {
            item.score = type === 'open_response' ? open[k++] ?? 17 : type === 'multiple_choice' ? 6 : 5;
          }
        }
        this.changed(true);
        toast('已按新高考数学卷的分值设好');
      });
      gaokao.title = '单选 5 分、多选 6 分、填空 5 分，解答题依次 13、15、15、17、17 分';
      fields.append(examName, tag, answers, gaokao);
    } else {
      fields.append(subtitle, minutesWrap, answers);
    }

    this.summary = el('p', 'paper-summary');
    const body = el('div', 'paper-sections');
    const { sections, missing } = this.sections();
    if (!sections.length) {
      const empty = el('div', 'bank-empty');
      empty.append(el('p', '', '这份试卷还没有题目。'), button('去题库挑题', 'btn primary', () => this.shell.setMode('bank')));
      body.append(empty);
    }
    for (const section of sections) {
      const block = el('section', 'paper-section');
      block.append(el('h3', '', section.heading));
      const list = el('ol', 'paper-items');
      section.rows.forEach(({ item, number }, index) => {
        const row = el('li', 'paper-item');
        const score = el('label', 'score');
        const input = el('input', 'text-input');
        input.type = 'number';
        input.min = '0';
        input.max = '100';
        input.step = '0.5';
        input.value = item.score;
        input.setAttribute('aria-label', `第 ${number} 题的分值`);
        input.addEventListener('input', () => { item.score = Number(input.value) || 0; this.changed(); });
        score.append(input, el('span', '', '分'));
        const tools = el('div', 'item-tools');
        const up = button('↑', 'icon-btn', () => this.move(item, -1), '上移');
        const down = button('↓', 'icon-btn', () => this.move(item, 1), '下移');
        up.disabled = index === 0;
        down.disabled = index === section.rows.length - 1;
        tools.append(up, down, button('×', 'icon-btn', () => this.remove(item), '从试卷里拿掉'));
        const stem = mathText('span', 'stem-line compact-math', oneLine(item.question.stem));
        stem.title = `${TYPE_LABEL[item.question.question_type]}，${DIFFICULTY_LABEL[item.question.difficulty]}`;
        row.append(el('span', 'no', `${number}．`), stem, score, tools);
        list.append(row);
      });
      block.append(list);
      body.append(block);
    }
    if (missing.length) {
      const note = el('p', 'paper-missing');
      note.append(`有 ${missing.length} 道题已经从题库删掉了，导出时会跳过。`);
      note.append(button('从试卷里清掉', 'link-btn', () => {
        this.paper.items = this.paper.items.filter((item) => item.question);
        this.changed(true);
      }));
      body.append(note);
    }

    const actions = el('div', 'paper-actions');
    const word = button('导出 Word', 'btn primary', () => this.download('docx', word));
    const pdf = button('导出 PDF', 'btn primary', () => this.download('pdf', pdf));
    const empty = !sections.length;
    word.disabled = empty;
    pdf.disabled = empty;
    const print = button('打印', 'btn', () => this.print());
    print.disabled = empty;
    actions.append(word, pdf, print, button('从题库添加', 'btn', () => this.shell.setMode('bank')));
    actions.append(this.confirmingDelete
      ? this.deleteConfirm()
      : button('删除试卷', 'link-btn danger-link', () => { this.confirmingDelete = true; this.renderEditor(); }));

    this.root.append(back, title, styleRow, fields, this.summary, body, actions);
    this.renderSummary();
    renderMath(body);
  }

  deleteConfirm() {
    const wrap = el('span', 'inline-confirm');
    wrap.append(
      el('span', '', '删除这份试卷？题库里的题不受影响。'),
      button('删除', 'btn danger', () => this.destroy()),
      button('取消', 'btn', () => { this.confirmingDelete = false; this.renderEditor(); }),
    );
    return wrap;
  }

  renderSummary() {
    if (!this.summary) return;
    const kept = this.paper.items.filter((item) => item.question);
    const total = kept.reduce((sum, item) => sum + Number(item.score || 0), 0);
    this.summary.textContent = kept.length
      ? `共 ${kept.length} 道题，满分 ${scoreText(total)} 分。题号按题型排：单选、多选、填空、解答。`
      : '';
  }

  renderPreview() {
    const panel = this.shell.panel;
    panel.open(false);
    panel.header({ heading: '试卷预览', note: this.paper.show_answers ? '附答案与解析' : '不含答案' });
    if (!this.frame) {
      this.frame = el('iframe', 'paper-frame');
      this.frame.title = '试卷预览';
    }
    panel.frame(this.frame);
    this.frame.src = `/api/papers/${this.paper.id}/print?answers=${this.paper.show_answers}&dark=${isDark()}&v=${encodeURIComponent(this.paper.updated_at)}`;
  }

  async download(format, trigger) {
    await this.flush();
    await downloadPaper(this.paper.id, { format, answers: this.paper.show_answers, title: this.paper.title, trigger });
  }

  async print() {
    await this.flush();
    window.open(`/api/papers/${this.paper.id}/print?answers=${this.paper.show_answers}&auto=true`, '_blank');
  }

  async destroy() {
    try {
      const data = await request(`/api/papers/${this.paper.id}`, { method: 'DELETE' });
      this.shell.setCounts(data.counts);
      toast('已删除试卷');
      await this.renderList();
    } catch (error) {
      toast(`没删掉：${error.message}`);
    }
  }
}
