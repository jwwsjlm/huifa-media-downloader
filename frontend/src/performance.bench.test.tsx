import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Profiler, type ProfilerOnRenderCallback } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider, initialState, reducer, useApp, type AppState } from './state';
import { makeSnapshot, makeTask } from './test-fixtures';
import type { CoreEvent, Snapshot } from './types';

// Opt in explicitly: HUIFA_PERF=1 npm test -- src/performance.bench.test.tsx
// These are relative CPU measurements in jsdom with development React, without
// StrictMode. They do not measure browser frame time, layout, network, or disk.
// Keep generated input, delivery order, and sample counts unchanged for comparisons.
const enabled = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env.HUIFA_PERF === '1';
const transport = vi.hoisted(() => ({ listener: null as null | ((event: CoreEvent) => void), command: vi.fn() }));
vi.mock('./transport', () => ({ host: {
  desktop: false, capabilities: () => ({ browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }),
  subscribe: async (listener: (event: CoreEvent) => void, connection: (value: boolean) => void) => {
    transport.listener = listener; connection(true); return () => { transport.listener = null; };
  },
  command: transport.command, assetUrl: (id: string) => `/api/files/${id}`, notify: async () => {},
} }));

const TASK_COUNTS = [1000, 10000];
const REDUCER_EVENTS = 240;
const APP_EVENTS = 60;
const WARMUPS = 1;
const REDUCER_SAMPLES = 7;
const APP_SAMPLES = 5;
const ACTIVE_TASKS = 4;
type Scenario = 'newest' | 'title-search';

function generatedSnapshot(taskCount: number): Snapshot {
  return { ...makeSnapshot(), tasks: Array.from({ length: taskCount }, (_, index) => ({
    ...makeTask(`perf-${index}`, `Task ${String(index * 7919 % taskCount).padStart(5, '0')}`),
    status: index < ACTIVE_TASKS ? 'downloading' : 'completed',
    progress: index < ACTIVE_TASKS ? 1 : 100,
    stageProgress: index < ACTIVE_TASKS ? 1 : 100,
    createdAt: new Date(1700000000000 + (taskCount - index) * 1000).toISOString(),
  })) };
}

function progressEvents(count: number): CoreEvent[] {
  return Array.from({ length: count }, (_, index) => ({ kind: 'task.upsert', data: {
    id: `perf-${index % ACTIVE_TASKS}`, progress: 2 + Math.floor(index / ACTIVE_TASKS), stageProgress: 2 + Math.floor(index / ACTIVE_TASKS),
  } }));
}

function distribution(samples: number[]) {
  const sorted = [...samples].sort((a, b) => a - b);
  return { medianMs: sorted[Math.floor(sorted.length / 2)], p95Ms: sorted[Math.ceil(sorted.length * 0.95) - 1], samplesMs: samples };
}

function verifyTrajectory(state: AppState, taskCount: number, eventCount: number) {
  expect(state.taskIds).toHaveLength(taskCount);
  expect(state.taskIds[0]).toBe('perf-0');
  expect(state.taskIds[taskCount - 1]).toBe(`perf-${taskCount - 1}`);
  for (let index = 0; index < ACTIVE_TASKS; index++) {
    const finalValue = 1 + eventCount / ACTIVE_TASKS;
    expect(state.tasks[`perf-${index}`].progress).toBe(finalValue);
    expect(state.tasks[`perf-${index}`].stageProgress).toBe(finalValue);
    expect(state.tasks[`perf-${index}`].status).toBe('downloading');
  }
  expect(state.tasks[`perf-${taskCount - 1}`].progress).toBe(100);
}

function output(record: Record<string, unknown>) {
  console.log(`PERF_JSON ${JSON.stringify({ suite: 'frontend-progress-v1', environment: 'jsdom/development-react/no-strict-mode', ...record })}`);
}

describe.skipIf(!enabled)('opt-in generated-data performance audit', () => {
  for (const taskCount of TASK_COUNTS) {
    it(`replays ${REDUCER_EVENTS} real reducer deltas with ${taskCount} tasks`, () => {
      const snapshot = generatedSnapshot(taskCount); const events = progressEvents(REDUCER_EVENTS);
      const samples: number[] = [];
      for (let sample = -WARMUPS; sample < REDUCER_SAMPLES; sample++) {
        let state = reducer(initialState, { type: 'snapshot', snapshot });
        const start = performance.now();
        for (const event of events) state = reducer(state, { type: 'event', event });
        const elapsed = performance.now() - start;
        verifyTrajectory(state, taskCount, REDUCER_EVENTS);
        if (sample >= 0) samples.push(elapsed);
      }
      output({ path: 'reducer', tasks: taskCount, events: REDUCER_EVENTS, warmups: WARMUPS, ...distribution(samples) });
    }, 60000);

    for (const scenario of ['newest', 'title-search'] as const) {
      it(`replays ${APP_EVENTS} real App deltas with ${taskCount} tasks (${scenario})`, async () => {
        const snapshot = generatedSnapshot(taskCount); const events = progressEvents(APP_EVENTS);
        const samples: number[] = []; const renderSamples: number[] = []; const commitSamples: number[] = [];
        for (let sample = -WARMUPS; sample < APP_SAMPLES; sample++) {
          cleanup(); transport.command.mockReset();
          transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'cookies.list' ? [] : true);
          let latest: AppState | null = null; let renderMs = 0; let commits = 0;
          const onRender: ProfilerOnRenderCallback = (_id, _phase, actualDuration) => { renderMs += actualDuration; commits++; };
          function Probe() { latest = useApp().state; return null; }
          render(<Profiler id="App" onRender={onRender}><AppProvider><App /><Probe /></AppProvider></Profiler>);
          await screen.findByRole('listbox', { name: 'Download task list' });
          if (scenario === 'title-search') {
            fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'Task' } });
            fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), { target: { value: 'title' } });
          }
          const viewport = screen.getByRole('listbox', { name: 'Download task list' });
          fireEvent.click(within(viewport).getAllByRole('option')[0]);
          const initialOrder = [...latest!.taskIds];
          renderMs = 0; commits = 0;
          const start = performance.now();
          // Separate acts force currently synchronous updates to commit separately.
          // No network pacing is simulated; deferred implementations can coalesce
          // this fixed burst, with their final flush included in elapsed wall time.
          for (const event of events) await act(async () => { transport.listener?.(event); });
          await waitFor(() => verifyTrajectory(latest!, taskCount, APP_EVENTS), { interval: 1, timeout: 2000 });
          const elapsed = performance.now() - start;
          expect(latest!.taskIds).toEqual(initialOrder);
          expect(screen.getByRole('listbox', { name: 'Download task list' })).toBe(viewport);
          expect(within(viewport).getAllByRole('option', { selected: true })).toHaveLength(1);
          if (sample >= 0) { samples.push(elapsed); renderSamples.push(renderMs); commitSamples.push(commits); }
        }
        cleanup();
        output({ path: 'App', scenario: scenario as Scenario, tasks: taskCount, events: APP_EVENTS, warmups: WARMUPS,
          ...distribution(samples), profiler: distribution(renderSamples), commitCounts: commitSamples });
      }, 60000);
    }
  }
});
