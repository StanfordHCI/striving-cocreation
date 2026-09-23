import { useState, useEffect, useRef, useMemo, useCallback, type CSSProperties } from 'react';
import {
  motion, AnimatePresence, useScroll, useMotionValueEvent, useReducedMotion,
} from 'framer-motion';
import './graph.css';
import graphData from './graph.json';
import { paperHierarchy } from '../../data/paperHierarchy';

/* =============================================================
   Scroll-driven hierarchy and laptop animation
   Background: fictional screens with blur baked into the image files.
   Foreground: the worked example from Figure 1 of the UIST paper.
   Click → node details and the related nodes at each level.
   ============================================================= */

/* Public-directory assets are referenced as runtime strings, so Astro's
   `base` rewriting doesn't reach them — prefix BASE_URL by hand.  Trailing
   slash is stripped so this works whether base is '/' (dev) or
   '/striving-cocreation' (GitHub Pages). */
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '');

const clampProgress = (value: number) => Math.max(0, Math.min(1, value));
const easeProgress = (value: number) => {
  const t = clampProgress(value);
  return t * t * (3 - 2 * t);
};

const BACKGROUND_FRAMES = [
  'research.png',
  'notes.png',
  'inbox.png',
  'schedule.png',
].map(name => `${BASE}/demo-screens/${name}`);

interface Node {
  id: string;
  x: number;
  y: number;
  type: 'op' | 'ac' | 'av' | 'st';
  enterAt: number;
  parent?: string;
  text: string;
  source: string;
  labelSide?: 'l' | 'r';
}
interface Edge { from: string; to: string }

/* The published example is shown after the Figure 1 edit. Its labels are
   fixed; we do not invent a history of inferred revisions. */
interface StrivingDef {
  id: string;
  x: number;
  y: number;
  labelSide: 'l' | 'r';
  text: string;
  source: string;
}

type GraphEntity = { id: number; text: string; source: string };
type GraphNode = GraphEntity & { parents: number[] };
type GraphData = {
  provenance: { label: string; url: string; description: string };
  goals: GraphEntity[];
  activities: GraphNode[];
  actions: GraphNode[];
  operations: GraphNode[];
};
const DATA: GraphData = graphData;

// Leave the top and bottom of the composition for the full published labels.
const GOAL_POSITION: Record<number, { x: number; y: number; labelSide: 'l' | 'r' }> = {
  110: { x: 12, y: 14, labelSide: 'r' },
  112: { x: 88, y: 86, labelSide: 'l' },
};
const STRIVING_DEFS: StrivingDef[] = DATA.goals.map(g => ({
  id: `st${g.id}`,
  ...GOAL_POSITION[g.id],
  text: g.text,
  source: g.source,
}));

function buildGraph(): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  STRIVING_DEFS.forEach(s => {
    nodes.push({ id: s.id, x: s.x, y: s.y, type: 'st', enterAt: 0,
      text: s.text, source: s.source, labelSide: s.labelSide });
    const goalId = Number(s.id.slice(2));
    const dx = s.x - 50, dy = s.y - 50, length = Math.hypot(dx, dy);
    const point = (fraction: number, offset = 0) => ({
      x: 50 + dx * fraction - dy / length * offset,
      y: 50 + dy * fraction + dx / length * offset,
    });
    // Each published activity keeps its own node and its own supporting actions.
    DATA.activities.filter(a => a.parents.includes(goalId)).forEach(activity => {
      const avId = `av${activity.id}`;
      nodes.push({ id: avId, ...point(.56), type: 'av', enterAt: 0,
        parent: s.id, text: activity.text, source: activity.source });
      edges.push({ from: avId, to: s.id });
      const actions = DATA.actions.filter(a => a.parents.includes(activity.id));
      actions.forEach((action, j) => {
        const acId = `ac${action.id}`;
        const offset = (j - (actions.length - 1) / 2) * 14;
        nodes.push({ id: acId, ...point(.38, offset), type: 'ac', enterAt: 0,
          parent: avId, text: action.text, source: action.source });
        edges.push({ from: acId, to: avId });
        const operations = DATA.operations.filter(o => o.parents.includes(action.id));
        operations.forEach((operation, k) => {
          const opId = `op${operation.id}`;
          nodes.push({ id: opId, ...point(.10 + k * .12, offset * .85), type: 'op',
            enterAt: 0, parent: acId,
            text: operation.text, source: operation.source });
          edges.push({ from: opId, to: acId });
        });
      });
    });
  });
  // Pace the worked example deliberately: evenly spaced nodes within each
  // level, with all children visible before their parents. Random arrival
  // times left long stretches of scrolling with only one operation on screen.
  const revealPhases: Record<Node['type'], readonly [number, number]> = {
    op: [0.03, 0.33],
    ac: [0.39, 0.59],
    av: [0.65, 0.74],
    st: [0.83, 0.92],
  };
  for (const { type } of paperHierarchy) {
    const levelNodes = nodes.filter(node => node.type === type);
    const [start, end] = revealPhases[type];
    levelNodes.forEach((node, index) => {
      node.enterAt = start + (end - start) * index / Math.max(1, levelNodes.length - 1);
    });
  }
  return { nodes, edges };
}

