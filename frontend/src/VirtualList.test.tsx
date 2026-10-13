import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { keyboardIndex, ROW_HEIGHT, virtualRange, VirtualList } from './VirtualList';

describe('56px virtual lists', () => {
  it('bounds rendered rows at large offsets and clips empty lists', () => {
    expect(ROW_HEIGHT).toBe(56); expect(virtualRange(0, 0, 400)).toEqual([0, 0]);
    expect(virtualRange(100000, 56000, 336)).toEqual([996, 1010]);
    expect(virtualRange(5, 50000, 400)).toEqual([5, 5]);
  });
  it('renders a bounded subset of 100000 rows with accurate positions', () => {
    const ids = Array.from({ length: 100000 }, (_, index) => `row-${index}`);
    render(<VirtualList ids={ids} selected="row-0" onSelect={() => {}} renderRow={id => id} label="Tasks" scrollKey="test-large" />);
    const list = screen.getByRole('listbox'); expect(screen.getAllByRole('option').length).toBeLessThan(20);
    Object.defineProperty(list, 'scrollTop', { writable: true, value: 56000 }); fireEvent.scroll(list);
    const rows = screen.getAllByRole('option'); expect(rows[0].getAttribute('aria-posinset')).toBe('997'); expect(rows[0].getAttribute('aria-setsize')).toBe('100000');
    expect((rows[0] as HTMLElement).style.top).toBe('55776px');
  });
  it('uses keyboard navigation and keeps selection identity after an unrelated progress render', () => {
    function List() { const [selected, setSelected] = useState('b'); return <VirtualList ids={['a', 'b', 'c']} selected={selected} onSelect={setSelected} renderRow={id => <span>{id}</span>} label="Tasks" scrollKey="test-keyboard" />; }
    const { rerender } = render(<List />); const list = screen.getByRole('listbox'); list.focus();
    fireEvent.keyDown(list, { key: 'End' }); expect(screen.getByRole('option', { selected: true }).textContent).toBe('c');
    rerender(<List />); expect(screen.getByRole('option', { selected: true }).textContent).toBe('c');
    fireEvent.keyDown(list, { key: 'ArrowUp' }); expect(screen.getByRole('option', { selected: true }).textContent).toBe('b');
    fireEvent.keyDown(list, { key: 'Home' }); expect(screen.getByRole('option', { selected: true }).textContent).toBe('a');
    expect(keyboardIndex('ArrowDown', -1, 3)).toBe(0); expect(keyboardIndex('End', 0, 0)).toBe(-1);
  });
});
