/**
 * The behaviour graph explorer.
 *
 * A port of the Sigma/graphology explorer from the research build: every entity
 * is a circle colored by its level, every relation a link, and the whole thing is
 * something you interrogate rather than read top to bottom. Click a node to
 * drop into its two-hop neighbourhood; hover to see what it touches; filter by
 * text, by relation kind, or by how many edges you can stand at once.
 *
 * The original leaned on sigma + graphology + forceatlas2 + elkjs. Those were
 * removed in the cleanup and have not come back — the layouts here are the two
 * the original actually used, written out directly: a layered pass that ranks
 * by hierarchy depth and orders each rank by its neighbours' barycentre, and a
 * Fruchterman-Reingold spring layout for when you want the network's own shape
 * rather than the hierarchy's. Both are deterministic, so the graph does not
 * rearrange itself underneath you between renders.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import api from '../api/client';
import type { Entity, Relation } from '../api/contracts';
import { PAPER_ACCENT, PAPER_INK, PAPER_PALETTE, type HierarchyLevel } from './hierarchy/tokens';
import './GraphView.css';

const RANKS: HierarchyLevel[] = ['goal', 'activity', 'action', 'operation'];

/** Color and size identify the level; every node stays circular. */
const RADIUS: Record<HierarchyLevel, number> = {
  goal: 18,
  activity: 14,
  action: 10,
  operation: 6,
};

/** Minimum radii in screen pixels, including when Fit zooms out a large graph. */
const MIN_RADIUS: Record<HierarchyLevel, number> = {
  goal: 12, activity: 10, action: 7, operation: 4,
};

const NODE_COLORS = PAPER_PALETTE;
const MIN_SCALE = 0.01;

/** Shared by the canvas and legend, with outlines that stay visible at every zoom. */
function NodeCircle({ level, x = 0, y = 0, radius }: {
  level: HierarchyLevel; x?: number; y?: number; radius: number;
}) {
  return <circle className="gv-node-mark" cx={x} cy={y} r={radius}
                 fill={NODE_COLORS[level].fill} stroke={NODE_COLORS[level].stroke}
                 strokeWidth={level === 'operation' ? 1.8 : 1.5}
                 vectorEffect="non-scaling-stroke" />;
}

/** Idle previews for lower levels; strivings and active nodes use their full text. */
const LABEL_CHARS: Record<HierarchyLevel, number> = {
  goal: 34, activity: 30, action: 24, operation: 18,
};
const LABEL_FONT_SIZE = 12;
const LABEL_LINE_HEIGHT = 16;

const LEVEL_LABEL: Record<HierarchyLevel, string> = {
  goal: 'Strivings',
  activity: 'Activities',
  action: 'Actions',
  operation: 'Operations',
};

/**
 * Relations are keyed by "kind": the family, except that supports and hinders
 * earn their own colours — those two are the claims a user most wants to find,
 * and the original singled them out for the same reason.
 */
const KIND: Record<string, { color: string; dash?: string; width: number; label: string }> = {
  structural: { color: PAPER_INK, width: 1.3, label: 'structural' },
  behavioral: { color: '#8a7358', width: 1.3, dash: '5 4', label: 'behavioural' },
  temporal:   { color: '#b4a189', width: 1.1, dash: '2 4', label: 'temporal' },
  revision:   { color: '#a8724f', width: 1.3, dash: '8 4', label: 'revision' },
  supports:   { color: PAPER_ACCENT, width: 1.8, label: 'supports' },
  hinders:    { color: '#c0603f', width: 1.8, label: 'hinders' },
};

function edgeKind(relation: Relation): string {
  const subtype = relation.subtype ?? '';
  if (subtype === 'supports' || subtype === 'hinders') return subtype;
  return relation.type || 'structural';
}

const MARGIN = 70;
const RANK_GAP = 165;
const FOCUS_HOPS = 2;
/** Enough pull to keep sparsely-linked operations from forming a distant ring. */
const GRAVITY = 0.09;

type Node = { id: number; level: HierarchyLevel; text: string; x: number; y: number; r: number };
type Edge = { id: string; from: Node; to: Node; kind: string; subtype: string };
type Layout = 'layered' | 'force';

/** Positions seeded from the entity id, so a reload lands in the same place. */
function seededPoint(id: number, span: number): { x: number; y: number } {
  const seed = id * 9301 + 49297;
  return {
    x: ((seed % 233280) / 233280) * span,
    y: (((seed * 7) % 233280) / 233280) * span,
  };
}