const { nodes: NODES, edges: EDGES } = buildGraph();
const NODES_BY_ID = Object.fromEntries(NODES.map(n => [n.id, n]));

const HIERARCHY_LEVELS = paperHierarchy.map(level => ({
  ...level,
  enterAt: Math.min(...NODES.filter(node => node.type === level.type).map(node => node.enterAt)),
}));
const FIRST_OPERATION_AT = HIERARCHY_LEVELS[0].enterAt;
const FIRST_OPERATION = NODES.find(node => node.type === 'op')!;

function descendantsOf(id: string): string[] {
  const out: string[] = [];
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop()!;
    EDGES.filter(e => e.to === cur).forEach(e => {
      out.push(e.from);
      stack.push(e.from);
    });
  }
  return out;
}

/* =============================================================
   Public-safe background visualization
   ============================================================= */

/* These images are generated from fictional layouts, without source screenshots.
   Their blur is part of the files, not a CSS privacy mask. They are decorative
   and separate from the inspector's evidence. */
function BackgroundTimelapse({ progress, laptopProgress, preview = false }: { progress: number; laptopProgress: number; preview?: boolean }) {
  const reducedMotion = useReducedMotion();
  const position = Math.max(0, Math.min(1, progress));
  const idx = reducedMotion ? 0 : position * (BACKGROUND_FRAMES.length - 1);
  const lo = Math.floor(idx);
  const blend = idx - lo;
  const visibility = (preview ? 1 : Math.min(1, position / 0.04)) * (1 - laptopProgress * 0.9);

  return (
    <div className="v5-bg" aria-hidden="true" style={{ opacity: visibility }}>
      {BACKGROUND_FRAMES.map((src, index) => (
        <img
          key={src}
          className="v5-bg-img"
          src={src}
          alt=""
          loading={preview && index === 0 ? 'eager' : 'lazy'}
          decoding="async"
          draggable={false}
          style={{
            opacity: index === lo ? 1 : index === lo + 1 ? blend : 0,
            transform: reducedMotion || position === 0
              ? 'scale(1.15)'
              : `translate3d(${(position - 0.5) * (index % 2 ? -5 : 5)}%, ${(0.5 - position) * 7}%, 0) scale(1.15)`,
          }}
        />
      ))}
      <div className="v5-bg-tint" />
    </div>
  );
}

/* =============================================================
   The graph — levels arrive in order; visible nodes are interactive
   ============================================================= */

