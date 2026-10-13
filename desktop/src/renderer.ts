type Data = Record<string, any>;
type Child = Node | string | number | null | undefined | false;
type Page = 'downloads' | 'accounts' | 'completed' | 'publish' | 'settings' | 'about';
type Pair = [string, string];
const state: Record<string, Data> = {};
const selected: Record<string, string> = { tasks: '', completed: '', publish: '' };
const selectedCache: Record<string, Data> = {};
const scrolls = new Map<string, number>();
let page: Page = 'downloads';
let loaded = false;
let backendDisconnected = false;
let settingsDraft: Data | null = null;
let optionsDraft: Data | null = null;
let settingsDirty = false;
let settingsTab = 'downloads';
let secretDraft = '';
let urlsDraft = '';
let modal: { kind: string; data: Data } | null = null;
let modalNeedsRender = false;
let returnFocus: HTMLElement | null = null;
let lastMessage = '';
let renderPending = false;
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const root = document.querySelector<HTMLDivElement>('#app')!;
const main = el('main', { class: 'main', id: 'main-content' });
const nav = el('nav', { class: 'navigation', 'aria-label': 'Navigation' });
const footer = el('footer', { class: 'statusbar', role: 'status' });
const modalRoot = el('div', { class: 'modal-root' });
const toastRoot = el('div', { class: 'toasts', 'aria-live': 'polite' });
root.append(el('div', { class: 'app-shell' }, nav, main), footer, modalRoot, toastRoot);

