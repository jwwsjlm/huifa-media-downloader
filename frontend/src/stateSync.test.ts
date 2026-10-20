import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initialState, reducer, type Action } from './state';
import { MAX_BUFFERED_EVENTS, MAX_BUFFERED_EVENT_BYTES, PROGRESS_INTERVAL_MS, startStateSync } from './stateSync';
import { makeSnapshot } from './test-fixtures';
import type { CoreEvent, Snapshot } from './types';

const settle = async () => { for (let index = 0; index < 16; index++) await Promise.resolve(); };
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((done, failed) => { resolve = done; reject = failed; }); return { promise, resolve, reject }; }
function harness(immediateConnection = true, shouldPoll = () => true) {
  let state = initialState; let listener!: (event: CoreEvent) => void; let connection!: (connected: boolean) => void;
  const actions: Action[] = []; const unlisten = vi.fn(); const onError = vi.fn(); const onEvent = vi.fn();
  const snapshot = { ...makeSnapshot(), revision: 100 };
  const command = vi.fn(async (name: string, args?: unknown): Promise<unknown> => name === 'snapshot' ? snapshot : { revision: (args as { since: number }).since, reset: false, events: [] });
  const subscribe = vi.fn(async (received: typeof listener, connected: typeof connection): Promise<() => void> => { listener = received; connection = connected; if (immediateConnection) connected(true); return unlisten; });
  const start = () => startStateSync({
    host: { command: command as never, subscribe }, dispatch: action => { actions.push(action); state = reducer(state, action); },
    getTask: id => state.tasks[id], shouldPoll, onSnapshot: vi.fn(), onEvent, onError,
  });
  return { start, command, snapshot, actions, onError, onEvent, unlisten, subscribe, state: () => state,
    emit: (event: CoreEvent) => listener(event), connect: (connected: boolean) => connection(connected) };
}

let stop: (() => void) | undefined;
beforeEach(() => vi.useFakeTimers());
afterEach(() => { stop?.(); stop = undefined; vi.useRealTimers(); });

