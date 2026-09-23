import React, { useState, useEffect, useRef, useMemo } from 'react';
import Modal from './Modal';
import './CommandPalette.css';
import type { EntitySummary, EntityType } from '../api/contracts';

const COMMAND_TYPES = {
  NAVIGATE: 'navigate',
  ENTITY: 'entity',
  FILTER: 'filter',
  ACTION: 'action',
} as const;

type CommandType = typeof COMMAND_TYPES[keyof typeof COMMAND_TYPES];
type Command = {
  type: CommandType;
  id: string;
  label: string;
  action: () => void;
  shortcut?: string;
  icon?: string;
  entityType?: EntityType;
  entityId?: number;
};

type CommandPaletteProps = {
  isOpen: boolean;
  onClose: () => void;
  entities: EntitySummary[];
  onNavigate: (view: string) => void;
  onSelectEntity: (entityId: number) => void;
  onExport?: () => void;
};

export default function CommandPalette({ 
  isOpen, 
  onClose, 
  entities, 
  onNavigate, 
  onSelectEntity,
  onExport,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Build command list
  const commands = useMemo(() => {
    const items: Command[] = [];

    // Navigation commands
    items.push(
      { type: COMMAND_TYPES.NAVIGATE, id: 'nav-goals', label: 'Go to Goals', shortcut: 'G', action: () => onNavigate('goals') },
      { type: COMMAND_TYPES.NAVIGATE, id: 'nav-browse', label: 'Go to Browse', shortcut: 'B', action: () => onNavigate('browse') },
    );

    // Action commands
    items.push(
      { type: COMMAND_TYPES.ACTION, id: 'export', label: 'Export data...', icon: '↓', action: () => onExport?.() },
    );

    // Entity search
    entities.forEach(entity => {
      items.push({
        type: COMMAND_TYPES.ENTITY,
        id: `entity-${entity.id}`,
        label: entity.text || 'Unnamed',
        entityType: entity.type,
        entityId: entity.id,
        action: () => onSelectEntity(entity.id),
      });
    });

    return items;
  }, [entities, onNavigate, onSelectEntity, onExport]);

  // Filter commands based on query
  const filteredCommands = useMemo(() => {
    if (!query.trim()) {
      // Show navigation and actions first when no query
      return commands.filter(c => c.type !== COMMAND_TYPES.ENTITY).slice(0, 10);
    }

    const q = query.toLowerCase();
    return commands
      .filter(c => c.label.toLowerCase().includes(q) || c.entityType?.toLowerCase().includes(q))
      .slice(0, 20);
  }, [commands, query]);

  // Focus input when opened
  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setSelectedIndex(0);
      const focusTimer = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(focusTimer);
    }
  }, [isOpen]);

  // Handle keyboard navigation
  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          setSelectedIndex(i => Math.min(i + 1, filteredCommands.length - 1));
          break;
        case 'ArrowUp':
          e.preventDefault();
          setSelectedIndex(i => Math.max(i - 1, 0));
          break;
        case 'Enter':
          e.preventDefault();
          if (filteredCommands[selectedIndex]) {
            filteredCommands[selectedIndex].action();
            onClose();
          }
          break;
        case 'Escape':
          e.preventDefault();
          onClose();
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, filteredCommands, selectedIndex, onClose]);

  // Scroll selected item into view
  useEffect(() => {
    const selected = listRef.current?.children.item(selectedIndex);
    if (selected instanceof HTMLElement) {
      selected.scrollIntoView({
        block: 'nearest',
      });
    }
  }, [selectedIndex]);

  if (!isOpen) return null;

  const getEntityIcon = (type?: EntityType) => {
    switch (type) {
      case 'goal': return '★';
      case 'activity': return '◉';
      case 'action': return '◈';
      case 'operation': return '○';
      default: return '•';
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      className="command-palette"
      closeOnEscape={false}
      ariaLabel="Command palette"
    >
      <>
        <div className="command-input-wrapper">
          <svg className="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="11" cy="11" r="8"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <input
            ref={inputRef}
            type="text"
            className="command-input"
            aria-label="Search commands and entities"
            placeholder="Search entities, navigate, or run commands..."
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelectedIndex(0);
            }}
          />
          <kbd className="shortcut-hint">esc</kbd>
        </div>

        <div className="command-list" ref={listRef}>
          {filteredCommands.length === 0 ? (
            <div className="no-results">No results found</div>
          ) : (
            filteredCommands.map((cmd, index) => (
              <button
                type="button"
                key={cmd.id}
                className={`command-item ${index === selectedIndex ? 'selected' : ''}`}
                onClick={() => {
                  cmd.action();
                  onClose();
                }}
                onMouseEnter={() => setSelectedIndex(index)}
              >
                <span className="command-icon">
                  {cmd.type === COMMAND_TYPES.NAVIGATE && '→'}
                  {cmd.type === COMMAND_TYPES.ACTION && (cmd.icon || '⚡')}
                  {cmd.type === COMMAND_TYPES.ENTITY && getEntityIcon(cmd.entityType)}
                </span>
                <span className="command-label">{cmd.label}</span>
                {cmd.entityType && (
                  <span className={`entity-type-badge ${cmd.entityType}`}>
                    {cmd.entityType}
                  </span>
                )}
                {cmd.shortcut && (
                  <kbd className="command-shortcut">{cmd.shortcut}</kbd>
                )}
              </button>
            ))
          )}
        </div>

        <div className="command-footer">
          <span className="footer-hint">
            <kbd>↑↓</kbd> navigate
          </span>
          <span className="footer-hint">
            <kbd>↵</kbd> select
          </span>
          <span className="footer-hint">
            <kbd>esc</kbd> close
          </span>
        </div>
      </>
    </Modal>
  );
}