function Graph({
  progress, activeLevel, showHint, selectedId, hoveredId, setHoveredId, onSelect,
}: {
  progress: number;
  activeLevel?: Node['type'];
  showHint: boolean;
  selectedId: string | null;
  hoveredId: string | null;
  setHoveredId: (id: string | null) => void;
  onSelect: (id: string) => void;
}) {
  const focusId = selectedId ?? hoveredId;
  const focusLineage = useMemo(() => {
    if (!focusId) return null;
    const set = new Set<string>([focusId]);
    let cur: string | undefined = focusId;
    while (cur) {
      const parent: string | undefined = NODES_BY_ID[cur]?.parent;
      if (parent) set.add(parent);
      cur = parent;
    }
    descendantsOf(focusId).forEach(id => set.add(id));
    return set;
  }, [focusId]);

  const inFocus = (id: string) => !focusLineage || focusLineage.has(id);

  const isPresent = (id: string) => progress >= (NODES_BY_ID[id]?.enterAt ?? 0);

  const colorOf = (type: Node['type']) => `var(--screen-${type})`;
  const fillOf = colorOf;
  const sizeOf = (t: Node['type']) =>
    t === 'op' ? 0.85 :
    t === 'ac' ? 1.4 :
    t === 'av' ? 2.6 :
                 3.4;

  const strivings = NODES.filter(n => n.type === 'st');
  const hasOperations = progress >= FIRST_OPERATION_AT;

  const legend = <ol
        className="v5-legend"
        aria-label="Hierarchy levels"
        aria-hidden={!hasOperations}
        style={{
          opacity: hasOperations ? 1 : 0,
          visibility: hasOperations ? 'visible' : 'hidden',
          transition: 'opacity 0.18s ease',
        }}
      >
        {HIERARCHY_LEVELS.map(level => <li
          key={level.type}
          className="v5-legend-row"
          data-state={activeLevel === level.type ? 'current' : progress < level.enterAt ? 'upcoming' : 'introduced'}
          aria-current={activeLevel === level.type ? 'step' : undefined}
        >
          <span className={`v5-dot ${level.type}`} aria-hidden="true" />{level.title}
        </li>)}
      </ol>;

  return (
    <>
      {legend}
      <div className="v5-graph-wrap">

      <svg className="v5-svg" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet">
        {EDGES.map(e => {
          const from = NODES_BY_ID[e.from], to = NODES_BY_ID[e.to];
          const present = isPresent(e.from) && isPresent(e.to);
          const focus = focusLineage && focusLineage.has(e.from) && focusLineage.has(e.to);
          return (
            <line
              key={`${e.from}-${e.to}`}
              x1={from.x} y1={from.y}
              x2={to.x}   y2={to.y}
              stroke={focusLineage && focus ? colorOf(to.type) : 'var(--screen-edge)'}
              strokeWidth={focusLineage && focus ? 0.22 : 0.14}
              strokeLinecap="round"
              style={{
                opacity: !present ? 0
                  : focusLineage ? (focus ? 0.95 : 0.04)
                  : 'var(--screen-edge-opacity)',
                transition: 'opacity 0.35s ease, stroke 0.18s ease, stroke-width 0.18s ease',
              }}
            />
          );
        })}
        {NODES.map(n => {
          const present = isPresent(n.id);
          const focus = inFocus(n.id);
          const r = sizeOf(n.type);
          const highlighted = present && n.type === activeLevel && !focusLineage;
          const inviting = showHint && n.id === FIRST_OPERATION.id;
          return (
            <g
              key={n.id}
              data-node-id={n.id}
              data-level={n.type}
              data-highlighted={highlighted}
              onMouseEnter={() => present && setHoveredId(n.id)}
              onMouseLeave={() => setHoveredId(null)}
              onClick={() => present && onSelect(n.id)}
              role="button"
              tabIndex={present ? 0 : -1}
              aria-hidden={!present}
              aria-label={n.text}
              onFocus={() => present && setHoveredId(n.id)}
              onBlur={() => setHoveredId(null)}
              onKeyDown={event => {
                if (present && (event.key === 'Enter' || event.key === ' ')) {
                  event.preventDefault();
                  onSelect(n.id);
                }
              }}
              style={{
                opacity: !present ? 0 : !focus ? 0.16 : activeLevel && !highlighted && !focusLineage ? 0.82 : 1,
                transition: 'opacity 0.35s ease',
                cursor: present ? 'pointer' : 'default',
                pointerEvents: present ? 'auto' : 'none',
              }}
            >
              <circle cx={n.x} cy={n.y} r={r + 1.2} fill="transparent" className="v5-node-target" />
              {highlighted && <circle cx={n.x} cy={n.y} r={r + 1}
                fill="none" stroke={colorOf(n.type)} strokeWidth="0.35" opacity="0.6" />}
              {inviting && <circle className="v5-node-pulse" cx={n.x} cy={n.y} r={r + 2.1}
                fill="none" stroke="var(--screen-av)" strokeWidth="0.3" />}
              {present && (hoveredId === n.id || selectedId === n.id) && (
                <circle
                  cx={n.x} cy={n.y}
                  r={r + 2.2}
                  fill="none"
                  stroke={colorOf(n.type)}
                  strokeWidth="0.35"
                  opacity={0.7}
                />
              )}
              <circle className={inviting ? 'v5-node-inviting' : undefined} cx={n.x} cy={n.y} r={r} fill={fillOf(n.type)} stroke={'var(--screen-ink)'} strokeWidth={highlighted ? 0.3 : 0.22} />
            </g>
          );
        })}
      </svg>

      {showHint && <div className="v5-node-hint" role="status"
        style={{
          left: `clamp(var(--graph-tooltip-half), ${FIRST_OPERATION.x}%, calc(100% - var(--graph-tooltip-half)))`,
          top: `${FIRST_OPERATION.y + 5}%`,
          '--hint-arrow-offset': `min(0px, calc(var(--graph-size) * ${FIRST_OPERATION.x / 100} - var(--graph-tooltip-half)))`,
        } as CSSProperties}>
        <span className="v5-graph-hint-pointer">Hover over this node.<br />Click to explore its connections.</span>
        <span className="v5-graph-hint-touch">Tap this node to explore its connections.</span>
      </div>}

      {/* Hover label — shows the example text for the currently-hovered
          (or focused) op / action / activity node.  Strivings have
          their own large card labels rendered below. */}
      {(() => {
        const id = hoveredId ?? selectedId;
        if (!id) return null;
        const n = NODES_BY_ID[id];
        if (!n || n.type === 'st') return null;
        if (!isPresent(id)) return null;
        const tag = n.type === 'op' ? 'OPERATION'
                  : n.type === 'ac' ? 'ACTION'
                  : 'ACTIVITY';
        // Open toward the center, away from the two persistent striving labels.
        const above = n.y >= 50;
        return (
          <div
            className={`v5-hover-label v5-hover-label-${n.type}`}
            data-pinned={selectedId === id}
            style={{
              left: `clamp(var(--graph-tooltip-half), ${n.x}%, calc(100% - var(--graph-tooltip-half)))`,
              top: `${n.y + (above ? -1 : 1) * (sizeOf(n.type) + 3)}%`,
              transform: `translate(-50%, ${above ? '-100%' : '0'})`,
            }}
          >
            <span className="v5-hover-tag">{tag}</span>
            <span className="v5-hover-text">{n.text}</span>
          </div>
        );
      })()}

      {strivings.map(s => {
        const present = isPresent(s.id);
        const focus = inFocus(s.id);
        const side = s.labelSide ?? 'r';
        const offset = 7;
        const lx = side === 'l' ? s.x - offset : s.x + offset;
        return (
          <div
            key={`lbl-${s.id}`}
            className="v5-st-label final"
            data-side={side}
            onClick={() => present && onSelect(s.id)}
            onMouseEnter={() => present && setHoveredId(s.id)}
            onMouseLeave={() => setHoveredId(null)}
            style={{
              left: `${lx}%`,
              top: `${s.y}%`,
              opacity: !present ? 0 : (focus ? 1 : 0.2),
              transform: `translate(${side === 'l' ? '-100%' : '0%'}, -50%)`,
              pointerEvents: present ? 'auto' : 'none',
              transition: 'opacity 0.4s ease',
            }}
          >
            <span className="v5-st-label-text">{s.text}</span>
          </div>
        );
      })}
    </div>
    </>
  );
}

