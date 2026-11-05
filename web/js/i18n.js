// 界面语言：中文（默认）或英文（English）。
//
// 做法：界面代码照常写中文。选了英文时，按对照表（i18n-en.js，由 tools/build_i18n.py 从代码里提取中文字符串、
// 请模型翻译后生成）把页面上显示的文字换成英文：文本节点，以及 title、placeholder、aria-label 这几个属性。
// 动态加进来的内容由 MutationObserver 跟着换。
// - 完全一样的句子直接查表；带变量的句子（比如"出好 3 道"）按模板匹配，变量部分再递归翻译。
// - 题目、解析、模型的回答、你自己输入的内容不翻译：这些元素带 translate="no"（或者在 SKIP 列出的区域里）。
// - 查不到的保留中文。

const KEY = 'mingti.lang';
const CJK = /[㐀-鿿]/;
// 不翻译的区域：题目和解析、公式、模型的回答、你写的内容
const CONTENT = '[translate="no"], .katex, .print, .stem, .chat-body, script, style';
// 文字也不换的元素：输入框里是你写的字（但它们的 placeholder、title 照常翻译）、代码
const SKIP = `${CONTENT}, textarea, input, select, option, code, pre`;
const ATTRS = ['title', 'placeholder', 'aria-label'];

let lang = (() => {
  try {
    return localStorage.getItem(KEY) === 'en' ? 'en' : 'zh';
  } catch {
    return 'zh';
  }
})();

export const language = () => lang;

// 记下要用的语言；和现在用的不一样（需要刷新页面）时返回 true。本机存不了就不刷新，免得来回刷
export function setLanguage(next) {
  const value = next === 'en' ? 'en' : 'zh';
  if (value === lang) return false;
  try {
    localStorage.setItem(KEY, value);
    return localStorage.getItem(KEY) === value;
  } catch {
    return false;
  }
}

let exact = new Map();
let patterns = [];

// "出好 {0} 道" → /^出好 (.+?) 道$/；模板里没有中文的（比如"{0}，{1}"）太宽，不用
function compile([zh, en]) {
  const parts = zh.split(/(\{\d+\})/);
  const order = [];
  let source = '^';
  for (const part of parts) {
    const slot = /^\{(\d+)\}$/.exec(part);
    if (slot) {
      order.push(Number(slot[1]));
      source += '([\\s\\S]*?)';
    } else {
      source += part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }
  }
  return { re: new RegExp(`${source}$`), order, en, weight: zh.replace(/\{\d+\}/g, '').length };
}

// 太宽的模板不用：没有中文的（比如"{0}，{1}"），以及两头都是空、中间只有两三个字的（比如"{0}题干{1}{2}"，
// 会把"构思题干与选项"翻成"构思 stem 与选项"）
function usable(zh) {
  const fixed = zh.replace(/\{\d+\}/g, '');
  const cjk = (fixed.match(/[㐀-鿿]/g) || []).length;
  if (!cjk) return false;
  return !(/^\{\d+\}/.test(zh) && /\{\d+\}$/.test(zh) && cjk <= 3);
}

export function loadDictionary(dictionary) {
  exact = new Map(Object.entries(dictionary.exact || {}));
  patterns = (dictionary.patterns || [])
    .filter(([zh]) => usable(zh))
    .map(compile)
    // 固定文字多的模板先试，更准；一样多的，空位少的先试（"{0}{1}{2}"那种会把两个词并成一个空）
    .sort((a, b) => b.weight - a.weight || a.order.length - b.order.length);
}

