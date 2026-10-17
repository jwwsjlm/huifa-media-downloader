import { memo, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';

export const ROW_HEIGHT = 56;
export function virtualRange(count: number, top: number, height: number): [number, number] {
  const start = Math.max(0, Math.min(count, Math.floor(Math.max(0, top) / ROW_HEIGHT) - 4));
  return [start, Math.min(count, Math.max(start, Math.ceil((Math.max(0, top) + Math.max(height, ROW_HEIGHT)) / ROW_HEIGHT) + 4))];
}
export function keyboardIndex(key: string, current: number, length: number): number {
  if (!length) return -1;
  if (key === 'Home') return 0;
  if (key === 'End') return length - 1;
  if (key === 'ArrowUp') return Math.max(0, current - 1);
  if (key === 'ArrowDown') return Math.min(length - 1, current + 1);
  return Math.max(0, current);
}
interface Props { ids: string[]; selected: string; onSelect: (id: string) => void; selection?: string[]; onSelectionChange?: (ids: string[]) => void; renderRow: (id: string) => ReactNode; label: string; scrollKey: string; onContextMenu?: (id: string, position: { x: number; y: number }, trigger: HTMLElement) => void }
const scrolls = new Map<string, number>();
export const VirtualList = memo(function VirtualList({ ids, selected, onSelect, selection, onSelectionChange, renderRow, label, scrollKey, onContextMenu }: Props) {
  const ref = useRef<HTMLDivElement>(null); const idPrefix = useId().replace(/:/g, '');
  const anchor = useRef(selected);
  const multiple = selection !== undefined && onSelectionChange !== undefined;
  const selectedIds = useMemo(() => new Set(selection || [selected]), [selection, selected]);
  useEffect(() => { if (selection?.length === 1) anchor.current = selection[0]; }, [selection]);
  function select(id: string, shift = false, toggle = false) {
    if (multiple) {
      const anchorIndex = ids.indexOf(anchor.current);
      if (shift && anchorIndex >= 0) {
        const index = ids.indexOf(id);
        const range = ids.slice(Math.min(anchorIndex, index), Math.max(anchorIndex, index) + 1);
        onSelectionChange(toggle ? [...new Set([...selection, ...range])] : range);
      } else {
        anchor.current = id;
        onSelectionChange(toggle ? selectedIds.has(id) ? selection.filter(value => value !== id) : [...selection, id] : [id]);
      }
    }
    onSelect(id);
  }
  const [viewport, setViewport] = useState({ top: scrolls.get(scrollKey) || 0, height: 400 });
  useEffect(() => {
    const element = ref.current; if (!element) return;
    element.scrollTop = scrolls.get(scrollKey) || 0;
    const resize = new ResizeObserver(() => setViewport(value => ({ ...value, height: element.clientHeight })));
    resize.observe(element);
    return () => resize.disconnect();
  }, [scrollKey]);
  const [start, end] = virtualRange(ids.length, viewport.top, viewport.height);
  const activeRendered = ids.slice(start, end).includes(selected);
  return <div ref={ref} className={`virtual-list${multiple ? ' multi-select-list' : ''}`} tabIndex={0} role="listbox" aria-label={label} aria-multiselectable={multiple || undefined} aria-activedescendant={activeRendered ? `${idPrefix}-${selected}` : undefined}
    onScroll={event => { const top = event.currentTarget.scrollTop; scrolls.set(scrollKey, top); setViewport(value => ({ ...value, top })); }}
    onKeyDown={event => {
      if (event.target !== event.currentTarget) return;
      if (multiple && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
        event.preventDefault(); onSelectionChange(ids); return;
      }
      if (event.target === event.currentTarget && onContextMenu && (event.key === 'ContextMenu' || event.shiftKey && event.key === 'F10') && ids.includes(selected)) {
        event.preventDefault();
        const row = document.getElementById(`${idPrefix}-${selected}`); const bounds = (row || event.currentTarget).getBoundingClientRect();
        onContextMenu(selected, { x: bounds.left + 12, y: Math.max(bounds.top, event.currentTarget.getBoundingClientRect().top) + Math.min(bounds.height, ROW_HEIGHT) }, event.currentTarget);
        return;
      }
      if (event.target !== event.currentTarget || !['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter'].includes(event.key)) return;
      event.preventDefault();
      const index = keyboardIndex(event.key, ids.indexOf(selected), ids.length); if (index < 0) return;
      select(ids[index], event.shiftKey, event.ctrlKey || event.metaKey);
      const element = event.currentTarget; const rowTop = index * ROW_HEIGHT;
      if (rowTop < element.scrollTop) element.scrollTop = rowTop;
      else if (rowTop + ROW_HEIGHT > element.scrollTop + element.clientHeight) element.scrollTop = rowTop + ROW_HEIGHT - element.clientHeight;
    }}>
    <div className="virtual-canvas" style={{ height: ids.length * ROW_HEIGHT }}>
      {ids.slice(start, end).map((id, offset) => <div key={id} id={`${idPrefix}-${id}`} role="option" aria-selected={selectedIds.has(id)} aria-posinset={start + offset + 1} aria-setsize={ids.length}
        className={`list-row ${selectedIds.has(id) ? 'selected' : ''}`} style={{ top: (start + offset) * ROW_HEIGHT, height: ROW_HEIGHT }}
        onMouseDown={multiple ? event => { if (!(event.target as HTMLElement).closest('button,input,select,a')) event.preventDefault(); } : undefined}
        onContextMenu={onContextMenu ? event => { event.preventDefault(); if (multiple && selectedIds.has(id)) onSelect(id); else select(id); onContextMenu(id, { x: event.clientX, y: event.clientY }, ref.current!); } : undefined}
        onClick={event => { if (!(event.target as HTMLElement).closest('button,input,select,a')) { select(id, event.shiftKey, event.ctrlKey || event.metaKey); ref.current?.focus({ preventScroll: true }); } }}>{renderRow(id)}</div>)}
    </div>
  </div>;
});
