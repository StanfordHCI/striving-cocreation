import { useId, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown, Lock, Search } from 'lucide-react';
import type { HierarchyEntityType, HierarchyNode } from '../../api/contracts';
import './table.css';

type HierarchyTableProps = {
  roots: HierarchyNode[];
  selectedId: number | null;
  onSelectEntity: (id: number | null) => void;
};

type Row = { node: HierarchyNode; parents: Map<number, HierarchyNode>; children: Set<number> };
type SortKey = 'id' | 'text' | 'seen';
type Sort = { key: SortKey; direction: 'ascending' | 'descending' };

const TYPES: HierarchyEntityType[] = ['goal', 'activity', 'action', 'operation'];
const LABELS: Record<HierarchyEntityType, string> = {
  goal: 'Goal', activity: 'Activity', action: 'Action', operation: 'Operation',
};
const PLURALS: Record<HierarchyEntityType, string> = {
  goal: 'Goals', activity: 'Activities', action: 'Actions', operation: 'Operations',
};
const PARENTS: Partial<Record<HierarchyEntityType, string>> = {
  activity: 'Goals', action: 'Activities', operation: 'Actions',
};
const CHILDREN: Partial<Record<HierarchyEntityType, string>> = {
  goal: 'Activities', activity: 'Actions', action: 'Operations',
};
const PAGE_SIZE = 50;
const DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function collectRows(roots: HierarchyNode[]): Row[] {
  const rows = new Map<number, Row>();
  function visit(node: HierarchyNode, parent?: HierarchyNode) {
    const existing = rows.get(node.id);
    const row = existing ?? { node, parents: new Map<number, HierarchyNode>(), children: new Set<number>() };
    if (parent) row.parents.set(parent.id, parent);
    if (existing) return;
    rows.set(node.id, row);
    for (const child of node.children) {
      row.children.add(child.id);
      visit(child, node);
    }
  }
  roots.forEach((root) => visit(root));
  return [...rows.values()];
}

