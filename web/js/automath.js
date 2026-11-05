// 把模型写的说明文字里的纯文本公式认出来，转成 LaTeX 写进 $…$，和题目用同一套数学字体。
//
// 题干、选项、解析步骤都按要求写成了 LaTeX，但易错点、考查方法、命题意图、过程记录这些说明文字，
// 模型常写成纯文本：V=πr²h、√3/2、a_{n+1}、AC²>AG²+CG²，甚至裸的 \frac{3}{8}。
// 这里只在显示时转换，不改存着的内容；已经写在 $…$ 里的不动，KaTeX 排不出来的保持原样。
// 分数线照原样留着（1/2x 到底是几分之几，不替模型猜）。

const SUPERSCRIPT = { '²': '2', '³': '3', '⁴': '4', 'ⁿ': 'n', 'ˣ': 'x' };
const SYMBOLS = {
  '−': '-', '–': '-', '∣': '|', '′': "'", '%': '\\%', '…': '\\cdots ', '*': '\\cdot ',
  '≤': '\\leqslant ', '≥': '\\geqslant ', '≠': '\\ne ', '≈': '\\approx ', '±': '\\pm ', '·': '\\cdot ', '×': '\\times ', '÷': '\\div ',
  '∈': '\\in ', '∉': '\\notin ', '⊂': '\\subset ', '⊆': '\\subseteq ', '∪': '\\cup ', '∩': '\\cap ',
  '⊥': '\\perp ', '∥': '\\parallel ', '→': '\\to ', '⇒': '\\Rightarrow ', '⇔': '\\Leftrightarrow ',
  '∞': '\\infty ', '∠': '\\angle ', '△': '\\triangle ', '°': '^\\circ ',
  α: '\\alpha ', β: '\\beta ', γ: '\\gamma ', δ: '\\delta ', ε: '\\varepsilon ', ζ: '\\zeta ', η: '\\eta ', θ: '\\theta ',
  λ: '\\lambda ', μ: '\\mu ', ξ: '\\xi ', π: '\\pi ', ρ: '\\rho ', σ: '\\sigma ', τ: '\\tau ', φ: '\\varphi ', ϕ: '\\varphi ', ω: '\\omega ',
  Δ: '\\Delta ', Σ: '\\sum ', Ω: '\\Omega ',
};
const NAMES = 'arcsin|arccos|arctan|sin|cos|tan|cot|ln|lg|log|exp|max|min';
const GREEK_WORDS = { alpha: 'α', beta: 'β', gamma: 'γ', theta: 'θ', lambda: 'λ', omega: 'ω', phi: 'φ', pi: 'π' }; // 有时直接写英文名
// 这些是普通的词，不是点名或变量（标准里的缩写、界面上会出现的词）
const WORDS = new Set(['AI', 'API', 'PDF', 'PNG', 'SVG', 'HTML', 'JSON', 'HTTP', 'URL', 'UI', 'ID', 'OK', 'AQI', 'GDP', 'CPI', 'PPT', 'vs', 'etc']);

const CJK = /[\u4e00-\u9fff]/;
// 单独一个大写字母一般是点，但这些说法后面的是选项或方案，不排成斜体
const OPTION_BEFORE = /(?:方案|选项|误选|错选|漏选|改选|应选|选)[\sA-H、,，或和与及]*$/;
const ONLY_OPTIONS = /^(?:[^A-Z]*[：:])?\s*[A-H](?:\s*[、,，]\s*[A-H])*\s*[。．]?$/;
// 已经写成公式的部分
const WRITTEN = /(\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])/;
// 可能是公式的一段：中文和中文标点之外，只由这些字符组成
const RUN = /[A-Za-z0-9α-ωΑ-Ω²³⁴ⁿˣ₀-₉√\\'′+\-−–*/=<>≤≥≠≈±·×÷^_()[\]{}|∣,.:%…∈∉⊂⊆∪∩⊥∥→⇒⇔∞∠△° ]+/g;

const isRaw = (text) => /\\[a-zA-Z]/.test(text); // 已经是 LaTeX，只是没写 $

function isVariable(word) {
  if (WORDS.has(word)) return false;
  return /^[A-Z]{1,6}$/.test(word) // 点、线段、平面：A、AB、PAC
    || /^[a-z]{1,3}$/.test(word) // 变量：x、ab
    || /^[A-Z][a-z]$/.test(word) // Sn
    || /^[a-z]O[a-z]$/.test(word) // xOy 平面
    || word === 'sqrt' || word in GREEK_WORDS
    || new RegExp(`^[a-zA-Z]{0,3}(?:${NAMES})[a-zA-Z]{0,3}$`).test(word); // sinx、xlnx
}

function isFormula(core) {
  if (isRaw(core)) return true;
  const words = core.match(/[A-Za-z]+/g) || [];
  if (!words.every(isVariable)) return false;
  if (words.length || /[α-ωΑ-Ω√∞△∠]/.test(core)) return true;
  // 只有数字：有等号、不等号、分数线才算公式，单独的数和编号不算；2-3 这样的短横多半是"到"，也不算
  return /[=<>≤≥≠≈/×÷^]|\d\s*[−+]\s*\d|[([{]\s*[-−]?\d+\s*,/.test(core);
}

// 把写在下标、上标后面的 {} 留作分组，其余的 {} 是集合的花括号
function escapeBraces(text) {
  const stack = [];
  let out = '';
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === '{') {
      const group = text[i - 1] === '_' || text[i - 1] === '^';
      stack.push(group);
      out += group ? '{' : '\\{';
    } else if (char === '}') {
      out += stack.pop() ? '}' : '\\}';
    } else out += char;
  }
  return out;
}

function closing(text) {
  let depth = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === '(') depth += 1;
    else if (text[i] === ')') {
      depth -= 1;
      if (!depth) return i;
    }
  }
  return -1;
}

