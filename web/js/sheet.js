// 题目：把题干、答案和解析排成教辅上印的样子。
// 这是给老师直接用的，只放题目本身；审题、复核这些过程信息留在中间的出题过程里。

import { DIFFICULTY_LABEL, TYPE_LABEL, autoMath, el, mathText, normalize, pips, tidyMath } from './shared.js';

export function optionColumns(options) {
  const longest = Math.max(
    ...options.map((o) => o.text.replace(/\\[a-zA-Z]+/g, 'x').replace(/[{}$\s^_]/g, '').length),
  );
  if (longest <= 10) return 'cols-4';
  if (longest <= 24) return 'cols-2';
  return 'cols-1';
}

function curriculumLabel(curriculum) {
  if (!curriculum) return '';
  return [curriculum.volume, curriculum.chapter, curriculum.section, curriculum.name]
    .filter((part, i, all) => part && all.indexOf(part) === i)
    .join(' › ');
}

function listOrDash(items) {
  if (!items || !items.length) return el('p', '', '—');
  const list = el('ul');
  for (const item of items) list.append(mathText('li', '', autoMath(item)));
  return list;
}

// draft：题目（完整结果或只有题干的预览）；preview=true 时只排题干和选项。
// ended：预览的题已经不会再往下写了（没出成、停下了），写明原因，不再显示"还在写"
export function buildSheet(draft, { brief = {}, meta = {}, preview = false, reveal = false, ended = '' } = {}) {
  const paper = el('article', 'paper');
  const head = el('header', 'paper-head');
  const tags = el('div', 'paper-tags');
  tags.append(el('span', 'paper-type', TYPE_LABEL[draft.question_type] || draft.question_type));
  if (brief.grade) tags.append(el('span', 'paper-tag', brief.grade));
  if (brief.teaching_purpose) tags.append(el('span', 'paper-tag', brief.teaching_purpose));
  const target = draft.target_difficulty || brief.target_difficulty || draft.estimated_difficulty || 3;
  const difficulty = el('div', 'paper-difficulty');
  difficulty.append(pips(target), el('span', '', DIFFICULTY_LABEL[target] || ''));
  if (draft.estimated_difficulty && draft.estimated_difficulty !== target) {
    difficulty.title = `目标难度 ${target}，模型估计 ${draft.estimated_difficulty}`;
    difficulty.append(el('span', 'paper-estimate', `模型估计${DIFFICULTY_LABEL[draft.estimated_difficulty]}`));
  }
  head.append(tags, difficulty);
  paper.append(head);

  const path = curriculumLabel(draft.curriculum) || meta.knowledge_label;
  if (path) paper.append(el('p', 'paper-path', path));

  paper.append(mathText('div', 'stem print', draft.stem_latex || draft.stem_plain));
  // 配图：服务端按图纸画好的 SVG（只含线条、点和点名），放在题干下面
  if (draft.figure_svg) {
    const figure = el('figure', 'q-figure');
    figure.innerHTML = draft.figure_svg;
    paper.append(figure);
  }
  if (draft.options && draft.options.length) {
    const list = el('ol', `options print ${optionColumns(draft.options)}`);
    for (const option of draft.options) {
      const item = el('li');
      item.append(el('span', 'option-key', `${option.key}.`), mathText('span', '', option.text));
      list.append(item);
    }
    paper.append(list);
  }

  if (preview) {
    const pending = el('div', `paper-pending${ended ? ' ended' : ''}`);
    if (ended) pending.append(el('span', '', ended));
    else pending.append(el('span', 'pending-pen'), el('span', '', '题干已通过审题，答案和解析还在写'));
    paper.append(pending);
    return paper;
  }

  paper.append(el('div', 'answer-rule', '答案与解析'));
  // 返工到上限时交付的题：写明复核还有什么意见没解决，老师知道该核对哪里
  if (draft.solution_issue) {
    const notice = mathText('p', 'review-notice', '');
    notice.append(el('b', '', '待核对　'), document.createTextNode(`复核还有意见没解决：${autoMath(draft.solution_issue)}`));
    paper.append(notice);
  }
  // 答案、分析、详解三段用同一种字体字号，开头都是加粗的标签
  const answerRow = el('p', 'answer-row print');
  answerRow.append(el('b', '', '答案　'));
  // 选择题的答案按试卷习惯写成"ABC"，也不再重复列一遍"A、B、C"
  const choice = draft.question_type === 'single_choice' || draft.question_type === 'multiple_choice';
  const letters = choice ? (String(draft.answer_value || '').toUpperCase().match(/[A-H]/g) || []).join('') : '';
  answerRow.append(mathText('span', `answer-mark${reveal ? ' reveal' : ''}`, letters || draft.answer_value));
  paper.append(answerRow);
  if (!choice && draft.final_answer && normalize(draft.final_answer) !== normalize(draft.answer_value)) {
    paper.append(mathText('p', 'answer-final print', draft.final_answer));
  }

  // 解析按试卷的样子排：【分析】一句思路，【详解】一行一句推理、不编号（从原卷导入的解答也这样排）；
  // 只有没整理过的完整推导（解析详略选"详细"时）才按步骤编号
  if (draft.solution_method) {
    const method = mathText('p', 'method print', '');
    method.append(el('b', '', '分析　'), document.createTextNode(autoMath(draft.solution_method)));
    paper.append(method);
  }
  if (examStyle(draft)) {
    const lines = el('div', 'solution-lines print');
    (draft.solution_steps || []).forEach((step, index) => {
      const line = mathText('p', '', '');
      if (index === 0) line.append(el('b', '', '详解　'));
      line.append(document.createTextNode(tidyMath(step)));
      lines.append(line);
    });
    paper.append(lines);
  } else {
    const steps = el('ol', 'steps-print print');
    for (const step of draft.solution_steps || []) steps.append(mathText('li', '', step));
    paper.append(steps);
  }

  // 整理成标准答案之前，解题员写的完整推导；审题和复核都是按它核对的
  if (draft.full_solution_steps && draft.full_solution_steps.length) {
    const full = el('details', 'full-derivation');
    const summary = el('summary');
    summary.append('完整推导', el('small', '', `${draft.full_solution_steps.length} 步`));
    full.append(summary);
    if (draft.full_solution_method) {
      const method = mathText('p', 'method print', '');
      method.append(el('b', '', '思路　'), document.createTextNode(autoMath(draft.full_solution_method)));
      full.append(method);
    }
    const list = el('ol', 'steps-print print');
    for (const step of draft.full_solution_steps) list.append(mathText('li', '', step));
    full.append(list);
    paper.append(full);
  }

  const notes = el('section', 'paper-notes');
  const intent = el('div');
  intent.append(el('h4', '', '命题意图'), mathText('p', '', autoMath(draft.authoring_intent) || '—'));
  const methods = el('div');
  methods.append(el('h4', '', '考查方法'), listOrDash(draft.assessed_methods));
  const mistakes = el('div');
  mistakes.append(el('h4', '', '易错点'), listOrDash(draft.common_mistakes));
  notes.append(intent, methods, mistakes);
  paper.append(notes);
  return paper;
}

// 解析按试卷的样子一行一句排（不编号）：整理过的解析，或者从原卷导入的解答
function examStyle(draft) {
  return Boolean((draft.full_solution_steps && draft.full_solution_steps.length) || draft.imported);
}

export function draftAsText(draft) {
  const lines = [`【题目】${draft.stem_latex || draft.stem_plain}`];
  for (const option of draft.options || []) lines.push(`${option.key}. ${option.text}`);
  lines.push(`【答案】${draft.answer_value}`);
  if (draft.solution_method) lines.push(`【分析】${draft.solution_method}`);
  const steps = draft.solution_steps || [];
  if (examStyle(draft)) {
    steps.forEach((step, i) => lines.push(i === 0 ? `【详解】${step}` : step));
  } else {
    lines.push('【解析】');
    steps.forEach((step, i) => lines.push(`${i + 1}. ${step}`));
  }
  return lines.join('\n');
}