function lastSeen(node: HierarchyNode): number | null {
  const value = node.timestamp_end || node.timestamp_start;
  if (!value) return null;
  // Backend timestamps without an offset are UTC.
  const timestamp = Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export default function HierarchyTable({ roots, selectedId, onSelectEntity }: HierarchyTableProps) {
  const [query, setQuery] = useState('');
  const [type, setType] = useState<HierarchyEntityType>('goal');
  const [sort, setSort] = useState<Sort>({ key: 'seen', direction: 'descending' });
  const [page, setPage] = useState(0);
  const tabsId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const rows = useMemo(() => collectRows(roots), [roots]);
  const counts = useMemo(() => {
    const totals: Record<HierarchyEntityType, number> = { goal: 0, activity: 0, action: 0, operation: 0 };
    for (const { node } of rows) totals[node.type] += 1;
    return totals;
  }, [rows]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return rows.filter(({ node, parents }) => {
      if (node.type !== type) return false;
      const searchable = [
        `#${node.id}`, node.type, node.text,
        ...[...parents.values()].map((parent) => `#${parent.id} ${parent.text}`),
      ].join(' ').toLocaleLowerCase();
      return searchable.includes(needle);
    }).sort((a, b) => {
      let comparison: number;
      if (sort.key === 'seen') {
        const left = lastSeen(a.node);
        const right = lastSeen(b.node);
        if (left === null || right === null) {
          return left === right ? a.node.id - b.node.id : left === null ? 1 : -1;
        }
        comparison = left - right;
      } else if (sort.key === 'text') {
        comparison = a.node.text.localeCompare(b.node.text);
      } else {
        comparison = a.node.id - b.node.id;
      }
      return (sort.direction === 'ascending' ? comparison : -comparison) || a.node.id - b.node.id;
    });
  }, [rows, query, type, sort]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const offset = currentPage * PAGE_SIZE;
  const visible = filtered.slice(offset, offset + PAGE_SIZE);
  const columnCount = 4 + (PARENTS[type] ? 1 : 0) + (CHILDREN[type] ? 1 : 0);

  function selectType(next: HierarchyEntityType) {
    setType(next);
    setPage(0);
  }

  function sortBy(key: SortKey) {
    setSort((current) => ({
      key,
      direction: current.key === key && current.direction === 'ascending' ? 'descending' : 'ascending',
    }));
    setPage(0);
  }

  function column(key: SortKey, label: string) {
    const sorted = sort.key === key;
    const Icon = sorted ? sort.direction === 'ascending' ? ArrowUp : ArrowDown : ArrowUpDown;
    return (
      <th scope="col" aria-sort={sorted ? sort.direction : undefined}>
        <button type="button" onClick={() => sortBy(key)}>
          {label}<Icon size={13} aria-hidden="true" />
        </button>
      </th>
    );
  }

  return (
    <div className="hierarchy-table-view">
      <div className="hierarchy-level-tabs" role="tablist" aria-label="Hierarchy levels">
        {TYPES.map((value, index) => (
          <button
            key={value}
            ref={(element) => { tabRefs.current[index] = element; }}
            id={`${tabsId}-${value}`}
            type="button"
            role="tab"
            aria-selected={type === value}
            aria-controls={`${tabsId}-panel`}
            tabIndex={type === value ? 0 : -1}
            onClick={() => selectType(value)}
            onKeyDown={(event) => {
              let next: number;
              if (event.key === 'ArrowRight') next = (index + 1) % TYPES.length;
              else if (event.key === 'ArrowLeft') next = (index + TYPES.length - 1) % TYPES.length;
              else if (event.key === 'Home') next = 0;
              else if (event.key === 'End') next = TYPES.length - 1;
              else return;
              event.preventDefault();
              selectType(TYPES[next]);
              tabRefs.current[next]?.focus();
            }}
          >
            {PLURALS[value]} <span>{counts[value]}</span>
          </button>
        ))}
      </div>
      <div className="hierarchy-table-panel" id={`${tabsId}-panel`} role="tabpanel" aria-labelledby={`${tabsId}-${type}`}>
        <div className="hierarchy-table-toolbar">
          <label className="hierarchy-table-search">
            <Search size={16} aria-hidden="true" />
            <input
              type="search"
              aria-label="Filter hierarchy by label, ID, or parent"
              placeholder="Filter by label, ID, or parent…"
              value={query}
              onChange={(event) => { setQuery(event.target.value); setPage(0); }}
            />
          </label>
          <span className="hierarchy-table-count" role="status">{filtered.length} of {counts[type]} {PLURALS[type].toLowerCase()}</span>
        </div>
        <div className="hierarchy-table-scroll" tabIndex={0} role="region" aria-label="Hierarchy items">
          <table className="hierarchy-data-table">
            <caption>Saved hierarchy. Select an item to inspect its details and evidence.</caption>
            <thead>
              <tr>
                {column('id', 'ID')}
                {column('text', LABELS[type])}
                {PARENTS[type] && <th scope="col">{PARENTS[type]}</th>}
                {CHILDREN[type] && <th scope="col">{CHILDREN[type]}</th>}
                <th scope="col">Confidence</th>
                {column('seen', 'Last seen')}
              </tr>
            </thead>
            <tbody>
              {visible.map(({ node, parents, children }) => {
                const seen = lastSeen(node);
                const confidence = node.metadata.confidence;
                return (
                  <tr key={node.id} className={selectedId === node.id ? 'is-selected' : undefined} onClick={() => onSelectEntity(node.id)}>
                    <td className="hierarchy-table-id">#{node.id}</td>
                    <td className="hierarchy-table-description">
                      <button type="button" aria-label={`Inspect ${LABELS[node.type].toLowerCase()} #${node.id}: ${node.text}`}>
                        {node.text}
                      </button>
                      {node.locked && <Lock size={13} aria-label="Locked" />}
                    </td>
                    {PARENTS[type] && <td>
                      <div className="hierarchy-table-parents">
                        {parents.size === 0 ? <span className="hierarchy-table-missing">—</span> : [...parents.values()].map((parent) => (
                          <button type="button" key={parent.id} title={parent.text} aria-label={`Inspect parent #${parent.id}: ${parent.text}`} onClick={(event) => { event.stopPropagation(); onSelectEntity(parent.id); }}>
                            #{parent.id}
                          </button>
                        ))}
                      </div>
                    </td>}
                    {CHILDREN[type] && <td className="hierarchy-table-number">{children.size}</td>}
                    <td className="hierarchy-table-number">{typeof confidence === 'number' || (typeof confidence === 'string' && confidence.trim()) ? confidence : <span className="hierarchy-table-missing">—</span>}</td>
                    <td className="hierarchy-table-date">
                      {seen === null ? <span className="hierarchy-table-missing">—</span> : <time dateTime={new Date(seen).toISOString()} title="Capture time in your local time zone">{DATE_FORMAT.format(seen)}</time>}
                    </td>
                  </tr>
                );
              })}
              {visible.length === 0 && (
                <tr><td colSpan={columnCount} className="hierarchy-table-empty">
                  {counts[type] === 0 ? `No ${PLURALS[type].toLowerCase()} yet.` : `No ${PLURALS[type].toLowerCase()} match this search.`}
                  {query && <button type="button" onClick={() => { setQuery(''); setPage(0); }}>Clear search</button>}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="hierarchy-table-footer">
          <span>{filtered.length === 0 ? '0 items' : `${offset + 1}–${Math.min(offset + PAGE_SIZE, filtered.length)} of ${filtered.length}`}</span>
          {pageCount > 1 && <div className="hierarchy-table-pagination">
            <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button>
            <span>Page {currentPage + 1} of {pageCount}</span>
            <button type="button" disabled={currentPage === pageCount - 1} onClick={() => setPage(currentPage + 1)}>Next</button>
          </div>}
        </div>
      </div>
    </div>
  );
}
