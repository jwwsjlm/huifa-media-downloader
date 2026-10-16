import { StrictMode, useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Button, Modal } from './components';

it('restores focus after closing a modal in StrictMode', () => {
  const show = vi.spyOn(HTMLDialogElement.prototype, 'showModal').mockImplementation(function (this: HTMLDialogElement) {
    this.open = true; this.querySelector('button')?.focus();
  });
  function Harness() {
    const [open, setOpen] = useState(false);
    return <><Button onClick={() => setOpen(true)}>Open</Button>{open && <Modal title="Test" t={(_zh, en) => en} close={() => setOpen(false)}><input /></Modal>}</>;
  }
  render(<StrictMode><Harness /></StrictMode>);
  const trigger = screen.getByRole('button', { name: 'Open' });
  trigger.focus(); fireEvent.click(trigger);
  expect(screen.getByRole('dialog')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.activeElement).toBe(trigger);
  show.mockRestore();
});
