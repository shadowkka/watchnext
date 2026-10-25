import {escapeHTML} from './core.js';

export const themes = [
  ['graphite', 'Графит', 'Тёплый тёмный · лаванда'],
  ['midnight', 'Полночь', 'Глубокий синий · бирюза'],
  ['light', 'Светлая', 'Светлый фон · индиго'],
  ['glass-light', 'Liquid Glass · светлая', 'Прозрачные поверхности · голубой'],
  ['glass-dark', 'Liquid Glass · тёмная', 'Тёмное стекло · лаванда'],
  ['system', 'Как на устройстве', 'Светлая или графит'],
];
export const normalizeTheme = value => themes.some(([id]) => id === value) ? value : 'graphite';
export function applyTheme(value) {
  const theme = normalizeTheme(value);
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('wn-theme', theme); } catch {}
  updateThemeColor();
  return theme;
}
export function updateThemeColor() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
}
export function themeChoices(selected) {
  return `<div class="theme-choices" role="group" aria-label="Тема оформления">${themes.map(([id, label, description]) => `<label class="theme-choice"><input type="radio" name="uiTheme" value="${id}" ${id === selected ? 'checked' : ''}><span class="theme-preview theme-preview-${id}" aria-hidden="true"><i></i><i></i><i></i></span><span class="theme-name">${label}</span><span class="theme-description">${description}</span></label>`).join('')}</div>`;
}
export function skeletonCards(label = 'Загружаем фильмы…', count = 3) {
  return `<div class="loading-cards" role="status" aria-live="polite"><p class="loading-label">${escapeHTML(label)}</p><div class="cards" aria-hidden="true">${Array.from({length: Math.max(1, Math.min(6, count))}, () => '<div class="skeleton-card"><div class="skeleton skeleton-poster"></div><div class="skeleton-copy"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-short"></div></div></div>').join('')}</div></div>`;
}
