import type { Dispatch } from 'react';
import type { Action } from './state';
import type { RequestOptions } from './transport';
import type { CoreEvent, Snapshot, StateSync, Task, TaskProgress } from './types';

export const PROGRESS_INTERVAL_MS = 200;
export const MAX_BUFFERED_EVENTS = 1024;
export const MAX_BUFFERED_EVENT_BYTES = 4 * 1024 * 1024;
const progressFields = new Set(['progress', 'stageProgress', 'stageIndeterminate', 'speed', 'eta', 'downloadedBytes', 'totalBytes', 'totalBytesEstimated']);
const revisionOf = (value: number | undefined): number | null => Number.isSafeInteger(value) && value! >= 0 ? value! : null;
type SyncHost = {
  command<T>(command: string, args?: unknown, options?: RequestOptions): Promise<T>;
  subscribe(listener: (event: CoreEvent) => void, connection: (connected: boolean) => void): Promise<() => void>;
};
type Options = {
  host: SyncHost; dispatch: Dispatch<Action>; getTask: (id: string) => Task | undefined; shouldPoll: () => boolean;
  onSnapshot: (snapshot: Snapshot, signal: AbortSignal) => void; onEvent: (event: CoreEvent) => void; onBufferedEvent?: (event: CoreEvent) => void; onError: (reason: unknown) => void;
};
export type StateSyncController = { dispose: () => void; refresh: () => Promise<void> };

/** One state request at a time; revision gaps are repaired before later events
 * are applied. Only progress values can wait for the next 200 ms flush. */
