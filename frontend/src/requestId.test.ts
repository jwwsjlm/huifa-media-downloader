import { expect, it, vi } from 'vitest';
import { newRequestId } from './requestId';

it('creates a version 4 UUID when plain HTTP does not expose randomUUID', () => {
  const original = globalThis.crypto;
  const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(255));
  vi.stubGlobal('crypto', { getRandomValues });
  try {
    expect(newRequestId()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff');
    expect(getRandomValues).toHaveBeenCalledOnce();
  } finally { vi.stubGlobal('crypto', original); }
});
