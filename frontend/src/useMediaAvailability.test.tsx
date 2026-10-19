import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useMediaAvailability } from './useMediaAvailability';

const transport = vi.hoisted(() => ({ command: vi.fn() }));
vi.mock('./transport', () => ({ host: transport }));

beforeEach(() => { transport.command.mockReset().mockResolvedValue([]); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('visible media availability', () => {
  it('checks only explicit IDs and ignores content-equivalent arrays and inactive views', async () => {
    transport.command.mockResolvedValue([{ id: 'selected', status: 'missing' }, { id: 'visible', status: 'available' }]);
    const { result, rerender } = renderHook(({ ids, enabled }) => useMediaAvailability(ids, enabled), { initialProps: { ids: ['visible', 'selected', 'visible'], enabled: false } });
    await act(async () => {});
    expect(transport.command).not.toHaveBeenCalled();
    rerender({ ids: ['visible', 'selected', 'visible'], enabled: true });
    await act(async () => {});
    expect(transport.command).toHaveBeenCalledExactlyOnceWith('media.availability', { ids: ['selected', 'visible'] });
    expect(result.current.availability).toEqual({ selected: 'missing', visible: 'available' });
    rerender({ ids: ['selected', 'visible'], enabled: true });
    await act(async () => {});
    expect(transport.command).toHaveBeenCalledTimes(1);
    rerender({ ids: [], enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(transport.command).toHaveBeenCalledTimes(1);
  });

  it('keeps one request in flight, coalesces scrolling and discards the old window response', async () => {
    let finish!: (rows: unknown) => void;
    transport.command.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValue([{ id: 'latest', status: 'available' }]);
    const { result, rerender } = renderHook(({ ids }) => useMediaAvailability(ids), { initialProps: { ids: ['old'] } });
    await act(async () => {});
    rerender({ ids: ['middle'] });
    rerender({ ids: ['latest'] });
    act(() => { window.dispatchEvent(new Event('focus')); result.current.refresh(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(transport.command).toHaveBeenCalledTimes(1);
    await act(async () => finish([{ id: 'old', status: 'missing' }]));
    expect(transport.command).toHaveBeenCalledTimes(2);
    expect(transport.command).toHaveBeenLastCalledWith('media.availability', { ids: ['latest'] });
    expect(result.current.availability).toEqual({ latest: 'available' });
  });

  it('refreshes on demand, focus and the visible timer, recovering restored files and treating failures as unknown', async () => {
    transport.command.mockResolvedValueOnce([{ id: 'media', status: 'missing' }])
      .mockResolvedValueOnce([{ id: 'media', status: 'available' }])
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue([{ id: 'media', status: 'available' }]);
    const { result } = renderHook(() => useMediaAvailability(['media']));
    await act(async () => {});
    expect(result.current.availability.media).toBe('missing');
    await act(async () => result.current.refresh());
    expect(result.current.availability.media).toBe('available');
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(result.current.availability.media).toBe('unknown');
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(transport.command).toHaveBeenCalledTimes(3);
    visibility.mockReturnValue('visible');
    await act(async () => document.dispatchEvent(new Event('visibilitychange')));
    expect(result.current.availability.media).toBe('available');
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(transport.command).toHaveBeenCalledTimes(5);
  });

  it('invalidates late responses when disabled and removes refresh listeners on unmount', async () => {
    let finish!: (rows: unknown) => void;
    transport.command.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { result, rerender, unmount } = renderHook(({ enabled }) => useMediaAvailability(['media'], enabled), { initialProps: { enabled: true } });
    await act(async () => {});
    rerender({ enabled: false });
    await act(async () => finish([{ id: 'media', status: 'missing' }]));
    expect(result.current.availability).toEqual({});
    transport.command.mockResolvedValue([{ id: 'media', status: 'available' }]);
    rerender({ enabled: true });
    await act(async () => {});
    expect(result.current.availability.media).toBe('available');
    unmount();
    await act(async () => { window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(60_000); });
    expect(transport.command).toHaveBeenCalledTimes(2);
  });

  it('bounds each request to 128 IDs and rejects unsolicited or invalid result values', async () => {
    const ids = Array.from({ length: 130 }, (_, index) => `media-${index}`);
    transport.command.mockImplementation(async (_command, args) => [{ id: args.ids[0], status: 'available' }, { id: args.ids[1], status: 'deleted' }, { id: 'unrequested', status: 'missing' }]);
    const { result } = renderHook(() => useMediaAvailability(ids));
    await act(async () => {});
    expect(transport.command.mock.calls.map(([, args]) => args.ids.length)).toEqual([128, 2]);
    expect(Object.keys(result.current.availability)).toHaveLength(130);
    expect(result.current.availability['media-0']).toBe('available');
    expect(result.current.availability.unrequested).toBeUndefined();
    expect(Object.values(result.current.availability)).not.toContain('deleted');
  });
});