export function startStateSync(options: Options): StateSyncController {
  const { host, dispatch } = options;
  const lifetime = new AbortController();
  let disposed = false; let loaded = false; let buffering = true; let cursor: number | null = null;
  let connectionState: boolean | null = null;
  let needsSnapshot = true; let recoveryRequested = false; let requesting: Promise<void> | null = null;
  let reconcileRequired = false;
  let unlisten: (() => void) | undefined; let subscribing = false; let progressTimer: ReturnType<typeof setTimeout> | undefined;
  let bufferedBytes = 0;
  let knownTasks: Record<string, Task> = {};
  const buffered: CoreEvent[] = []; const progress = new Map<string, TaskProgress>();

  const flushProgress = () => {
    if (progressTimer !== undefined) clearTimeout(progressTimer); progressTimer = undefined;
    if (!progress.size) return;
    dispatch({ type: 'tasks.progress', tasks: [...progress.values()] }); progress.clear();
  };
  const deliver = (event: CoreEvent) => {
    options.onEvent(event);
    if (event.kind === 'task.upsert') {
      const patch = event.data as unknown as Partial<Task> & { id: string }; const id = String(patch.id);
      const previous = knownTasks[id] || options.getTask(id);
      const pureProgress = previous && Object.entries(patch).every(([key, value]) => {
        if (key === 'id' || progressFields.has(key)) return true;
        const before = previous[key as keyof Task];
        return before === value || typeof value === 'object' && JSON.stringify(before) === JSON.stringify(value);
      });
      knownTasks[id] = { ...previous, ...patch, id } as Task;
      if (pureProgress) {
        const values = Object.fromEntries(Object.entries(patch).filter(([key]) => progressFields.has(key)));
        progress.set(id, { ...progress.get(id), ...values, id });
        if (progress.size >= MAX_BUFFERED_EVENTS) flushProgress();
        else progressTimer ??= setTimeout(flushProgress, PROGRESS_INTERVAL_MS);
        return;
      }
    }
    flushProgress();
    if (event.kind === 'task.remove') delete knownTasks[typeof event.data === 'string' ? event.data : String((event.data as { id: string }).id)];
    dispatch({ type: 'event', event });
  };
  const buffer = (event: CoreEvent) => {
    if (needsSnapshot) return;
    const bytes = JSON.stringify(event).length * 2;
    if (buffered.length >= MAX_BUFFERED_EVENTS || bufferedBytes + bytes > MAX_BUFFERED_EVENT_BYTES) {
      // Once history was discarded, only a new snapshot may establish a cursor.
      buffered.length = 0; bufferedBytes = 0; needsSnapshot = true; buffering = true; return;
    }
    buffered.push(event); bufferedBytes += bytes;
  };
  const accept = (event: CoreEvent): boolean => {
    const revision = revisionOf(event.revision);
    if (revision !== null && cursor !== null) {
      if (revision <= cursor) return true;
      if (revision !== cursor + 1) return false;
      cursor = revision;
    }
    deliver(event); return true;
  };
  const drain = () => {
    if (needsSnapshot) return;
    const pending = buffered.splice(0); bufferedBytes = 0;
    if (pending.every(event => revisionOf(event.revision) !== null)) pending.sort((a, b) => a.revision! - b.revision!);
    buffering = false;
    for (const event of pending) {
      if (buffering || !accept(event)) { buffering = true; buffer(event); recoveryRequested = true; }
    }
  };
  const sync = (full = false): Promise<void> => {
    if (disposed) return Promise.resolve();
    if (full) { needsSnapshot = true; buffering = true; flushProgress(); }
    if (requesting) return requesting;
    const snapshotRequest = needsSnapshot || !loaded || cursor === null;
    needsSnapshot = false; recoveryRequested = false;
    if (snapshotRequest) buffering = true;
    requesting = (async () => {
      let followUp = false;
      try {
        if (snapshotRequest) {
          const snapshot = await host.command<Snapshot>('snapshot', { includeTools: false }, { signal: lifetime.signal, timeoutMs: 10000 });
          if (disposed) return;
          progress.clear();
          knownTasks = Object.fromEntries(snapshot.tasks.map(task => [task.id, task]));
          cursor = revisionOf(snapshot.revision); loaded = true;
          reconcileRequired = cursor !== null;
          dispatch({ type: 'snapshot', snapshot }); options.onSnapshot(snapshot, lifetime.signal);
        } else {
          const since = cursor!;
          const result = await host.command<StateSync>('state.sync', { since: cursor }, { signal: lifetime.signal, timeoutMs: 10000 });
          if (disposed) return;
          if (result.reset || revisionOf(result.revision) === null || !Array.isArray(result.events)) {
            // A confirmed history/epoch reset invalidates older buffered frames.
            buffered.length = 0; bufferedBytes = 0; needsSnapshot = true; buffering = true; return;
          }
          if (result.revision < since) { reconcileRequired = true; followUp = true; return; }
          for (const event of result.events) {
            if (!accept(event)) { needsSnapshot = true; buffering = true; return; }
          }
          if (cursor !== null && result.revision > cursor) { needsSnapshot = true; buffering = true; return; }
          reconcileRequired = false;
        }
        recoveryRequested = false; drain();
        // A stale native read may have started before the observed gap. If its
        // history still cannot close the gap, establish a fresh snapshot once.
        if (!snapshotRequest && recoveryRequested) { needsSnapshot = true; buffering = true; recoveryRequested = false; }
        followUp = reconcileRequired;
      } catch (reason) {
        if (!disposed) options.onError(reason);
      } finally {
        requesting = null;
        if (!disposed && (needsSnapshot || recoveryRequested || followUp)) void sync();
      }
    })();
    return requesting;
  };
  const receive = (event: CoreEvent) => {
    if (disposed) return;
    if (event.kind === 'snapshot.required' || event.kind === 'host.resync') { void sync(true); return; }
    if (event.kind === 'host.error') { options.onError((event.data as { message: string }).message); return; }
    if (buffering) { if (loaded) options.onBufferedEvent?.(event); buffer(event); return; }
    if (!accept(event)) { options.onBufferedEvent?.(event); buffering = true; buffer(event); recoveryRequested = true; void sync(); }
  };
  const reconcile = () => { if (!disposed && document.visibilityState === 'visible') void sync(); };
  const subscribe = async () => {
    if (disposed || unlisten || subscribing) return;
    subscribing = true;
    try {
      const cleanup = await host.subscribe(receive, connected => {
        if (disposed) return;
        const reconnected = connected && connectionState === false && Boolean(unlisten); connectionState = connected;
        dispatch({ type: 'connection', connected });
        if (reconnected && loaded) void sync(true);
      });
      if (disposed) { cleanup(); return; }
      unlisten = cleanup;
      await sync(true);
    } catch (reason) {
      if (!disposed) { connectionState = false; dispatch({ type: 'connection', connected: false }); options.onError(reason); void sync(); }
    } finally { subscribing = false; }
  };
  const timer = setInterval(() => {
    if (!unlisten) void subscribe();
    if (!loaded || buffering || reconcileRequired || connectionState !== true || options.shouldPoll()) reconcile();
  }, 2000);
  window.addEventListener('focus', reconcile); document.addEventListener('visibilitychange', reconcile);
  void subscribe();
  const dispose = () => {
    disposed = true; lifetime.abort(); unlisten?.();
    clearInterval(timer); if (progressTimer !== undefined) clearTimeout(progressTimer);
    progress.clear(); buffered.length = 0; bufferedBytes = 0; knownTasks = {};
    window.removeEventListener('focus', reconcile); document.removeEventListener('visibilitychange', reconcile);
  };
  return { dispose, refresh: async () => { await sync(true); while (requesting && !disposed) await requesting; } };
}
