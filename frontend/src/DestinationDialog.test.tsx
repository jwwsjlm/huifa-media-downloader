import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { DestinationDialog } from './DestinationDialog';
import { makeTask } from './test-fixtures';

const command=vi.hoisted(()=>vi.fn());
vi.mock('./transport',()=>({host:{command}}));
beforeEach(()=>command.mockReset().mockResolvedValue(true));
const task={...makeTask('conflict'),destinationConflict:{filename:'clip.mp4',files:['clip.mp4','clip.en.srt']}};
it('requires a second explicit confirmation before replacing another source',async()=>{
  const close=vi.fn();
  render(<DestinationDialog task={task} t={(_zh,en)=>en} close={close}/>);
  fireEvent.click(screen.getByRole('button',{name:'Replace existing files…'}));
  expect(command).not.toHaveBeenCalled();
  expect(screen.getByRole('alert').textContent).toContain('Failure or cancellation keeps the old files');
  fireEvent.click(screen.getByRole('button',{name:'Confirm replacement and download'}));
  await waitFor(()=>expect(command).toHaveBeenCalledWith('tasks.destination',{id:task.id,choice:'overwrite',confirmed:true}));
  await waitFor(()=>expect(close).toHaveBeenCalledOnce());
});
it('renames without granting replacement permission',async()=>{
  render(<DestinationDialog task={task} t={(_zh,en)=>en} close={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('New filename'),{target:{value:'clip (2).mp4'}});
  fireEvent.click(screen.getByRole('button',{name:'Download with new name'}));
  await waitFor(()=>expect(command).toHaveBeenCalledWith('tasks.destination',{id:task.id,choice:'rename',filename:'clip (2).mp4'}));
});
