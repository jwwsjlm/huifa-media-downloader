import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CoverStudio } from './CoverStudio';

const transport = vi.hoisted(() => ({ command: vi.fn(), result: vi.fn(), upload: vi.fn() }));
vi.mock('./transport', () => ({ host: transport }));
const media = { id: 'cover-media', taskId: 'task', title: 'Sample media', filename: 'sample.mp4', size: 10, duration: 1 };
const translate = (_zh: string, en: string) => en;
const run = (operation: () => Promise<unknown>) => { void operation().catch(() => {}); };
beforeEach(() => {
  transport.command.mockReset(); transport.result.mockReset(); transport.upload.mockReset();
  transport.command.mockImplementation(async () => ({ previewUrl: 'data:image/png;base64,AA==' }));
  transport.result.mockResolvedValue(undefined);
});

describe('cover operation lifecycle', () => {
  it('blocks competing edits during an upload, then releases that upload when restoring the original', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let finishUpload!: (value: { id: string }) => void;
    transport.upload.mockReturnValue(new Promise(resolve => { finishUpload = resolve; }));
    const view = render(<CoverStudio media={media} close={vi.fn()} run={run} t={translate} />);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Export JPEG' }) as HTMLButtonElement).disabled).toBe(false));
    const input = view.container.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [new File(['first'], 'first.png')] } });
    expect((screen.getByRole('button', { name: 'Uploading…' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Restore original cover' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Generate AI variation' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Export JPEG' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Close' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(input, { target: { files: [new File(['second'], 'second.png')] } });
    fireEvent.click(screen.getByRole('button', { name: 'Restore original cover' }));
    expect(transport.upload).toHaveBeenCalledTimes(1);
    expect(confirm).not.toHaveBeenCalled();
    await act(async () => finishUpload({ id: 'first-upload' }));
    fireEvent.click(screen.getByRole('button', { name: 'Restore original cover' }));
    expect(transport.command).toHaveBeenCalledWith('cover.release_upload', { assetId: 'first-upload' });
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cover.preview', expect.objectContaining({ assetId: '', original: true })));
    confirm.mockRestore();
  });

  it('releases replaced uploads and the remaining upload when the editor closes', async () => {
    transport.upload.mockResolvedValueOnce({ id: 'first-upload' }).mockResolvedValueOnce({ id: 'second-upload' });
    const view = render(<CoverStudio media={media} close={vi.fn()} run={run} t={translate} />);
    const input = view.container.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [new File(['first'], 'first.png', { type: 'image/png' })] } });
    await waitFor(() => expect(transport.upload).toHaveBeenCalledTimes(1));
    await act(async () => {});
    fireEvent.change(input, { target: { files: [new File(['second'], 'second.png', { type: 'image/png' })] } });
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cover.release_upload', { assetId: 'first-upload' }));
    view.unmount();
    expect(transport.command).toHaveBeenCalledWith('cover.release_upload', { assetId: 'second-upload' });
  });

  it('releases an upload that finishes after the editor has closed', async () => {
    let finishUpload!: (value: { id: string }) => void;
    transport.upload.mockReturnValue(new Promise(resolve => { finishUpload = resolve; }));
    const view = render(<CoverStudio media={media} close={vi.fn()} run={run} t={translate} />);
    fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [new File(['late'], 'late.png')] } });
    view.unmount();
    await act(async () => finishUpload({ id: 'late-upload' }));
    expect(transport.command).toHaveBeenCalledWith('cover.release_upload', { assetId: 'late-upload' });
  });

  it('releases an uploaded source after generation without releasing exported or generated assets', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    transport.upload.mockResolvedValue({ id: 'uploaded-source' });
    transport.command.mockImplementation(async command => ({ previewUrl: 'data:image/png;base64,AA==', ...(command === 'cover.export' ? { assetId: 'exported-cover' } : command === 'cover.generate' ? { assetId: 'generated-cover' } : {}) }));
    const view = render(<CoverStudio media={media} close={vi.fn()} run={run} t={translate} />);
    fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [new File(['source'], 'source.png')] } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Export JPEG' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Export JPEG' }));
    await waitFor(() => expect(transport.result).toHaveBeenCalledWith(media.id, 'download', 'cover'));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Generate AI variation' }) as HTMLButtonElement).disabled).toBe(false));
    expect(transport.command.mock.calls.filter(([command]) => command === 'cover.release_upload')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Generate AI variation' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cover.release_upload', { assetId: 'uploaded-source' }));
    fireEvent.click(screen.getByRole('button', { name: 'Restore original cover' }));
    view.unmount();
    expect(transport.command.mock.calls.filter(([command]) => command === 'cover.release_upload')).toEqual([['cover.release_upload', { assetId: 'uploaded-source' }]]);
  });

  it('keeps export active until the save dialog finishes and never sends an AI cancellation', async () => {
    let finishSave!: () => void;
    transport.result.mockImplementation(() => new Promise<void>(resolve => { finishSave = resolve; }));
    render(<CoverStudio media={media} close={vi.fn()} run={run} t={translate} />);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Export JPEG' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Export JPEG' }));
    await waitFor(() => expect(transport.result).toHaveBeenCalledWith(media.id, 'download', 'cover'));
    expect((screen.getByRole('button', { name: 'Exporting…' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
    expect(transport.command.mock.calls.some(([command]) => command === 'cover.cancel')).toBe(false);
    await act(async () => finishSave());
    expect((screen.getByRole('button', { name: 'Export JPEG' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('waits for cancelled AI work to settle before enabling another generation', async () => {
    let rejectGeneration!: (reason: Error) => void;
    transport.command.mockImplementation(command => command === 'cover.generate'
      ? new Promise((_resolve, reject) => { rejectGeneration = reject; })
      : Promise.resolve({ previewUrl: 'data:image/png;base64,AA==' }));
    render(<CoverStudio media={media} close={vi.fn()} run={run} t={translate} />);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Generate AI variation' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Generate AI variation' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel AI creation' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cover.cancel', { mediaId: media.id }));
    expect((screen.getByRole('button', { name: 'Generating…' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => rejectGeneration(new Error('Cancelled')));
    expect((screen.getByRole('button', { name: 'Generate AI variation' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole('status').textContent).toBe('AI creation cancelled');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