/**
 * Fruchterman-Reingold: links pull, everything pushes, temperature cools.
 * Deterministic, and fast enough at these sizes to run inside a memo.
 */
function springLayout(
  ids: number[],
  links: Array<[number, number]>,
): Map<number, { x: number; y: number }> {
  const count = ids.length;
  const span = Math.max(600, Math.sqrt(count) * 190);
  const index = new Map(ids.map((id, position) => [id, position]));
  const xs = new Float64Array(count);
  const ys = new Float64Array(count);
  ids.forEach((id, position) => {
    const point = seededPoint(id, span);
    xs[position] = point.x;
    ys[position] = point.y;
  });

  const k = Math.sqrt((span * span) / count);
  const pairs = links
    .map(([a, b]) => [index.get(a), index.get(b)] as [number | undefined, number | undefined])
    .filter((pair): pair is [number, number] => pair[0] !== undefined && pair[1] !== undefined);

  // Big graphs get fewer passes; the original made the same trade at 500 nodes.
  const iterations = count > 250 ? 120 : 300;
  const dx = new Float64Array(count);
  const dy = new Float64Array(count);

  for (let step = 0; step < iterations; step += 1) {
    dx.fill(0);
    dy.fill(0);

    for (let a = 0; a < count; a += 1) {
      for (let b = a + 1; b < count; b += 1) {
        let vx = xs[a] - xs[b];
        let vy = ys[a] - ys[b];
        let distance = Math.hypot(vx, vy);
        if (distance < 0.01) {
          // Coincident nodes would divide by zero; nudge them apart instead.
          vx = ((a * 7 + b) % 11) - 5;
          vy = ((a * 13 + b) % 11) - 5;
          distance = Math.hypot(vx, vy) || 1;
        }
        const force = (k * k) / distance;
        const ux = (vx / distance) * force;
        const uy = (vy / distance) * force;
        dx[a] += ux; dy[a] += uy;
        dx[b] -= ux; dy[b] -= uy;
      }
    }

    for (const [a, b] of pairs) {
      const vx = xs[a] - xs[b];
      const vy = ys[a] - ys[b];
      const distance = Math.hypot(vx, vy) || 0.01;
      const force = (distance * distance) / k;
      const ux = (vx / distance) * force;
      const uy = (vy / distance) * force;
      dx[a] -= ux; dy[a] -= uy;
      dx[b] += ux; dy[b] += uy;
    }

    const temperature = (span / 10) * (1 - step / iterations);
    const centreX = span / 2;
    const centreY = span / 2;
    for (let a = 0; a < count; a += 1) {
      // Gravity keeps disconnected islands from drifting off the canvas.
      dx[a] += (centreX - xs[a]) * GRAVITY;
      dy[a] += (centreY - ys[a]) * GRAVITY;
      const magnitude = Math.hypot(dx[a], dy[a]) || 1;
      const limit = Math.min(magnitude, temperature);
      xs[a] += (dx[a] / magnitude) * limit;
      ys[a] += (dy[a] / magnitude) * limit;
    }
  }

  // The simulation wanders well past its starting span, so rescale the result
  // into a box sized for the node count — otherwise Fit shows a postage stamp.
  // The span is measured between percentiles rather than extremes: a single
  // node flung out by repulsion must not squash everything else into a dot.
  const target = Math.max(700, Math.sqrt(count) * 175);
  const spanOf = (values: Float64Array): [number, number] => {
    const sorted = [...values].sort((a, b) => a - b);
    const low = sorted[Math.floor((count - 1) * 0.03)];
    const high = sorted[Math.ceil((count - 1) * 0.97)];
    return [low, Math.max(high - low, 1)];
  };
  const [lowX, spreadX] = spanOf(xs);
  const [lowY, spreadY] = spanOf(ys);
  const factor = target / Math.max(spreadX, spreadY);
  // Outliers keep their direction but land on the edge of the frame rather
  // than defining it — a weakly-linked operation flung out by repulsion would
  // otherwise stretch the canvas around a graph that fills none of it.
  const clamp = (value: number) => Math.max(0, Math.min(target, value));

  const placed = new Map<number, { x: number; y: number }>();
  ids.forEach((id, position) => placed.set(id, {
    x: clamp((xs[position] - lowX) * factor),
    y: clamp((ys[position] - lowY) * factor),
  }));
  return placed;
}

