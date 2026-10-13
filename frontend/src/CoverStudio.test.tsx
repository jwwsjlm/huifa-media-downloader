import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CoverStudio } from './CoverStudio';

const transport = vi.hoisted(() => ({ command: vi.fn(), result: vi.fn() }));
vi.mock('./transport', () => ({ host: transport }));
const media = { id: 'cover-media', taskId: 'task', title: 'Sample media', filename: 'sample.mp4', size: 10, duration: 1 };
const translate = (_zh: string, en: string) => en;
const run = (operation: () => Promise<unknown>) => { void operation().catch(() => {}); };
beforeEach(() => {
  transport.command.mockReset(); transport.result.mockReset();
  transport.command.mockImplementation(async () => ({ previewUrl: 'data:image/png;base64,AA==' }));
  transport.result.mockResolvedValue(undefined);
});

describe('cover operation lifecycle', () => {
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
    expect((screen.getByRole('button', { name: 'Generate AI variation' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => rejectGeneration(new Error('Cancelled')));
    expect((screen.getByRole('button', { name: 'Generate AI variation' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole('status').textContent).toBe('AI creation cancelled');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
