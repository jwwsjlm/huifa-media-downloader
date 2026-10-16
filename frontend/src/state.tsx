import { createContext, useContext, useReducer, type Dispatch, type ReactNode } from 'react';
import type { CoreEvent, DownloadOptions, Json, Media, Page, Settings, Snapshot, Task, Tool } from './types';

export interface ViewState { page: Page; urls: string; quality: string | null; taskSearch: string; taskStatus: string; taskSort: string; taskSelection: string; mediaSearch: string; mediaSelection: string; settingsDraft: Settings | null; optionsDraft: DownloadOptions | null; settingsTab: string }
export interface AppState { loaded: boolean; connected: boolean; version: string; tasks: Record<string, Task>; taskIds: string[]; taskListVersion: number; activeTaskCount: number; media: Record<string, Media>; mediaIds: string[]; mediaByTask: Record<string, string>; settings: Settings; tools: Tool[]; view: ViewState }
export const initialState: AppState = {
  loaded: false, connected: false, version: '', tasks: {}, taskIds: [], taskListVersion: 0, activeTaskCount: 0, media: {}, mediaIds: [], mediaByTask: {}, settings: {}, tools: [],
  view: { page: /^\/mcp\/?$/.test(window.location.pathname) ? 'mcp' : 'downloads', urls: '', quality: null, taskSearch: '', taskStatus: 'all', taskSort: 'newest', taskSelection: '', mediaSearch: '', mediaSelection: '', settingsDraft: null, optionsDraft: null, settingsTab: 'downloads' },
};
export type Action = { type: 'snapshot'; snapshot: Snapshot } | { type: 'event'; event: CoreEvent } | { type: 'connection'; connected: boolean } | { type: 'view'; patch: Partial<ViewState> } | { type: 'setting'; key: string; value: Json } | { type: 'option'; key: string; value: DownloadOptions[string] } | { type: 'saved'; settings: Settings } | { type: 'cookie.saved'; id: string };
const active = (task?: Task): number => task && ['downloading', 'probing', 'processing'].includes(task.status) ? 1 : 0;
export function reducer(state: AppState, action: Action): AppState {
  if (action.type === 'connection') return { ...state, connected: action.connected };
  if (action.type === 'view') return { ...state, view: { ...state.view, ...action.patch } };
  if (action.type === 'setting') return { ...state, view: { ...state.view, settingsDraft: { ...(state.view.settingsDraft || state.settings), [action.key]: action.value } } };
  if (action.type === 'option') return { ...state, view: { ...state.view, optionsDraft: { ...(state.view.optionsDraft || state.settings.downloadOptions || {}), [action.key]: action.value } } };
  if (action.type === 'saved') return { ...state, settings: action.settings, view: { ...state.view, settingsDraft: null, optionsDraft: null, quality: null } };
  if (action.type === 'cookie.saved') return { ...state, settings: { ...state.settings, downloadOptions: { ...state.settings.downloadOptions, cookieId: action.id } }, view: { ...state.view, optionsDraft: state.view.optionsDraft ? { ...state.view.optionsDraft, cookieId: action.id } : null } };
  if (action.type === 'snapshot') {
    const tasks = Object.fromEntries(action.snapshot.tasks.map(task => [String(task.id), task]));
    const media = Object.fromEntries(action.snapshot.media.map(item => [String(item.id), item]));
    const taskIds = action.snapshot.tasks.map(task => String(task.id)); const mediaIds = action.snapshot.media.map(item => String(item.id));
    return { ...state, ...action.snapshot, tasks, taskIds, taskListVersion: state.taskListVersion + 1, activeTaskCount: action.snapshot.tasks.reduce((count, task) => count + active(task), 0), media, mediaIds, mediaByTask: Object.fromEntries(action.snapshot.media.map(item => [item.taskId, item.id])), view: { ...state.view, taskSelection: state.view.taskSelection || taskIds[0] || '', mediaSelection: state.view.mediaSelection || mediaIds[0] || '' }, loaded: true, connected: true };
  }
  const { kind, data } = action.event;
  if (kind === 'task.upsert') {
    const task = data as unknown as Task; const id = String(task.id);
    if (!id || id === 'undefined') return state;
    const previous = state.tasks[id]; const next = { ...previous, ...task, id };
    const listChanged = !previous || (['title', 'url', 'status', 'createdAt'] as const).some(key => previous[key] !== next[key]);
    return { ...state, tasks: { ...state.tasks, [id]: next }, taskIds: previous ? state.taskIds : [...state.taskIds, id], taskListVersion: state.taskListVersion + Number(listChanged), activeTaskCount: state.activeTaskCount + active(next) - active(previous), view: state.view.taskSelection ? state.view : { ...state.view, taskSelection: id } };
  }
  if (kind === 'task.remove') {
    const id = typeof data === 'string' ? data : String((data as { id: string }).id);
    const tasks = { ...state.tasks }; delete tasks[id];
    return { ...state, tasks, taskIds: state.taskIds.filter(value => value !== id), taskListVersion: state.taskListVersion + 1, activeTaskCount: state.activeTaskCount - active(state.tasks[id]), view: state.view.taskSelection === id ? { ...state.view, taskSelection: '' } : state.view };
  }
  if (kind === 'media.upsert') {
    const item = data as unknown as Media; const id = String(item.id);
    const next = { ...state.media[id], ...item, id }; const mediaByTask = { ...state.mediaByTask };
    const previous = state.media[id];
    if (previous && previous.taskId !== next.taskId && mediaByTask[previous.taskId] === id) delete mediaByTask[previous.taskId];
    mediaByTask[next.taskId] = id;
    return { ...state, media: { ...state.media, [id]: next }, mediaIds: state.media[id] ? state.mediaIds : [...state.mediaIds, id], mediaByTask, view: state.view.mediaSelection ? state.view : { ...state.view, mediaSelection: id } };
  }
  if (kind === 'media.remove') {
    const id = String((data as { id: string }).id); const item = state.media[id];
    if (!item) return state;
    const media = { ...state.media }; delete media[id];
    const mediaByTask = { ...state.mediaByTask };
    if (mediaByTask[item.taskId] === id) delete mediaByTask[item.taskId];
    return { ...state, media, mediaIds: state.mediaIds.filter(value => value !== id), mediaByTask, view: state.view.mediaSelection === id ? { ...state.view, mediaSelection: '' } : state.view };
  }
  if (kind === 'settings.updated') {
    const settings = data as Settings;
    const changedCookie = settings.downloadOptions?.cookieId !== state.settings.downloadOptions?.cookieId;
    return { ...state, settings, view: changedCookie && state.view.optionsDraft ? { ...state.view, optionsDraft: { ...state.view.optionsDraft, cookieId: settings.downloadOptions?.cookieId ?? 'auto' } } : state.view };
  }
  if (kind === 'tools.updated') return { ...state, tools: Array.isArray(data) ? data as unknown as Tool[] : state.tools };
  return state;
}
const Context = createContext<{ state: AppState; dispatch: Dispatch<Action> } | null>(null);
export function AppProvider({ children }: { children: ReactNode }) { const [state, dispatch] = useReducer(reducer, initialState); return <Context.Provider value={{ state, dispatch }}>{children}</Context.Provider>; }
export function useApp() { const value = useContext(Context); if (!value) throw new Error('AppProvider is missing'); return value; }
