import type { Snapshot, Task } from './types';

export function makeTask(id: string, title = id): Task { return { id, title, url: `https://example.test/${id}`, status: 'downloading', stage: 'download', progress: 1, stageProgress: 1, stageIndeterminate: false, speed: 1024, eta: 20, error: null, createdAt: '2026-10-14T00:00:00Z', options: {} }; }
export function makeSnapshot(): Snapshot { return { version: '0.2.0', tasks: [makeTask('a', 'Alpha'), makeTask('b', 'Beta')], media: [], settings: { uiLanguage: 'en-US', downloadOptions: { contentMode: 'video', quality: 'best' } }, tools: [] }; }