/** Everything within `hops` of the centre, following relations either way. */
function neighbourhood(
  centre: number,
  adjacency: Map<number, Set<number>>,
  hops: number,
): Set<number> {
  const visited = new Set([centre]);
  let frontier = [centre];
  for (let hop = 0; hop < hops; hop += 1) {
    const next: number[] = [];
    for (const id of frontier) {
      for (const neighbour of adjacency.get(id) ?? []) {
        if (visited.has(neighbour)) continue;
        visited.add(neighbour);
        next.push(neighbour);
      }
    }
    frontier = next;
  }
  return visited;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function wrapLabel(text: string, maxWidth: number, measure: (value: string) => number): string[] {
  const lines: string[] = [];
  for (const paragraph of text.split(/\r?\n/)) {
    let line = '';
    for (const word of paragraph.match(/\S+\s*|\s+/g) ?? []) {
      if (line && measure((line + word).trimEnd()) > maxWidth) {
        lines.push(line);
        line = '';
      }
      if (measure(word.trimEnd()) <= maxWidth) {
        line += word;
      } else {
        // Long URLs and unbroken identifiers also wrap without losing text.
        for (const character of word) {
          if (line && measure(line + character) > maxWidth) {
            lines.push(line);
            line = '';
          }
          line += character;
        }
      }
    }
    lines.push(line);
  }
  return lines;
}

type LabelBox = { left: number; top: number; width: number; height: number };
function overlapArea(a: LabelBox, b: LabelBox): number {
  return Math.max(0, Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left))
    * Math.max(0, Math.min(a.top + a.height, b.top + b.height) - Math.max(a.top, b.top));
}

