import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SiteSupport } from './SiteSupport';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
const t = (_zh: string, en: string) => en;
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(800); });
beforeEach(() => { vi.useFakeTimers(); command.mockReset(); });
afterEach(() => vi.useRealTimers());

it('debounces pasted URLs and warns about generic support without blocking direct media', async () => {
  command.mockResolvedValue({ host: 'example.test', url: 'https://example.test/video.mp4', status: 'generic', extractors: [] });
  const view = render(<SiteSupport input="https://example.test/old" t={t} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(400); });
  view.rerender(<SiteSupport input="https://example.test/video.mp4" t={t} />);
  await settle();
  expect(command).toHaveBeenCalledTimes(1);
  expect(command).toHaveBeenCalledWith('sites.check', { url: 'https://example.test/video.mp4' }, expect.any(Object));
  expect(screen.getByRole('status').textContent).toContain('You can still try generic extraction');
  expect(command.mock.calls.every(([name]) => name === 'sites.check')).toBe(true);
});

it('opens a standalone domain query and asks for a full URL when only a domain is inconclusive', async () => {
  command.mockResolvedValue({ host: 'example.test', status: 'needs_url', extractors: [] });
  render(<SiteSupport input="" t={t} />);
  fireEvent.click(screen.getByRole('button', { name: 'Check website support' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Website domain or video URL' }), { target: { value: 'example.test' } });
  await settle();
  expect(command).toHaveBeenCalledWith('sites.check', { url: 'example.test' }, expect.any(Object));
  expect(screen.getByRole('status').textContent).toContain('A domain alone cannot confirm support');
});

it('ignores stale responses and refreshes after yt-dlp updates', async () => {
  let finish!: (value: unknown) => void;
  command.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValue({ host: 'new.test', status: 'supported', extractors: ['new'] });
  const view = render(<SiteSupport input="https://old.test/video" version="one" t={t} />); await settle();
  const signal = command.mock.calls[0][2].signal as AbortSignal;
  view.rerender(<SiteSupport input="https://new.test/video" version="one" t={t} />); await settle();
  expect(signal.aborted).toBe(true);
  await act(async () => finish({ host: 'old.test', status: 'unsupported', extractors: [] }));
  expect(screen.getByRole('status').textContent).toContain('new.test');
  expect(screen.queryByText('old.test')).toBeNull();
  view.rerender(<SiteSupport input="https://new.test/video" version="two" t={t} />); await settle();
  expect(command).toHaveBeenCalledTimes(3);
});

it('shows explicitly broken support and query failures separately', async () => {
  command.mockResolvedValueOnce({ host: 'broken.test', status: 'unsupported', extractors: [] }).mockRejectedValueOnce(new Error('yt-dlp is not installed'));
  const view = render(<SiteSupport input="https://broken.test/video" t={t} />); await settle();
  expect(screen.getByRole('status').textContent).toContain('marks this link as unsupported');
  view.rerender(<SiteSupport input="https://next.test/video" t={t} />); await settle();
  expect(screen.getByRole('alert').textContent).toContain('runtime component');
  expect(screen.queryByRole('status')).toBeNull();
});

it('does not run queries for empty text or claim that one result covers a batch', async () => {
  const view = render(<SiteSupport input="" t={t} />); await settle();
  view.rerender(<SiteSupport input={'https://a.test/video\nhttps://b.test/video'} t={t} />); await settle();
  expect(command).not.toHaveBeenCalled();
});
