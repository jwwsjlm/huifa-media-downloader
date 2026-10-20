import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { CompressionDialog } from './CompressionDialog';
import { makeTask } from './test-fixtures';
import type { CompressionPreview } from './compression';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
const source: CompressionPreview = {
  source: { sizeBytes: 4000000000, duration: 600, width: 2160, height: 3840, displayWidth: 2160, displayHeight: 3840, fps: '60000/1001', variableFrameRate: true, videoBitrateBps: 50000000, videoBitrateEstimated: false, hdr: 'hdr10', bitDepth: 10, audioTracks: 2, subtitleTracks: 3, attachmentTracks: 2 },
  plan: null, capabilities: [{ encoder: 'libx265', label: 'H.265 (software)', available: true, reason: null }, { encoder: 'hevc_nvenc', label: 'NVIDIA H.265', available: false, reason: 'No compatible NVIDIA encoder is available.' }], blockingReasons: [], warnings: [],
};
const props = { task: makeTask('local'), close: vi.fn(), queued: vi.fn(), run: (operation: () => Promise<unknown>) => { void operation(); }, t: (_zh: string, en: string) => en };
beforeEach(() => {
  props.close.mockReset(); props.queued.mockReset(); command.mockReset();
  command.mockImplementation(async (name, args) => {
    if (name === 'probe.cancel') return true;
    if (name === 'tasks.convert') return { ...makeTask('compressed'), status: 'waiting_processing' };
    if (!args.compression.targetBytes && !args.compression.videoBitrateBps) return structuredClone(source);
    const videoBitrateBps = args.compression.videoBitrateBps || Math.floor(args.compression.targetBytes * 8 / source.source.duration) - 100000;
    return { ...structuredClone(source), plan: { encoder: args.compression.encoder || 'libx265', videoBitrateBps, estimatedSizeBytes: args.compression.targetBytes || Math.round((videoBitrateBps + 100000) * source.source.duration / 8), width: args.compression.maxWidth || 2160, height: args.compression.maxHeight || 3840, fps: args.compression.fps || '60000/1001', targetBytes: args.compression.targetBytes } };
  });
});
afterEach(() => { vi.useRealTimers(); });

it('stops the initial loading message after a failed check and can retry the backend analysis', async () => {
  const base = command.getMockImplementation()!;
  command.mockImplementationOnce(async () => { throw new Error('Compression video analysis timed out'); });
  render(<CompressionDialog {...props} />);
  expect(await screen.findByRole('alert')).toBeTruthy();
  expect(screen.queryByText('Checking the local video and encoders…')).toBeNull();
  expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(true);
  command.mockImplementation(base);
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
  await screen.findByRole('textbox', { name: 'Target size' });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
});

it('explains the approximate target, keeps the actual source size as the slider maximum and submits decimal target bytes', async () => {
  render(<CompressionDialog {...props} />);
  const target = await screen.findByRole('textbox', { name: 'Target size' });
  const slider = screen.getByRole('slider', { name: 'Target size percentage' });
  expect(slider.getAttribute('aria-valuetext')).toContain('4 GB');
  expect(screen.getByText(/The target is approximate; actual output size may vary\./)).toBeTruthy();
  fireEvent.change(target, { target: { value: '4.1GB' } });
  expect(screen.getByRole('alert').textContent).toContain('cannot exceed');
  expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(target, { target: { value: '4MB' } });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Start compression' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('tasks.convert', { id: 'local', requestId: expect.any(String), compression: { targetBytes: 4000000, encoder: 'libx265' } }));
  expect(props.queued).toHaveBeenCalledWith(expect.objectContaining({ id: 'compressed' }));
});