function roots(text) {
  let out = '';
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] !== '√') {
      out += text[i];
      continue;
    }
    const rest = text.slice(i + 1);
    const end = rest[0] === '(' ? closing(rest) : -1;
    const simple = rest.match(/^\s*(\d+(?:\.\d+)?|[A-Za-zα-ω])/);
    if (end > 0) {
      out += `\\sqrt{${roots(rest.slice(1, end))}}`;
      i += end + 1;
    } else if (simple) {
      out += `\\sqrt{${simple[1]}}`;
      i += simple[0].length;
    } else out += '\\surd ';
  }
  return out;
}

function toTex(core) {
  if (isRaw(core)) return core;
  let tex = core.replace(/sqrt(?=\()/g, '√').replace(/[a-z]+/g, (word) => GREEK_WORDS[word] || word);
  tex = tex.replace(/[²³⁴ⁿˣ]+/g, (marks) => `^{${[...marks].map((mark) => SUPERSCRIPT[mark]).join('')}}`);
  tex = tex.replace(/[₀-₉]+/g, (marks) => `_{${[...marks].map((mark) => mark.charCodeAt(0) - 0x2080).join('')}}`);
  tex = escapeBraces(tex);
  tex = tex.replace(/[A-Za-z]+/g, (word) => word.replace(new RegExp(NAMES, 'g'), (name) => `\\${name} `));
  tex = tex.replace(/([A-Za-z])(\d+)/g, '$1_{$2}'); // x1、AB1、S20
  tex = tex.replace(/([_^])(\d{2,})/g, '$1{$2}').replace(/_([A-Z]{2,})/g, '_{$1}').replace(/\^[-−](\d+)/g, '^{-$1}'); // x^10、k_OA、x^-2
  tex = tex.replace(/e\^/g, '\\mathrm{e}^').replace(/(\\ln )\s*e(?![A-Za-z_^])/g, '$1\\mathrm{e}');
  tex = roots(tex).replace(/\/\//g, '∥');
  return tex.replace(/[−–∣′%…*≤≥≠≈±·×÷∈∉⊂⊆∪∩⊥∥→⇒⇔∞∠△°α-ωΑ-Ω]/g, (char) => SYMBOLS[char] ?? char).trim();
}

function renders(tex) {
  try {
    globalThis.katex?.renderToString(tex, { throwOnError: true });
    return true;
  } catch {
    return false;
  }
}

// 开头的序号 (1)、首尾的空格和标点、配不上对的括号都不算公式；末尾的短横是连字符（λ-控制函数）
function edges(run) {
  let lead = run.match(/^(?:[\s,.:]|\(\d\)\s*)*/)[0];
  let core = run.slice(lead.length);
  let tail = core.match(/[\s,.:-]*$/)[0];
  core = core.slice(0, core.length - tail.length);
  const count = (pattern) => (core.match(pattern) || []).length;
  while (core && /[)\]]$/.test(core) && count(/[)\]]/g) > count(/[([]/g)) {
    tail = core.at(-1) + tail;
    core = core.slice(0, -1);
  }
  while (core && /^[([]/.test(core) && count(/[([]/g) > count(/[)\]]/g)) {
    lead += core[0];
    core = core.slice(1);
  }
  return [lead, core, tail];
}

function convert(run, prior, after, mixed) {
  const [lead, core, tail] = edges(run);
  if (!core || (/^[A-H]$/.test(core) && OPTION_BEFORE.test(prior + lead))) return run;
  if (isFormula(core)) {
    const tex = toTex(core);
    if (!renders(tex)) return run;
    // 紧挨着汉字时留一点空，和题干里"已知 $x$ 满足"的排法一样
    const left = !lead && CJK.test(prior.slice(-1)) ? ' ' : '';
    const right = !tail && after && CJK.test(after) ? ' ' : '';
    return `${lead}${left}$${tex}$${right}${tail}`;
  }
  // 整段不像公式，但中文里夹着的可能只有其中一截是（"API 返回 x^2"那样的情况）
  if (mixed && /\s/.test(core)) {
    const pieces = core.split(/(\s+)/).map((piece, index) => (index % 2 ? piece : convert(piece, '', '', false)));
    return lead + pieces.join('') + tail;
  }
  return run;
}

// text：说明文字；返回同一段话，公式写在 $…$ 里（交给 renderMath 排）
export function autoMath(text) {
  const source = String(text ?? '');
  if (ONLY_OPTIONS.test(source)) return source; // "答案：A、C"
  const mixed = CJK.test(source); // 全是英文的句子不拆开看，免得 is、the 这样的词被当成变量
  return source
    .split(WRITTEN)
    .map((part, index) => (index % 2 ? part : part.replace(RUN, (run, at) => convert(run, part.slice(0, at), part[at + run.length], mixed))))
    .join('');
}
