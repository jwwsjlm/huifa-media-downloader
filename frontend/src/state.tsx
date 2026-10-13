import { createContext, useContext, useReducer, type Dispatch, type ReactNode } from 'react';
import type { CoreEvent, DownloadOptions, Json, Media, Page, Settings, Snapshot, Task, Tool } from './types';

export interface ViewState { page: Page; urls: string; taskSearch: string; taskStatus: string; taskSort: string; taskSelection: string; mediaSearch: string; mediaSelection: string; settingsDraft: Settings | null; optionsDraft: DownloadOptions | null; settingsTab: string }
export interface AppState { loaded: boolean; connected: boolean; version: string; tasks: Record<string, Task>; taskIds: string[]; media: Record<string, Media>; mediaIds: string[]; settings: Settings; tools: Tool[]; view: ViewState }
export const initialState: AppState = {
  loaded: false, connected: false, version: '', tasks: {}, taskIds: [], media: {}, mediaIds: [], settings: {}, tools: [],
  view: { page: 'downloads', urls: '', taskSearch: '', taskStatus: 'all', taskSort: 'newest', taskSelection: '', mediaSearch: '', mediaSelection: '', settingsDraft: null, optionsDraft: null, settingsTab: 'downloads' },
};
export type Action = { type: 'snapshot'; snapshot: Snapshot } | { type: 'event'; event: CoreEvent } | { type: 'connection'; connected: boolean } | { type: 'view'; patch: Partial<ViewState> } | { type: 'setting'; key: string; value: Json } | { type: 'option'; key: string; value: DownloadOptions[string] } | { type: 'saved'; settings: Settings };
export function reducer(state: AppState, action: Action): AppState {
  if (action.type === 'connection') return { ...state, connected: action.connected };
  if (action.type === 'view') return { ...state, view: { ...state.view, ...action.patch } };
  if (action.type === 'setting') return { ...state, view: { ...state.view, settingsDraft: { ...(state.view.settingsDraft || state.settings), [action.key]: action.value } } };
  if (action.type === 'option') return { ...state, view: { ...state.view, optionsDraft: { ...(state.view.optionsDraft || state.settings.downloadOptions || {}), [action.key]: action.value } } };
  if (action.type === 'saved') return { ...state, settings: action.settings, view: { ...state.view, settingsDraft: null, optionsDraft: null } };
  if (action.type === 'snapshot') {
    const tasks = Object.fromEntries(action.snapshot.tasks.map(task => [String(task.id), task]));
    const media = Object.fromEntries(action.snapshot.media.map(item => [String(item.id), item]));
    const taskIds = action.snapshot.tasks.map(task => String(task.id)); const mediaIds = action.snapshot.media.map(item => String(item.id));
    return { ...state, ...action.snapshot, tasks, taskIds, media, mediaIds, view: { ...state.view, taskSelection: state.view.taskSelection || taskIds[0] || '', mediaSelection: state.view.mediaSelection || mediaIds[0] || '' }, loaded: true, connected: true };
  }
  const { kind, data } = action.event;
  if (kind === 'task.upsert') {
    const task = data as unknown as Task; const id = String(task.id);
    if (!id || id === 'undefined') return state;
    return { ...state, tasks: { ...state.tasks, [id]: { ...state.tasks[id], ...task, id } }, taskIds: state.tasks[id] ? state.taskIds : [...state.taskIds, id], view: state.view.taskSelection ? state.view : { ...state.view, taskSelection: id } };
  }
  if (kind === 'task.remove') {
    const id = typeof data === 'string' ? data : String((data as { id: string }).id);
    const tasks = { ...state.tasks }; delete tasks[id];
    return { ...state, tasks, taskIds: state.taskIds.filter(value => value !== id), view: state.view.taskSelection === id ? { ...state.view, taskSelection: '' } : state.view };
  }
  if (kind === 'media.upsert') {
    const item = data as unknown as Media; const id = String(item.id);
    return { ...state, media: { ...state.media, [id]: { ...state.media[id], ...item, id } }, mediaIds: state.media[id] ? state.mediaIds : [...state.mediaIds, id], view: state.view.mediaSelection ? state.view : { ...state.view, mediaSelection: id } };
  }
  if (kind === 'settings.updated') return { ...state, settings: data as Settings };
  if (kind === 'tools.updated') return { ...state, tools: Array.isArray(data) ? data as unknown as Tool[] : state.tools };
  return state;
}
const Context = createContext<{ state: AppState; dispatch: Dispatch<Action> } | null>(null);
export function AppProvider({ children }: { children: ReactNode }) { const [state, dispatch] = useReducer(reducer, initialState); return <Context.Provider value={{ state, dispatch }}>{children}</Context.Provider>; }
export function useApp() { const value = useContext(Context); if (!value) throw new Error('AppProvider is missing'); return value; }