it('keeps separate drafts, offers only lower portrait sizes and rational frame rates, and disables unsupported GPU encoding', async () => {
  render(<CompressionDialog {...props} />);
  const target = await screen.findByRole('textbox', { name: 'Target size' });
  fireEvent.change(target, { target: { value: '2G' } });
  fireEvent.click(screen.getByRole('button', { name: 'Advanced' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Video bitrate' }), { target: { value: '8Mbps' } });
  const resolution = screen.getByRole('combobox', { name: 'Resolution' });
  expect(resolution.textContent).toContain('1440 × 2560'); expect(resolution.textContent).not.toContain('3840 × 2160');
  expect(screen.getByRole('option', { name: 'Keep source · Variable frame rate · 59.94 fps' })).toBeTruthy();
  fireEvent.change(resolution, { target: { value: '1080' } });
  fireEvent.change(screen.getByRole('combobox', { name: 'Frame rate' }), { target: { value: '30000/1001' } });
  expect((screen.getByRole('checkbox', { name: 'GPU encoding acceleration' }) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole('checkbox', { name: 'GPU encoding acceleration' }) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByText('No compatible NVIDIA encoder is available.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Simple' }));
  expect((screen.getByRole('textbox', { name: 'Target size' }) as HTMLInputElement).value).toBe('2G');
  fireEvent.click(screen.getByRole('button', { name: 'Advanced' }));
  expect((screen.getByRole('textbox', { name: 'Video bitrate' }) as HTMLInputElement).value).toBe('8Mbps');
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Start compression' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('tasks.convert', { id: 'local', requestId: expect.any(String), compression: { videoBitrateBps: 8000000, maxWidth: 1080, maxHeight: 1920, fps: '30000/1001', encoder: 'libx265' } }));
  expect(screen.getByText(/estimated only/)).toBeTruthy();
});

it('debounces changing targets, cancels the obsolete probe and never accepts its late response', async () => {
  vi.useFakeTimers(); const base = command.getMockImplementation()!;
  let finishOld!: (value: unknown) => void;
  command.mockImplementation((name, args) => name === 'tasks.convertPreview' && args.compression.targetBytes === 2000000000 ? new Promise(resolve => { finishOld = resolve; }) : base(name, args));
  render(<CompressionDialog {...props} />); await act(async () => {});
  const target = screen.getByRole('textbox', { name: 'Target size' });
  fireEvent.change(target, { target: { value: '2G' } });
  await act(async () => { await vi.advanceTimersByTimeAsync(250); });
  const old = command.mock.calls.find(([name, args]) => name === 'tasks.convertPreview' && args.compression.targetBytes === 2000000000)!;
  fireEvent.change(target, { target: { value: '1G' } });
  expect(command).toHaveBeenCalledWith('probe.cancel', { id: old[1].requestId });
  await act(async () => finishOld({ ...source, plan: { encoder: 'libx265', videoBitrateBps: 10000000, estimatedSizeBytes: 2000000000, width: 2160, height: 3840, fps: null, targetBytes: 2000000000 } }));
  expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(249); });
  expect(command.mock.calls.some(([name, args]) => name === 'tasks.convertPreview' && args.compression.targetBytes === 1000000000)).toBe(false);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByText('Approximate target size · 1 GB')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Start compression' }));
  await act(async () => {});
  expect(command).toHaveBeenCalledWith('tasks.convert', { id: 'local', requestId: expect.any(String), compression: { targetBytes: 1000000000, encoder: 'libx265' } });
});

it('defaults to an available GPU encoder and never starts without a valid plan', async () => {
  const base = command.getMockImplementation()!;
  command.mockImplementation(async (name, args) => {
    const value = await base(name, args);
    return name === 'tasks.convertPreview' ? { ...value, capabilities: [{ encoder: 'hevc_nvenc', label: 'NVIDIA H.265', available: true, reason: null }] } : value;
  });
  render(<CompressionDialog {...props} />);
  await screen.findByRole('textbox', { name: 'Target size' });
  expect((screen.getByRole('checkbox', { name: 'GPU encoding acceleration' }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Advanced' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Video bitrate' }), { target: { value: '8Mbps' } });
  expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(true);
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Start compression' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('tasks.convert', { id: 'local', requestId: expect.any(String), compression: { videoBitrateBps: 8000000, encoder: 'hevc_nvenc' } }));
});

it('shows target savings and allows verified GPU acceleration in simple size mode', async () => {
  const base = command.getMockImplementation()!;
  command.mockImplementation(async (name, args) => {
    const value = await base(name, args);
    return name === 'tasks.convertPreview' ? { ...value, capabilities: [{ encoder: 'hevc_nvenc', label: 'NVIDIA H.265', available: true, reason: null }] } : value;
  });
  render(<CompressionDialog {...props} />);
  fireEvent.change(await screen.findByRole('textbox', { name: 'Target size' }), { target: { value: '2G' } });
  expect(screen.getByText('Target reduction: 50%')).toBeTruthy();
  expect((screen.getByRole('checkbox', { name: 'GPU encoding acceleration' }) as HTMLInputElement).checked).toBe(true);
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Start compression' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('tasks.convert', { id: 'local', requestId: expect.any(String), compression: { targetBytes: 2000000000, encoder: 'hevc_nvenc' } }));
});

it('keeps the selected GPU and its rejection visible when another GPU supports the changed resolution', async () => {
  const base = command.getMockImplementation()!;
  const rejection = 'NVIDIA cannot encode the selected resolution.';
  command.mockImplementation(async (name, args) => {
    const value = await base(name, args);
    if (name !== 'tasks.convertPreview') return value;
    const supported = !args.compression.maxWidth;
    return { ...value, capabilities: [
      { encoder: 'hevc_nvenc', label: 'NVIDIA H.265', available: supported, reason: supported ? null : rejection },
      { encoder: 'hevc_qsv', label: 'Intel H.265', available: true, reason: null },
    ] };
  });
  render(<CompressionDialog {...props} />);
  await screen.findByRole('textbox', { name: 'Target size' });
  fireEvent.click(screen.getByRole('button', { name: 'Advanced' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(screen.getByRole('combobox', { name: 'Resolution' }), { target: { value: '1080' } });
  expect(await screen.findByText(`NVIDIA H.265 · ${rejection}`)).toBeTruthy();
  expect((screen.getByRole('checkbox', { name: 'GPU encoding acceleration' }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(true);
  expect(command.mock.calls.filter(([name, args]) => name === 'tasks.convertPreview' && args.compression.maxWidth === 1080).every(([, args]) => args.compression.encoder === 'hevc_nvenc')).toBe(true);
});

it('selects the first supported GPU and preserves manual CPU selection through new previews and mode changes', async () => {
  const base = command.getMockImplementation()!;
  command.mockImplementation(async (name, args) => {
    const value = await base(name, args);
    return name === 'tasks.convertPreview' ? { ...value, capabilities: [
      { encoder: 'hevc_nvenc', label: 'NVIDIA H.265', available: false, reason: 'Unavailable' },
      { encoder: 'hevc_qsv', label: 'Intel H.265', available: true, reason: null },
      { encoder: 'hevc_amf', label: 'AMD H.265', available: true, reason: null },
    ] } : value;
  });
  render(<CompressionDialog {...props} />);
  const target = await screen.findByRole('textbox', { name: 'Target size' });
  const gpu = screen.getByRole('checkbox', { name: 'GPU encoding acceleration' }) as HTMLInputElement;
  expect(gpu.checked).toBe(true);
  await waitFor(() => expect(command.mock.calls.some(([name, args]) => name === 'tasks.convertPreview' && args.compression.encoder === 'hevc_qsv')).toBe(true));
  fireEvent.click(gpu);
  fireEvent.change(target, { target: { value: '2G' } });
  fireEvent.click(screen.getByRole('button', { name: 'Advanced' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Video bitrate' }), { target: { value: '8Mbps' } });
  fireEvent.click(screen.getByRole('button', { name: 'Simple' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(false));
  expect(gpu.checked).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Start compression' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('tasks.convert', { id: 'local', requestId: expect.any(String), compression: { targetBytes: 2000000000, encoder: 'libx265' } }));
});

it('shows the preview rejection and keeps compression disabled instead of discarding source tracks', async () => {
  const base = command.getMockImplementation()!;
  command.mockImplementation(async (name, args) => {
    const value = await base(name, args);
    return name === 'tasks.convertPreview' && args.compression.targetBytes ? { ...value, plan: null, blockingReasons: ['The target is too small to preserve all source audio and subtitles.'] } : value;
  });
  render(<CompressionDialog {...props} />);
  await screen.findByRole('textbox', { name: 'Target size' });
  expect(await screen.findByText('The target is too small to preserve all source audio and subtitles.')).toBeTruthy();
  expect(screen.getByText('Output: HEVC · MKV · HDR10 · 10-bit · Saves a new file and keeps the source.')).toBeTruthy();
  fireEvent.click(screen.getByText('Compression and preservation details'));
  expect(screen.getByText('Preserves 2 audio tracks, 3 subtitle tracks and 2 attachments.')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Start compression' }) as HTMLButtonElement).disabled).toBe(true);
  expect(command.mock.calls.some(([name]) => name === 'tasks.convert')).toBe(false);
});

it.each(['cancelled', 'queued'] as const)('cancels only pending queue preparation, waits for its result and handles %s completion', async outcome => {
  const base = command.getMockImplementation()!;
  let resolvePreparation!: (value: unknown) => void; let rejectPreparation!: (reason: Error) => void;
  command.mockImplementation((name, args) => name === 'tasks.convert' ? new Promise((resolve, reject) => { resolvePreparation = resolve; rejectPreparation = reject; }) : base(name, args));
  render(<CompressionDialog {...props} />);
  await screen.findByRole('textbox', { name: 'Target size' });
  const start = screen.getByRole('button', { name: 'Start compression' });
  await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(start);
  expect(screen.getByRole('progressbar', { name: 'Adding compression to queue' })).toBeTruthy();
  const prepare = command.mock.calls.find(([name]) => name === 'tasks.convert')!;
  expect(prepare[1].requestId).toEqual(expect.any(String));
  expect(command.mock.calls.filter(([name]) => name === 'tasks.convertPreview').some(([, args]) => args.requestId === prepare[1].requestId)).toBe(false);
  const beforeCancellation = command.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'Cancel preparation' }));
  expect(command.mock.calls.slice(beforeCancellation)).toEqual([['probe.cancel', { id: prepare[1].requestId }]]);
  expect((screen.getByRole('button', { name: 'Cancelling preparation…' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { bubbles: true, cancelable: true }));
  expect(command.mock.calls.slice(beforeCancellation)).toHaveLength(1);
  expect(props.close).not.toHaveBeenCalled(); expect(props.queued).not.toHaveBeenCalled();
  const queued = { ...makeTask('compressed'), status: 'waiting_processing' };
  await act(async () => { if (outcome === 'queued') resolvePreparation(queued); else rejectPreparation(new Error('Operation cancelled during compression preparation')); });
  expect(props.close).toHaveBeenCalledTimes(1);
  if (outcome === 'queued') expect(props.queued).toHaveBeenCalledExactlyOnceWith(queued);
  else expect(props.queued).not.toHaveBeenCalled();
  expect(screen.queryByRole('alert')).toBeNull();
  expect(command.mock.calls.some(([name]) => name === 'tasks.cancel' || name === 'tasks.delete')).toBe(false);
});

it('cancels pending queue preparation on unmount without closing again or reporting a cancellation error', async () => {
  const base = command.getMockImplementation()!; let rejectPreparation!: (reason: Error) => void;
  command.mockImplementation((name, args) => name === 'tasks.convert' ? new Promise((_resolve, reject) => { rejectPreparation = reject; }) : base(name, args));
  const view = render(<CompressionDialog {...props} />);
  await screen.findByRole('textbox', { name: 'Target size' });
  const start = screen.getByRole('button', { name: 'Start compression' });
  await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(start);
  const prepare = command.mock.calls.find(([name]) => name === 'tasks.convert')!;
  view.unmount();
  expect(command).toHaveBeenCalledWith('probe.cancel', { id: prepare[1].requestId });
  await act(async () => rejectPreparation(new Error('Operation cancelled during compression preparation')));
  expect(props.close).not.toHaveBeenCalled(); expect(props.queued).not.toHaveBeenCalled();
});
