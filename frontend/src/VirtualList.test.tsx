import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
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
  it('reports the overscanned window only when its IDs change', () => {
    const ids = Array.from({ length: 100000 }, (_, index) => `media-${index}`);
    const changed = vi.fn();
    const props = { ids, selected: ids[0], onSelect: () => {}, renderRow: (id: string) => id, label: 'Media', scrollKey: 'test-visible' };
    const { rerender } = render(<VirtualList {...props} onVisibleIdsChange={changed} />);
    expect(changed).toHaveBeenCalledExactlyOnceWith(ids.slice(0, 12));
    rerender(<VirtualList {...props} ids={[...ids]} onVisibleIdsChange={visible => changed(visible)} />);
    expect(changed).toHaveBeenCalledTimes(1);
    const list = screen.getByRole('listbox');
    Object.defineProperty(list, 'scrollTop', { writable: true, value: 56000 }); fireEvent.scroll(list);
    expect(changed).toHaveBeenLastCalledWith(ids.slice(996, 1012));
    expect(changed.mock.calls[1][0]).toHaveLength(screen.getAllByRole('option').length);
    fireEvent.scroll(list);
    expect(changed).toHaveBeenCalledTimes(2);
    rerender(<VirtualList {...props} ids={[]} onVisibleIdsChange={changed} />);
    expect(changed).toHaveBeenLastCalledWith([]);
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
  it('selects a stable Shift range, toggles with Ctrl or Meta, and preserves the range on right-click', () => {
    const context = vi.fn();
    function List() {
      const [selected, setSelected] = useState('a'); const [selection, setSelection] = useState(['a']);
      return <VirtualList ids={['a', 'b', 'c', 'd']} selected={selected} selection={selection} onSelectionChange={setSelection} onSelect={setSelected} onContextMenu={context} renderRow={id => id} label="Tasks" scrollKey="test-multiple" />;
    }
    render(<List />);
    const chosen = () => screen.queryAllByRole('option', { selected: true }).map(row => row.textContent);
    const row = (name: string) => screen.getByRole('option', { name });
    expect(screen.getByRole('listbox').getAttribute('aria-multiselectable')).toBe('true');
    expect(fireEvent.mouseDown(row('c'), { shiftKey: true })).toBe(false);
    fireEvent.click(row('c'), { shiftKey: true }); expect(chosen()).toEqual(['a', 'b', 'c']);
    fireEvent.click(row('b'), { shiftKey: true }); expect(chosen()).toEqual(['a', 'b']);
    fireEvent.click(row('d'), { ctrlKey: true }); expect(chosen()).toEqual(['a', 'b', 'd']);
    fireEvent.click(row('b'), { metaKey: true }); expect(chosen()).toEqual(['a', 'd']);
    fireEvent.contextMenu(row('a')); expect(chosen()).toEqual(['a', 'd']); expect(context.mock.calls[0][0]).toBe('a');
    fireEvent.contextMenu(row('c')); expect(chosen()).toEqual(['c']);
  });
  it('extends and contracts keyboard ranges from their anchor', () => {
    function List() {
      const [selected, setSelected] = useState('b'); const [selection, setSelection] = useState(['b']);
      return <VirtualList ids={['a', 'b', 'c', 'd']} selected={selected} selection={selection} onSelectionChange={setSelection} onSelect={setSelected} renderRow={id => id} label="Tasks" scrollKey="test-multiple-keyboard" />;
    }
    render(<List />); const list = screen.getByRole('listbox');
    const chosen = () => screen.queryAllByRole('option', { selected: true }).map(row => row.textContent);
    fireEvent.keyDown(list, { key: 'ArrowDown', shiftKey: true }); expect(chosen()).toEqual(['b', 'c']);
    fireEvent.keyDown(list, { key: 'ArrowDown', shiftKey: true }); expect(chosen()).toEqual(['b', 'c', 'd']);
    fireEvent.keyDown(list, { key: 'ArrowUp', shiftKey: true }); expect(chosen()).toEqual(['b', 'c']);
    fireEvent.keyDown(list, { key: 'Home' }); expect(chosen()).toEqual(['a']);
  });
  it('selects every virtual item with Ctrl+A without rendering every row', () => {
    const ids = Array.from({ length: 10000 }, (_, index) => `task-${index}`);
    function List() {
      const [selection, setSelection] = useState<string[]>([]);
      return <><output aria-label="Selected count">{selection.length}</output><VirtualList ids={ids} selected={ids[0]} selection={selection} onSelectionChange={setSelection} onSelect={() => {}} renderRow={id => id} label="Tasks" scrollKey="test-multiple-all" /></>;
    }
    render(<List />); const list = screen.getByRole('listbox');
    expect(fireEvent.keyDown(list, { key: 'a', ctrlKey: true })).toBe(false);
    expect(screen.getByLabelText('Selected count').textContent).toBe('10000');
    expect(screen.getAllByRole('option').length).toBeLessThan(20);
    expect(screen.getAllByRole('option', { selected: true })).toHaveLength(screen.getAllByRole('option').length);
  });
});