// 前面：数字、空格、圆点、井号；后面：空格、省略号、句号、冒号、问号、感叹号、逗号、分号
const AFFIX = /^([\d\s.•·#-]*)([\s\S]*?)([\s…。.:：!！?？,，、;；]*)$/;
const PUNCT = { '。': '.', '：': ':', '！': '!', '？': '?', '，': ',', '、': ',', '；': ';' };
const punctuation = (text) => text.replace(/[。：！？，、；]/g, (mark) => PUNCT[mark]);

// 中文英文之间的句子：英文句子之间加一个空格（换行的不加）
function joinSentences(pieces) {
  return pieces.reduce((out, piece) => (out && !/\s$/.test(out) && !/^\s/.test(piece) ? `${out} ${piece}` : out + piece), '');
}

export function translate(text, depth = 0) {
  if (!text || !CJK.test(text) || depth > 4) return text;
  const lead = text.match(/^\s*/)[0];
  const tail = text.match(/\s*$/)[0];
  const core = text.trim();
  const hit = exact.get(core);
  if (hit !== undefined) return lead + hit + tail;
  // 前后带着编号、省略号、冒号这类字符（"1 基础""构思题干与选项…""考点："）：去掉再查，整个查到了再接回去
  const affix = AFFIX.exec(core);
  if (affix && (affix[1] || affix[3]) && affix[2]) {
    const inner = translate(affix[2], depth + 1);
    if (!CJK.test(inner)) return lead + affix[1] + inner + punctuation(affix[3]) + tail;
  }
  // 几句话连在一起（比如按钮提示"按下面的设置出题：……。考点不限，……。"）：先一句一句查，句句都换掉了就用这个；
  // 不然模板会把后面几句都塞进最后一个空里
  const sentences = core.split(/(?<=[。！？\n])/).filter(Boolean);
  const each = sentences.length > 1 ? sentences.map((sentence) => translate(sentence, depth + 1)) : null;
  if (each && each.every((sentence) => !CJK.test(sentence))) return lead + joinSentences(each) + tail;
  for (const pattern of patterns) {
    const match = pattern.re.exec(core);
    if (!match) continue;
    const values = match.slice(1).map((value) => translate(value, depth + 1));
    const out = pattern.en.replace(/\{(\d+)\}/g, (_, n) => values[pattern.order.indexOf(Number(n))] ?? '');
    return lead + out + tail;
  }
  if (each && each.some((sentence, i) => sentence !== sentences[i])) return lead + joinSentences(each) + tail;
  // 用顿号、逗号、分号、斜杠、›隔开的几段（比如"高一，课后作业""必修第一册 › 集合"）：分开查
  const pieces = core.split(/(\s*[，、；/·›]\s*)/);
  if (pieces.length > 1) {
    const done = pieces.map((piece, i) => (i % 2 ? { '，': ', ', '、': ', ', '；': '; ', '/': '/', '·': ' · ', '›': ' › ' }[piece.trim()] ?? piece : translate(piece, depth + 1)));
    if (done.some((piece, i) => i % 2 === 0 && piece !== pieces[i])) return lead + done.join('') + tail;
  }
  return text;
}

function skipped(node) {
  const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  return !element || Boolean(element.closest(SKIP));
}

function translateElement(element) {
  if (element.closest?.(CONTENT)) return;
  for (const name of ATTRS) {
    const value = element.getAttribute?.(name);
    if (value && CJK.test(value)) {
      const out = translate(value);
      if (out !== value) element.setAttribute(name, out);
    }
  }
}

function translateTree(root) {
  if (root.nodeType === Node.TEXT_NODE) {
    if (!skipped(root)) {
      const out = translate(root.data);
      if (out !== root.data) root.data = out;
    }
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  translateElement(root); // 输入框本身的 placeholder、title 也要换
  if (skipped(root)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      if (node.nodeType !== Node.ELEMENT_NODE) return NodeFilter.FILTER_ACCEPT;
      if (node.closest(CONTENT)) return NodeFilter.FILTER_REJECT;
      // 输入框、代码：属性照换，里面的文字不碰
      if (node.closest(SKIP)) {
        translateElement(node);
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const texts = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.nodeType === Node.TEXT_NODE) texts.push(node);
    else translateElement(node);
  }
  for (const node of texts) {
    if (!CJK.test(node.data)) continue;
    const out = translate(node.data);
    if (out !== node.data) node.data = out;
  }
}

// 选了英文：先把已有的页面换掉，再盯着之后加进来、改过的内容
export async function startI18n() {
  if (lang !== 'en') return;
  document.documentElement.lang = 'en';
  try {
    const { EN } = await import('./i18n-en.js');
    loadDictionary(EN);
  } catch {
    return; // 没有对照表：照常显示中文
  }
  document.title = translate(document.title);
  translateTree(document.body);
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'characterData') translateTree(record.target);
      else if (record.type === 'attributes') translateElement(record.target);
      else record.addedNodes.forEach((node) => translateTree(node));
    }
  }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
}