describe('live state synchronization', () => {
  it('coalesces full and partial progress without delaying status, error or deletion', async () => {
    const test = harness(); stop = test.start().dispose; await settle(); test.actions.length = 0;
    const previous = test.state();
    test.emit({ kind: 'task.upsert', data: JSON.parse(JSON.stringify({ ...previous.tasks.a, progress: 2 })) });
    for (let index = 3; index <= 60; index++) test.emit({ kind: 'task.upsert', data: { id: 'a', progress: index, speed: index } });
    expect(test.actions).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(PROGRESS_INTERVAL_MS - 1); expect(test.state()).toBe(previous);
    await vi.advanceTimersByTimeAsync(1);
    expect(test.actions.map(action => action.type)).toEqual(['tasks.progress']);
    expect(test.state().tasks.a.progress).toBe(60); expect(test.state().tasks.b).toBe(previous.tasks.b);
    expect(test.state().taskIds).toBe(previous.taskIds); expect(test.state().view).toBe(previous.view);
    expect(test.state().taskChangeVersion).toBe(previous.taskChangeVersion);
    test.actions.length = 0;
    test.emit({ kind: 'task.upsert', data: { id: 'a', progress: 70 } });
    test.emit({ kind: 'task.upsert', data: { id: 'a', status: 'failed', error: 'disk full' } });
    expect(test.actions.map(action => action.type)).toEqual(['tasks.progress', 'event']);
    expect(test.state().tasks.a).toMatchObject({ status: 'failed', error: 'disk full', progress: 70 });
    expect(test.state().pendingTaskCount).toBe(1);
    test.emit({ kind: 'task.remove', data: 'a' }); expect(test.state().tasks.a).toBeUndefined();
    await vi.advanceTimersByTimeAsync(PROGRESS_INTERVAL_MS); expect(test.state().tasks.a).toBeUndefined();
  });

  it('polls deltas without rebuilding unchanged task objects and repairs a lost event once', async () => {
    const test = harness(); stop = test.start().dispose; await settle();
    const previous = test.state(); test.actions.length = 0;
    await vi.advanceTimersByTimeAsync(2000);
    expect(test.command.mock.calls.map(call => call[0])).toEqual(['snapshot', 'state.sync', 'state.sync']);
    expect(test.state()).toBe(previous); expect(test.actions).toHaveLength(0);
    const replay = deferred<unknown>(); test.command.mockReturnValueOnce(replay.promise);
    test.emit({ kind: 'task.upsert', revision: 102, data: { id: 'a', status: 'completed', progress: 100 } });
    expect(test.state()).toBe(previous);
    replay.resolve({ revision: 102, reset: false, events: [
      { kind: 'task.upsert', revision: 101, data: { id: 'a', status: 'paused' } },
      { kind: 'task.upsert', revision: 102, data: { id: 'a', status: 'completed', progress: 100 } },
    ] });
    await settle();
    expect(test.state().tasks.a.status).toBe('completed'); expect(test.onEvent).toHaveBeenCalledTimes(2);
    test.emit({ kind: 'task.upsert', revision: 101, data: { id: 'a', status: 'downloading' } });
    expect(test.state().tasks.a.status).toBe('completed'); expect(test.onEvent).toHaveBeenCalledTimes(2);
    expect(test.command.mock.calls.at(-1)).toEqual(['state.sync', { since: 100 }, expect.objectContaining({ timeoutMs: 10000 })]);
  });

  it('replaces state after a history reset and after reconnecting', async () => {
    const test = harness(); stop = test.start().dispose; await settle();
    const restarted: Snapshot = { ...test.snapshot, revision: 500, tasks: [{ ...test.snapshot.tasks[0], status: 'paused' }] };
    test.command.mockResolvedValueOnce({ revision: 499, reset: true, events: [] }).mockResolvedValueOnce(restarted);
    await vi.advanceTimersByTimeAsync(2000);
    expect(test.state().tasks.a.status).toBe('paused'); expect(test.state().taskIds).toEqual(['a']);
    test.command.mockResolvedValueOnce({ ...restarted, revision: 501, tasks: [{ ...restarted.tasks[0], status: 'completed' }] });
    test.connect(false); test.connect(true); await settle();
    expect(test.state().tasks.a.status).toBe('completed');
    expect(test.command.mock.calls.map(call => call[0])).toEqual(['snapshot', 'state.sync', 'state.sync', 'snapshot', 'state.sync', 'snapshot', 'state.sync']);
  });

  it('does not fetch a second initial snapshot when the event stream opens late', async () => {
    const test = harness(false); stop = test.start().dispose; await settle();
    test.connect(true); await settle(); expect(test.command.mock.calls.filter(call => call[0] === 'snapshot')).toHaveLength(1);
    test.connect(false); test.connect(true); await settle(); expect(test.command.mock.calls.filter(call => call[0] === 'snapshot')).toHaveLength(2);
  });

  it('retries failed subscriptions and reconciles an idle catalog until listening succeeds', async () => {
    const test = harness(true, () => false); test.snapshot.tasks.forEach(task => { task.status = 'completed'; });
    test.subscribe.mockRejectedValueOnce(new Error('first listen failed')).mockRejectedValueOnce(new Error('second listen failed'));
    stop = test.start().dispose; await settle();
    expect(test.state().loaded).toBe(true);
    expect(test.subscribe).toHaveBeenCalledTimes(1); expect(test.onError).toHaveBeenCalledTimes(1);
    test.command.mockResolvedValueOnce({ revision: 101, reset: false, events: [{ kind: 'task.remove', revision: 101, data: 'a' }] });
    await vi.advanceTimersByTimeAsync(2000);
    expect(test.state().tasks.a).toBeUndefined(); expect(test.subscribe).toHaveBeenCalledTimes(2);
    test.snapshot.revision = 101; test.snapshot.tasks = test.snapshot.tasks.filter(task => task.id !== 'a');
    await vi.advanceTimersByTimeAsync(2000);
    expect(test.subscribe).toHaveBeenCalledTimes(3); expect(test.state().connected).toBe(true);
    const reads = test.command.mock.calls.length; await vi.advanceTimersByTimeAsync(4000); expect(test.command).toHaveBeenCalledTimes(reads);
    stop(); stop = undefined; expect(test.unlisten).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0);
    window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')); await settle();
    expect(test.command).toHaveBeenCalledTimes(reads);
  });

  it('does not retry or report a pending subscription failure after disposal', async () => {
    const test = harness(true, () => false); const subscription = deferred<() => void>(); test.subscribe.mockReturnValueOnce(subscription.promise);
    stop = test.start().dispose; stop(); stop = undefined;
    subscription.reject(new Error('late listen failure')); await settle(); await vi.advanceTimersByTimeAsync(6000);
    expect(test.subscribe).toHaveBeenCalledOnce(); expect(test.command).not.toHaveBeenCalled(); expect(test.onError).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('uses one fresh snapshot when an old native read cannot close a revision gap', async () => {
    const test = harness(); stop = test.start().dispose; await settle();
    test.command.mockResolvedValueOnce({ revision: 100, reset: false, events: [] }).mockResolvedValueOnce({
      ...test.snapshot, revision: 200, tasks: [{ ...test.snapshot.tasks[0], status: 'paused', progress: 80 }],
    });
    test.emit({ kind: 'task.upsert', revision: 102, data: { id: 'a', progress: 60 } }); await settle();
    expect(test.command.mock.calls.map(call => call[0])).toEqual(['snapshot', 'state.sync', 'state.sync', 'snapshot', 'state.sync']);
    expect(test.state().tasks.a).toMatchObject({ status: 'paused', progress: 80 });
  });

  it.each(['count', 'bytes'])('bounds event %s during a hung snapshot and refreshes again after overflow', async limit => {
    const test = harness(); const first = deferred<unknown>();
    const latest = { ...test.snapshot, revision: 5000, tasks: [{ ...test.snapshot.tasks[0], status: 'completed', progress: 100 }] };
    test.command.mockReturnValueOnce(first.promise).mockResolvedValueOnce(latest);
    stop = test.start().dispose; await settle();
    if (limit === 'count') for (let index = 0; index < MAX_BUFFERED_EVENTS * 3; index++) test.emit({ kind: 'task.upsert', revision: 101 + index, data: { id: 'a', progress: index } });
    else test.emit({ kind: 'settings.updated', revision: 101, data: { large: 'x'.repeat(MAX_BUFFERED_EVENT_BYTES / 2) } });
    expect(test.command).toHaveBeenCalledTimes(1); expect(test.state().loaded).toBe(false);
    first.resolve(test.snapshot); await settle();
    expect(test.command.mock.calls.filter(call => call[0] === 'snapshot')).toHaveLength(2); expect(test.state().tasks.a.status).toBe('completed');
    expect(test.state().tasks.a.progress).toBe(100); expect(test.onError).not.toHaveBeenCalled();
  });

  it.each(['task.remove', 'settings.updated'])('keeps %s buffered across a timed-out caller and reused native snapshot', async kind => {
    const test = harness(true, () => false); const timedOut = deferred<unknown>(); const native = deferred<unknown>();
    test.snapshot.tasks.forEach(task => { task.status = 'completed'; });
    test.command.mockReturnValueOnce(timedOut.promise).mockReturnValueOnce(native.promise);
    stop = test.start().dispose; await settle();
    const event: CoreEvent = { kind, revision: 101, data: kind === 'task.remove' ? 'a' : { uiLanguage: 'zh-CN' } };
    test.emit(event); timedOut.reject(new DOMException('Request timed out.', 'TimeoutError')); await settle();
    await vi.advanceTimersByTimeAsync(2000);
    native.resolve(test.snapshot); await settle();
    if (kind === 'task.remove') expect(test.state().tasks.a).toBeUndefined();
    else expect(test.state().settings.uiLanguage).toBe('zh-CN');
    expect(test.command.mock.calls.at(-1)).toEqual(['state.sync', { since: 101 }, expect.objectContaining({ timeoutMs: 10000 })]);
    const calls = test.command.mock.calls.length; await vi.advanceTimersByTimeAsync(4000); expect(test.command).toHaveBeenCalledTimes(calls);
  });

  it('reconciles dropped overflow history after a reused idle snapshot before polling stops', async () => {
    const test = harness(true, () => false); const timedOut = deferred<unknown>(); const native = deferred<unknown>();
    test.snapshot.tasks.forEach(task => { task.status = 'completed'; });
    test.command.mockReturnValueOnce(timedOut.promise).mockReturnValueOnce(native.promise).mockResolvedValueOnce({
      revision: 101, reset: false, events: [{ kind: 'task.remove', revision: 101, data: 'a' }],
    });
    stop = test.start().dispose; await settle();
    test.emit({ kind: 'settings.updated', revision: 101, data: { large: 'x'.repeat(MAX_BUFFERED_EVENT_BYTES / 2) } });
    timedOut.reject(new DOMException('Request timed out.', 'TimeoutError')); await settle();
    native.resolve(test.snapshot); await settle();
    expect(test.state().tasks.a).toBeUndefined();
    expect(test.command.mock.calls.map(call => call[0])).toEqual(['snapshot', 'snapshot', 'state.sync']);
  });

  it('discards pre-reset frames when the restarted host uses a lower revision', async () => {
    const test = harness(); stop = test.start().dispose; await settle();
    test.command.mockResolvedValueOnce({ revision: 10, reset: true, events: [] }).mockResolvedValueOnce({
      ...test.snapshot, revision: 10, tasks: [{ ...test.snapshot.tasks[0], status: 'paused' }],
    });
    test.emit({ kind: 'task.upsert', revision: 102, data: { id: 'a', status: 'completed' } }); await settle();
    expect(test.state().tasks.a.status).toBe('paused');
    expect(test.command.mock.calls.map(call => call[0])).toEqual(['snapshot', 'state.sync', 'state.sync', 'snapshot', 'state.sync']);
  });

  it('refreshes through the same cursor while preserving removal, settings and drafts racing its snapshot', async () => {
    const test = harness(true, () => false); const controller = test.start(); stop = controller.dispose; await settle();
    const pending = deferred<unknown>(); test.command.mockReturnValueOnce(pending.promise);
    const refresh = controller.refresh();
    test.emit({ kind: 'task.remove', revision: 101, data: 'a' });
    test.emit({ kind: 'settings.updated', revision: 102, data: { uiLanguage: 'zh-CN' } });
    pending.resolve(test.snapshot); await refresh;
    expect(test.state().tasks.a).toBeUndefined(); expect(test.state().settings.uiLanguage).toBe('zh-CN');
    expect(test.command.mock.calls.at(-1)).toEqual(['state.sync', { since: 102 }, expect.objectContaining({ timeoutMs: 10000 })]);
  });

  it('cancels outstanding reads and timers and ignores late snapshot results', async () => {
    const test = harness(); const pending = deferred<unknown>(); test.command.mockReturnValueOnce(pending.promise);
    stop = test.start().dispose; await settle();
    const request = test.command.mock.calls[0] as unknown as [string, unknown, { signal: AbortSignal }];
    const actionCount = test.actions.length; stop(); stop = undefined;
    expect(request[2].signal.aborted).toBe(true); expect(test.unlisten).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0);
    pending.resolve(test.snapshot); await settle();
    expect(test.actions).toHaveLength(actionCount); expect(test.state().loaded).toBe(false);
  });

  it('removes a subscription that finishes after the view closes', async () => {
    const test = harness(); const subscription = deferred<() => void>(); test.subscribe.mockReturnValueOnce(subscription.promise);
    stop = test.start().dispose; stop(); stop = undefined;
    subscription.resolve(test.unlisten); await settle();
    expect(test.unlisten).toHaveBeenCalledOnce(); expect(test.command).not.toHaveBeenCalled();
  });
});
