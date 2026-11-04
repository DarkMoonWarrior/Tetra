// 拖动分隔条调整三栏宽度：左栏和中间之间一条，中间和右栏之间一条。
// 宽度记在这台电脑的浏览器里；双击分隔条恢复默认；也可以用键盘左右方向键调整。

import { $, readStorage, writeStorage } from './shared.js';

const MIN_THREAD = 420;

const PANES = [
  {
    handle: '#railSplit',
    key: 'mingti.rail-width',
    variable: '--rail-w',
    min: 200,
    max: () => Math.min(440, window.innerWidth - MIN_THREAD),
    // 左栏贴着窗口左边，宽度就是鼠标的横坐标
    widthAt: (x) => x,
    sign: 1,
  },
  {
    handle: '#sheetSplit',
    key: 'mingti.sheet-width',
    variable: '--sheet-w',
    min: 340,
    max: () => window.innerWidth - railWidth() - MIN_THREAD,
    // 右栏贴着窗口右边，往左拖变宽
    widthAt: (x) => window.innerWidth - x,
    sign: -1,
  },
];

function railWidth() {
  const rail = $('#rail');
  return getComputedStyle(rail).position === 'fixed' ? 0 : rail.getBoundingClientRect().width;
}

function clamp(pane, width) {
  return Math.round(Math.max(pane.min, Math.min(pane.max(), width)));
}

function apply(pane, width) {
  document.documentElement.style.setProperty(pane.variable, `${width}px`);
  const handle = $(pane.handle);
  handle.setAttribute('aria-valuenow', String(width));
  handle.setAttribute('aria-valuemin', String(pane.min));
  handle.setAttribute('aria-valuemax', String(Math.max(pane.min, Math.round(pane.max()))));
}

function reset(pane) {
  document.documentElement.style.removeProperty(pane.variable);
  writeStorage(localStorage, pane.key, null);
}

function current(pane) {
  const target = pane.variable === '--rail-w' ? $('#rail') : $('#sheetPanel');
  return target.getBoundingClientRect().width;
}

function setup(pane) {
  const handle = $(pane.handle);
  const saved = readStorage(localStorage, pane.key);
  if (Number(saved) > 0) apply(pane, clamp(pane, Number(saved)));

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing');
    const move = (e) => apply(pane, clamp(pane, pane.widthAt(e.clientX)));
    const stop = () => {
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', stop);
      handle.removeEventListener('pointercancel', stop);
      writeStorage(localStorage, pane.key, Math.round(current(pane)));
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', stop);
    handle.addEventListener('pointercancel', stop);
  });

  handle.addEventListener('dblclick', () => reset(pane));

  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 64 : 16;
    let width = null;
    if (event.key === 'ArrowRight') width = current(pane) + step * pane.sign;
    else if (event.key === 'ArrowLeft') width = current(pane) - step * pane.sign;
    else if (event.key === 'Home') {
      reset(pane);
      event.preventDefault();
      return;
    }
    if (width === null) return;
    event.preventDefault();
    const next = clamp(pane, width);
    apply(pane, next);
    writeStorage(localStorage, pane.key, next);
  });
}

export function initSplitters() {
  for (const pane of PANES) setup(pane);
  // 窗口变窄后，记住的宽度可能放不下，按新的窗口重新收一收
  window.addEventListener('resize', () => {
    for (const pane of PANES) {
      const saved = Number(readStorage(localStorage, pane.key));
      if (saved > 0) apply(pane, clamp(pane, saved));
    }
  });
}