export default function GraphView({
  refreshKey,
  selectedId,
  onSelectEntity,
}: {
  refreshKey: number;
  selectedId: number | null;
  onSelectEntity: (id: number | null) => void;
}) {
  const [entities, setEntities] = useState<Entity[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [scope, setScope] = useState<'full' | 'activities'>('full');
  const [showOperations, setShowOperations] = useState(false);
  const [layout, setLayout] = useState<Layout>('force');
  const [query, setQuery] = useState('');
  const [edgeLimit, setEdgeLimit] = useState(0);        // 0 means every edge
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(() => new Set());
  const [focusId, setFocusId] = useState<number | null>(null);
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const [keyboardNodeId, setKeyboardNodeId] = useState<number | null>(null);
  const [fontsReady, setFontsReady] = useState(false);
  const labelContext = useMemo(() => document.createElement('canvas').getContext('2d'), []);

  const [view, setView] = useState({ x: 0, y: 0, scale: 1 });
  const dragging = useRef<{ x: number; y: number; ox: number; oy: number; moved: boolean } | null>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const activeNodeId = hoveredId ?? keyboardNodeId;

  useEffect(() => {
    let cancelled = false;
    void document.fonts.ready.then(() => { if (!cancelled) setFontsReady(true); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const wanted: HierarchyLevel[] = showOperations
      ? ['goal', 'activity', 'action', 'operation']
      : ['goal', 'activity', 'action'];

    Promise.all(wanted.map((level) =>
      api.getEntitiesByType(level, { include_relations: true, limit: 400 })))
      .then((results) => {
        if (controller.signal.aborted) return;
        setEntities(results.flatMap((result) => result.entities));
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'Could not load the graph');
        setLoading(false);
      });
    return () => controller.abort();
  }, [refreshKey, showOperations]);

  // Adjacency over everything loaded, so focus can reach through nodes the
  // current scope happens to hide.
  const adjacency = useMemo(() => {
    const map = new Map<number, Set<number>>();
    const link = (a: number, b: number) => {
      if (!map.has(a)) map.set(a, new Set());
      map.get(a)?.add(b);
    };
    for (const entity of entities) {
      for (const relation of (entity.relations?.outgoing ?? []) as Relation[]) {
        const target = relation.target_id;
        if (target === undefined) continue;
        link(entity.id, target);
        link(target, entity.id);
      }
    }
    return map;
  }, [entities]);

  const { nodes, edges, kinds, counts, width, height, hiddenByLimit } = useMemo(() => {
    const allowedLevels = new Set<HierarchyLevel>(
      scope === 'activities' ? ['goal', 'activity'] : RANKS,
    );
    const focusSet = focusId === null ? null : neighbourhood(focusId, adjacency, FOCUS_HOPS);

    const visible = entities.filter((entity) => {
      const level = entity.type as HierarchyLevel;
      if (!RANKS.includes(level)) return false;
      if (!allowedLevels.has(level)) return false;
      return focusSet === null || focusSet.has(entity.id);
    });

    const tally: Record<string, number> = {};
    for (const entity of visible) tally[entity.type] = (tally[entity.type] ?? 0) + 1;

    // Structural parentage: the ranking for the layered pass, the springs for
    // the force pass.
    const parentsOf = new Map<number, number[]>();
    const structural: Array<[number, number]> = [];
    const present = new Set(visible.map((entity) => entity.id));
    for (const entity of visible) {
      for (const relation of (entity.relations?.outgoing ?? []) as Relation[]) {
        const target = relation.target_id;
        if (target === undefined || !present.has(target)) continue;
        structural.push([entity.id, target]);
        if ((relation.subtype ?? '') === 'part_of') {
          parentsOf.set(entity.id, [...(parentsOf.get(entity.id) ?? []), target]);
        }
      }
    }

    const placed = new Map<number, Node>();
    let canvasWidth = 0;
    let canvasHeight = 0;

    if (layout === 'force') {
      // Nothing links them, so the simulation has no opinion about where they
      // go — and scattering them stretches the canvas around a graph that then
      // fills a third of it. They get a tidy tray underneath instead.
      const linkedIds = new Set(structural.flat());
      const connected = visible.filter((entity) => linkedIds.has(entity.id));
      const isolated = visible.filter((entity) => !linkedIds.has(entity.id));

      const points = springLayout(connected.map((entity) => entity.id), structural);
      let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
      for (const point of points.values()) {
        minX = Math.min(minX, point.x); maxX = Math.max(maxX, point.x);
        minY = Math.min(minY, point.y); maxY = Math.max(maxY, point.y);
      }
      if (!Number.isFinite(minX)) { minX = 0; maxX = 0; minY = 0; maxY = 0; }

      for (const entity of connected) {
        const level = entity.type as HierarchyLevel;
        const point = points.get(entity.id) ?? { x: 0, y: 0 };
        placed.set(entity.id, {
          id: entity.id, level, text: entity.text, r: RADIUS[level],
          x: MARGIN + point.x - minX, y: MARGIN + point.y - minY,
        });
      }

      const graphWidth = (maxX - minX) + MARGIN * 2;
      let trayHeight = 0;
      if (isolated.length > 0) {
        const cell = 58;
        const perRow = Math.max(1, Math.floor((graphWidth - MARGIN * 2) / cell));
        const top = (maxY - minY) + MARGIN * 2 + 24;
        isolated.forEach((entity, position) => {
          const level = entity.type as HierarchyLevel;
          placed.set(entity.id, {
            id: entity.id, level, text: entity.text, r: RADIUS[level],
            x: MARGIN + (position % perRow) * cell + cell / 2,
            y: top + Math.floor(position / perRow) * cell,
          });
        });
        trayHeight = Math.ceil(isolated.length / perRow) * cell + 24;
      }

      canvasWidth = graphWidth;
      canvasHeight = (maxY - minY) + MARGIN * 2 + trayHeight;
    } else {
      // Rank by level, then order each rank by where its parents sit above it.
      const order = new Map<number, number>();
      const rows: Array<{ level: HierarchyLevel; items: Entity[] }> = [];
      for (const level of RANKS) {
        const items = visible.filter((entity) => entity.type === level);
        if (items.length === 0) continue;
        if (rows.length > 0) {
          items.sort((a, b) => barycentre(a) - barycentre(b));
        }
        items.forEach((entity, position) => order.set(entity.id, position));
        rows.push({ level, items });
      }

      function barycentre(entity: Entity): number {
        const positions = (parentsOf.get(entity.id) ?? [])
          .map((id) => order.get(id))
          .filter((value): value is number => value !== undefined);
        return positions.length
          ? positions.reduce((a, b) => a + b, 0) / positions.length
          : Number.MAX_SAFE_INTEGER;
      }

      // Labels sit under the dots, so spacing has to clear the label rather
      // than the node — at these text lengths the dot is never the wide part.
      const step: Record<HierarchyLevel, number> = {
        goal: 240, activity: 215, action: 104, operation: 58,
      };
      const widest = Math.max(1, ...rows.map((row) => row.items.length * step[row.level]));
      let y = MARGIN;
      for (const row of rows) {
        const spacing = step[row.level];
        const rowWidth = row.items.length * spacing;
        let x = MARGIN + (widest - rowWidth) / 2 + spacing / 2;
        for (const entity of row.items) {
          placed.set(entity.id, {
            id: entity.id, level: row.level, text: entity.text,
            r: RADIUS[row.level], x, y,
          });
          x += spacing;
        }
        y += RANK_GAP;
      }
      canvasWidth = widest + MARGIN * 2;
      canvasHeight = y - RANK_GAP + MARGIN * 2;
    }

    const seen = new Set<string>();
    const kept: Edge[] = [];
    const available = new Set<string>();
    let dropped = 0;
    for (const entity of entities) {
      for (const relation of (entity.relations?.outgoing ?? []) as Relation[]) {
        const from = placed.get(relation.source_id ?? entity.id);
        const to = placed.get(relation.target_id ?? -1);
        if (!from || !to || from.id === to.id) continue;
        const kind = edgeKind(relation);
        const key = `${from.id}-${to.id}-${kind}`;
        if (seen.has(key)) continue;
        seen.add(key);
        available.add(kind);
        if (hiddenKinds.has(kind)) continue;
        if (edgeLimit > 0 && kept.length >= edgeLimit) { dropped += 1; continue; }
        kept.push({ id: key, from, to, kind, subtype: relation.subtype ?? kind });
      }
    }

    return {
      nodes: [...placed.values()],
      edges: kept,
      kinds: [...available].sort(),
      counts: tally,
      width: canvasWidth,
      height: canvasHeight,
      hiddenByLimit: dropped,
    };
  }, [entities, adjacency, scope, layout, focusId, hiddenKinds, edgeLimit]);

  /** Everything one hop from whatever the pointer or the inspector is on. */
  const highlighted = useMemo(() => {
    const anchor = activeNodeId ?? selectedId;
    if (anchor === null) return null;
    const near = new Set(adjacency.get(anchor) ?? []);
    near.add(anchor);
    return near;
  }, [activeNodeId, selectedId, adjacency]);

  const matching = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return null;
    return new Set(nodes.filter((node) => node.text.toLowerCase().includes(needle)).map((n) => n.id));
  }, [query, nodes]);

  // Place labels in screen pixels so their text stays readable at every zoom.
  // Strivings reserve space first and are never hidden by hover, search, or overlap.
  const labels = useMemo(() => {
    const frameWidth = frameRef.current?.clientWidth ?? 800;
    const frameHeight = frameRef.current?.clientHeight ?? 600;
    if (labelContext) labelContext.font = `600 ${LABEL_FONT_SIZE}px ${getComputedStyle(document.documentElement).fontFamily}`;
    const measure = (value: string) => labelContext?.measureText(value).width ?? value.length * 7;
    const maxTextWidth = Math.max(80, Math.min(260, frameWidth - 40));
    const priority = (node: Node) => node.level === 'goal' ? -4 : node.id === activeNodeId ? -3
      : node.id === selectedId ? -2 : matching?.has(node.id) ? -1 : RANKS.indexOf(node.level);
    const candidates = nodes.filter((node) => {
      const active = node.id === selectedId || node.id === activeNodeId;
      if (node.level === 'goal' || active) return true;
      if ((highlighted && !highlighted.has(node.id)) || (matching && !matching.has(node.id))) return false;
      return (node.level === 'activity' && view.scale >= 0.5)
        || matching?.has(node.id) || view.scale > 1.15;
    }).sort((a, b) => priority(a) - priority(b));
    const nodeBoxes = nodes.map((node) => {
      const radius = Math.max(node.r * view.scale, MIN_RADIUS[node.level]) + 3;
      return { left: view.x + node.x * view.scale - radius, top: view.y + node.y * view.scale - radius,
        width: radius * 2, height: radius * 2 };
    });
    const placed: Array<{ node: Node; lines: string[]; box: LabelBox; x: number; y: number; radius: number; displaced: boolean }> = [];
    for (const node of candidates) {
      const required = node.level === 'goal' || node.id === selectedId || node.id === activeNodeId;
      const lines = wrapLabel(required ? node.text : truncate(node.text, LABEL_CHARS[node.level]), maxTextWidth, measure);
      const width = Math.ceil(Math.max(...lines.map((line) => measure(line.trimEnd())))) + 12;
      const height = lines.length * LABEL_LINE_HEIGHT + 8;
      const x = view.x + node.x * view.scale;
      const y = view.y + node.y * view.scale;
      const radius = Math.max(node.r * view.scale, MIN_RADIUS[node.level]);
      const below = { left: x - width / 2, top: y + radius + 6, width, height };
      const positions = [below,
        { ...below, top: y - radius - height - 6 },
        { ...below, left: x + radius + 8, top: y - height / 2 },
        { ...below, left: x - radius - width - 8, top: y - height / 2 },
        { ...below, left: x + radius + 8 },
        { ...below, left: x - radius - width - 8 },
        { ...below, left: x + radius + 8, top: y - radius - height - 6 },
        { ...below, left: x - radius - width - 8, top: y - radius - height - 6 },
      ];
      if (required) {
        // A small sideways offset often clears a dense branch without moving
        // its label far above or below the node.
        for (const offset of [width / 4, width / 2, width]) {
          positions.push({ ...below, left: x + radius + 8 + offset, top: y - height / 2 },
            { ...below, left: x - radius - width - 8 - offset, top: y - height / 2 });
        }
        // Nearby striving labels can use another row instead of disappearing.
        for (let offset = height + 8; offset < frameHeight; offset += height + 8) {
          positions.push({ ...below, top: below.top + offset }, { ...below, top: below.top - offset });
        }
      }
      let box = below;
      let bestScore = Infinity;
      for (const position of positions) {
        const candidate = x >= 0 && x <= frameWidth && y >= 0 && y <= frameHeight
          ? { ...position, left: Math.max(8, Math.min(position.left, frameWidth - width - 8)),
            top: Math.max(8, Math.min(position.top, frameHeight - height - 8)) }
          : position;
        const labelOverlap = placed.reduce((total, other) => total + overlapArea(candidate, other.box), 0);
        const nodeOverlap = nodeBoxes.reduce((total, other) => total + overlapArea(candidate, other), 0);
        const distance = Math.hypot(x - Math.max(candidate.left, Math.min(x, candidate.left + width)),
          y - Math.max(candidate.top, Math.min(y, candidate.top + height)));
        const score = labelOverlap * 10 + nodeOverlap + Math.max(0, distance - radius - 6);
        if (score < bestScore) { box = candidate; bestScore = score; }
        if (score === 0) break;
      }
      if (!required && placed.some((other) => overlapArea(box, other.box) > 0)) continue;
      placed.push({ node, lines, box, x, y, radius,
        displaced: Math.abs(box.left - below.left) > 1 || Math.abs(box.top - below.top) > 1 });
    }
    return placed;
  }, [nodes, view, matching, highlighted, selectedId, activeNodeId, labelContext, fontsReady]);

  const maxEdgeSlider = useMemo(
    () => Math.max(200, Math.ceil(edges.length / 100) * 100 + 200),
    [edges.length],
  );

  // ── pan and zoom ─────────────────────────────────────────────────────────

  const onWheel = useCallback((event: React.WheelEvent) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    setView((current) => ({
      ...current,
      scale: Math.min(3, Math.max(MIN_SCALE, current.scale - event.deltaY * 0.002)),
    }));
  }, []);

  function startDrag(event: React.MouseEvent) {
    if (event.button !== 0) return;
    dragging.current = { x: event.clientX, y: event.clientY, ox: view.x, oy: view.y, moved: false };
  }
  function onDrag(event: React.MouseEvent) {
    const start = dragging.current;
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) start.moved = true;
    setView((current) => ({ ...current, x: start.ox + dx, y: start.oy + dy }));
  }
  const endDrag = () => { dragging.current = null; };

  const fit = useCallback(() => {
    const frame = frameRef.current;
    if (!frame || width === 0) return;
    const scale = Math.max(MIN_SCALE, Math.min(1.4,
      (frame.clientWidth - 48) / width, (frame.clientHeight - 48) / height));
    setView({
      x: (frame.clientWidth - width * scale) / 2,
      y: (frame.clientHeight - height * scale) / 2,
      scale,
    });
  }, [width, height]);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(frame);
    return () => observer.disconnect();
  }, [fit, loading]);

  function reset() {
    setFocusId(null);
    setScope('full');
    setQuery('');
    setEdgeLimit(0);
    setHiddenKinds(new Set());
    onSelectEntity(null);
  }

  /** A click that did not pan opens the node; a second click focuses on it. */
  function activate(node: Node) {
    if (dragging.current?.moved) return;
    if (selectedId === node.id) setFocusId(focusId === node.id ? null : node.id);
    onSelectEntity(selectedId === node.id && focusId === node.id ? null : node.id);
  }

  if (loading) return <div className="gv-state">Loading the graph…</div>;
  if (error) return <div className="gv-state is-error" role="alert">{error}</div>;

  return (
    <div className="gv">
      <div className="gv-toolbar">
        <div className="gv-segmented" role="group" aria-label="Scope">
          <button type="button" className={scope === 'activities' ? 'is-active' : undefined}
                  aria-pressed={scope === 'activities'} onClick={() => setScope('activities')}>
            Activities only
          </button>
          <button type="button" className={scope === 'full' ? 'is-active' : undefined}
                  aria-pressed={scope === 'full'} onClick={() => setScope('full')}>
            Full graph
          </button>
        </div>

        <div className="gv-segmented" role="group" aria-label="Layout">
          <button type="button" className={layout === 'layered' ? 'is-active' : undefined}
                  aria-pressed={layout === 'layered'} onClick={() => setLayout('layered')}
                  title="Rank by hierarchy depth">
            Layered
          </button>
          <button type="button" className={layout === 'force' ? 'is-active' : undefined}
                  aria-pressed={layout === 'force'} onClick={() => setLayout('force')}
                  title="Let the relations decide the shape">
            Force
          </button>
        </div>

        <label className="gv-toggle">
          <input type="checkbox" checked={showOperations}
                 onChange={(event) => setShowOperations(event.target.checked)} />
          Show operations
        </label>

        <input
          className="gv-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter nodes…"
          aria-label="Filter nodes by text"
        />

        <label className="gv-slider">
          <span>Edges: {edgeLimit === 0 ? 'all' : edgeLimit}</span>
          <input type="range" min={0} max={maxEdgeSlider} step={50} value={edgeLimit}
                 onChange={(event) => setEdgeLimit(Number(event.target.value))}
                 aria-label="Maximum edges drawn" />
        </label>

        {focusId !== null && <span className="gv-focus-chip">Focused · 2 hops</span>}
        <button type="button" className="gv-reset" onClick={reset}>Reset view</button>

        <div className="gv-zoom">
          <button type="button" onClick={() => setView((v) => ({ ...v, scale: Math.max(MIN_SCALE, v.scale - 0.15) }))} aria-label="Zoom out">−</button>
          <span>{Math.round(view.scale * 100)}%</span>
          <button type="button" onClick={() => setView((v) => ({ ...v, scale: Math.min(3, v.scale + 0.15) }))} aria-label="Zoom in">+</button>
          <button type="button" className="gv-fit" onClick={fit}>Fit</button>
        </div>
      </div>

      <div
        className="gv-frame"
        ref={frameRef}
        onWheel={onWheel}
        onMouseDown={startDrag}
        onMouseMove={onDrag}
        onMouseUp={endDrag}
        onMouseLeave={() => { endDrag(); setHoveredId(null); }}
      >
        {nodes.length === 0 ? (
          <div className="gv-state">
            {entities.length === 0
              ? 'Nothing to draw yet — record for a while first.'
              : 'Nothing matches these filters.'}
          </div>
        ) : (
          <svg className="gv-canvas" width="100%" height="100%" role="img" aria-label="Behaviour graph">
            <defs>
              {Object.entries(KIND).map(([kind, style]) => (
                <marker key={kind} id={`gv-tip-${kind}`} viewBox="0 0 8 8" refX="7" refY="4"
                        markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                  <path d="M 0 1 L 7 4 L 0 7 z" fill={style.color} />
                </marker>
              ))}
            </defs>

            <g transform={`translate(${view.x}, ${view.y}) scale(${view.scale})`}>
              <g>
                {edges.map((edge) => {
                  const style = KIND[edge.kind] ?? KIND.structural;
                  const dim = highlighted !== null
                    && !(highlighted.has(edge.from.id) && highlighted.has(edge.to.id));
                  const mx = (edge.from.x + edge.to.x) / 2;
                  const my = (edge.from.y + edge.to.y) / 2;
                  // A slight bow keeps parallel relations between the same two
                  // nodes from stacking into one indistinguishable line.
                  const bow = (edge.to.x - edge.from.x) * 0.06;
                  return (
                    <path
                      key={edge.id}
                      d={`M ${edge.from.x} ${edge.from.y} Q ${mx - bow} ${my + bow} ${edge.to.x} ${edge.to.y}`}
                      fill="none"
                      stroke={style.color}
                      strokeWidth={style.width}
                      vectorEffect="non-scaling-stroke"
                      strokeDasharray={style.dash}
                      markerEnd={`url(#gv-tip-${edge.kind})`}
                      opacity={dim ? 0.16 : 0.6}
                    >
                      <title>{edge.subtype}</title>
                    </path>
                  );
                })}
              </g>

              <g>
                {nodes.map((node) => {
                  const radius = Math.max(node.r, MIN_RADIUS[node.level] / view.scale);
                  const selected = node.id === selectedId;
                  const focused = node.id === focusId;
                  const hovered = node.id === activeNodeId;
                  const dim = (highlighted !== null && !highlighted.has(node.id))
                    || (matching !== null && !matching.has(node.id));
                  return (
                    <g
                      key={node.id}
                      className={`gv-node${selected ? ' is-selected' : ''}${focused ? ' is-focus' : ''}${dim ? ' is-muted' : ''}`}
                      aria-label={`${node.level}: ${node.text}`}
                      aria-pressed={selected}
                      onMouseEnter={() => setHoveredId(node.id)}
                      onMouseLeave={() => setHoveredId(null)}
                      onFocus={() => setKeyboardNodeId(node.id)}
                      onBlur={() => setKeyboardNodeId(null)}
                      onClick={(event) => { event.stopPropagation(); activate(node); }}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          activate(node);
                        }
                      }}
                    >
                      <circle className="gv-node-hit" cx={node.x} cy={node.y}
                              r={Math.max(radius, 10 / view.scale)} fill="transparent" />
                      <NodeCircle level={node.level} x={node.x} y={node.y} radius={radius} />
                      <circle className="gv-node-ring" cx={node.x} cy={node.y}
                              r={radius + 3 / view.scale} fill="none" strokeWidth={2}
                              vectorEffect="non-scaling-stroke"
                              visibility={selected || focused || hovered || matching?.has(node.id) ? 'visible' : 'hidden'} />
                      <title>{`${node.level} · ${node.text}`}</title>
                    </g>
                  );
                })}
              </g>
            </g>
            <g className="gv-label-layer" aria-hidden="true">
              {labels.map(({ node, lines, box, x, y, radius, displaced }) => {
                const targetX = Math.max(box.left, Math.min(x, box.left + box.width));
                const targetY = Math.max(box.top, Math.min(y, box.top + box.height));
                const distance = Math.hypot(targetX - x, targetY - y) || 1;
                return (
                  <g key={node.id} className="gv-node-label" data-entity-id={node.id} data-level={node.level}>
                    {displaced && <line className="gv-label-leader"
                      x1={x + (targetX - x) * radius / distance} y1={y + (targetY - y) * radius / distance}
                      x2={targetX} y2={targetY} />}
                    <text className="gv-label" x={box.left + box.width / 2} y={box.top + LABEL_FONT_SIZE + 4}
                          style={{ fontSize: LABEL_FONT_SIZE }} strokeWidth={3} textAnchor="middle" fill={PAPER_INK}>
                      {lines.map((line, index) => (
                        <tspan key={index} x={box.left + box.width / 2} dy={index === 0 ? 0 : LABEL_LINE_HEIGHT}>{line}</tspan>
                      ))}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>
        )}
      </div>

      <div className="gv-legend-bar">
        <div className="gv-counts">
          {RANKS.filter((level) => counts[level]).map((level) => (
            <span className="gv-count" key={level}>
              <svg className="gv-legend-mark" viewBox="-14 -14 28 28" width="28" height="28" aria-hidden="true">
                <NodeCircle level={level} radius={MIN_RADIUS[level]} />
              </svg>
              {LEVEL_LABEL[level]} ({counts[level]})
            </span>
          ))}
        </div>

        <div className="gv-kinds" aria-label="Relation kinds">
          {kinds.map((kind) => {
            const style = KIND[kind] ?? KIND.structural;
            const off = hiddenKinds.has(kind);
            return (
              <button
                type="button"
                key={kind}
                className={`gv-kind${off ? ' is-off' : ''}`}
                aria-pressed={!off}
                onClick={() => setHiddenKinds((current) => {
                  const next = new Set(current);
                  if (next.has(kind)) next.delete(kind); else next.add(kind);
                  return next;
                })}
              >
                <svg width="18" height="8" aria-hidden="true">
                  <line x1="1" y1="4" x2="17" y2="4" stroke={style.color}
                        strokeWidth={style.width + 0.4} strokeDasharray={style.dash} strokeLinecap="round" />
                </svg>
                {style.label}
              </button>
            );
          })}
          {hiddenByLimit > 0 && <span className="gv-dropped">{hiddenByLimit} edges hidden by the limit</span>}
        </div>
      </div>
    </div>
  );
}