function section(name: string): Data { return state[name] || {}; }
function rows(name: string): Data[] { return section(name).rows || []; }
function english(): boolean {
  if (section('settings').locale) return !text(section('settings').locale).toLowerCase().startsWith('zh');
  const language = section('settings').values?.ui_language || 'auto';
  return language === 'en-US' || language === 'en' || language === 'auto' && !navigator.language.toLowerCase().startsWith('zh');
}
function t(zh: string, en: string): string { const translated = section('settings').translations?.[en]; return typeof translated === 'string' && translated ? translated : english() ? en : zh; }
function text(value: unknown): string { return value == null ? '' : String(value); }
function yes(value: unknown): boolean { return value === true || ['true', '1', 'yes', 'on'].includes(text(value).toLowerCase()); }
function el<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Data = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = text(value);
    else if (key === 'style') Object.assign(node.style, value);
    else if (['value', 'checked', 'disabled', 'readOnly', 'selected', 'open'].includes(key)) (node as any)[key] = value;
    else if (value !== false && value != null) node.setAttribute(key, text(value));
  }
  for (const child of children) if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(text(child)));
  return node;
}
const iconPaths: Record<string, string> = {
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4',
  accounts: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m14-14a4 4 0 0 1 0 8m6 6v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  library: 'M4 3v18M8 3v18m4-18v18M17 3l4 17',
  send: 'm22 2-7 20-4-9-9-4 20-7Zm0 0L11 13',
  settings: 'M4 7h16M4 17h16M8 4v6m8 4v6',
  info: 'M12 11v6m0-10h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  folder: 'M3 7h6l2 2h10v11H3V7Zm0 0V4h7l2 3',
  search: 'm21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  plus: 'M12 5v14M5 12h14',
  close: 'm6 6 12 12M6 18 18 6',
  play: 'm8 4 12 8-12 8V4Z',
  pause: 'M8 4v16M16 4v16',
  check: 'm5 12 4 4L19 6',
  refresh: 'M20 7v5h-5M4 17v-5h5m-4-3a8 8 0 0 1 13-4l2 2M4 17l2 2a8 8 0 0 0 13-4',
  copy: 'M9 9h12v12H9V9ZM5 15H3V3h12v2',
  image: 'M3 3h18v18H3V3Zm0 14 5-5 4 4 4-4 5 5M9 7h.01',
  log: 'M6 3h12v18H6V3Zm3 5h6m-6 4h6m-6 4h4',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  external: 'M14 3h7v7m0-7L10 14M10 3H3v18h18v-7',
  chevron: 'm9 5 7 7-7 7',
  video: 'M3 5h12v14H3V5Zm12 5 6-4v12l-6-4',
};
function icon(name: string): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', iconPaths[name] || iconPaths.info); svg.append(path);
  return svg;
}
function button(label: string, run: () => unknown, options: Data = {}): HTMLButtonElement {
  const { icon: name, tone = '', ...attrs } = options;
  return el('button', { type: 'button', class: `button ${tone}`, onclick: () => Promise.resolve(run()).catch(reportError), ...attrs }, name ? icon(name) : null, el('span', {}, label));
}
function fieldLabel(label: string, control: HTMLElement, hint?: string): HTMLLabelElement {
  return el('label', { class: 'field' }, el('span', { class: 'field-label' }, label), control, hint ? el('small', {}, hint) : null);
}
function input(id: string, value: unknown, change: (value: string) => unknown, attrs: Data = {}): HTMLInputElement {
  return el('input', { id, value: text(value), oninput: (event: Event) => change((event.target as HTMLInputElement).value), ...attrs });
}
function select(id: string, value: unknown, choices: [string, string][], change: (value: string) => unknown, attrs: Data = {}): HTMLSelectElement {
  return el('select', { id, value, onchange: (event: Event) => change((event.target as HTMLSelectElement).value), ...attrs }, ...choices.map(([key, label]) => el('option', { value: key, selected: key === text(value) }, label)));
}
function check(id: string, label: string, checked: unknown, change: (value: boolean) => unknown, disabled = false): HTMLLabelElement {
  return el('label', { class: 'check' }, el('input', { id, type: 'checkbox', checked: yes(checked), disabled, onchange: (event: Event) => change((event.target as HTMLInputElement).checked) }), el('span', {}, label));
}
function badge(label: string, tone = ''): HTMLElement { return el('span', { class: `badge ${tone}` }, label); }
function empty(title: string, detail: string, name = 'download'): HTMLElement {
  return el('div', { class: 'empty' }, icon(name), el('h3', {}, title), el('p', {}, detail));
}
function heading(title: string, subtitle: string, actions: Child[] = []): HTMLElement {
  return el('header', { class: 'page-header' }, el('div', {}, el('h1', {}, title), el('p', {}, subtitle)), el('div', { class: 'actions' }, ...actions));
}
function toast(message: string, danger = false): void {
  if (!message) return;
  const item: HTMLDivElement = el('div', { class: `toast ${danger ? 'danger' : ''}` }, el('span', {}, message), button(t('关闭', 'Dismiss'), () => item.remove(), { icon: 'close', tone: 'icon-only', 'aria-label': t('关闭提示', 'Dismiss message') }));
  toastRoot.append(item); setTimeout(() => item.remove(), danger ? 10000 : 5500);
}
function reportError(error: unknown): void { toast(error instanceof Error ? error.message : text(error), true); }
async function call(target: string, action: string, ...args: unknown[]): Promise<any> {
  try { return await window.huifa.call(target, action, ...args); }
  catch (error) { reportError(error); return undefined; }
}
function debounce(key: string, run: () => unknown, wait = 180): void {
  clearTimeout(timers.get(key)); timers.set(key, setTimeout(() => { timers.delete(key); void run(); }, wait));
}
function updateFilter(target: string, action: string, property: string, value: string): void {
  section(target)[property] = value;
  debounce(`${target}.${action}`, () => call(target, action, value));
}
function go(next: Page): void {
  page = next; render();
  if (next === 'completed') void call('app', 'refreshCompletedMedia');
  if (next === 'publish') void call('app', 'refreshPublishQueue');
  if (next === 'accounts') void call('accounts', 'refresh');
}
function scheduleRender(): void {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; render(); });
}
function preserve(container: HTMLElement, build: () => void): void {
  const active = document.activeElement as HTMLInputElement | HTMLTextAreaElement | null;
  const id = active && container.contains(active) ? active.id : '';
  const activeValue = id && active && ['INPUT', 'TEXTAREA'].includes(active.tagName) && !['checkbox', 'radio', 'range'].includes(active.type) ? active.value : null;
  const caret = active && ['INPUT', 'TEXTAREA'].includes(active.tagName) ? [active.selectionStart, active.selectionEnd] : null;
  container.querySelectorAll<HTMLElement>('[data-scroll]').forEach(node => scrolls.set(node.dataset.scroll!, node.scrollTop));
  const detailStates = new Map(Array.from(container.querySelectorAll<HTMLDetailsElement>('details[id]')).map(node => [node.id, node.open]));
  build();
  container.querySelectorAll<HTMLElement>('[data-scroll]').forEach(node => { node.scrollTop = scrolls.get(node.dataset.scroll!) || 0; });
  detailStates.forEach((open, key) => { const node = document.getElementById(key); if (node instanceof HTMLDetailsElement) node.open = open; });
  if (id) {
    const replacement = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null;
    if (replacement && activeValue !== null) replacement.value = activeValue;
    replacement?.focus({ preventScroll: true });
    if (caret && replacement && caret[0] !== null && caret[1] !== null) try { replacement.setSelectionRange(caret[0], caret[1]); } catch { /* Number/select controls have no text selection. */ }
  }
}
function virtualRange(count: number, top: number, height: number, rowHeight: number): [number, number] {
  const start = Math.max(0, Math.min(count, Math.floor(Math.max(0, top) / rowHeight) - 4));
  return [start, Math.min(count, Math.max(start, Math.ceil((Math.max(0, top) + Math.max(height, rowHeight)) / rowHeight) + 4))];
}
function retainSelection(current: string, list: Data[], key: string): string {
  return current || text(list[0]?.[key]);
}
function taskProgress(task: Data): { value: number; indeterminate: boolean; label: string } {
  const processing = ['processing', 'waiting_processing', 'processing_failed'].includes(task.status) || task.processingPaused;
  const frozen = ['paused', 'completed', 'deleted', 'failed', 'partial_failed', 'canceled', 'processing_failed'].includes(task.status);
  const stage = task.stageProgress;
  const raw = task.status === 'completed' ? 100 : frozen && !processing ? task.progress : processing ? stage : stage == null ? task.progress : stage;
  const value = Math.max(0, Math.min(100, Number(raw) || 0));
  const indeterminate = !frozen && (yes(task.stageIndeterminate) || processing && stage == null);
  return { value, indeterminate, label: indeterminate ? t('等待阶段进度', 'Waiting for stage progress') : `${value.toFixed(1)}%` };
}
function verifyListInvariants(): void {
  const assert = (condition: boolean) => { if (!condition) throw new Error('Renderer list invariant failed'); };
  assert(virtualRange(0, 0, 400, 72).join() === '0,0');
  const range = virtualRange(100000, 72000, 432, 72);
  assert(range[0] === 996 && range[1] === 1010);
  assert(retainSelection('same-id', [{ taskId: 'other-id' }], 'taskId') === 'same-id');
  assert(taskProgress({ status: 'processing', progress: 100, stageProgress: 12 }).value === 12);
  assert(taskProgress({ status: 'processing', progress: 100, stageProgress: null }).indeterminate);
}
function virtualList(name: string, list: Data[], key: string, rowHeight: number, draw: (row: Data) => HTMLElement, onSelect?: (row: Data) => void): HTMLElement {
  const viewport = el('div', { class: 'virtual-list', id: `list-${name}`, 'data-scroll': name, tabindex: 0, role: onSelect ? 'listbox' : 'list', 'aria-label': t('连续列表', 'Continuous list') });
  const canvas = el('div', { class: 'virtual-canvas', style: { height: `${list.length * rowHeight}px` } });
  viewport.append(canvas);
  let previousRange = '';
  const paint = () => {
    if (!viewport.isConnected) return;
    const [start, end] = virtualRange(list.length, viewport.scrollTop, viewport.clientHeight, rowHeight);
    const signature = `${start}:${end}`;
    if (signature === previousRange) return;
    previousRange = signature; canvas.replaceChildren();
    for (let index = start; index < end; index++) {
      const row = list[index]; const id = text(row[key]);
      const node = draw(row); node.classList.add('list-row'); node.style.top = `${index * rowHeight}px`; node.style.height = `${rowHeight}px`;
      node.dataset.key = id;
      if (onSelect) {
        node.setAttribute('role', 'option'); node.setAttribute('aria-selected', text(selected[name] === id));
        node.classList.toggle('selected', selected[name] === id);
        node.addEventListener('click', (event) => { if ((event.target as HTMLElement).closest('button,input,select,a')) return; onSelect(row); });
      } else node.setAttribute('role', 'listitem');
      canvas.append(node);
    }
  };
  viewport.addEventListener('scroll', () => { scrolls.set(name, viewport.scrollTop); paint(); }, { passive: true });
  viewport.addEventListener('keydown', event => {
    if (!onSelect || event.target !== viewport || !['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter'].includes(event.key)) return;
    event.preventDefault();
    const current = list.findIndex(row => text(row[key]) === selected[name]);
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? list.length - 1 : event.key === 'ArrowUp' ? Math.max(0, current - 1) : event.key === 'ArrowDown' ? Math.min(list.length - 1, current + 1) : Math.max(0, current);
    if (list[index]) { viewport.scrollTop = Math.max(0, index * rowHeight - viewport.clientHeight / 2); scrolls.set(name, viewport.scrollTop); onSelect(list[index]); requestAnimationFrame(() => document.getElementById(`list-${name}`)?.focus()); }
  });
  const observer = new ResizeObserver(() => { previousRange = ''; paint(); }); observer.observe(viewport);
  requestAnimationFrame(() => { viewport.scrollTop = scrolls.get(name) || 0; paint(); });
  // shortcut: each render creates one viewport observer, disconnect when its viewport leaves the DOM.
  const removal = new MutationObserver(() => { if (!viewport.isConnected) { observer.disconnect(); removal.disconnect(); } });
  removal.observe(main, { childList: true, subtree: true }); removal.observe(modalRoot, { childList: true, subtree: true });
  return viewport;
}
function chooseRow(name: string, row: Data, key: string): void { selected[name] = text(row[key]); selectedCache[name] = row; render(); }
function currentRow(name: string, key: string): Data | null {
  const list = rows(name); selected[name] = retainSelection(selected[name], list, key);
  const row = list.find(item => text(item[key]) === selected[name]);
  if (row) selectedCache[name] = row;
  return row || (text(selectedCache[name]?.[key]) === selected[name] ? selectedCache[name] : null);
}
function thumbnail(url: unknown, name = 'video', large = false): HTMLElement {
  const valid = /^(https?:|file:|huifa-media:|data:image\/)/i.test(text(url));
  const fallback = el('div', { class: `thumbnail ${large ? 'large' : ''}` }, icon(name));
  if (valid) { const img = el('img', { src: text(url), alt: '', loading: 'lazy', onerror: () => img.remove() }); fallback.append(img); }
  return fallback;
}
function statusLabel(status: string): string {
  const labels: Record<string, Pair> = { queued: ['下载排队', 'Download queued'], downloading: ['下载中', 'Downloading'], paused: ['已暂停', 'Paused'], '暂停中': ['暂停中', 'Pausing'], waiting_processing: ['等待处理', 'Processing queued'], processing: ['处理中', 'Processing'], processing_failed: ['处理失败', 'Processing failed'], completed: ['已完成', 'Completed'], deleted: ['文件缺失', 'File missing'], failed: ['下载失败', 'Download failed'], partial_failed: ['部分失败', 'Partly failed'], canceled: ['已取消', 'Canceled'], canceling: ['取消中', 'Canceling'], waiting_selection: ['等待选择', 'Awaiting selection'], parsing_collection: ['解析合集', 'Reading collection'], pending: ['等待发布', 'Pending'], uploading: ['发布中', 'Publishing'], success: ['发布成功', 'Published'] };
  const pair = labels[status]; return pair ? t(...pair) : status;
}
function tone(status: string): string { return status.includes('failed') ? 'danger' : ['completed', 'success'].includes(status) ? 'success' : ['downloading', 'processing', 'uploading'].includes(status) ? 'accent' : ''; }
function progressBar(task: Data): HTMLElement {
  const progress = taskProgress(task);
  return el('div', { class: `progress ${progress.indeterminate ? 'indeterminate' : ''}`, role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': progress.indeterminate ? null : progress.value, 'aria-label': statusLabel(task.status || '') }, el('span', { style: { width: `${progress.value}%` } }));
}
function processingPlan(task?: Data): string {
  const values = section('settings').values || {};
  const plan = task?.processingPlan || { encoder: values.transcode_encoder, subtitles: values.subtitle_language, jpeg: yes(values.download_cover_convert_jpeg), jpegQuality: values.cover_jpeg_quality, prependCover: yes(values.prepend_cover_enabled), prependFrames: values.prepend_cover_frames };
  return [!plan.encoder || plan.encoder === 'original' ? t('保持原编码', 'Keep source codec') : plan.encoder, !plan.subtitles || plan.subtitles === 'none' ? t('无字幕', 'No subtitles') : t(`字幕 ${plan.subtitles}`, `Subtitles ${plan.subtitles}`), plan.jpeg ? `JPEG ${plan.jpegQuality || 90}%` : t('原始封面', 'Original cover'), plan.prependCover ? t(`封面片头 ${plan.prependFrames || 3} 帧`, `Cover intro ${plan.prependFrames || 3} frames`) : ''].filter(Boolean).join(' · ');
}
function renderNav(): void {
  const pages: [Page, Pair, string][] = [['downloads', ['下载任务', 'Downloads'], 'download'], ['accounts', ['账号', 'Accounts'], 'accounts'], ['completed', ['已完成', 'Completed'], 'library'], ['publish', ['发布队列', 'Publish queue'], 'send'], ['settings', ['设置', 'Settings'], 'settings'], ['about', ['关于', 'About'], 'info']];
  nav.replaceChildren(el('div', { class: 'brand' }, el('div', { class: 'brand-mark' }, icon('download')), el('div', {}, el('strong', {}, 'Huifa'), el('small', {}, 'MEDIA WORKSPACE'))), el('div', { class: 'nav-links' }, ...pages.map(([id, pair, name]) => button(t(...pair), () => go(id), { icon: name, class: `nav-link ${id === page ? 'active' : ''}`, 'aria-current': id === page ? 'page' : null, title: t(...pair) }))), el('div', { class: 'nav-bottom' }, el('span', { class: 'connection-dot', style: backendDisconnected ? { background: 'var(--danger)' } : {} }), el('span', {}, backendDisconnected ? t('服务已断开', 'Service disconnected') : loaded ? t('服务已连接', 'Service connected') : t('连接服务中', 'Connecting')), el('small', {}, section('app').appVersion || '')));
  if (page === 'downloads' && loaded && !backendDisconnected) {
    const categories: [string, Pair, string][] = [['all', ['全部任务', 'All tasks'], 'download'], ['active', ['进行中', 'Active'], 'play'], ['queued', ['下载排队', 'Download queue'], 'download'], ['waiting_processing', ['处理排队', 'Processing queue'], 'refresh'], ['processing', ['处理中', 'Processing'], 'settings'], ['paused', ['已暂停', 'Paused'], 'pause'], ['completed', ['已完成', 'Completed'], 'check'], ['processing_failed', ['处理失败', 'Processing failed'], 'info'], ['failed', ['下载失败', 'Download failed'], 'close']];
    const group = el('div', { class: 'nav-categories', 'aria-label': t('任务分类', 'Task categories') }, el('small', { class: 'category-label' }, t('任务分类', 'Task categories')), ...categories.map(([value, pair, name]) => button(t(...pair), () => call('tasks', 'setStatusFilter', value), { icon: name, class: `nav-link category-link ${section('tasks').statusFilter === value ? 'active' : ''}`, title: t(...pair), 'aria-pressed': section('tasks').statusFilter === value })));
    nav.insertBefore(group, nav.querySelector('.nav-bottom'));
  }
}
function render(): void {
  const values = section('settings').values || {};
  const preference = values.appearance_theme || 'system';
  document.documentElement.dataset.theme = preference === 'system' ? matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light' : preference;
  document.documentElement.lang = section('settings').locale || (english() ? 'en-US' : 'zh-CN');
  renderNav();
  preserve(main, () => {
    if (backendDisconnected) main.replaceChildren(empty(t('后台服务已断开', 'Backend disconnected'), t('请重新启动应用。已经保存的任务将在启动后恢复。', 'Restart the app to restore your saved tasks.'), 'info'));
    else if (!loaded) main.replaceChildren(empty(t('连接下载服务', 'Connecting to download service'), t('正在载入任务、媒体和设置…', 'Loading tasks, media, and settings…')));
    else main.replaceChildren(({ downloads: downloadPage, accounts: accountsPage, completed: completedPage, publish: publishPage, settings: settingsPage, about: aboutPage }[page])());
  });
  footer.replaceChildren(el('span', { class: 'status-summary' }, section('app').statusSummary || t('就绪', 'Ready')), el('span', { class: 'status-message', title: section('app').message || '' }, section('app').shutdownStarted ? t('正在保存并安全退出…', 'Saving and shutting down…') : section('app').message || ''), el('span', { class: 'status-shortcut' }, t('Ctrl+1–6 切换页面', 'Ctrl+1–6 switch pages')));
  renderModal();
}
function downloadPage(): HTMLElement {
  const tasks = section('tasks'); const list = rows('tasks'); const chosen = currentRow('tasks', 'taskId');
  const values = section('settings').values || {}; const options = section('settings').downloadOptions || {};
  const composer: HTMLFormElement = el('form', { class: 'composer panel', onsubmit: async (event: Event) => { event.preventDefault(); if (!urlsDraft.trim()) return; if (await call('app', 'submitUrls', urlsDraft)) { urlsDraft = ''; render(); } } },
    el('div', { class: 'composer-title' }, el('strong', {}, t('新建下载', 'New download')), el('span', { class: 'muted' }, t('支持单条、合集和批量链接', 'Single links, collections, and batches')), button(t('快速选项', 'Quick options'), () => openModal('quick'), { icon: 'settings', tone: 'subtle' })),
    el('div', { class: 'composer-input' }, el('textarea', { id: 'url-input', rows: 2, value: urlsDraft, placeholder: t('粘贴视频或合集链接，每行一个…', 'Paste video or collection links, one per line…'), 'aria-label': t('下载链接', 'Download links'), oninput: (event: Event) => { urlsDraft = (event.target as HTMLTextAreaElement).value; }, onkeydown: (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') (event.currentTarget as HTMLElement).closest('form')?.requestSubmit(); } }), button(t('添加下载', 'Add downloads'), () => composer.requestSubmit(), { icon: 'plus', tone: 'primary' })),
    el('div', { class: 'batch-plan' }, badge(t('此批次计划', 'Batch plan')), el('span', {}, `${options.content_mode === 'audio' ? t('音频', 'Audio') : options.content_mode === 'manual' ? t('手动选格式', 'Choose format') : t('视频', 'Video')} · ${values.quality === 'best' ? t('最佳画质', 'Best quality') : values.quality || 'best'} · ${processingPlan()}`), el('small', {}, t('加入后固定；设置更改仅影响新任务', 'Frozen on add; setting changes affect new tasks'))));
  const toolbar = el('div', { class: 'toolbar' }, input('task-search', tasks.filterText, value => updateFilter('tasks', 'setFilterText', 'filterText', value), { type: 'search', placeholder: t('搜索所有任务：标题、链接、编号', 'Search all tasks: title, URL, ID'), 'aria-label': t('搜索任务', 'Search tasks') }),
    select('task-status', tasks.statusFilter || 'all', [['all', t('全部状态', 'All statuses')], ['active', t('进行中', 'Active')], ['queued', t('下载排队', 'Download queue')], ['waiting_processing', t('处理排队', 'Processing queue')], ['processing', t('处理中', 'Processing')], ['paused', t('已暂停', 'Paused')], ['completed', t('已完成', 'Completed')], ['processing_failed', t('处理失败', 'Processing failed')], ['failed', t('下载失败', 'Download failed')], ['deleted', t('文件缺失', 'Missing files')], ['canceled', t('已取消', 'Canceled')]], value => call('tasks', 'setStatusFilter', value), { 'aria-label': t('状态筛选', 'Status filter') }),
    select('task-sort', tasks.sortMode || 'newest', [['newest', t('最新优先', 'Newest first')], ['oldest', t('最早优先', 'Oldest first')], ['title', t('标题排序', 'Title')]], value => call('tasks', 'setSortMode', value), { 'aria-label': t('排序', 'Sort') }),
    button(t('暂停全部', 'Pause all'), () => call('app', 'pauseAllTasks'), { icon: 'pause', tone: 'icon-only', title: t('暂停全部', 'Pause all'), 'aria-label': t('暂停全部', 'Pause all') }), button(t('继续全部', 'Resume all'), () => call('app', 'resumeAllTasks'), { icon: 'play', tone: 'icon-only', title: t('继续全部', 'Resume all'), 'aria-label': t('继续全部', 'Resume all') }), button(t('清理', 'Clean up'), () => openModal('cleanup'), { icon: 'trash', tone: 'icon-only', title: t('清理', 'Clean up'), 'aria-label': t('清理', 'Clean up') }));
  const tableHeading = el('div', { class: 'task-table-heading task-columns' }, el('span', {}, t('名称', 'Name')), el('span', {}, t('阶段 / 进度', 'Stage / progress')), el('span', { class: 'task-speed' }, t('速度', 'Speed')), el('span', { class: 'task-eta' }, t('剩余', 'ETA')), el('span', { class: 'task-size' }, t('大小', 'Size')));
  const body = el('div', { class: 'workspace', id: 'task-workspace' }, el('section', { class: 'list-panel panel' }, el('div', { class: 'list-caption' }, el('strong', {}, t('任务', 'Tasks')), el('span', {}, t(`${tasks.count || 0} 项 · 活动任务置顶`, `${tasks.count || 0} tasks · Active first`))), tableHeading, list.length ? virtualList('tasks', list, 'taskId', 56, taskRow, row => chooseRow('tasks', row, 'taskId')) : empty(t('没有匹配的任务', 'No matching tasks'), tasks.filterText || tasks.statusFilter !== 'all' ? t('调整搜索或状态筛选。', 'Adjust search or status filters.') : t('在上方粘贴链接开始下载。', 'Paste a link above to start downloading.'))), taskDetail(chosen));
  return el('div', { class: 'page downloads-page' }, heading(t('下载任务', 'Downloads'), t('下载与处理独立排队，原始媒体先保存到本地。', 'Downloads and processing have separate queues. Source media is saved first.'), [button(t('下载目录', 'Download folder'), () => call('app', 'openDownloadDirectory'), { icon: 'folder' })]), composer, toolbar, body);
}
function taskRow(task: Data): HTMLElement {
  const progress = taskProgress(task);
  const stage = task.status === 'waiting_processing' ? task.waitingReason || t('等待处理槽位', 'Waiting for a processing slot') : task.status === 'processing_failed' ? task.failedStep || task.error : task.stageText || statusLabel(task.status);
  return el('div', { class: 'task-row-content task-columns', title: task.title || task.url }, el('div', { class: 'task-name' }, icon(task.taskKind === 'collection' ? 'library' : 'video'), el('div', { class: 'row-main' }, el('strong', { class: 'truncate' }, task.title || t('等待获取媒体信息', 'Waiting for media information')), el('small', { class: `task-status ${tone(task.status)}` }, statusLabel(task.status)))), el('div', { class: 'task-stage' }, el('div', { class: 'task-stage-label' }, el('span', { class: 'truncate', title: stage }, stage), el('small', {}, progress.label)), progressBar(task)), el('span', { class: 'task-speed truncate' }, task.speed || '—'), el('span', { class: 'task-eta truncate' }, task.eta || '—'), el('span', { class: 'task-size truncate' }, task.size || '—'));
}
function taskDetail(task: Data | null): HTMLElement {
  const detail = el('aside', { class: 'detail panel', id: 'task-detail-panel', 'aria-label': t('任务详情', 'Task details') });
  if (!task) return el('aside', { class: 'detail panel', id: 'task-detail-panel' }, empty(t('任务详情', 'Task details'), t('选中任务以查看计划、进度和操作。', 'Select a task to see its plan, progress, and actions.'), 'info'), recentCompleted());
  const id = text(task.taskId); const processing = ['processing', 'waiting_processing', 'processing_failed'].includes(task.status);
  const primary: Child[] = [];
  if (task.status === 'waiting_selection') primary.push(button(task.taskKind === 'collection' ? t('选择项目', 'Choose items') : t('选择格式', 'Choose format'), () => call('app', task.taskKind === 'collection' ? 'openCollectionSelection' : 'openFormatSelection', id), { icon: 'check', tone: 'primary' }));
  if (task.status === 'paused') primary.push(button(task.processingPaused ? t('继续处理', 'Resume processing') : t('继续下载', 'Resume download'), () => call('app', 'startTask', id), { icon: 'play', tone: 'primary' }));
  if (['failed', 'partial_failed', 'canceled', 'processing_failed'].includes(task.status)) primary.push(button(task.status === 'processing_failed' ? t('重试处理', 'Retry processing') : t('重试下载', 'Retry download'), () => call('app', 'retryTask', id), { icon: 'refresh', tone: 'primary' }));
  if (['downloading', 'queued', 'processing', 'waiting_processing'].includes(task.status)) primary.push(button(processing ? t('暂停处理', 'Pause processing') : t('暂停下载', 'Pause download'), () => call('app', 'pauseTask', id), { icon: 'pause' }));
  if (['queued', 'downloading', 'processing', 'waiting_processing', 'waiting_selection', 'parsing_collection', 'paused'].includes(task.status)) primary.push(button(t('取消', 'Cancel'), () => call('app', 'cancelTask', id), { icon: 'close' }));
  const more: Child[] = [button(t('复制来源', 'Copy source'), () => call('app', 'copyTaskSource', id), { icon: 'copy' }), button(t('复制目录', 'Copy folder'), () => call('app', 'copyTaskFolder', id), { icon: 'folder' }), button(t('任务日志', 'Task log'), () => openLog(id), { icon: 'log' }), button(t('删除任务', 'Delete task'), () => openModal('delete', { taskId: id, title: task.title }), { icon: 'trash', tone: 'danger' })];
  if (!task.localHistory && ['completed', 'deleted'].includes(task.status)) more.unshift(button(t('重新下载', 'Download again'), () => openModal('redownload', { taskId: id }), { icon: 'download' }));
  if (!task.localHistory && task.status === 'completed' && task.taskKind === 'video') more.unshift(button(t('转换格式', 'Convert format'), () => openModal('convert', { taskId: id, encoder: section('settings').values?.transcode_encoder || 'libx264' }), { icon: 'refresh' }));
  detail.append(el('div', { class: 'detail-scroll', 'data-scroll': 'task-detail' }, el('div', { class: 'detail-top' }, el('span', { class: 'eyebrow' }, t('已选任务', 'Selected task')), badge(statusLabel(task.status), tone(task.status))), thumbnail(task.thumbnailUrl, task.taskKind === 'collection' ? 'library' : 'video', true), el('h2', {}, task.title || t('等待媒体信息', 'Waiting for media information')), el('code', { class: 'task-id' }, id), !rows('tasks').some(row => row.taskId === id) ? el('p', { class: 'muted' }, t('所选任务被当前筛选隐藏。', 'Selected task is hidden by the current filter.')) : null,
    el('div', { class: 'detail-stage' }, el('strong', {}, task.stageText || statusLabel(task.status)), el('span', {}, taskProgress(task).label)), progressBar(task), task.waitingReason ? el('p', { class: 'notice' }, task.waitingReason) : null, task.failedStep ? el('p', { class: 'notice danger' }, t(`失败步骤：${task.failedStep}`, `Failed step: ${task.failedStep}`)) : null, task.error ? el('p', { class: 'error-text' }, task.error) : null, task.reconnectMessage ? el('p', { class: 'notice' }, task.reconnectMessage) : null,
    el('div', { class: 'actions detail-actions' }, ...primary), el('dl', { class: 'facts' }, el('dt', {}, t('创建时间', 'Created')), el('dd', {}, task.createdAt || '—'), el('dt', {}, t('清晰度 / 大小', 'Quality / size')), el('dd', {}, [task.selectedQuality, task.size].filter(Boolean).join(' · ') || '—'), el('dt', {}, t('处理计划', 'Processing plan')), el('dd', {}, processingPlan(task)), el('dt', {}, t('来源', 'Source')), el('dd', { class: 'break-text' }, task.url || '—'), el('dt', {}, t('媒体路径', 'Media path')), el('dd', { class: 'break-text' }, task.mediaPath || t('尚未保存', 'Not saved yet'))), el('details', { id: 'task-more' }, el('summary', {}, t('更多操作', 'More actions')), el('div', { class: 'actions wrap' }, ...more)), recentCompleted()));
  detail.append(el('div', { class: 'detail-footer' }, button(task.taskKind === 'collection' ? t('查看合集项目', 'View collection items') : t('定位结果文件', 'Reveal result file'), () => call('app', task.taskKind === 'collection' ? 'openCollectionDetails' : 'openTaskLocation', id), { icon: task.taskKind === 'collection' ? 'library' : 'folder', tone: 'primary', disabled: task.taskKind !== 'collection' && !task.mediaPath && task.status !== 'completed' && task.status !== 'deleted' })));
  return detail;
}
function recentCompleted(): HTMLElement {
  const recent = section('tasks').recentCompleted || rows('tasks').filter(row => row.status === 'completed').slice(0, 4);
  return el('section', { class: 'recent', id: 'recent-completed' }, el('div', { class: 'section-heading' }, el('h3', {}, t('最近完成', 'Recently completed')), button(t('媒体库', 'Library'), () => go('completed'), { tone: 'text' })), recent.length ? el('div', {}, ...recent.slice(0, 4).map((task: Data) => el('div', { class: 'recent-row' }, button(task.title || task.taskId, () => chooseRow('tasks', task, 'taskId'), { tone: 'text truncate' }), button(t('定位', 'Reveal'), () => call('app', 'openTaskLocation', task.taskId), { icon: 'folder', tone: 'icon-only', title: t('定位结果文件', 'Reveal result file'), 'aria-label': t('定位结果文件', 'Reveal result file') })))) : el('p', { class: 'muted' }, t('完成的媒体会显示在这里。', 'Completed media appears here.')));
}
function accountsPage(): HTMLElement {
  return el('div', { class: 'page' }, heading(t('账号', 'Accounts'), t('选择默认发布平台，管理账号登录与 Cookie 状态。', 'Choose default publishing platforms and manage account sign-in.'), [button(t('刷新', 'Refresh'), () => call('accounts', 'refresh'), { icon: 'refresh' }), button(t('保存账号', 'Save accounts'), () => call('accounts', 'save'), { icon: 'check', tone: 'primary' })]),
    el('div', { class: 'panel notice' }, t(`已选 ${section('accounts').targetCount || 0} 个默认发布平台。登录、检查和发布使用应用管理的 Chromium。`, `${section('accounts').targetCount || 0} default publishing platforms selected. Sign-in, validation, and publishing use the app-managed Chromium.`)),
    el('div', { class: 'page-scroll', 'data-scroll': 'accounts' }, el('div', { class: 'panel accounts-table' }, el('div', { class: 'account-row table-heading' }, el('span', {}, t('默认发布平台', 'Default platform')), el('span', {}, t('账号名称', 'Account name')), el('span', {}, t('登录状态', 'Sign-in status')), el('span', {}, t('操作', 'Actions'))), ...rows('accounts').map(account => {
      const accountInput = input(`account-${account.platform}`, account.account, value => { account.account = value; }, { placeholder: 'default', disabled: account.busy, onblur: () => call('accounts', 'setAccount', account.platform, accountInput.value), 'aria-label': t(`${account.label} 账号`, `${account.label} account`) });
      const action = async (name: string) => { await call('accounts', 'setAccount', account.platform, accountInput.value); await call('accounts', 'runAction', account.platform, name); };
      return el('div', { class: 'account-row' }, check(`target-${account.platform}`, account.label, account.target, value => call('accounts', 'setTarget', account.platform, value)), accountInput, badge(account.busy ? t('处理中', 'Working') : account.stateText, account.stateTone), el('div', { class: 'actions' }, button(t('登录', 'Sign in'), () => action('login'), { disabled: account.busy, title: account.loginHint }), button(t('检查', 'Check'), () => action('check'), { icon: 'check', disabled: account.busy })));
    }))));
}
function completedPage(): HTMLElement {
  const media = section('completed'); const chosen = currentRow('completed', 'mediaId');
  const filters: [string, Pair, string][] = [['all', ['全部', 'All'], 'totalCount'], ['needs_distribution', ['待发布', 'Needs publishing'], 'needsDistributionCount'], ['published', ['已发布', 'Published'], 'publishedCount'], ['queued', ['队列中', 'Queued'], 'queuedCount'], ['retry_needed', ['需重试', 'Retry needed'], 'retryNeededCount'], ['complete', ['全部完成', 'All targets complete'], 'completeCount']];
  return el('div', { class: 'page' }, heading(t('已完成', 'Completed media'), t(`完整媒体库 · ${media.totalCount || 0} 个媒体`, `Full media library · ${media.totalCount || 0} media`), [button(t('刷新', 'Refresh'), () => call('app', 'refreshCompletedMedia'), { icon: 'refresh' })]),
    el('div', { class: 'toolbar' }, input('media-search', media.searchText, value => updateFilter('completed', 'setSearchText', 'searchText', value), { type: 'search', placeholder: t('搜索标题、作者、路径、平台', 'Search title, author, path, platform'), 'aria-label': t('搜索媒体库', 'Search media library') })),
    el('div', { class: 'filter-tabs' }, ...filters.map(([key, pair, count]) => button(`${t(...pair)} ${media[count] || 0}`, () => call('completed', 'setFilter', key), { class: `filter-tab ${media.filter === key ? 'active' : ''}`, 'aria-pressed': media.filter === key }))),
    el('div', { class: 'workspace' }, el('section', { class: 'panel list-panel' }, el('div', { class: 'list-caption' }, el('strong', {}, t('媒体库', 'Media library')), el('span', {}, `${media.count || 0} / ${media.totalCount || 0}`)), rows('completed').length ? virtualList('completed', rows('completed'), 'mediaId', 84, row => el('div', { class: 'media-row-content' }, thumbnail(row.thumbnailUrl), el('div', { class: 'row-main' }, el('div', { class: 'row-heading' }, el('strong', { class: 'truncate' }, row.title || t('未命名媒体', 'Untitled media')), badge(row.complete ? t('发布完成', 'All targets complete') : row.retryNeeded ? t('需重试', 'Retry needed') : row.queued ? t('发布队列中', 'Queued') : row.published ? t('部分已发布', 'Partly published') : t('待发布', 'Needs publishing'), row.retryNeeded ? 'danger' : row.complete ? 'success' : '')), el('div', { class: 'row-meta' }, el('span', { class: 'truncate' }, [row.uploader, row.sourcePlatform, row.downloadedAt].filter(Boolean).join(' · '))), el('div', { class: 'row-meta truncate' }, row.distributionText || t('尚未创建发布任务', 'No publishing tasks yet')))), row => chooseRow('completed', row, 'mediaId')) : empty(t('没有匹配的媒体', 'No matching media'), t('下载完成的媒体可在这里生成封面并发布。', 'Finished downloads can be prepared and published here.'), 'library')), mediaDetail(chosen)));
}
function mediaDetail(media: Data | null): HTMLElement {
  if (!media) return el('aside', { class: 'detail panel' }, empty(t('媒体详情', 'Media details'), t('选择媒体查看发布状态和文件操作。', 'Select media to see publishing status and file actions.'), 'library'));
  const id = Number(media.mediaId);
  return el('aside', { class: 'detail panel' }, el('div', { class: 'detail-scroll', 'data-scroll': 'media-detail' }, el('span', { class: 'eyebrow' }, t('已选媒体', 'Selected media')), thumbnail(media.thumbnailUrl, 'image', true), el('h2', {}, media.title), el('p', { class: 'muted' }, [media.uploader, media.downloadedAt].filter(Boolean).join(' · ')), el('p', { class: 'notice' }, media.distributionText || t('等待发布', 'Ready to publish')), el('dl', { class: 'facts' }, el('dt', {}, t('来源平台', 'Source platform')), el('dd', {}, media.sourcePlatform || '—'), el('dt', {}, t('结果文件', 'Result file')), el('dd', { class: 'break-text' }, media.videoPath || '—')), el('div', { class: 'actions wrap' }, button(t('封面工作室', 'Cover studio'), () => call('app', 'openCoverStudio', id), { icon: 'image' }), button(t('发布队列', 'Publishing tasks'), () => call('app', 'showPublishQueueForMedia', id), { icon: 'send' }), button(t('创建发布任务', 'Create publishing task'), () => call('app', 'openPublishEditor', id), { icon: 'plus', tone: 'primary' }))), el('div', { class: 'detail-footer' }, button(t('定位结果文件', 'Reveal result file'), () => call('app', 'openMediaLocation', id), { icon: 'folder', tone: 'primary' })));
}
function publishPage(): HTMLElement {
  const queue = section('publish'); const chosen = currentRow('publish', 'taskId');
  return el('div', { class: 'page' }, heading(t('发布队列', 'Publish queue'), queue.mediaId ? t(`当前媒体 #${queue.mediaId} 的发布任务`, `Publishing tasks for media #${queue.mediaId}`) : t('查看所有平台的发布进度和结果。', 'Track publishing progress and results across platforms.'), [queue.mediaId ? button(t('显示全部', 'Show all media'), () => call('publish', 'setMediaId', 0), { icon: 'library' }) : null, button(t('刷新', 'Refresh'), () => call('app', 'refreshPublishQueue'), { icon: 'refresh' })]),
    el('div', { class: 'toolbar' }, input('publish-search', queue.searchText, value => updateFilter('publish', 'setSearchText', 'searchText', value), { type: 'search', placeholder: t('搜索标题、平台、账号、状态、结果', 'Search title, platform, account, status, result'), 'aria-label': t('搜索发布队列', 'Search publishing queue') }), select('publish-status', queue.statusFilter || 'all', [['all', t('全部状态', 'All statuses')], ['pending', t('等待', 'Pending')], ['uploading', t('发布中', 'Publishing')], ['success', t('成功', 'Success')], ['failed', t('失败', 'Failed')]], value => call('publish', 'setStatusFilter', value), { 'aria-label': t('发布状态', 'Publishing status') })),
    el('div', { class: 'workspace' }, el('section', { class: 'list-panel panel' }, el('div', { class: 'list-caption' }, el('strong', {}, t('发布任务', 'Publishing tasks')), el('span', {}, t(`${queue.count || 0} 项`, `${queue.count || 0} tasks`))), rows('publish').length ? virtualList('publish', rows('publish'), 'taskId', 78, row => el('div', { class: 'publish-row-content' }, el('span', { class: 'platform-mark' }, text(row.platform).slice(0, 2).toUpperCase()), el('div', { class: 'row-main' }, el('div', { class: 'row-heading' }, el('strong', { class: 'truncate' }, row.title), badge(statusLabel(row.status), tone(row.status))), el('div', { class: 'row-meta' }, `${row.platform} · ${row.account || 'default'} · ${row.createdAt || ''}`), el('div', { class: 'row-meta truncate' }, row.result || t('尚未执行', 'Not started')))), row => chooseRow('publish', row, 'taskId')) : empty(t('没有匹配的发布任务', 'No matching publishing tasks'), t('在“已完成”页面选择媒体创建发布任务。', 'Create publishing tasks from Completed media.'), 'send')), publishDetail(chosen)));
}
function publishDetail(task: Data | null): HTMLElement {
  if (!task) return el('aside', { class: 'detail panel' }, empty(t('发布详情', 'Publishing details'), t('选择任务查看结果和重试操作。', 'Select a task to see its result and retry actions.'), 'send'));
  const url = text(task.result).match(/https?:\/\/[^\s<>"']+/)?.[0];
  return el('aside', { class: 'detail panel' }, el('div', { class: 'detail-scroll', 'data-scroll': 'publish-detail' }, el('div', { class: 'detail-top' }, el('span', { class: 'eyebrow' }, `#${task.taskId}`), badge(statusLabel(task.status), tone(task.status))), el('h2', {}, task.title), el('dl', { class: 'facts' }, el('dt', {}, t('平台', 'Platform')), el('dd', {}, task.platform), el('dt', {}, t('账号', 'Account')), el('dd', {}, task.account || 'default'), el('dt', {}, t('创建时间', 'Created')), el('dd', {}, task.createdAt)), el('h3', {}, t('发布结果', 'Publishing result')), el('p', { class: `result-text ${task.status === 'failed' ? 'error-text' : ''}` }, task.result || t('尚未执行', 'Not started')), el('div', { class: 'actions wrap' }, url ? button(t('打开发布结果', 'Open published result'), () => window.huifa.openExternal(url), { icon: 'external' }) : null, task.result ? button(t('复制结果', 'Copy result'), () => call('app', 'copyText', text(task.result)), { icon: 'copy' }) : null)), task.canRun || task.canRetry ? el('div', { class: 'detail-footer' }, button(task.canRetry ? t('重试发布', 'Retry publishing') : t('执行发布', 'Publish now'), () => call('app', task.canRetry ? 'retryPublishTask' : 'runPublishTask', Number(task.taskId)), { icon: task.canRetry ? 'refresh' : 'send', tone: 'primary' })) : null);
}

type SettingField = { key: string; label: Pair; type?: 'number' | 'check' | 'select' | 'textarea' | 'path'; options?: [string, Pair][]; hint?: Pair; min?: number; max?: number; step?: number; path?: 'directory' | 'file'; readonly?: boolean };
const choice = (value: string, zh: string, en: string): [string, Pair] => [value, [zh, en]];
const audioChoices = ['best', 'aac', 'alac', 'flac', 'm4a', 'mp3', 'opus', 'vorbis', 'wav'].map(value => choice(value, value === 'best' ? '最佳可用音频' : value.toUpperCase(), value === 'best' ? 'Best available audio' : value.toUpperCase()));
const subtitleChoices: [string, Pair][] = [choice('none', '不下载字幕', 'Do not download subtitles'), choice('all', '全部可用字幕', 'All available subtitles'), choice('zh-Hans', '简体中文', 'Simplified Chinese'), choice('zh-Hant', '繁体中文', 'Traditional Chinese'), choice('en', '英语', 'English'), choice('ja', '日语', 'Japanese'), choice('ko', '韩语', 'Korean'), choice('es', '西班牙语', 'Spanish'), choice('fr', '法语', 'French'), choice('de', '德语', 'German'), choice('ru', '俄语', 'Russian'), choice('pt', '葡萄牙语', 'Portuguese'), choice('ar', '阿拉伯语', 'Arabic'), choice('hi', '印地语', 'Hindi'), choice('id', '印度尼西亚语', 'Indonesian'), choice('vi', '越南语', 'Vietnamese'), choice('th', '泰语', 'Thai'), choice('tr', '土耳其语', 'Turkish')];
const encoderChoices = [choice('original', '保持原编码', 'Keep source codec'), choice('libx264', 'H.264 · CPU', 'H.264 · CPU'), choice('libx265', 'H.265 · CPU', 'H.265 · CPU'), choice('libsvtav1', 'AV1 · CPU (SVT)', 'AV1 · CPU (SVT)'), choice('libaom-av1', 'AV1 · CPU (AOM)', 'AV1 · CPU (AOM)'), choice('librav1e', 'AV1 · CPU (rav1e)', 'AV1 · CPU (rav1e)'), choice('h264_nvenc', 'H.264 · NVIDIA', 'H.264 · NVIDIA'), choice('hevc_nvenc', 'H.265 · NVIDIA', 'H.265 · NVIDIA'), choice('av1_nvenc', 'AV1 · NVIDIA', 'AV1 · NVIDIA'), choice('h264_qsv', 'H.264 · Intel', 'H.264 · Intel'), choice('hevc_qsv', 'H.265 · Intel', 'H.265 · Intel'), choice('av1_qsv', 'AV1 · Intel', 'AV1 · Intel'), choice('h264_amf', 'H.264 · AMD', 'H.264 · AMD'), choice('hevc_amf', 'H.265 · AMD', 'H.265 · AMD'), choice('av1_amf', 'AV1 · AMD', 'AV1 · AMD')];
const qualityChoices = ['best', '8k', '4k', '2k', '1080p', '720p', '480p', '360p', '240p', 'qcif'].map(value => choice(value, value === 'best' ? '最佳可用画质' : value === 'qcif' ? '144p' : value.toUpperCase(), value === 'best' ? 'Best available quality' : value === 'qcif' ? '144p' : value.toUpperCase()));
const settingsFields: Record<string, SettingField[]> = {
  downloads: [
    { key: 'download_dir', label: ['下载目录', 'Download folder'], type: 'path', path: 'directory' },
    { key: 'processing_temp_dir', label: ['处理临时目录', 'Processing temporary folder'], type: 'path', path: 'directory', hint: ['留空使用默认位置；需有足够空间', 'Leave empty for the default location; allow sufficient free space'] },
    { key: 'filename_template', label: ['文件名模板', 'Filename template'], hint: ['yt-dlp 模板，例如 %(title)s [%(id)s].%(ext)s', 'yt-dlp template, e.g. %(title)s [%(id)s].%(ext)s'] },
    { key: 'organize_task_folder', label: ['为每个任务创建文件夹', 'Create a folder for each task'], type: 'check' },
    { key: 'quality', label: ['默认清晰度上限', 'Default quality limit'], type: 'select', options: qualityChoices },
    { key: 'playlist_mode', label: ['链接解析', 'Collection link handling'], type: 'select', options: [choice('auto', '自动识别单视频 / 合集', 'Detect video or collection'), choice('single', '只解析单个视频', 'Single video only'), choice('playlist', '按合集解析', 'Parse as collection')] },
    { key: 'transcode_encoder', label: ['下载后视频编码器', 'Video encoder after download'], type: 'select', options: encoderChoices, hint: ['保持原编码不会转码；硬件编码器需要兼容设备', 'Keep source codec skips transcoding; hardware encoders need compatible devices'] },
    { key: 'transcode_codec', label: ['输出编码（由编码器决定）', 'Output codec (set by encoder)'], readonly: true },
    { key: 'transcode_device', label: ['编码设备（由编码器决定）', 'Encoding device (set by encoder)'], readonly: true },
    { key: 'subtitle_language', label: ['字幕语言', 'Subtitle language'], hint: ['none 不下载；例如 zh-Hans、en、all', 'none disables subtitles; e.g. zh-Hans, en, all'] },
    { key: 'download_performance_mode', label: ['下载性能模式', 'Download performance mode'], type: 'select', options: [choice('smart', '智能调度', 'Smart scheduling'), choice('manual', '手动并发', 'Manual concurrency')] },
    { key: 'max_concurrent', label: ['并发下载任务', 'Concurrent downloads'], type: 'number', min: 1, max: 8 },
    { key: 'fragment_concurrent', label: ['每个任务的并发分片', 'Concurrent fragments per task'], type: 'number', min: 1, max: 32 },
    { key: 'request_delay', label: ['请求间隔（秒）', 'Request delay (seconds)'], type: 'number', min: 0, max: 60, step: 0.1 },
  ],
  network: [
    { key: 'proxy', label: ['下载代理', 'Download proxy'], hint: ['完整 HTTP / HTTPS / SOCKS 地址，留空直连', 'Full HTTP / HTTPS / SOCKS address; leave empty for direct access'] },
    { key: 'download_cookie_source', label: ['Cookie 来源', 'Cookie source'], type: 'select', options: [choice('none', '不使用 Cookie', 'No cookies'), choice('embedded', '应用内置浏览器', 'App-managed browser'), choice('browser', '本机浏览器', 'Installed browser'), choice('file', 'Netscape 文件', 'Netscape file')] },
    { key: 'download_cookie_file', label: ['Cookie 文件', 'Cookie file'], type: 'path', path: 'file' },
    { key: 'download_cookie_browser', label: ['浏览器来源', 'Browser source'], type: 'select', options: ['chrome', 'edge', 'firefox', 'brave', 'chromium', 'opera', 'vivaldi', 'safari'].map(value => choice(value, value, value)) },
    { key: 'download_cookie_profile', label: ['浏览器配置文件', 'Browser profile'], hint: ['留空使用默认配置文件', 'Leave empty for the default profile'] },
    { key: 'download_cookie_keyring', label: ['浏览器钥匙串', 'Browser keyring'], hint: ['仅支持的平台需要指定', 'Specify only when required by your platform'] },
    { key: 'download_cookie_container', label: ['Firefox 容器', 'Firefox container'] },
  ],
  tools: [
    { key: 'ffmpeg_path', label: ['FFmpeg 可执行文件', 'FFmpeg executable'], type: 'path', path: 'file', hint: ['留空使用内置工具', 'Leave empty to use the bundled tool'] },
    { key: 'ffprobe_path', label: ['FFprobe 可执行文件', 'FFprobe executable'], type: 'path', path: 'file' },
    { key: 'deno_path', label: ['Deno 可执行文件', 'Deno executable'], type: 'path', path: 'file' },
    { key: 'ffmpeg_build_channel', label: ['FFmpeg 更新构建', 'FFmpeg update build'], type: 'select', options: [choice('nvenc_13_0', 'NVIDIA NVENC 13.0 兼容版', 'NVIDIA NVENC 13.0 compatible'), choice('latest', '最新上游构建', 'Latest upstream build')] },
    { key: 'ytdlp_core_mode', label: ['yt-dlp 核心', 'yt-dlp engine'], type: 'select', options: [choice('auto', '自动选择', 'Automatic'), choice('external', '外部可执行文件', 'External executable'), choice('builtin', '内置 Python 模块', 'Bundled Python module')] },
    { key: 'ytdlp_ejs_source', label: ['yt-dlp-ejs 来源', 'yt-dlp-ejs source'], type: 'select', options: [choice('auto', '自动', 'Automatic'), choice('npm', 'npm', 'npm'), choice('github', 'GitHub', 'GitHub'), choice('local', '本地', 'Local')] },
    { key: 'github_download_route', label: ['GitHub 下载线路', 'GitHub download route'], type: 'select', options: [choice('auto', '自动择优', 'Automatic selection'), choice('direct', 'GitHub 直连', 'Direct GitHub')] },
    { key: 'github_mirror_urls', label: ['自定义 GitHub 镜像', 'Custom GitHub mirrors'], type: 'textarea', hint: ['每行一个公开 HTTP / HTTPS 地址；线路设置可测试并选择镜像', 'One public HTTP / HTTPS URL per line; test and select mirrors in Route settings'] },
    { key: 'auto_check_updates', label: ['启动后自动检查应用更新', 'Automatically check application updates'], type: 'check' },
    { key: 'update_prerelease', label: ['包含预发布版本', 'Include prerelease versions'], type: 'check' },
    { key: 'update_channel', label: ['应用更新通道', 'Application update channel'], hint: ['留空使用默认发布通道', 'Leave empty for the default release channel'] },
  ],
  appearance: [
    { key: 'ui_language', label: ['界面语言', 'Interface language'], type: 'select', options: [choice('auto', '跟随系统', 'Follow system'), choice('zh-CN', '简体中文', '简体中文'), choice('en-US', 'English', 'English')] },
    { key: 'appearance_theme', label: ['界面主题', 'Appearance'], type: 'select', options: [choice('system', '跟随系统', 'Follow system'), choice('dark', '深色', 'Dark'), choice('light', '浅色', 'Light')] },
    { key: 'desktop_notifications', label: ['下载与发布完成时发送桌面通知', 'Notify when downloads and publishing finish'], type: 'check' },
    { key: 'cover_preset', label: ['默认封面预设', 'Default cover preset'], type: 'select', options: [choice('landscape_16_9', '横版 16:9', 'Landscape 16:9'), choice('portrait_3_4', '竖版 3:4', 'Portrait 3:4'), choice('portrait_9_16', '竖版 9:16', 'Portrait 9:16'), choice('landscape_4_3', '横版 4:3', 'Landscape 4:3'), choice('square_1_1', '正方形 1:1', 'Square 1:1')] },
    { key: 'cover_fit_mode', label: ['封面适配方式', 'Cover fit mode'], type: 'select', options: [choice('crop', '裁切填满', 'Crop to fill'), choice('pad', '保留全图并留白', 'Fit with padding')] },
    { key: 'cover_focus_x', label: ['默认裁切焦点 X（%）', 'Default crop focus X (%)'], type: 'number', min: 0, max: 100 },
    { key: 'cover_focus_y', label: ['默认裁切焦点 Y（%）', 'Default crop focus Y (%)'], type: 'number', min: 0, max: 100 },
    { key: 'download_cover_convert_jpeg', label: ['下载封面自动转为 JPEG', 'Convert downloaded covers to JPEG'], type: 'check' },
    { key: 'cover_jpeg_quality', label: ['JPEG 质量', 'JPEG quality'], type: 'number', min: 50, max: 100 },
    { key: 'prepend_cover_enabled', label: ['在视频片头插入下载封面', 'Prepend the downloaded cover to video'], type: 'check' },
    { key: 'prepend_cover_frames', label: ['片头封面帧数', 'Cover intro frame count'], type: 'number', min: 1, max: 300 },
    { key: 'cover_ai_model', label: ['AI 图像模型', 'AI image model'] },
    { key: 'cover_ai_api_url', label: ['图像 API 地址', 'Image API endpoint'], hint: ['留空使用 OpenAI；也支持兼容的图像生成接口', 'Leave empty for OpenAI; compatible image generation endpoints are supported'] },
  ],
};
const optionGroups: { id: string; label: Pair; hint: Pair; fields: SettingField[] }[] = [
  { id: 'media', label: ['媒体格式', 'Media format'], hint: ['下载来源格式、音轨和设备兼容性。', 'Source format, audio tracks, and device compatibility.'], fields: [
    { key: 'content_mode', label: ['内容类型', 'Content type'], type: 'select', options: [choice('video', '视频与音频', 'Video with audio'), choice('audio', '仅音频', 'Audio only'), choice('manual', '逐任务选择格式', 'Choose format per task')] },
    { key: 'audio_format', label: ['音频输出格式', 'Audio output format'], type: 'select', options: audioChoices },
    { key: 'audio_track', label: ['音轨偏好', 'Audio track preference'], type: 'select', options: [choice('default', '默认音轨', 'Default audio'), choice('original', '原始音轨', 'Original audio'), choice('all', '全部音轨', 'All tracks'), ...['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko', 'es', 'fr', 'de', 'ru', 'pt', 'ar', 'hi', 'id', 'vi', 'th', 'tr'].map(value => choice(value, value, value))] },
    { key: 'container', label: ['视频容器', 'Video container'], type: 'select', options: [choice('auto', '自动 / 来源格式', 'Automatic / source format'), choice('mp4', 'MP4', 'MP4'), choice('mkv', 'MKV', 'MKV')] },
    { key: 'video_fps', label: ['视频帧率偏好', 'Preferred video frame rate'], type: 'select', options: ['best', '240', '120', '60', '50', '30', '25', '24'].map(value => choice(value, value === 'best' ? '最佳可用' : `${value} fps`, value === 'best' ? 'Best available' : `${value} fps`)) },
    { key: 'source_video_codec', label: ['来源编码偏好', 'Preferred source codec'], type: 'select', options: ['auto', 'h264', 'h265', 'av1', 'vp9'].map(value => choice(value, value === 'auto' ? '自动' : value.toUpperCase(), value === 'auto' ? 'Automatic' : value.toUpperCase())) },
    { key: 'vr_mode', label: ['VR 格式偏好', 'VR format preference'], type: 'select', options: [choice('any', '任意', 'Any'), choice('none', '普通平面视频', 'Standard video'), choice('2d360', '2D 360°', '2D 360°'), choice('3d180', '3D 180°', '3D 180°'), choice('3d360', '3D 360°', '3D 360°')] },
    { key: 'compatibility_target', label: ['兼容设备', 'Device compatibility'], type: 'select', options: ['auto', 'windows', 'macos', 'linux', 'ios', 'android'].map(value => choice(value, value === 'auto' ? '不限制' : value, value === 'auto' ? 'No preference' : value)) },
  ] },
  { id: 'collection', label: ['合集与筛选', 'Collections and filters'], hint: ['设置整个合集的选择、顺序与下载范围。0 表示不限制。', 'Choose collection selection, order, and download range. 0 means no limit.'], fields: [
    { key: 'collection_mode', label: ['合集处理', 'Collection processing'], type: 'select', options: [choice('select', '弹出项目选择', 'Choose items'), choice('all', '下载全部', 'Download all'), choice('single', '仅单个视频', 'Single video only')] },
    { key: 'collection_order', label: ['下载顺序', 'Download order'], type: 'select', options: [choice('original', '来源顺序', 'Source order'), choice('reverse', '反向顺序', 'Reverse order'), choice('random', '随机顺序', 'Random order')] },
    { key: 'first_n', label: ['仅前 N 项', 'First N items only'], type: 'number', min: 0, max: 1000000 },
    { key: 'playlist_items', label: ['指定项目编号', 'Specific item indices'], hint: ['例如 1,3,5-10', 'For example 1,3,5-10'] },
    { key: 'date_after', label: ['上传日期起始', 'Uploaded after'], hint: ['YYYY-MM-DD', 'YYYY-MM-DD'] },
    { key: 'date_before', label: ['上传日期结束', 'Uploaded before'], hint: ['YYYY-MM-DD', 'YYYY-MM-DD'] },
    { key: 'duration_min', label: ['最小时长（秒）', 'Minimum duration (seconds)'], type: 'number', min: 0, max: 31536000 },
    { key: 'duration_max', label: ['最大时长（秒）', 'Maximum duration (seconds)'], type: 'number', min: 0, max: 31536000 },
  ] },
  { id: 'live', label: ['直播、片段与章节', 'Live streams, sections, and chapters'], hint: ['时间支持秒数或 HH:MM:SS。', 'Time accepts seconds or HH:MM:SS.'], fields: [
    { key: 'live_filter', label: ['直播筛选', 'Live stream filter'], type: 'select', options: [choice('videos', '仅非直播视频', 'Non-live videos'), choice('all', '全部内容', 'All content'), choice('live', '仅直播', 'Live streams only')] },
    { key: 'live_from_start', label: ['从直播开始处下载', 'Download live streams from the beginning'], type: 'check' },
    { key: 'wait_for_live', label: ['等待尚未开始的直播', 'Wait for upcoming live streams'], type: 'check' },
    { key: 'wait_min', label: ['最短轮询间隔（秒）', 'Minimum poll interval (seconds)'], type: 'number', min: 1, max: 86400 },
    { key: 'wait_max', label: ['最长轮询间隔（秒）', 'Maximum poll interval (seconds)'], type: 'number', min: 1, max: 86400 },
    { key: 'section_start', label: ['片段开始时间', 'Section start time'] },
    { key: 'section_end', label: ['片段结束时间', 'Section end time'] },
    { key: 'split_chapters', label: ['按章节拆分文件', 'Split files by chapters'], type: 'check' },
  ] },
  { id: 'extras', label: ['字幕、封面与元数据', 'Subtitles, covers, and metadata'], hint: ['选择随媒体保存或嵌入的内容。', 'Choose additional files and metadata to save or embed.'], fields: [
    { key: 'subtitle_format', label: ['字幕文件格式', 'Subtitle file format'], type: 'select', options: ['best', 'vtt', 'srt', 'ass', 'lrc'].map(value => choice(value, value === 'best' ? '最佳可用' : value.toUpperCase(), value === 'best' ? 'Best available' : value.toUpperCase())) },
    { key: 'embed_subtitles', label: ['嵌入字幕', 'Embed subtitles'], type: 'check' },
    { key: 'write_thumbnail', label: ['保存来源封面', 'Save source cover'], type: 'check' },
    { key: 'write_description', label: ['保存描述文件', 'Save description file'], type: 'check' },
    { key: 'write_comments', label: ['保存评论', 'Save comments'], type: 'check' },
    { key: 'write_info_json', label: ['保存媒体信息文件', 'Save media information file'], type: 'check' },
    { key: 'embed_metadata', label: ['嵌入标题和描述', 'Embed title and description'], type: 'check' },
    { key: 'embed_chapters', label: ['嵌入章节', 'Embed chapters'], type: 'check' },
    { key: 'embed_thumbnail', label: ['嵌入封面', 'Embed cover'], type: 'check' },
  ] },
  { id: 'sponsor', label: ['SponsorBlock 与速度限制', 'SponsorBlock and speed limit'], hint: ['SponsorBlock 依赖来源网站与社区标记。', 'SponsorBlock depends on source support and community annotations.'], fields: [
    { key: 'sponsorblock_mode', label: ['SponsorBlock 行为', 'SponsorBlock mode'], type: 'select', options: [choice('off', '关闭', 'Off'), choice('mark', '标记片段', 'Mark segments'), choice('remove', '移除片段', 'Remove segments')] },
    { key: 'rate_limit', label: ['下载速度上限', 'Download speed limit'], hint: ['留空不限速；例如 5M、800K', 'Leave empty for no limit; e.g. 5M, 800K'] },
  ] },
];
function ensureSettingsDraft(): void {
  if (!settingsDraft) settingsDraft = { ...(section('settings').values || {}) };
  if (!optionsDraft) optionsDraft = { ...(section('settings').downloadOptions || {}) };
}
function editSetting(key: string, value: unknown, option = false): void {
  ensureSettingsDraft(); (option ? optionsDraft! : settingsDraft!)[key] = value; settingsDirty = true;
  if (!option && key === 'transcode_encoder') {
    const encoder = text(value);
    settingsDraft!.transcode_codec = encoder === 'original' ? 'original' : encoder.includes('av1') || encoder.includes('aom') || encoder.includes('rav1e') ? 'av1' : encoder.includes('265') || encoder.includes('hevc') ? 'h265' : 'h264';
    settingsDraft!.transcode_device = encoder.includes('nvenc') ? 'nvidia' : encoder.includes('qsv') ? 'intel' : encoder.includes('amf') ? 'amd' : encoder === 'original' ? 'auto' : 'cpu';
  }
  const dirtyNode = document.getElementById('settings-dirty'); if (dirtyNode) dirtyNode.textContent = t('有未保存修改', 'Unsaved changes');
}
function settingControl(definition: SettingField, option = false, prefix = 'setting'): HTMLElement {
  ensureSettingsDraft(); const store = option ? optionsDraft! : settingsDraft!;
  const id = `${prefix}-${definition.key}`; const value = store[definition.key];
  const change = (next: unknown) => editSetting(definition.key, next, option);
  const label = t(...definition.label);
  if (definition.type === 'check') return el('div', { class: 'field check-field' }, check(id, label, value, change));
  let control: HTMLElement;
  if (definition.key === 'subtitle_language') {
    const custom = !subtitleChoices.some(([key]) => key === value);
    const customInput = input(`${id}-custom`, custom ? value : '', change, { disabled: !custom, placeholder: t('输入来源语言标识或表达式', 'Enter a source language identifier or expression'), 'aria-label': t('自定义字幕语言', 'Custom subtitle language') });
    control = el('div', {}, select(id, custom ? '__custom' : value, [...subtitleChoices.map(([key, pair]): [string, string] => [key, t(...pair)]), ['__custom', t('自定义语言', 'Custom language')]], next => { customInput.disabled = next !== '__custom'; if (next === '__custom') { customInput.focus(); if (customInput.value) change(customInput.value); } else change(next); }), customInput);
  } else if (definition.type === 'select') {
    let choices = (definition.options || []).map(([key, pair]): [string, string] => [key, t(...pair)]);
    if (definition.key === 'github_download_route') choices = [...choices, ...rows('routes').filter(row => row.id !== 'direct').map((row): [string, string] => [row.id, row.name])];
    if (definition.key === 'ui_language') choices = [['auto', t('跟随系统', 'Follow system')], ...(section('settings').languages || []).map((language: Data): [string, string] => [language.locale, language.name])];
    if (definition.key === 'cover_preset' && section('cover').presets?.length) choices = section('cover').presets.map((row: Data): [string, string] => [row.id, row.label]);
    if (value && !choices.some(([key]) => key === value)) choices.push([text(value), text(value)]);
    control = select(id, value, choices, change);
  } else if (definition.type === 'textarea') control = el('textarea', { id, rows: 3, value: text(value), oninput: (event: Event) => change((event.target as HTMLTextAreaElement).value) });
  else {
    const summaries: Record<string, Record<string, string>> = { transcode_codec: { original: t('保持来源编码', 'Keep source codec'), h264: 'H.264 / AVC', h265: 'H.265 / HEVC', av1: 'AV1' }, transcode_device: { auto: t('自动', 'Automatic'), cpu: 'CPU', nvidia: 'NVIDIA GPU', intel: 'Intel GPU', amd: 'AMD GPU' } };
    const displayed = definition.readonly ? summaries[definition.key]?.[text(value)] || value : value;
    const textInput = input(id, displayed, next => change(definition.type === 'number' && option ? Number(next) : next), { type: definition.type === 'number' ? 'number' : 'text', min: definition.min, max: definition.max, step: definition.step || 1, readOnly: definition.readonly || false });
    control = definition.type === 'path' ? el('div', { class: 'path-control' }, textInput, button(t('选择', 'Browse'), async () => { const path = await window.huifa.choosePath({ kind: definition.path || 'file', title: label, defaultPath: text(textInput.value) }); if (path) { textInput.value = path; change(path); } }, { icon: 'folder', 'aria-label': t(`选择${label}`, `Choose ${label}`) })) : textInput;
  }
  return fieldLabel(label, control, definition.hint ? t(...definition.hint) : undefined);
}
function settingsGroup(title: string, description: string, fields: SettingField[], option = false, prefix = 'setting'): HTMLElement {
  return el('section', { class: 'settings-group panel' }, el('div', { class: 'section-heading' }, el('div', {}, el('h2', {}, title), el('p', { class: 'muted' }, description))), el('div', { class: 'form-grid' }, ...fields.map(field => settingControl(field, option, prefix))));
}
async function saveSettings(): Promise<boolean> {
  ensureSettingsDraft();
  optionsDraft!.organize_task_folder = yes(settingsDraft!.organize_task_folder);
  optionsDraft!.processing_temp_dir = text(settingsDraft!.processing_temp_dir);
  optionsDraft!.prepend_cover_enabled = yes(settingsDraft!.prepend_cover_enabled);
  optionsDraft!.prepend_cover_frames = Number(settingsDraft!.prepend_cover_frames) || 3;
  const result = await call('settings', 'save', settingsDraft, optionsDraft, secretDraft);
  if (result === true) {
    settingsDirty = false; secretDraft = ''; settingsDraft = null; optionsDraft = null;
    const snapshot = await call('host', 'snapshot'); if (snapshot) applyState(snapshot); else render();
    return true;
  }
  return false;
}
function settingsPage(): HTMLElement {
  ensureSettingsDraft();
  const content = el('div', { class: 'settings-content page-scroll', 'data-scroll': `settings-${settingsTab}` });
  if (settingsTab === 'downloads') {
    content.append(settingsGroup(t('下载与处理', 'Downloads and processing'), t('保存的方案统一应用到新加入的批量任务。', 'The saved plan applies to every new task in a batch.'), settingsFields.downloads));
    for (const group of optionGroups) content.append(el('details', { class: 'settings-fold panel', id: `option-group-${group.id}`, open: group.id === 'media' }, el('summary', {}, el('strong', {}, t(...group.label)), el('span', { class: 'muted' }, t(...group.hint))), el('div', { class: 'form-grid' }, ...group.fields.map(field => settingControl(field, true)))));
    const categories: [string, Pair][] = [['sponsor', ['赞助广告', 'Sponsor']], ['intro', ['片头', 'Intro']], ['outro', ['片尾', 'Outro']], ['selfpromo', ['自我推广', 'Self promotion']], ['interaction', ['互动提醒', 'Interaction']], ['preview', ['预告', 'Preview']], ['music_offtopic', ['非音乐片段', 'Non-music sections']], ['filler', ['填充片段', 'Filler']]];
    content.append(el('section', { class: 'settings-group panel' }, el('h2', {}, t('SponsorBlock 类别', 'SponsorBlock categories')), el('div', { class: 'checkbox-grid' }, ...categories.map(([key, pair]) => check(`sponsor-${key}`, t(...pair), (optionsDraft!.sponsorblock_categories || []).includes(key), checked => { const chosen = new Set<string>(optionsDraft!.sponsorblock_categories || []); checked ? chosen.add(key) : chosen.delete(key); editSetting('sponsorblock_categories', [...chosen], true); }))), el('p', { class: 'muted' }, t('任务文件夹、处理临时目录及封面片头与主设置保持一致。', 'Task folders, processing temporary folder, and cover intro share the main settings.'))));
  } else if (settingsTab === 'network') {
    content.append(settingsGroup(t('网络与下载身份', 'Network and download identity'), t('先保存设置，再检查 Cookie 或启动登录。', 'Save your settings before checking cookies or signing in.'), settingsFields.network), el('div', { class: 'panel settings-group' }, el('h2', {}, t('下载 Cookie', 'Download cookies')), el('div', { class: 'actions wrap' }, button(t('检查 Cookie', 'Check cookies'), async () => { if (await saveSettings()) await call('app', 'checkDownloadCookies'); }, { icon: 'check' }), button(t('打开内置登录', 'Open app-managed sign-in'), async () => { if (await saveSettings()) await call('app', 'openDownloadCookieLogin'); }, { icon: 'accounts' }), button(t('查看内置 Cookie', 'View app-managed cookies'), () => openCookies(), { icon: 'log' }))));
  } else if (settingsTab === 'tools') {
    content.append(settingsGroup(t('工具与更新来源', 'Tools and update sources'), t('留空的工具路径使用应用内置版本。', 'Empty tool paths use app-managed versions.'), settingsFields.tools), el('div', { class: 'panel settings-group' }, el('div', { class: 'section-heading' }, el('h2', {}, t('GitHub 线路', 'GitHub routes')), button(t('测试与管理线路', 'Test and manage routes'), () => openModal('routes'), { icon: 'settings' })), el('p', { class: 'muted' }, t('可分别查看元数据和附件连通性；测试结果保存在线路配置中。', 'View metadata and asset connectivity separately; probe results are stored in route profiles.'))), updatePanel());
  } else {
    content.append(settingsGroup(t('外观、通知与封面', 'Appearance, notifications, and covers'), t('主题和语言在保存后立即生效。', 'Theme and language apply when saved.'), settingsFields.appearance),
      el('section', { class: 'settings-group panel' }, el('h2', {}, t('OpenAI API 密钥', 'OpenAI API key')), el('p', { class: 'muted' }, section('settings').hasOpenAiKey ? t('已保存在系统凭据库。留空保留当前密钥。', 'Stored in the system credential store. Leave empty to keep it.') : t('尚未保存密钥。用于封面工作室的 AI 创作。', 'No saved key. Used for AI creation in Cover studio.')), el('div', { class: 'path-control' }, input('api-key', secretDraft, value => { secretDraft = value; settingsDirty = true; }, { type: 'password', autocomplete: 'new-password', placeholder: t('输入新密钥（不会显示已保存密钥）', 'Enter a new key (saved keys are never displayed)'), 'aria-label': 'OpenAI API key' }), button(t('清除密钥', 'Clear saved key'), () => openModal('clear-key'), { tone: 'danger', disabled: !section('settings').hasOpenAiKey }))),
      el('section', { class: 'settings-group panel' }, el('h2', {}, t('默认发布平台', 'Default publishing platforms')), el('div', { class: 'checkbox-grid' }, ...rows('accounts').map(account => check(`settings-target-${account.platform}`, account.label, text(settingsDraft!.publish_target_platforms).split(',').includes(account.platform), checked => { const targets = new Set(text(settingsDraft!.publish_target_platforms).split(',').filter(Boolean)); checked ? targets.add(account.platform) : targets.delete(account.platform); editSetting('publish_target_platforms', [...targets].join(',')); }))), button(t('管理平台账号', 'Manage platform accounts'), () => go('accounts'), { icon: 'accounts', tone: 'subtle' })),
      diagnosticsPanel());
  }
  const tabs: [string, Pair][] = [['downloads', ['下载与处理', 'Downloads']], ['network', ['网络与 Cookie', 'Network']], ['tools', ['工具与更新', 'Tools & updates']], ['appearance', ['外观与封面', 'Appearance & covers']]];
  return el('div', { class: 'page' }, heading(t('设置', 'Settings'), t('管理下载方案、运行时工具和发布准备。', 'Manage download plans, runtime tools, and publishing preparation.'), [el('span', { class: 'muted', id: 'settings-dirty' }, settingsDirty ? t('有未保存修改', 'Unsaved changes') : t('已保存', 'Saved')), button(t('重新载入', 'Reload'), () => settingsDirty ? openModal('reload-settings') : reloadSettings(), { icon: 'refresh' }), button(t('扫描已有媒体', 'Scan local media'), async () => { const folder = await window.huifa.choosePath({ kind: 'directory', title: t('扫描已有媒体', 'Scan local media'), defaultPath: text(settingsDraft?.download_dir) }); if (folder) await call('app', 'scanLocalHistory', folder); }, { icon: 'library' }), button(t('保存全部设置', 'Save all settings'), saveSettings, { icon: 'check', tone: 'primary' })]), el('div', { class: 'filter-tabs settings-tabs' }, ...tabs.map(([id, pair]) => button(t(...pair), () => { settingsTab = id; render(); }, { class: `filter-tab ${settingsTab === id ? 'active' : ''}`, 'aria-pressed': settingsTab === id }))), content);
}
async function reloadSettings(): Promise<void> { settingsDraft = null; optionsDraft = null; secretDraft = ''; settingsDirty = false; const snapshot = await call('host', 'snapshot'); if (snapshot) applyState(snapshot); }
function updatePanel(): HTMLElement {
  const updates = section('updates'); const application = updates.applicationUpdate || {};
  const runtime = updates.runtimeResults || [];
  return el('section', { class: 'settings-group panel', id: 'update-panel' },
    el('div', { class: 'section-heading' }, el('h2', {}, t('程序更新', 'Application update')), button(t('检查程序更新', 'Check application update'), async () => { if (await saveSettings()) await call('updates', 'checkApplication'); }, { icon: 'refresh', disabled: updates.applicationBusy || !updates.applicationSupported })), el('p', { class: 'muted' }, updates.applicationStatus),
    updates.applicationBusy ? el('div', { class: 'update-progress' }, progressBar({ status: 'downloading', progress: updates.applicationProgress }), el('span', {}, `${updates.applicationProgress || 0}%`), button(t('取消下载', 'Cancel download'), () => call('updates', 'cancelApplicationDownload'))) : null,
    application.version ? el('div', { class: 'update-release' }, el('strong', {}, t(`可用版本 ${application.version}`, `Available version ${application.version}`)), button(application.downloaded ? t('重启并安装', 'Restart and install') : t('下载更新', 'Download update'), () => application.downloaded ? openModal('install-update', { version: application.version }) : call('updates', 'downloadApplication'), { icon: 'download', tone: 'primary', disabled: updates.applicationBusy }), application.releaseNotes ? el('details', { id: 'release-notes' }, el('summary', {}, t('更新说明', 'Release notes')), el('pre', { class: 'release-notes' }, application.releaseNotes)) : null) : null,
    el('hr'), el('div', { class: 'section-heading' }, el('h2', {}, t('运行时组件', 'Runtime components')), button(updates.runtimeBusy ? t('检查 / 安装中', 'Checking / installing') : t('检查组件', 'Check components'), async () => { if (await saveSettings()) await call('updates', 'checkRuntimeComponents'); }, { icon: 'refresh', disabled: updates.runtimeBusy })), el('p', { class: 'muted' }, updates.runtimeStatus), ...runtime.map((component: Data) => el('div', { class: 'component-row' }, el('div', {}, el('strong', {}, component.name), el('p', { class: 'muted' }, t(`当前 ${component.current || '未检测'} · 最新 ${component.latest || '—'}`, `Current ${component.current || 'not detected'} · Latest ${component.latest || '—'}`)), component.error ? el('small', { class: 'error-text' }, component.error) : el('small', {}, component.checksumAvailable ? t('发布方 SHA-256 可用', 'Publisher SHA-256 available') : t('发布方未提供 SHA-256', 'No publisher SHA-256'))), button(component.installing ? t('安装中…', 'Installing…') : component.install_available ? t('安装', 'Install') : component.has_update ? t('更新', 'Update') : t('已是最新', 'Up to date'), async () => { if (await call('updates', 'componentNeedsChecksumConfirmation', component.name)) openModal('checksum', { component: component.name }); else await call('updates', 'installRuntimeComponent', component.name, false); }, { icon: 'download', disabled: updates.runtimeBusy || !component.actionable }))));
}
function diagnosticsPanel(): HTMLElement {
  return el('section', { class: 'panel settings-group' }, el('h2', {}, t('日志、诊断与语言包', 'Logs, diagnostics, and language packs')), el('p', { class: 'muted' }, t('诊断 ZIP 包包括运行时信息和脱敏日志，不包括 Cookie 值或 API 密钥。', 'Diagnostic ZIP files contain runtime information and redacted logs, without cookie values or API keys.')), el('div', { class: 'actions wrap' }, button(t('打开日志目录', 'Open log folder'), () => call('app', 'openLogDirectory'), { icon: 'log' }), button(t('导出诊断包', 'Export diagnostics'), exportDiagnostics, { icon: 'download' }), button(t('打开语言包目录', 'Open language pack folder'), () => call('app', 'openLanguagePackDirectory'), { icon: 'folder' })));
}
async function exportDiagnostics(): Promise<void> {
  const defaultPath = await call('app', 'diagnosticsDefaultPath');
  const path = await window.huifa.choosePath({ kind: 'save', title: t('导出诊断包', 'Export diagnostics'), defaultPath: text(defaultPath), filters: [{ name: 'ZIP', extensions: ['zip'] }] });
  if (!path) return;
  const result = await call('app', 'exportDiagnostics', path);
  if (result) openModal('diagnostics', { path: result });
}
function aboutPage(): HTMLElement {
  const app = section('app');
  const dependencies: [string, Pair, string][] = [['yt-dlp', ['跨站媒体信息提取与下载。', 'Cross-site media extraction and downloading.'], 'Unlicense'], ['FFmpeg / FFprobe', ['流合并、媒体探测和转码。', 'Stream merging, media probing, and transcoding.'], t('上游许可证', 'Upstream licenses')], ['yt-dlp-ejs / Deno', ['处理来源网站的 JavaScript。', 'JavaScript support for source websites.'], t('上游许可证', 'Upstream licenses')], ['social-auto-upload', ['多平台登录、Cookie 校验和发布。', 'Platform sign-in, cookie validation, and publishing.'], 'MIT'], ['Playwright / Chromium', ['应用管理的浏览器自动化运行时。', 'App-managed browser automation runtime.'], 'Apache-2.0 / Chromium'], ['Electron / Python / PySide6', ['桌面界面与现有下载服务。', 'Desktop interface and existing download services.'], t('上游许可证', 'Upstream licenses')]];
  return el('div', { class: 'page' }, heading(t('关于', 'About'), t('版本、支持站点与诊断信息。', 'Version, supported sites, and diagnostic information.')), el('div', { class: 'page-scroll', 'data-scroll': 'about' }, el('section', { class: 'panel about-identity' }, el('div', { class: 'brand-mark' }, icon('download')), el('div', {}, el('h2', {}, app.appName || 'Huifa Media Downloader'), el('p', { class: 'muted' }, `${t('版本', 'Version')} ${app.appVersion || '—'} · Electron`), el('p', {}, t('从下载到封面准备和多平台发布，一个连续的媒体工作空间。', 'A continuous media workspace, from downloads to cover preparation and publishing.')), button(t('查看支持的网站', 'View supported sites'), async () => { openModal('sites'); await call('sites', 'load'); }, { icon: 'external' }))), el('section', { class: 'panel settings-group' }, el('h2', {}, t('内置组件', 'Included components')), ...dependencies.map(([name, pair, license]) => el('div', { class: 'dependency-row' }, el('strong', {}, name), el('span', { class: 'muted' }, t(...pair)), el('small', {}, license)))), diagnosticsPanel()));
}

function openModal(kind: string, data: Data = {}): void {
  if (!modal) returnFocus = document.activeElement as HTMLElement | null;
  if (kind === 'quick') {
    ensureSettingsDraft();
    data.previousValues = { ...settingsDraft }; data.previousOptions = { ...optionsDraft }; data.previousDirty = settingsDirty;
  }
  modal = { kind, data }; modalNeedsRender = true; renderModal();
}
function dismissModal(): void {
  modal = null; modalNeedsRender = false; modalRoot.replaceChildren(); returnFocus?.focus({ preventScroll: true });
}
async function requestCoverAction(action: 'close' | 'restoreOriginal' | 'generate', prompt = ''): Promise<void> {
  if (section('cover').dirty) { openModal('discard-cover', { previous: modal, action, prompt }); return; }
  await call('cover', action, ...(action === 'generate' ? [prompt] : []));
  if (action === 'close') dismissModal();
}
async function requestCloseModal(): Promise<void> {
  if (!modal) return;
  const { kind, data } = modal;
  if (kind === 'cover') {
    if (section('cover').busy) { await call('cover', 'cancelGeneration'); return; }
    await requestCoverAction('close'); return;
  } else if (kind === 'discard-cover') { modal = data.previous; modalNeedsRender = true; renderModal(); return; }
  else if (kind === 'format') await call('app', 'cancelFormatSelection');
  else if (kind === 'collection' && !data.viewOnly) {
    if (await call('app', 'cancelCollectionSelection', text(data.taskId || section('collection').parentTaskId)) === false) return;
  } else if (kind === 'draft') await call('draft', 'close');
  else if (kind === 'quick') { settingsDraft = data.previousValues; optionsDraft = data.previousOptions; settingsDirty = data.previousDirty; }
  dismissModal();
}
function modalTitle(kind: string): string {
  const labels: Record<string, Pair> = { quick: ['下一批次下载方案', 'Next batch download plan'], delete: ['删除任务', 'Delete task'], cleanup: ['清理已完成任务记录', 'Clean up completed task records'], redownload: ['重新下载媒体', 'Download media again'], convert: ['转换视频格式', 'Convert video format'], format: ['选择下载格式', 'Choose download format'], collection: ['合集项目', 'Collection items'], draft: ['创建发布任务', 'Create publishing tasks'], cover: ['封面工作室', 'Cover studio'], log: ['任务日志', 'Task log'], cookies: ['内置浏览器 Cookie', 'App-managed browser cookies'], sites: ['支持的网站', 'Supported sites'], routes: ['GitHub 下载线路', 'GitHub download routes'], history: ['导入已有媒体', 'Import local media'], recovery: ['数据库恢复', 'Database recovery'], diagnostics: ['诊断包已导出', 'Diagnostics exported'], checksum: ['确认运行时组件安装', 'Confirm runtime installation'], 'install-update': ['重启并安装更新', 'Restart and install update'], 'clear-key': ['清除 API 密钥', 'Clear API key'], 'discard-cover': ['放弃未保存的封面修改？', 'Discard unsaved cover changes?'], 'reload-settings': ['重新载入设置？', 'Reload settings?'] };
  return labels[kind] ? t(...labels[kind]) : kind;
}
function renderModal(): void {
  if (!modalNeedsRender) return;
  modalNeedsRender = false;
  if (!modal) { modalRoot.replaceChildren(); return; }
  const { kind, data } = modal;
  preserve(modalRoot, () => {
    const builder: Record<string, () => { body: HTMLElement; actions: Child[]; subtitle?: string; size?: string }> = { quick: quickDialog, format: formatDialog, collection: collectionDialog, draft: draftDialog, cover: coverDialog, log: logDialog, cookies: cookieDialog, sites: siteDialog, routes: routeDialog, convert: convertDialog };
    const content = builder[kind] ? builder[kind]() : confirmationDialog(kind, data);
    const dialog = el('dialog', { class: `dialog ${content.size || ''}`, 'aria-labelledby': 'dialog-title', oncancel: (event: Event) => { event.preventDefault(); void requestCloseModal(); } },
      el('header', { class: 'dialog-header' }, el('div', {}, el('h2', { id: 'dialog-title' }, kind === 'recovery' && data.title ? data.title : modalTitle(kind)), content.subtitle ? el('p', { class: 'muted' }, content.subtitle) : null), button(t('关闭', 'Close'), requestCloseModal, { icon: 'close', tone: 'icon-only', 'aria-label': t('关闭对话框', 'Close dialog') })), content.body, el('footer', { class: 'dialog-footer' }, ...content.actions));
    modalRoot.replaceChildren(dialog); dialog.showModal();
  });
}
function refreshModal(): void { modalNeedsRender = true; renderModal(); }
function dialogBody(...children: Child[]): HTMLElement { return el('div', { class: 'dialog-body', 'data-scroll': `modal-${modal?.kind}` }, ...children); }
function confirmationDialog(kind: string, data: Data): { body: HTMLElement; actions: Child[]; size?: string; subtitle?: string } {
  let copy = ''; let detail = ''; let actions: Child[] = [button(t('取消', 'Cancel'), requestCloseModal)];
  const complete = async (target: string, action: string, ...args: unknown[]) => { const result = await call(target, action, ...args); if (result !== false && result !== undefined) dismissModal(); };
  if (kind === 'delete') {
    copy = t(`删除“${data.title || data.taskId}”？正在运行的任务会先停止。`, `Delete “${data.title || data.taskId}”? Active work stops first.`);
    detail = t('仅删除记录会保留媒体。删除记录和媒体仅清理此任务管理的文件。', 'Deleting the record keeps the media. Deleting both removes only files managed by this task.');
    const remove = async (files: boolean) => { const result = await call('app', 'deleteTask', data.taskId, files); if (result !== false && result !== undefined) { if (selected.tasks === data.taskId) { selected.tasks = ''; delete selectedCache.tasks; } dismissModal(); render(); } };
    actions.push(button(t('仅删除记录', 'Delete record only'), () => remove(false)), button(t('删除记录和媒体', 'Delete record and media'), () => remove(true), { tone: 'danger' }));
  } else if (kind === 'cleanup') {
    copy = t('清理所有已完成的任务记录？', 'Remove all completed download task records?'); detail = t('媒体文件和媒体库记录会保留。', 'Media files and the media library are kept.');
    actions.push(button(t('清理记录', 'Remove records'), () => complete('app', 'cleanupCompletedTasks'), { tone: 'primary' }));
  } else if (kind === 'redownload') {
    copy = t('重新下载会使用该任务的保存方案。', 'The download uses this task’s saved plan.');
    actions.push(button(t('重新下载', 'Download again'), () => complete('app', 'redownloadTask', data.taskId), { icon: 'download', tone: 'primary' }));
  } else if (kind === 'history') {
    copy = t(`在 ${data.folder} 中发现 ${data.count} 个未记录媒体。`, `${data.count} unrecorded media files found in ${data.folder}.`); detail = t('导入后显示为已完成任务。原始文件不会移动或修改。', 'Imported media appears as completed tasks. Original files stay in place.');
    actions.push(button(t('导入媒体', 'Import media'), () => complete('app', 'importScannedLocalHistory'), { icon: 'library', tone: 'primary' }));
  } else if (kind === 'recovery') {
    copy = data.summary; detail = data.detail; actions = [button(t('知道了', 'OK'), dismissModal, { tone: 'primary' })];
  } else if (kind === 'diagnostics') {
    copy = data.path; detail = t('诊断包已保存，敏感日志已脱敏。', 'The diagnostics bundle has been saved with sensitive log values redacted.');
    actions = [button(t('关闭', 'Close'), dismissModal), button(t('打开所在文件夹', 'Open containing folder'), () => call('app', 'openDiagnosticsLocation', data.path), { icon: 'folder', tone: 'primary' })];
  } else if (kind === 'checksum') {
    copy = t(`${data.component} 的官方发布资源未提供 SHA-256。`, `The official ${data.component} release asset has no publisher SHA-256.`); detail = t('程序仍会检查压缩包结构和可执行文件格式。继续下载并安装？', 'The app will still check archive structure and executable format. Continue downloading and installing?');
    actions.push(button(t('继续安装', 'Continue installation'), () => complete('updates', 'installRuntimeComponent', data.component, true), { tone: 'primary' }));
  } else if (kind === 'install-update') {
    copy = t(`重启并安装 ${data.version}？`, `Restart and install ${data.version}?`); detail = t('程序将保存任务状态、安全停止后台工作，再退出安装更新。', 'The app saves task state and safely stops background work before installing the update.');
    actions.push(button(t('重启并安装', 'Restart and install'), () => complete('updates', 'installApplication'), { tone: 'primary' }));
  } else if (kind === 'clear-key') {
    copy = t('清除系统凭据库中保存的 OpenAI API 密钥？', 'Remove the OpenAI API key from the system credential store?');
    actions.push(button(t('清除密钥', 'Clear key'), () => complete('settings', 'clearOpenAiKey'), { tone: 'danger' }));
  } else if (kind === 'reload-settings') {
    copy = t('放弃未保存修改，重新载入已保存的设置？', 'Discard unsaved edits and reload saved settings?');
    actions.push(button(t('重新载入', 'Reload'), async () => { dismissModal(); await reloadSettings(); }, { tone: 'primary' }));
  } else if (kind === 'discard-cover') {
    const action = data.action || 'close';
    copy = t('当前裁切位置或 AI 变体尚未保存。', 'The current crop or AI variation has not been saved.'); detail = action === 'generate' ? t('继续生成将使用当前封面创建新变体。请先导出需要保留的结果。', 'Continue generating from the current cover. Export the result first if you want to keep it.') : action === 'restoreOriginal' ? t('恢复后当前预览将替换为原始封面。', 'Restoring replaces the current preview with the original cover.') : t('关闭后原始封面保持不变。', 'The original cover is preserved.');
    actions.push(button(action === 'generate' ? t('放弃并生成', 'Discard and generate') : action === 'restoreOriginal' ? t('放弃并恢复', 'Discard and restore') : t('放弃并关闭', 'Discard and close'), async () => { modal = data.previous; refreshModal(); await call('cover', action, ...(action === 'generate' ? [text(data.prompt)] : [])); if (action === 'close') dismissModal(); }, { tone: 'danger' }));
  }
  return { body: dialogBody(el('p', { class: 'dialog-copy break-text' }, copy), detail ? el('p', { class: 'muted break-text' }, detail) : null), actions, size: 'small' };
}
function quickDialog(): { body: HTMLElement; actions: Child[]; subtitle: string } {
  const values = settingsFields.downloads.filter(field => ['quality', 'playlist_mode', 'transcode_encoder', 'subtitle_language', 'download_performance_mode'].includes(field.key));
  const options = optionGroups[0].fields.filter(field => ['content_mode', 'audio_format', 'container'].includes(field.key));
  options.push(optionGroups[1].fields[0]);
  const cover = settingsFields.appearance.filter(field => ['download_cover_convert_jpeg', 'prepend_cover_enabled'].includes(field.key));
  return { subtitle: t('统一应用于后续新任务；已加入任务的方案已固定。', 'Apply one plan to new tasks; existing tasks keep their frozen plans.'), body: dialogBody(el('div', { class: 'form-grid' }, ...values.map(field => settingControl(field, false, 'quick')), ...options.map(field => settingControl(field, true, 'quick')), ...cover.map(field => settingControl(field, false, 'quick')))), actions: [button(t('取消', 'Cancel'), requestCloseModal), button(t('打开全部设置', 'Open all settings'), () => { dismissModal(); go('settings'); }), button(t('保存批次方案', 'Save batch plan'), async () => { if (await saveSettings()) { dismissModal(); render(); } }, { tone: 'primary', icon: 'check' })] };
}
function formatDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  const formats = section('formats'); const data = modal!.data;
  if (data.choice == null || data.mode !== formats.contentMode) { data.choice = Number(formats.firstChoiceIndex ?? -1); data.mode = formats.contentMode; }
  return { size: 'wide', subtitle: formats.title || t('选择内容类型与精确格式', 'Choose content type and exact format'), body: dialogBody(el('div', { class: 'format-intro' }, thumbnail(formats.thumbnailUrl), el('div', { class: 'actions wrap' }, button(t('视频', 'Video'), () => call('formats', 'setContentMode', 'video'), { class: `button ${formats.contentMode === 'video' ? 'primary' : ''}`, disabled: !formats.hasVideo }), button(t('音频', 'Audio'), () => call('formats', 'setContentMode', 'audio'), { class: `button ${formats.contentMode === 'audio' ? 'primary' : ''}`, disabled: !formats.hasAudio }), formats.contentMode === 'audio' ? fieldLabel(t('音频输出', 'Audio output'), select('format-audio', formats.audioFormat, audioChoices.map(([key, pair]) => [key, t(...pair)]), value => call('formats', 'setAudioFormat', value))) : null)), el('div', { class: 'modal-list-holder' }, rows('formats').length ? virtualList('formats', rows('formats'), 'choiceIndex', 68, format => el('label', { class: `format-choice ${data.choice === format.choiceIndex ? 'chosen' : ''}` }, el('input', { type: 'radio', name: 'format-choice', checked: data.choice === format.choiceIndex, 'aria-label': format.label, onchange: () => { data.choice = format.choiceIndex; refreshModal(); } }), el('div', { class: 'row-main' }, el('strong', {}, format.label || (format.formatHeight || format.sourceHeight ? `${format.formatHeight || format.sourceHeight}p` : format.selector)), el('p', { class: 'muted truncate' }, [format.formatWidth ? `${format.formatWidth}×${format.sourceHeight || format.formatHeight}` : '', format.extension, format.codec, format.fps ? `${format.fps} fps` : '', format.hdr ? 'HDR' : '', format.hasAudio ? t('视频+音频', 'Video + audio') : '', format.abr ? `${format.abr} kbps` : '', format.language, format.formatNote].filter(Boolean).join(' · '))), badge(format.kind === 'audio' ? t('音频', 'Audio') : t('视频', 'Video')))) : empty(t('没有可用格式', 'No available formats'), t('请返回任务日志查看解析结果。', 'Check the task log for extraction details.')))), actions: [button(t('取消', 'Cancel'), requestCloseModal), button(t('开始下载', 'Start download'), async () => { const result = await call('app', 'confirmFormatSelection', Number(data.choice)); if (result !== false && result !== undefined) dismissModal(); }, { icon: 'download', tone: 'primary', disabled: Number(data.choice) < 0 })] };
}
function collectionDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  const collection = section('collection'); const data = modal!.data;
  const filterChoices: [string, string][] = [['all', t('全部项目', 'All items')], ['selected', t('已选择', 'Selected')], ['downloadable', t('可下载', 'Downloadable')], ['completed', t('已完成', 'Completed')], ['unavailable', t('不可下载', 'Unavailable')]];
  return { size: 'wide', subtitle: `${data.title || ''} · ${t(`共 ${collection.count || 0} 项，已选 ${collection.selectedCount || 0} 项`, `${collection.count || 0} items, ${collection.selectedCount || 0} selected`)}`, body: dialogBody(el('div', { class: 'toolbar' }, input('collection-search', collection.filterText, value => updateFilter('collection', 'setFilterText', 'filterText', value), { type: 'search', placeholder: t('搜索标题、作者、链接', 'Search title, author, URL'), 'aria-label': t('搜索合集', 'Search collection') }), select('collection-status', collection.statusFilter || 'all', filterChoices, value => call('collection', 'setStatusFilter', value), { 'aria-label': t('筛选项目', 'Filter items') }), select('collection-sort', collection.sortMode || 'collection_index', [['collection_index', t('来源顺序', 'Source order')], ['title', t('标题', 'Title')], ['uploader', t('作者', 'Uploader')], ['duration', t('时长', 'Duration')], ['upload_date', t('上传日期', 'Upload date')], ['availability', t('可用状态', 'Availability')]], value => call('collection', 'setSortMode', value, Boolean(collection.sortDescending)), { 'aria-label': t('合集排序', 'Collection sort') }), check('collection-descending', t('降序', 'Descending'), collection.sortDescending, value => call('collection', 'setSortMode', collection.sortMode || 'collection_index', value))),
    !data.viewOnly ? el('div', { class: 'actions collection-selection' }, button(t('全选', 'Select all'), () => call('collection', 'setSelectionMode', 'all'), { icon: 'check' }), button(t('全不选', 'Select none'), () => call('collection', 'setSelectionMode', 'none'), { icon: 'close' }), el('span', { class: 'muted' }, t('选择作用于整个合集；筛选不会隐藏已选数量。', 'Selection applies to the whole collection; filtering keeps the total selected count.'))) : null,
    el('div', { class: 'modal-list-holder collection-list-holder' }, rows('collection').length ? virtualList('collection', rows('collection'), 'collectionIndex', 76, entry => el('div', { class: 'collection-row-content' }, check(`collection-item-${entry.collectionIndex}`, '', entry.selected, value => call('collection', 'setSelected', Number(entry.collectionIndex), value), !entry.downloadable || data.viewOnly), thumbnail(entry.thumbnailUrl, entry.entryKind === 'collection' ? 'library' : 'video'), el('div', { class: 'row-main' }, el('strong', { class: 'truncate' }, `${entry.collectionIndex}. ${entry.title || t('未命名媒体', 'Untitled media')}`), el('p', { class: `row-meta truncate ${entry.disabledReason ? 'error-text' : ''}` }, entry.disabledReason || [entry.uploader, entry.uploadDate, entry.liveStatus].filter(Boolean).join(' · ')), entry.childStatus || entry.stageText ? el('p', { class: 'row-meta' }, entry.stageText || statusLabel(entry.childStatus)) : null), el('span', { class: 'muted' }, entry.durationText || '—'), entry.completed ? badge(t('已完成', 'Completed'), 'success') : entry.entryKind === 'collection' ? badge(t('合集', 'Collection')) : null)) : empty(t('没有匹配的项目', 'No matching items'), t('调整搜索或项目筛选。', 'Adjust search or item filters.'), 'library'))), actions: [button(data.viewOnly ? t('关闭', 'Close') : t('取消', 'Cancel'), requestCloseModal), !data.viewOnly ? button(t('下载已选项目', 'Download selected items'), async () => { if (await call('app', 'confirmCollectionSelection', text(data.taskId || collection.parentTaskId)) !== false) dismissModal(); }, { icon: 'download', tone: 'primary', disabled: !collection.selectedCount }) : null] };
}
function draftValue(key: string): string { return text(modal?.data.form?.[key] ?? section('draft')[key]); }
function editDraft(key: string, value: string): void {
  const data = modal!.data; data.form ||= {}; data.form[key] = value;
  debounce('draft.update', () => call('draft', 'update', { ...data.form }));
}
function draftField(key: string, label: string, attrs: Data = {}): HTMLElement {
  return fieldLabel(label, input(`draft-${key}`, draftValue(key), value => editDraft(key, value), attrs));
}
function draftPath(key: string, label: string): HTMLElement {
  const control = input(`draft-${key}`, draftValue(key), value => editDraft(key, value), { placeholder: t('留空使用下载封面', 'Leave empty to use the downloaded cover') });
  return fieldLabel(label, el('div', { class: 'path-control' }, control, button(t('选择', 'Browse'), async () => { const path = await window.huifa.choosePath({ kind: 'file', title: label, defaultPath: control.value, filters: [{ name: t('图片', 'Images'), extensions: ['jpg', 'jpeg', 'png', 'webp', 'bmp'] }] }); if (path) { control.value = path; editDraft(key, path); } }, { icon: 'image' })));
}
function draftDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  const draft = section('draft'); const data = modal!.data; data.platformAccounts ||= {};
  const extra: Child[] = [];
  if (draft.supportsVisibility) extra.push(fieldLabel(t('YouTube 可见范围', 'YouTube visibility'), select('draft-visibility', draftValue('visibility'), [['public', t('公开', 'Public')], ['unlisted', t('不公开列出', 'Unlisted')], ['private', t('私密', 'Private')]], value => editDraft('visibility', value))));
  if (draft.supportsCollection) extra.push(draftField('collection', t('合集 / 专辑', 'Collection / album')));
  if (draft.supportsPlaylist) extra.push(draftField('playlist', t('YouTube 播放列表', 'YouTube playlist')));
  if (draft.requiresPartition) extra.push(draftField('partition', t('Bilibili 分区 ID', 'Bilibili category ID'), { inputmode: 'numeric' }));
  if (draft.supportsSchedule) extra.push(fieldLabel(t('定时发布', 'Scheduled publication'), input('draft-schedule', draftValue('schedule').replace(' ', 'T'), value => editDraft('schedule', value.replace('T', ' ')), { type: 'datetime-local' })));
  extra.push(draftPath('thumbnail', t('封面', 'Cover')));
  if (draft.supportsDualThumbnail) extra.push(draftPath('thumbnailLandscape', t('横版封面', 'Landscape cover')), draftPath('thumbnailPortrait', t('竖版封面', 'Portrait cover')));
  return { size: 'wide', subtitle: draft.mediaTitle, body: dialogBody(el('div', { class: 'source-info' }, el('p', { class: 'break-text muted' }, `${t('来源', 'Source')}: ${draft.sourceUrl || '—'}`), draft.sourceIp ? el('p', { class: 'muted' }, `${t('来源 IP', 'Source IP')}: ${draft.sourceIp}`) : null), el('div', { class: 'form-grid' }, draftField('title', t('发布标题', 'Publishing title')), draftField('topics', t('话题', 'Topics'), { placeholder: '#topic1 #topic2' }), fieldLabel(t('描述', 'Description'), el('textarea', { id: 'draft-description', rows: 4, value: draftValue('description'), oninput: (event: Event) => editDraft('description', (event.target as HTMLTextAreaElement).value) })), fieldLabel(t('来源类型', 'Source type'), select('draft-sourceType', draftValue('sourceType'), [['original', t('原创', 'Original')], ['repost', t('转载', 'Repost')]], value => editDraft('sourceType', value)))),
    el('section', { class: 'draft-platforms panel' }, el('h3', {}, t('发布平台与账号', 'Platforms and accounts')), ...(draft.platforms || []).map((platform: Data) => el('div', { class: 'draft-platform-row' }, check(`draft-platform-${platform.platform}`, platform.label, platform.selected, value => { platform.selected = value; void call('draft', 'setPlatformSelected', platform.platform, value); }), input(`draft-account-${platform.platform}`, data.platformAccounts[platform.platform] ?? platform.account, value => { data.platformAccounts[platform.platform] = value; }, { placeholder: t('账号名称', 'Account name'), 'aria-label': t(`${platform.label} 账号`, `${platform.label} account`), onblur: (event: Event) => call('draft', 'setPlatformAccount', platform.platform, (event.target as HTMLInputElement).value) })))), el('div', { class: 'form-grid' }, ...extra), draft.lastMessage ? el('p', { class: 'notice danger' }, draft.lastMessage) : null), actions: [button(t('取消', 'Cancel'), requestCloseModal), button(t('保存并加入发布队列', 'Save to publishing queue'), async () => { clearTimeout(timers.get('draft.update')); await call('draft', 'update', data.form || {}); for (const [platform, account] of Object.entries(data.platformAccounts)) await call('draft', 'setPlatformAccount', platform, text(account)); await call('app', 'submitPublishDraft'); }, { icon: 'send', tone: 'primary', disabled: !(draft.platforms || []).some((platform: Data) => platform.selected) })] };
}
function coverDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  const cover = section('cover'); const data = modal!.data;
  if (data.outputPath == null) data.outputPath = cover.defaultOutputPath;
  if (data.prompt == null) data.prompt = t('保留原始主体和品牌细节，改善构图、光线、清晰度和点击吸引力，不添加误导性文字或人物。', 'Preserve the subject and brand details. Improve composition, lighting, clarity, and appeal without misleading text or people.');
  const image = el('img', { src: cover.previewUrl || '', alt: t('封面预览', 'Cover preview'), draggable: 'false' });
  const preview = el('div', { class: `cover-preview ${cover.fitMode === 'crop' ? 'crop' : ''}` }, cover.previewUrl ? image : empty(t('正在生成封面预览…', 'Preparing cover preview…'), '', 'image'), el('span', { class: 'cover-hint' }, cover.fitMode === 'crop' ? t('单击图像调整裁切焦点', 'Click the image to set crop focus') : t('留白模式保留完整图像', 'Padding preserves the full image')));
  preview.addEventListener('click', async event => {
    if (cover.fitMode !== 'crop' || cover.busy || !image.naturalWidth || !image.naturalHeight) return;
    const bounds = image.getBoundingClientRect();
    const scale = Math.min(bounds.width / image.naturalWidth, bounds.height / image.naturalHeight);
    const width = image.naturalWidth * scale; const height = image.naturalHeight * scale;
    const x = Math.max(0, Math.min(100, (event.clientX - bounds.left - (bounds.width - width) / 2) / width * 100));
    const y = Math.max(0, Math.min(100, (event.clientY - bounds.top - (bounds.height - height) / 2) / height * 100));
    await call('cover', 'setFocusX', x); await call('cover', 'setFocusY', y);
  });
  const range = (key: string, label: string) => fieldLabel(`${label} · ${Math.round(cover[key] || 0)}%`, input(`cover-${key}`, cover[key], value => { const output = document.getElementById(`cover-${key}-output`); if (output) output.textContent = `${Math.round(Number(value))}%`; }, { type: 'range', min: 0, max: 100, disabled: cover.fitMode !== 'crop' || cover.busy, onchange: (event: Event) => call('cover', key === 'focusX' ? 'setFocusX' : 'setFocusY', Number((event.target as HTMLInputElement).value)) }));
  const output = input('cover-output', data.outputPath, value => { data.outputPath = value; });
  return { size: 'cover-dialog wide', subtitle: cover.title || t('本地预览与 JPEG 导出保留原始下载文件。', 'Local preview and JPEG export preserve the original download.'), body: dialogBody(el('div', { class: 'cover-layout' }, preview, el('div', { class: 'cover-controls' },
    fieldLabel(t('封面预设', 'Cover preset'), select('cover-preset', cover.preset, (cover.presets || []).map((preset: Data) => [preset.id, preset.label]), value => call('cover', 'setPreset', value), { disabled: cover.busy })), fieldLabel(t('适配方式', 'Fit mode'), select('cover-fit', cover.fitMode, [['crop', t('裁切填满', 'Crop to fill')], ['pad', t('全图留白', 'Fit with padding')]], value => call('cover', 'setFitMode', value), { disabled: cover.busy })), range('focusX', t('裁切焦点 X', 'Crop focus X')), range('focusY', t('裁切焦点 Y', 'Crop focus Y')),
    fieldLabel(t('JPEG 质量', 'JPEG quality'), input('cover-quality', cover.quality, () => {}, { type: 'number', min: 50, max: 100, disabled: cover.busy, onchange: (event: Event) => call('cover', 'setQuality', Number((event.target as HTMLInputElement).value)) })), button(t('保存裁切与质量设置', 'Save crop and quality defaults'), () => call('cover', 'saveOptions'), { icon: 'check', disabled: !cover.active || cover.busy }),
    el('hr'), fieldLabel(t('JPEG 输出路径', 'JPEG output path'), el('div', { class: 'path-control' }, output, button(t('选择', 'Browse'), async () => { const path = await window.huifa.choosePath({ kind: 'save', title: t('导出 JPEG 封面', 'Export JPEG cover'), defaultPath: output.value || cover.defaultOutputPath, filters: [{ name: 'JPEG', extensions: ['jpg', 'jpeg'] }] }); if (path) { data.outputPath = path; output.value = path; } }, { icon: 'folder', disabled: cover.busy }))), button(t('导出 JPEG', 'Export JPEG'), () => call('cover', 'saveJpeg', output.value), { icon: 'download', tone: 'primary', disabled: !cover.active || cover.busy }),
    el('hr'), fieldLabel(t('AI 封面提示词', 'AI cover prompt'), el('textarea', { id: 'cover-prompt', rows: 4, value: data.prompt, disabled: cover.busy, oninput: (event: Event) => { data.prompt = (event.target as HTMLTextAreaElement).value; } })), button(cover.busy ? t('取消 AI 创作', 'Cancel AI creation') : t('生成 AI 变体', 'Generate AI variation'), () => cover.busy ? call('cover', 'cancelGeneration') : requestCoverAction('generate', text(data.prompt)), { icon: cover.busy ? 'close' : 'image', disabled: !cover.active, tone: cover.busy ? '' : 'primary' }))), el('p', { class: 'notice', role: 'status' }, cover.status || '')), actions: [button(t('恢复原始封面', 'Restore original cover'), () => requestCoverAction('restoreOriginal'), { icon: 'refresh', disabled: cover.busy || !cover.active }), button(t('复制封面', 'Copy cover'), () => call('cover', 'copyCover'), { icon: 'copy', disabled: cover.busy || !cover.active }), el('span', { class: 'footer-spacer' }), button(cover.busy ? t('取消创作', 'Cancel creation') : t('关闭', 'Close'), requestCloseModal)] };
}
async function openLog(taskId: string): Promise<void> {
  openModal('log', { taskId, content: t('正在读取日志…', 'Loading log…') });
  const content = await call('app', 'taskLogText', taskId);
  if (modal?.kind === 'log' && modal.data.taskId === taskId) { modal.data.content = text(content); refreshModal(); }
}
async function reloadLog(): Promise<void> {
  if (modal?.kind !== 'log') return;
  const taskId = modal.data.taskId; const content = await call('app', 'taskLogText', taskId);
  if (modal?.kind === 'log' && modal.data.taskId === taskId) { modal.data.content = text(content); refreshModal(); }
}
function logDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  const data = modal!.data;
  return { size: 'wide', subtitle: data.taskId, body: dialogBody(el('pre', { class: 'log-text', tabindex: 0 }, data.content || t('没有日志内容', 'No log entries'))), actions: [button(t('打开日志文件夹', 'Open log folder'), () => call('app', 'openTaskLogLocation', data.taskId), { icon: 'folder' }), button(t('复制', 'Copy'), () => call('app', 'copyText', text(data.content)), { icon: 'copy' }), button(t('清空日志', 'Clear log'), async () => { await call('app', 'clearTaskLog', data.taskId); await reloadLog(); }, { tone: 'danger' }), el('span', { class: 'footer-spacer' }), button(t('刷新', 'Refresh'), reloadLog, { icon: 'refresh' }), button(t('关闭', 'Close'), requestCloseModal)] };
}
async function openCookies(): Promise<void> { openModal('cookies', { query: '', includeValues: false, entries: [] }); await reloadCookies(); }
async function reloadCookies(): Promise<void> {
  if (modal?.kind !== 'cookies') return;
  const data = modal.data; const entries = await call('app', 'embeddedCookies', text(data.query), Boolean(data.includeValues));
  if (modal?.kind === 'cookies' && modal.data === data) { data.entries = Array.isArray(entries) ? entries : []; refreshModal(); }
}
function cookieDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  const data = modal!.data;
  return { size: 'wide', subtitle: t(`${data.entries.length} 条 Cookie · 敏感值默认隐藏`, `${data.entries.length} cookies · Values hidden by default`), body: dialogBody(el('div', { class: 'toolbar' }, input('cookie-search', data.query, value => { data.query = value; debounce('cookie-search', reloadCookies); }, { type: 'search', placeholder: t('搜索域名、名称、路径', 'Search domain, name, path'), 'aria-label': t('搜索 Cookie', 'Search cookies') }), check('cookie-show-values', t('显示敏感值', 'Show sensitive values'), data.includeValues, value => { data.includeValues = value; void reloadCookies(); })), data.includeValues ? el('p', { class: 'notice danger' }, t('Cookie 值等同于登录凭据。请勿截图或分享到聊天。', 'Cookie values are sign-in credentials. Avoid screenshots or sharing them.')) : null, el('div', { class: 'modal-list-holder cookie-list-holder' }, data.entries.length ? virtualList('cookies', data.entries, 'name', 104, entry => el('div', { class: 'cookie-row' }, el('strong', {}, `${entry.domain} · ${entry.name}`), el('code', { class: 'break-text' }, entry.value), el('small', { class: 'muted' }, `${t('路径', 'Path')} ${entry.path} · ${t('过期', 'Expires')} ${entry.expires} · Secure ${entry.secure} · HttpOnly ${entry.httpOnly} · SameSite ${entry.sameSite}`))) : empty(t('没有匹配的 Cookie', 'No matching cookies'), t('完成内置浏览器登录后可在这里查看。', 'App-managed sign-in cookies will appear here.'), 'accounts'))), actions: [button(t('刷新', 'Refresh'), reloadCookies, { icon: 'refresh' }), button(t('关闭', 'Close'), requestCloseModal)] };
}
function siteDialog(): { body: HTMLElement; actions: Child[]; subtitle: string } {
  const sites = section('sites');
  return { subtitle: t(`当前 yt-dlp 提供 ${sites.totalCount || 0} 个提取器`, `${sites.totalCount || 0} extractors in the current yt-dlp`), body: dialogBody(input('site-search', sites.filterText, value => updateFilter('sites', 'setFilterText', 'filterText', value), { type: 'search', placeholder: t('搜索 bilibili、youtube、instagram…', 'Search bilibili, youtube, instagram…'), 'aria-label': t('搜索网站', 'Search sites') }), sites.error ? el('p', { class: 'notice danger' }, sites.error) : null, el('div', { class: 'modal-list-holder sites-holder' }, rows('sites').length ? virtualList('sites', rows('sites'), 'name', 36, row => el('div', { class: 'site-row' }, row.name)) : empty(sites.loaded ? t('没有匹配的网站', 'No matching sites') : t('载入网站列表…', 'Loading supported sites…'), '', 'search'))), actions: [button(t('关闭', 'Close'), requestCloseModal)] };
}
function routeDialog(): { body: HTMLElement; actions: Child[]; subtitle: string; size: string } {
  ensureSettingsDraft(); const routes = section('routes'); const data = modal!.data;
  const custom = text(settingsDraft!.github_mirror_urls).split(/[\n\r,;|]+/).map(value => value.trim()).filter(Boolean);
  let profiles: Data = {}; try { profiles = JSON.parse(text(settingsDraft!.github_route_profiles) || '{}'); } catch { /* Saved probe profiles may come from older releases. */ }
  const routeRows = [{ id: 'auto', name: t('自动选择', 'Automatic selection'), url: t('根据元数据与附件测试结果择优', 'Choose by metadata and asset connectivity') }, ...rows('routes')];
  return { size: 'wide', subtitle: t('为更新检查与组件下载选择线路，并查看实际连通性。', 'Select routes for update checks and component downloads, with actual connectivity results.'), body: dialogBody(el('div', { class: 'routes-table' }, el('div', { class: 'route-row table-heading' }, el('span', {}, t('线路', 'Route')), el('span', {}, t('元数据', 'Metadata')), el('span', {}, t('附件', 'Assets')), el('span', {}, t('测试结果', 'Probe result'))), ...routeRows.map(route => {
    const saved = profiles[route.id] || {}; const info = { ...saved, ...route };
    const capability = (key: string, latency: string) => info[key] === true ? t(`可用 ${info[latency] || 0} ms`, `Available ${info[latency] || 0} ms`) : info[key] === false ? t('不可用', 'Unavailable') : '—';
    return el('div', { class: 'route-row' }, el('label', { class: 'route-name' }, el('input', { type: 'radio', name: 'route', checked: (settingsDraft!.github_download_route || 'auto') === route.id, onchange: () => editSetting('github_download_route', route.id) }), el('span', {}, el('strong', {}, route.name), el('small', { class: 'break-text muted' }, route.url), info.thirdParty || info.third_party ? el('small', {}, t('第三方线路', 'Third-party route')) : null)), el('span', { class: info.metadata_ok === false ? 'error-text' : '' }, capability('metadata_ok', 'metadata_latency_ms')), el('span', { class: info.asset_ok === false ? 'error-text' : '' }, capability('asset_ok', 'asset_latency_ms')), el('span', { class: 'route-status break-text muted', title: info.error || '' }, info.error || info.status || t('尚未测试', 'Not tested')));
  })), el('section', { class: 'custom-routes' }, el('h3', {}, t('自定义镜像', 'Custom mirrors')), ...custom.map((url, index) => el('div', { class: 'path-control custom-route' }, el('code', { class: 'break-text' }, url), button(t('移除', 'Remove'), () => { editSetting('github_mirror_urls', custom.filter((_, i) => i !== index).join('\n')); refreshModal(); }, { icon: 'trash', tone: 'subtle' }))), el('div', { class: 'path-control' }, input('route-new-url', data.newUrl, value => { data.newUrl = value; }, { type: 'url', placeholder: 'https://mirror.example.com/', 'aria-label': t('镜像地址', 'Mirror URL') }), button(t('添加镜像', 'Add mirror'), () => { const url = text(data.newUrl).trim(); try { const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol)) throw new Error(); } catch { toast(t('请输入完整的 HTTP / HTTPS 地址', 'Enter a full HTTP / HTTPS URL'), true); return; } if (custom.length >= 12) { toast(t('最多保存 12 个自定义镜像', 'Up to 12 custom mirrors'), true); return; } if (!custom.includes(url)) editSetting('github_mirror_urls', [...custom, url].join('\n')); data.newUrl = ''; refreshModal(); }, { icon: 'plus' }))), el('p', { class: 'muted' }, t('保存并测试会应用当前线路与自定义镜像；测试结果由后端安全保存。', 'Save and test applies the current route and mirrors; the backend stores probe results.'))), actions: [button(t('关闭', 'Close'), requestCloseModal), button(routes.busy ? t('测试中…', 'Testing…') : t('保存并测试线路', 'Save and test routes'), async () => { if (await saveSettings()) await call('routes', 'probe'); }, { icon: 'refresh', disabled: routes.busy }), button(t('保存线路', 'Save routes'), async () => { if (await saveSettings()) { dismissModal(); render(); } }, { icon: 'check', tone: 'primary' })] };
}
function convertDialog(): { body: HTMLElement; actions: Child[]; subtitle: string } {
  const data = modal!.data; if (data.encoder === 'original') data.encoder = 'libx264';
  return { subtitle: t('从已保存视频执行转换；来源文件由后端保留。', 'Convert the saved video; the backend retains the source.'), body: dialogBody(fieldLabel(t('输出编码器', 'Output encoder'), select('convert-encoder', data.encoder, encoderChoices.filter(([value]) => value !== 'original').map(([value, pair]) => [value, t(...pair)]), value => { data.encoder = value; }))), actions: [button(t('取消', 'Cancel'), requestCloseModal), button(t('开始转换', 'Start conversion'), async () => { await call('app', 'convertTask', data.taskId, text(data.encoder)); dismissModal(); }, { icon: 'refresh', tone: 'primary' })] };
}

