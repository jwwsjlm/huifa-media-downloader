import { describe, expect, it } from 'vitest';
import { initialState, reducer } from './state';
import { taskProgress } from './task';
import { makeTask, makeSnapshot } from './test-fixtures';
describe('task delta state', () => {
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