/* =============================================================
   Detail panel — slides in from the right when a node is selected
   ============================================================= */

function DetailPanel({
  nodeId, onClose, onSelect,
}: { nodeId: string; onClose: () => void; onSelect: (id: string) => void }) {
  const node = NODES_BY_ID[nodeId];
  if (!node) return null;

  // Walk up the lineage for breadcrumb-style navigation
  const lineage: Node[] = [];
  {
    let cur: Node | undefined = node;
    while (cur) {
      lineage.unshift(cur);
      cur = cur.parent ? NODES_BY_ID[cur.parent] : undefined;
    }
  }

  // Direct children for the "underneath" section
  const children = EDGES.filter(e => e.to === nodeId).map(e => NODES_BY_ID[e.from]);


  const tagOf = (t: Node['type']) =>
    t === 'op' ? 'OPERATION' :
    t === 'ac' ? 'ACTION' :
    t === 'av' ? 'ACTIVITY' :
                 'STRIVING';
  const tagClass = (t: Node['type']) => `v5-tag ${t}`;

  return (
    <motion.aside
      key={nodeId}
      className="v5-panel"
      initial={{ x: 60, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 60, opacity: 0 }}
      transition={{ type: 'spring', stiffness: 220, damping: 28, mass: 0.6 }}
    >
      <button className="v5-panel-close" type="button" onClick={onClose} aria-label="Close">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
          <path d="M18 6 6 18M6 6l12 12"/>
        </svg>
      </button>

      {/* Breadcrumb — only when there's actually a parent chain to
          navigate.  Root nodes (strivings) get no breadcrumb so we
          don't show a lonely [STRIVING] chip above the same tag in
          the panel head. */}
      {lineage.length > 1 && (
        <nav className="v5-crumbs">
          {lineage.slice(0, -1).map((n, i) => (
            <span key={n.id} className="v5-crumb-row">
              {i > 0 && <span className="v5-crumb-sep">›</span>}
              <button
                type="button"
                className="v5-crumb"
                onClick={() => onSelect(n.id)}
              >
                <span className={tagClass(n.type)}>{tagOf(n.type)}</span>
              </button>
            </span>
          ))}
        </nav>
      )}

      {/* The node's type and full label. */}
      <div className="v5-panel-head">
        <span className={tagClass(node.type)}>{tagOf(node.type)}</span>
        <p className="v5-panel-text">{node.text}</p>
      </div>

      {/* Children */}
      {children.length > 0 && (
        <section className="v5-panel-section">
          <div className="v5-panel-label">
            What's underneath ({children.length})
          </div>
          <ul className="v5-children">
            {children.slice(0, 18).map(c => (
              <li key={c.id}>
                <button
                  type="button"
                  className="v5-child"
                  onClick={() => onSelect(c.id)}
                >
                  <span className={tagClass(c.type)}>{tagOf(c.type)}</span>
                  <span className="v5-child-text">{c.text}</span>
                </button>
              </li>
            ))}
            {children.length > 18 && (
              <li className="v5-child-more">+ {children.length - 18} more</li>
            )}
          </ul>
        </section>
      )}
    </motion.aside>
  );
}