function applyState(snapshot: Record<string, Data>): void {
  if (!snapshot || typeof snapshot !== 'object') return;
  for (const [name, value] of Object.entries(snapshot)) {
    if (!value || typeof value !== 'object') continue;
    state[name] = { ...state[name], ...value };
  }
  loaded = true;
  if (snapshot.settings && !settingsDirty) { settingsDraft = null; optionsDraft = null; }
  const selectedTask = rows('tasks').find(row => text(row.taskId) === selected.tasks);
  if (selectedTask) selectedCache.tasks = selectedTask;
  if (modal) {
    const relevant: Record<string, string> = { format: 'formats', collection: 'collection', draft: 'draft', cover: 'cover', sites: 'sites', routes: 'routes' };
    if (snapshot[relevant[modal.kind]]) modalNeedsRender = true;
    if (modal.kind === 'format' && snapshot.formats) {
      if (snapshot.formats.pending) modal.data.seenPending = true;
      else if (modal.data.seenPending) dismissModal();
    }
    if (modal?.kind === 'draft' && snapshot.draft) {
      if (snapshot.draft.active) modal.data.seenActive = true;
      else if (modal.data.seenActive) dismissModal();
    }
  }
  const message = text(section('app').message);
  if (message && message !== lastMessage) { lastMessage = message; toast(message, /失败|错误|failed|error/i.test(message)); }
  scheduleRender();
}
function applyTasksDelta(delta: Data): void {
  const changes: Data[] = Array.isArray(delta.rows) ? delta.rows : [];
  const visible = new Map(rows('tasks').map(row => [text(row.taskId), row]));
  for (const row of changes) {
    const id = text(row.taskId);
    if (visible.has(id)) Object.assign(visible.get(id)!, row);
    if (selected.tasks === id) selectedCache.tasks = { ...selectedCache.tasks, ...row };
  }
  if (Array.isArray(delta.recentCompleted)) section('tasks').recentCompleted = delta.recentCompleted;
  if (page !== 'downloads') return;
  const changedIds = new Set(changes.map(row => text(row.taskId)));
  document.querySelectorAll<HTMLElement>('#list-tasks .list-row[data-key]').forEach(node => {
    const id = node.dataset.key!; const row = visible.get(id);
    if (!row || !changedIds.has(id)) return;
    const content = taskRow(row); node.replaceChildren(...Array.from(content.childNodes)); node.title = content.title;
  });
  const detail = document.getElementById('task-detail-panel');
  if (detail && changedIds.has(selected.tasks)) preserve(detail, () => { const next = taskDetail(currentRow('tasks', 'taskId')); detail.replaceChildren(...Array.from(next.childNodes)); });
  else if (Array.isArray(delta.recentCompleted)) document.getElementById('recent-completed')?.replaceWith(recentCompleted());
}
const lifecycleQueue: { kind: string; data: Data }[] = [];
function lifecycleDialog(kind: string, data: Data): void { if (modal) lifecycleQueue.push({ kind, data }); else openModal(kind, data); }
function handleEvent(event: HostEvent): void {
  const data = event.data;
  if (event.event === 'state') { applyState(data || {}); return; }
  if (event.event === 'tasksDelta') { applyTasksDelta(data || {}); return; }
  const args = Array.isArray(data) ? data : [];
  if (event.event === 'ready') { void call('host', 'snapshot').then(snapshot => snapshot && applyState(snapshot)); }
  else if (event.event === 'formatSelection') openModal('format');
  else if (event.event === 'collectionSelection' || event.event === 'collectionDetails') {
    openModal('collection', { taskId: text(args[0]), title: text(args[1]), viewOnly: event.event === 'collectionDetails' });
    void call('collection', 'setFilterText', ''); void call('app', 'showCollectionEntries', text(args[0]));
  } else if (event.event === 'publishEditor') openModal('draft');
  else if (event.event === 'publishEditorCompleted') { if (modal?.kind === 'draft') dismissModal(); }
  else if (event.event === 'coverStudio') openModal('cover');
  else if (event.event === 'navigation') go((['downloads', 'accounts', 'completed', 'publish', 'settings', 'about'] as Page[])[Number(args[0])] || 'downloads');
  else if (event.event === 'taskFocus') {
    selected.tasks = text(args[0]); page = 'downloads';
    void call('tasks', 'setStatusFilter', 'all'); void call('tasks', 'setFilterText', text(args[0])); render();
  } else if (event.event === 'publishTaskFocus') {
    selected.publish = text(args[0]); page = 'publish';
    void call('publish', 'setMediaId', 0); void call('publish', 'setSearchText', text(args[0])); render();
  } else if (event.event === 'localHistoryImport') lifecycleDialog('history', { folder: text(args[0]), count: Number(args[1]) });
  else if (event.event === 'databaseRecovery') lifecycleDialog('recovery', { title: text(args[0]), summary: text(args[1]), detail: text(args[2]) });
  else if (event.event === 'backendDisconnected') { loaded = false; backendDisconnected = true; reportError(data?.message || t('后台服务已断开，请重新启动应用。', 'Backend disconnected. Restart the app.')); render(); }
  else if (event.event === 'backendError') { reportError(data?.message || data?.error || t('后台服务异常', 'Backend service error')); }
}
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && /^[1-6]$/.test(event.key) && !modal) {
    event.preventDefault(); go((['downloads', 'accounts', 'completed', 'publish', 'settings', 'about'] as Page[])[Number(event.key) - 1]);
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f' && !modal) {
    const id = page === 'downloads' ? 'task-search' : page === 'completed' ? 'media-search' : page === 'publish' ? 'publish-search' : '';
    if (id) { event.preventDefault(); document.getElementById(id)?.focus(); }
  }
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if ((section('settings').values?.appearance_theme || 'system') === 'system') render(); });
setInterval(() => { if (!modal && lifecycleQueue.length) { const next = lifecycleQueue.shift()!; openModal(next.kind, next.data); } }, 250);
verifyListInvariants();
window.huifa.subscribe(handleEvent);
render();
void call('host', 'snapshot').then(snapshot => { if (snapshot) {
  applyState(snapshot);
  if (section('formats').pending && !modal) openModal('format', { seenPending: true });
  else if (section('draft').active && !modal) openModal('draft', { seenActive: true });
  else if (section('cover').active && !modal) openModal('cover');
} });
