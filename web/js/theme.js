// 界面主题：白天（light）、夜晚（dark）、跟随系统（system）。
// 选择存在个人设置里；本机另存一份（localStorage），页面一打开 index.html 里的小脚本就按它上色，不会先闪一下白底。
// 实际用的是 <html data-theme="light|dark">，深色的配色在 app.css 的 :root[data-theme="dark"] 里。

const KEY = 'mingti.theme';
const THEMES = ['light', 'dark', 'system'];
const system = window.matchMedia('(prefers-color-scheme: dark)');
let preference = (() => {
  try {
    return localStorage.getItem(KEY) || 'light';
  } catch {
    return 'light';
  }
})();

export function applyTheme(next = preference) {
  preference = THEMES.includes(next) ? next : 'light';
  try {
    localStorage.setItem(KEY, preference);
  } catch {
    // 存不了就只在这次打开时有效
  }
  const theme = preference === 'dark' || (preference === 'system' && system.matches) ? 'dark' : 'light';
  if (document.documentElement.dataset.theme === theme) return;
  document.documentElement.dataset.theme = theme;
  window.dispatchEvent(new CustomEvent('tetra:theme', { detail: theme }));
}

export const isDark = () => document.documentElement.dataset.theme === 'dark';

// 跟随系统时，电脑切换深浅色，界面马上跟着变
system.addEventListener('change', () => {
  if (preference === 'system') applyTheme('system');
});