/* =============================================================
   Root
   ============================================================= */

export default function GraphStory() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const dismissNode = useCallback(() => {
    setSelectedId(null);
    setHoveredId(null);
  }, []);
  const [hasInteracted, setHasInteracted] = useState(false);
  const [progress, setProgress] = useState(0);
  const [sceneSize, setSceneSize] = useState({ width: 0, height: 0, note: 0, title: 0 });
  const reducedMotion = useReducedMotion();
  const [staticScene, setStaticScene] = useState(false);
  // Match the server's initial markup before applying the browser preference.
  useEffect(() => setStaticScene(Boolean(reducedMotion)), [reducedMotion]);

  const sectionRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: sectionRef,
    offset: ['start start', 'end end'],
  });
  useMotionValueEvent(scrollYProgress, 'change', v => setProgress(Math.max(0, Math.min(1, v))));

  // Start with a visible laptop, enter its screen, then build the hierarchy.
  // The first node, definition, and legend arrive together as the zoom ends.
  const laptopGraphProgress = progress < 0.12 ? 0
    : FIRST_OPERATION_AT + (1 - FIRST_OPERATION_AT) * clampProgress((progress - 0.12) / 0.64);
  const graphProgress = staticScene ? 1 : laptopGraphProgress;

  useEffect(() => {
    const stage = sectionRef.current?.querySelector('.v5-sticky');
    if (!stage) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.intersectionRatio < 0.5) dismissNode();
    }, { threshold: 0.5 });
    observer.observe(stage);
    return () => observer.disconnect();
  }, [dismissNode]);

  // Dismiss the selection without intercepting links or other page controls.
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest(
        '.v5-panel, .v5-hover-label, [data-node-id], .v5-st-label',
      )) return;
      dismissNode();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') dismissNode();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    window.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      window.removeEventListener('keydown', onKey);
    };
  }, [dismissNode]);

  // Count how many nodes are present so far for the chrome readout
  const presentCount = NODES.filter(n => graphProgress >= n.enterAt).length;
  const currentLevel = staticScene ? undefined : HIERARCHY_LEVELS.filter(level => graphProgress >= level.enterAt).pop();
  const activeLevel = !staticScene && progress < 0.78 ? currentLevel?.type : undefined;
  const showHint = !hasInteracted && activeLevel === 'op';
  const hoverNode = (id: string | null) => {
    if (id) setHasInteracted(true);
    setHoveredId(id);
  };
  const selectNode = (id: string) => {
    setHasInteracted(true);
    setSelectedId(id);
  };

  // Emphasize the next destination once the laptop has settled.
  const linksProminent = progress >= 0.88;

  // Title rides the same beat as the links: invisible until the laptop
  // bezel has settled, then fades and rises into place above it.
  const titleReveal = staticScene ? 0 : clampProgress((progress - 0.94) / 0.04);
  const entryReveal = staticScene ? 0 : 1 - easeProgress(progress / 0.12);
  const exitReveal = staticScene ? 0 : easeProgress((progress - 0.78) / 0.17);
  const laptopReveal = Math.max(entryReveal, exitReveal);
  // Return to white before the laptop settles back into the page.
  const screenWhite = Math.min(1, exitReveal / 0.8);
  const softScreen = (0.65 + 0.35 * entryReveal) * (1 - screenWhite);
  const noteOpacity = staticScene ? 1 : !currentLevel ? 0
    : easeProgress((progress - 0.075) / 0.045) * (1 - Math.min(1, exitReveal * 4));

  // Measure wrapping text and the available viewport, including font changes.
  // The final laptop fits beneath either the closing title or the level note.
  useEffect(() => {
    const stage = sectionRef.current?.querySelector<HTMLElement>('.v5-sticky');
    if (!stage) return;
    const note = stage.querySelector<HTMLElement>('.v5-graph-note');
    const title = stage.querySelector<HTMLElement>('.v5-header');
    const measure = () => setSceneSize({
      width: stage.clientWidth,
      height: stage.clientHeight,
      note: note?.offsetHeight ?? 0,
      title: title?.offsetHeight ?? 0,
    });
    const observer = new ResizeObserver(measure);
    [stage, note, title].forEach(element => element && observer.observe(element));
    measure();
    return () => observer.disconnect();
  }, []);

  const topReserve = 24 + sceneSize.title + 32;
  const finalLaptopWidth = Math.max(100, Math.min(960, sceneSize.width * 0.8, (sceneSize.height - topReserve - 100) / 0.65));

  // Place the graph inside the visible screen, below the annotation and legend.
  // Convert viewport pixels to screen coordinates throughout the zoom.
  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    const stage = section.querySelector<HTMLElement>('.v5-sticky');
    const screen = section.querySelector<HTMLElement>('.v5-laptop-screen');
    const note = section.querySelector<HTMLElement>('.v5-graph-note');
    const laptop = section.querySelector<HTMLElement>('.v5-laptop-wrap');
    const legend = section.querySelector<HTMLElement>('.v5-legend');
    const root = section.parentElement;
    if (!stage || !screen || !note || !laptop || !legend || !root) return;
    let frame = 0;
    const layout = () => {
      const viewport = stage.getBoundingClientRect();
      const display = screen.getBoundingClientRect();
      const scale = display.width / screen.clientWidth;
      if (!scale) return;
      const reveal = Number(root.style.getPropertyValue('--laptop-reveal'));
      const afterNote = Math.max(0, (reveal - 0.25) / 0.75);
      const noteBottom = note.getBoundingClientRect().bottom - viewport.top;
      const padding = Math.min(20, display.height * 0.035);
      const left = Math.max(display.left + padding, viewport.left + 24);
      const right = Math.min(display.right - padding, viewport.right - 24);
      const top = Math.max(display.top + padding, viewport.top + (noteBottom + 28) * (1 - afterNote));
      const bottom = Math.min(display.bottom - padding, viewport.bottom - 80);
      const width = Math.max(0, right - left);
      const compactMobile = viewport.width <= 600;
      const legendFont = 14 - reveal * (compactMobile ? 6 : 3);
      const legendHeight = legend.getBoundingClientRect().height;
      const graphTop = top + legendHeight + Math.min(40, display.height * 0.06);
      const size = Math.max(40, Math.min(720, width * 0.84, bottom - graphTop));
      const centerX = (left + right) / 2;
      const centerY = (graphTop + bottom) / 2;
      const labelWidth = Math.min(360, width / 2 + size * 0.31 - 16);
      const tooltipWidth = Math.min(280, size * 0.95);
      const variables: Record<string, string> = {
        '--graph-size': `${size / scale}px`,
        '--graph-left': `${(centerX - display.left) / scale}px`,
        '--graph-top': `${(centerY - display.top) / scale}px`,
        '--graph-label-size': `${Math.max(10 - reveal * 2, Math.min(16, size * 0.045)) / scale}px`,
        '--graph-label-width': `${labelWidth / scale}px`,
        '--graph-tooltip-width': `${tooltipWidth / scale}px`,
        '--graph-tooltip-half': `${tooltipWidth / scale / 2}px`,
        '--graph-hit-radius': `${Math.min(3, 1200 / size)}px`,
        '--legend-left': `${(centerX - display.left) / scale}px`,
        '--legend-top': `${(top - display.top) / scale}px`,
        '--legend-width': `${width / scale}px`,
        '--legend-font-size': `${legendFont / scale}px`,
        '--legend-dot-size': `${(7 - reveal * 2) / scale}px`,
        '--legend-gap': `${Math.min(28, width * 0.025) / scale}px`,
        '--legend-padding': `${(8 - reveal * (compactMobile ? 6 : 4)) / scale}px`,
      };
      Object.entries(variables).forEach(([key, value]) => root.style.setProperty(key, value));
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(layout);
    };
    const resize = new ResizeObserver(schedule);
    [stage, screen, note, legend].forEach(element => resize.observe(element));
    const motion = new MutationObserver(schedule);
    motion.observe(laptop, { attributes: true, attributeFilter: ['style'] });
    schedule();
    return () => { cancelAnimationFrame(frame); resize.disconnect(); motion.disconnect(); };
  }, []);

  return (
    <div
      className={`v5-root v5-paper-example v5-embedded v5-immersive v5-soft v5-laptop-first${staticScene ? ' v5-static' : ''}`}
      data-laptop-compact={laptopReveal > 0.5}
      style={{
        '--laptop-reveal': laptopReveal,
        '--laptop-entry': entryReveal,
        '--laptop-exit': exitReveal,
        '--scene-top-reserve': `${topReserve}px`,
        '--laptop-final-width': sceneSize.width ? `${finalLaptopWidth}px` : 'min(960px, 80vw)',
        '--laptop-final-height': sceneSize.width ? `${finalLaptopWidth * 0.65}px` : 'min(624px, 52vw)',
        '--screen-load': 1,
        '--screen-arrival': `${softScreen * 100}%`,
        '--screen-white': `${screenWhite * 100}%`,
        '--screen-palette': '100%',
        '--screen-ink': '#322d2a',
        '--screen-edge': '#96877b',
        '--screen-background-opacity': softScreen,
        '--screen-edge-opacity': 0.65,
        '--scene-note-ink': 'var(--paper-ink)',
        '--scene-note-muted': 'var(--paper-muted)',
        '--scene-footer-ink': 'var(--paper-muted)',
      } as CSSProperties}
    >
      <section ref={sectionRef} className="v5-section" aria-label="Interactive example: from computer use to life goals">
        <div className="v5-sticky">
            <div className="v5-graph-note" style={{ opacity: noteOpacity }} aria-hidden={noteOpacity === 0}>
              <div className="v5-level-note">
                {/* Overlapping sizing copies reserve the longest definition's
                    height as the viewport and font change. */}
                {!staticScene && <div className="v5-level-sizing" aria-hidden="true">
                  {paperHierarchy.map(level => <div key={level.type}>
                    <p className="v5-level-title">{level.title}</p>
                    <p className="v5-level-definition">{level.definition}</p>
                  </div>)}
                </div>}
                <div className="v5-level-current" role="status" aria-live="polite" aria-atomic="true">
                  {currentLevel ? <div key={currentLevel.type} className="v5-level-copy" data-level={currentLevel.type}>
                    <p className="v5-level-title"><span className={`v5-dot ${currentLevel.type}`} aria-hidden="true" />{currentLevel.title}</p>
                    <p className="v5-level-definition">{currentLevel.definition}</p>
                  </div> : null}
                </div>
              </div>
            </div>
          <header
            className="v5-header"
            style={{
              opacity: titleReveal,
              transform: `translate(-50%, ${(1 - titleReveal) * 16}px)`,
            }}
          >
            <h2 className="v5-title">What Are You Really Trying to Do?</h2>
          </header>

          {/* One screen carries the blurred observations and graph through
              both the approach and pullback, without swapping scenes. */}
          <div className="v5-laptop-wrap" style={{ scale: 1, opacity: 1 }}>
            <div className="v5-laptop-lid">
              <div className="v5-laptop-lid-face">
                <div className="v5-laptop-camera" />
                <div className="v5-laptop-screen">
                  {/* Blurred screen timelapse lives INSIDE the laptop
                      screen so it shares the laptop's stacking context
                      (the scale transform isolates it otherwise) — the
                      current frame visibly drifts behind the graph. */}
                  <BackgroundTimelapse
                    progress={graphProgress}
                    laptopProgress={exitReveal}
                    preview
                  />
                  <Graph
                    progress={graphProgress}
                    activeLevel={activeLevel}
                    showHint={showHint}
                    selectedId={selectedId}
                    hoveredId={hoveredId}
                    setHoveredId={hoverNode}
                    onSelect={selectNode}
                  />
                </div>
              </div>
            </div>
            <div className="v5-laptop-base">
              <div className="v5-laptop-hinge" />
              <div className="v5-laptop-deck" />
              <div className="v5-laptop-notch" />
            </div>
          </div>

          <footer className="v5-footer" style={{ visibility: !presentCount ? 'hidden' : 'visible' }}>
            <div className="v5-footer-left">
              <span className="v5-footer-count">{presentCount} / {NODES.length} nodes</span>
            </div>
            <div className={`v5-footer-links ${linksProminent ? 'prominent' : ''}`}>
              <a href="#results">Results ↓</a>
            </div>
          </footer>

          {/* Keep the scroll cue beside the opening subject. */}
          <div
            className="v5-scrollcue"
            style={{ opacity: staticScene ? 0 : 1 - Math.min(1, progress / 0.03) }}
            aria-hidden="true"
          >
            <span className="v5-scrollcue-label">Scroll to build the hierarchy</span>
            <span className="v5-scrollcue-track">
              <span className="v5-scrollcue-dot" />
            </span>
          </div>

          <div className="v5-scrollbar">
            <div className="v5-scrollbar-fill" style={{ width: `${progress * 100}%` }} />
          </div>
        </div>
      </section>

      {staticScene && <dl className="v5-static-definitions">
        {paperHierarchy.map(level => <div key={level.type}>
          <dt>{level.title}</dt>
          <dd>{level.definition}</dd>
        </div>)}
      </dl>}

      <AnimatePresence>
        {selectedId && (
          <DetailPanel
            nodeId={selectedId}
            onClose={dismissNode}
            onSelect={setSelectedId}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
