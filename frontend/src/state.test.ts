import { describe, expect, it } from 'vitest';
import { initialState, reducer } from './state';
import { taskProgress } from './task';
import { makeTask, makeSnapshot } from './test-fixtures';
import { version as buildVersion } from '../package.json';
describe('task delta state', () => {
  it('does not select tasks on load, background updates, reconnect or removal', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.view.taskSelection).toBe('');
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { ...makeTask('c') } } });
    expect(state.view.taskSelection).toBe('');
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'a', progress: 50 } } });
    expect(state.view.taskSelection).toBe('');
    state = reducer(state, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.view.taskSelection).toBe('');
    state = reducer(state, { type: 'view', patch: { taskSelection: 'a' } });
    state = reducer(state, { type: 'event', event: { kind: 'task.remove', data: { id: 'a' } } });
    expect(state.view.taskSelection).toBe('');
    state = reducer(state, { type: 'snapshot', snapshot: { ...makeSnapshot(), tasks: [makeTask('b')] } });
    expect(state.view.taskSelection).toBe('');
  });
  it('starts at the packaged version and accepts the connected runtime version', () => {
    expect(initialState.version).toBe(buildVersion);
    const snapshot = makeSnapshot(); snapshot.version = '1.2.3';
    expect(reducer(initialState, { type: 'snapshot', snapshot }).version).toBe('1.2.3');
  });
  it('preserves runtime tools when a fast snapshot omits them', () => {
    const snapshot = makeSnapshot(); snapshot.tools = [{ name: 'qjs', available: true, version: '0.17.0' }];
    const state = reducer(initialState, { type: 'snapshot', snapshot });
    const fast = makeSnapshot(); delete fast.tools;
    const refreshed = reducer(state, { type: 'snapshot', snapshot: fast });
    expect(refreshed.tools).toBe(state.tools); expect(refreshed.loaded).toBe(true);
  });
  it('enables automatic cookies from a background sync while preserving unrelated drafts and task snapshots', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    state = reducer(state, { type: 'view', patch: { settingsDraft: { uiLanguage: 'zh-CN' }, optionsDraft: { cookieId: '', quality: '720p', proxy: 'draft-proxy' } } });
    const previous = state;
    state = reducer(state, { type: 'event', event: { kind: 'settings.updated', data: { ...state.settings, downloadOptions: { ...state.settings.downloadOptions, cookieId: 'auto' } } } });
    expect(state.settings.downloadOptions?.cookieId).toBe('auto');
    expect(state.view.optionsDraft).toEqual({ cookieId: 'auto', quality: '720p', proxy: 'draft-proxy' });
    expect(state.view.settingsDraft).toBe(previous.view.settingsDraft);
    expect(state.tasks).toBe(previous.tasks);
  });
  it('enables login cookies without overwriting other drafts or existing task identities', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    state = reducer(state, { type: 'view', patch: { settingsDraft: { uiLanguage: 'zh-CN' }, optionsDraft: { cookieId: 'old', quality: '720p', proxy: 'draft-proxy' } } });
    const previous = state;
    state = reducer(state, { type: 'cookie.saved', id: 'new' });
    expect(state.settings.downloadOptions?.cookieId).toBe('new');
    expect(state.view.optionsDraft).toEqual({ cookieId: 'new', quality: '720p', proxy: 'draft-proxy' });
    expect(state.view.settingsDraft).toBe(previous.view.settingsDraft);
    expect(state.settings.uiLanguage).toBe(previous.settings.uiLanguage);
    expect(state.tasks).toBe(previous.tasks);
    const clean = reducer(initialState, { type: 'cookie.saved', id: 'new' });
    expect(clean.view.optionsDraft).toBeNull();
  });
  it('removes media in another client without removing retained results on task removal', () => {
    const snapshot = makeSnapshot();
    snapshot.media = [{ id: 'm', taskId: 'a', title: 'Media', filename: 'a.mp4', size: 12, duration: 1 }];
    let state = reducer(initialState, { type: 'snapshot', snapshot });
    state = reducer(state, { type: 'view', patch: { mediaSelection: 'm', mediaSearch: 'draft' } });
    state = reducer(state, { type: 'event', event: { kind: 'task.remove', data: { id: 'a' } } });
    expect(state.media.m).toBeDefined();
    state = reducer(state, { type: 'event', event: { kind: 'media.remove', data: { id: 'm' } } });
    expect(state.media.m).toBeUndefined(); expect(state.mediaIds).toEqual([]); expect(state.mediaByTask.a).toBeUndefined();
    expect(state.view.mediaSelection).toBe(''); expect(state.view.mediaSearch).toBe('draft');
  });
  it('preserves untouched row references and all view drafts across progress events', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    state = reducer(state, { type: 'view', patch: { urls: 'a draft URL', taskSearch: 'Beta', taskStatus: 'paused', taskSelection: 'a', settingsDraft: { uiLanguage: 'zh-CN' }, optionsDraft: { quality: '720p' } } });
    const previous = state;
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'a', progress: 85, stageProgress: 85 } } });
    expect(state.tasks.a.progress).toBe(85); expect(state.tasks.b).toBe(previous.tasks.b); expect(state.tasks.a).not.toBe(previous.tasks.a);
    expect(state.view).toBe(previous.view); expect(state.taskIds).toBe(previous.taskIds); expect(previous.tasks.a.progress).toBe(1);
    expect(state.taskListVersion).toBe(previous.taskListVersion); expect(state.activeTaskCount).toBe(2);
    state = reducer(state, { type: 'event', event: { kind: 'settings.updated', data: { downloadOptions: { quality: '4k' } } } });
    expect(state.view.optionsDraft).toEqual({ quality: '720p' }); expect(state.view.settingsDraft).toEqual({ uiLanguage: 'zh-CN' });
  });
  it('keeps drafts when reconnecting and clears selection only when the task is removed', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    state = reducer(state, { type: 'view', patch: { taskSelection: 'a', urls: 'unfinished', mediaSearch: 'cover' } });
    state = reducer(state, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.view.urls).toBe('unfinished'); expect(state.view.taskSelection).toBe('a');
    state = reducer(state, { type: 'event', event: { kind: 'task.remove', data: { id: 'a' } } });
    expect(state.tasks.a).toBeUndefined(); expect(state.view.taskSelection).toBe(''); expect(state.taskIds).toEqual(['b']);
  });
  it('keeps batch quality through resync and applies newly saved defaults without changing existing tasks', () => {
    const snapshot = makeSnapshot(); snapshot.tasks[0].options = { quality: 'best' };
    let state = reducer(initialState, { type: 'snapshot', snapshot });
    state = reducer(state, { type: 'view', patch: { quality: 'worst' } });
    state = reducer(state, { type: 'snapshot', snapshot });
    expect(state.view.quality).toBe('worst');
    state = reducer(state, { type: 'saved', settings: { downloadOptions: { quality: '720p' } }, submitted: state.view });
    expect(state.view.quality).toBeNull(); expect(state.settings.downloadOptions?.quality).toBe('720p');
    expect(state.tasks.a.options.quality).toBe('best');
  });
  it('keeps settings, download and quality edits made after a save was submitted', () => {
    let state = reducer(initialState, { type: 'setting', key: 'uiLanguage', value: 'en-US' });
    state = reducer(state, { type: 'option', key: 'quality', value: '720p' });
    const submitted = state.view;
    state = reducer(state, { type: 'setting', key: 'appearanceTheme', value: 'light' });
    state = reducer(state, { type: 'option', key: 'quality', value: '1080p' });
    state = reducer(state, { type: 'view', patch: { quality: '4k' } });
    state = reducer(state, { type: 'saved', settings: { uiLanguage: 'en-US', downloadOptions: { quality: '720p' } }, submitted });
    expect(state.settings.downloadOptions?.quality).toBe('720p');
    expect(state.view.settingsDraft).toEqual({ uiLanguage: 'en-US', appearanceTheme: 'light' });
    expect(state.view.optionsDraft?.quality).toBe('1080p');
    expect(state.view.quality).toBe('4k');
  });
  it('shows processing progress independently from downloaded input progress', () => {
    expect(taskProgress({ ...makeTask('a'), status: 'processing', progress: 100, stageProgress: 12 })).toEqual({ value: 12, indeterminate: false });
    expect(taskProgress({ ...makeTask('a'), status: 'processing', progress: 100, stageProgress: null }).indeterminate).toBe(true);
    expect(taskProgress({ ...makeTask('a'), status: 'completed', stageProgress: 2 }).value).toBe(100);
    expect(taskProgress({ ...makeTask('a'), status: 'paused', stageIndeterminate: true }).indeterminate).toBe(false);
  });
  it('keeps the active count correct across status changes, additions, removals and resync', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'a', status: 'paused' } } });
    expect(state.activeTaskCount).toBe(1);
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { ...makeTask('c'), status: 'queued' } } });
    expect(state.activeTaskCount).toBe(1);
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'c', status: 'processing' } } });
    expect(state.activeTaskCount).toBe(2);
    state = reducer(state, { type: 'event', event: { kind: 'task.remove', data: 'b' } });
    state = reducer(state, { type: 'event', event: { kind: 'task.remove', data: 'missing' } });
    expect(state.activeTaskCount).toBe(1);
    state = reducer(state, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.activeTaskCount).toBe(2);
  });
  it('adds a compression command result only when a newer task event has not arrived', () => {
    const queued = { ...makeTask('compressed'), status: 'waiting_processing' as const };
    let state = reducer(initialState, { type: 'event', event: { kind: 'task.upsert', data: { ...queued } }, onlyIfTaskAbsent: true });
    expect(state.tasks.compressed.status).toBe('waiting_processing');
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'compressed', status: 'processing', stageProgress: 42 } } });
    const updated = state;
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { ...queued } }, onlyIfTaskAbsent: true });
    expect(state).toBe(updated);
    expect(state.tasks.compressed.stageProgress).toBe(42);
  });
  it('sums only measured downloading speeds across updates, filtering, removal and resync', () => {
    let state = reducer(initialState, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.downloadSpeed).toBe(2048);
    state = reducer(state, { type: 'view', patch: { taskSearch: 'Alpha', taskStatus: 'completed' } });
    expect(state.downloadSpeed).toBe(2048);
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'b', speed: 4096 } } });
    expect(state.downloadSpeed).toBe(5120);
    for (const status of ['paused', 'processing', 'completed', 'failed', 'cancelled']) {
      state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'b', status } } });
      expect(state.downloadSpeed).toBe(1024);
    }
    for (const speed of [null, -1, Infinity, '1 MiB/s']) {
      state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'a', speed } } });
      expect(state.downloadSpeed).toBe(0);
    }
    state = reducer(state, { type: 'event', event: { kind: 'task.upsert', data: { id: 'a', speed: 2048 } } });
    expect(state.downloadSpeed).toBe(2048);
    state = reducer(state, { type: 'event', event: { kind: 'task.remove', data: 'a' } });
    expect(state.downloadSpeed).toBe(0);
    state = reducer(state, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.downloadSpeed).toBe(2048);
  });
  it('resolves task media after snapshots, partial updates and reassignment', () => {
    const snapshot = makeSnapshot();
    snapshot.media = [{ id: 'm', taskId: 'a', title: 'Media', filename: 'a.mp4', size: 12, duration: 1 }];
    let state = reducer(initialState, { type: 'snapshot', snapshot });
    expect(state.media[state.mediaByTask.a]).toEqual(snapshot.media[0]);
    state = reducer(state, { type: 'event', event: { kind: 'media.upsert', data: { id: 'm', title: 'Edited' } } });
    expect(state.media[state.mediaByTask.a].title).toBe('Edited');
    state = reducer(state, { type: 'event', event: { kind: 'media.upsert', data: { id: 'm', taskId: 'b' } } });
    expect(state.mediaByTask.a).toBeUndefined(); expect(state.mediaByTask.b).toBe('m');
    state = reducer(state, { type: 'snapshot', snapshot: makeSnapshot() });
    expect(state.mediaByTask).toEqual({});
  });
});
