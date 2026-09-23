"""Compile user edits back into the hierarchy.

A *compile* is the deliberate counterpart to the incremental edits in
``tempo.hierarchy``: instead of committing one rename or reparent at a time, the
user marks up the whole tree — accepting, rejecting, relabelling, merging — and
then asks the system to re-think the hierarchy with that feedback as signal.

Every compile runs against a **working copy** of the database, so a preview
never touches live data:

    live tempo.db ──copy──> compile/<token>.db
                                │
                                ├─ apply the staged edit set
                                ├─ re-run activity + goal synthesis
                                └─ diff before/after
                                        │
                       accept ──────────┴────────── revert
                          │                            │
              swap copy over live               delete the copy

The synthesis jobs already read ``user_locked`` / ``user_edited`` /
``user_reassigned`` / ``user_annotations`` off entity metadata when they build
their prompts (see ``_user_constraints_block`` in both jobs), so applying the
edit set to the copy is what makes the re-run honour the user's intent. This
module's job is to write those flags faithfully and to keep the copy isolated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from sqlalchemy import select

from tempo.db import Database
from tempo.hierarchy import HierarchyError, HierarchyService
from tempo.models import Entity, Relation, RelationSubtype, RelationType
from tempo.store import Store

logger = logging.getLogger(__name__)

# Working copies older than this are swept on the next compile. A compile the
# user walked away from should not pin disk forever.
STALE_DRAFT_SECONDS = 24 * 60 * 60


@dataclass
class _SynthesisProgress:
    """Live counters the compile reports while re-synthesis runs.

    Re-synthesis is a single `await` that can last minutes, so the generator
    cannot yield from inside it. Instead the jobs update this object as they
    go and the generator polls it — which is what turns a still spinner into
    a progress report.
    """

    #: Which job is running: "activities", "goals", or "done".
    stage: str = "activities"
    #: Model calls started so far, across both jobs.
    calls: int = 0

    STAGE_LABELS = {
        "activities": "Grouping your actions into activities",
        "goals": "Synthesizing strivings from those activities",
        "done": "Finishing up",
    }

    def label(self) -> str:
        return self.STAGE_LABELS.get(self.stage, "Re-running the model")


class _CountingProvider:
    """Provider proxy that counts model calls without altering behaviour."""

    def __init__(self, inner: Any, progress: Optional[_SynthesisProgress]) -> None:
        self._inner = inner
        self._progress = progress

    def __getattr__(self, name: str) -> Any:
        # Everything the jobs touch beyond the two call methods.
        return getattr(self._inner, name)

    def _count(self) -> None:
        if self._progress is not None:
            self._progress.calls += 1

    async def chat_completion(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return await self._inner.chat_completion(*args, **kwargs)

    async def vision_completion(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return await self._inner.vision_completion(*args, **kwargs)


class CompileError(Exception):
    """A compile could not be started, accepted, or reverted."""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CompileNotFound(CompileError):
    status_code = 404


class CompileConflict(CompileError):
    status_code = 409


# ─────────────────────────────────────────────────────────────────────────────
# Edit set
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HierarchyEdits:
    """The user's markup of the tree, as staged in the editor.

    Nothing here is applied until :meth:`HierarchyCompiler.compile` runs it
    against a working copy, and nothing reaches the live database until the
    resulting draft is accepted.
    """

    # entity id -> replacement text
    text_overrides: dict[int, str] = field(default_factory=dict)
    # entities the user rejected outright
    rejected_ids: list[int] = field(default_factory=list)
    # entities the user pinned; synthesis must leave these alone
    locked_ids: list[int] = field(default_factory=list)
    # (survivor, absorbed) pairs
    goal_merges: list[tuple[int, int]] = field(default_factory=list)
    activity_merges: list[tuple[int, int]] = field(default_factory=list)
    # "actionId:activityId" — drop just that parent link, not the action
    removed_action_relations: list[str] = field(default_factory=list)
    # actions removed everywhere
    removed_action_ids: list[int] = field(default_factory=list)
    # (entity, new parent)
    reparents: list[tuple[int, int]] = field(default_factory=list)
    # free-text context the user attached while editing: {entity_id, type, text}
    annotations: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HierarchyEdits":
        """Build an edit set from the JSON body the editor posts."""

        def _int_keyed(raw: Any) -> dict[int, str]:
            out: dict[int, str] = {}
            for key, value in (raw or {}).items():
                try:
                    entity_id = int(key)
                except (TypeError, ValueError):
                    continue
                text = str(value).strip()
                if text:
                    out[entity_id] = text
            return out

        def _ids(raw: Any) -> list[int]:
            out: list[int] = []
            for value in raw or []:
                try:
                    out.append(int(value))
                except (TypeError, ValueError):
                    continue
            return list(dict.fromkeys(out))

        def _pairs(raw: Any) -> list[tuple[int, int]]:
            out: list[tuple[int, int]] = []
            for pair in raw or []:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                try:
                    keep, absorbed = int(pair[0]), int(pair[1])
                except (TypeError, ValueError):
                    continue
                if keep != absorbed:
                    out.append((keep, absorbed))
            return out

        def _relation_keys(raw: Any) -> list[str]:
            out: list[str] = []
            for value in raw or []:
                parts = str(value).split(":")
                if len(parts) != 2:
                    continue
                try:
                    int(parts[0]), int(parts[1])
                except ValueError:
                    continue
                out.append(f"{parts[0]}:{parts[1]}")
            return list(dict.fromkeys(out))

        def _annotations(raw: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for item in raw or []:
                if not isinstance(item, dict):
                    continue
                try:
                    entity_id = int(item.get("entity_id"))
                except (TypeError, ValueError):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                out.append({
                    "entity_id": entity_id,
                    "type": str(item.get("type") or "note")[:40],
                    "text": text[:2000],
                })
            return out

        return cls(
            text_overrides=_int_keyed(payload.get("text_overrides")),
            rejected_ids=_ids(payload.get("rejected_ids")),
            locked_ids=_ids(payload.get("locked_ids")),
            goal_merges=_pairs(payload.get("goal_merges")),
            activity_merges=_pairs(payload.get("activity_merges")),
            removed_action_relations=_relation_keys(payload.get("removed_action_relations")),
            removed_action_ids=_ids(payload.get("removed_action_ids")),
            reparents=_pairs(payload.get("reparents")),
            annotations=_annotations(payload.get("annotations")),
        )

    def is_empty(self) -> bool:
        return not any((
            self.text_overrides,
            self.rejected_ids,
            self.locked_ids,
            self.goal_merges,
            self.activity_merges,
            self.removed_action_relations,
            self.removed_action_ids,
            self.reparents,
            self.annotations,
        ))

    def summary(self) -> dict[str, int]:
        return {
            "text_edits": len(self.text_overrides),
            "rejections": len(self.rejected_ids),
            "locks": len(self.locked_ids),
            "goal_merges": len(self.goal_merges),
            "activity_merges": len(self.activity_merges),
            "action_removals": len(self.removed_action_relations) + len(self.removed_action_ids),
            "reparents": len(self.reparents),
            "annotations": len(self.annotations),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Compiler
# ─────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HierarchyCompiler:
    """Run compiles against working copies of one Tempo database."""

    def __init__(
        self,
        *,
        data_directory: Path,
        db_name: str = "tempo.db",
        drafts_dirname: str = "compile",
    ) -> None:
        self.data_directory = Path(data_directory).expanduser()
        self.db_name = db_name
        self.drafts_dir = self.data_directory / drafts_dirname
        # Serializes accept/revert against each other so two clients cannot
        # swap the live file at the same time.
        self._swap_lock = asyncio.Lock()

    # ── paths ────────────────────────────────────────────────────────────

    @property
    def live_path(self) -> Path:
        return self.data_directory / self.db_name

    def draft_path(self, token: str) -> Path:
        # Tokens are minted by us as hex; reject anything else rather than
        # letting a caller-supplied string walk out of the drafts directory.
        if not token or any(char not in "0123456789abcdef" for char in token):
            raise CompileNotFound(f"Unknown compile draft {token!r}")
        return self.drafts_dir / f"draft_{token}.db"

    def meta_path(self, token: str) -> Path:
        return self.draft_path(token).with_suffix(".json")

    # ── draft lifecycle ──────────────────────────────────────────────────

    def _sweep_stale_drafts(self) -> None:
        if not self.drafts_dir.exists():
            return
        cutoff = time.time() - STALE_DRAFT_SECONDS
        for path in self.drafts_dir.glob("draft_*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                logger.debug("Could not sweep stale compile draft %s", path, exc_info=True)

    def _copy_live_database(self, token: str) -> Path:
        """Copy the live database (and its WAL sidecars) to a draft file."""
        if not self.live_path.exists():
            raise CompileError(
                "No Tempo database yet — record some activity before compiling."
            )
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        destination = self.draft_path(token)
        shutil.copy2(self.live_path, destination)
        # The live database runs in WAL mode, so recent writes may live only in
        # the sidecar files. Carry them across or the copy reads as stale.
        for suffix in ("-wal", "-shm"):
            sidecar = self.live_path.with_name(self.live_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, destination.with_name(destination.name + suffix))
        return destination

    def _write_meta(self, token: str, meta: dict[str, Any]) -> None:
        self.meta_path(token).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def read_meta(self, token: str) -> dict[str, Any]:
        path = self.meta_path(token)
        if not path.exists():
            raise CompileNotFound(f"Unknown compile draft {token!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CompileNotFound(f"Compile draft {token!r} is unreadable") from exc

    def _discard_draft(self, token: str) -> None:
        base = self.draft_path(token)
        for path in (
            base,
            base.with_name(base.name + "-wal"),
            base.with_name(base.name + "-shm"),
            self.meta_path(token),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning("Could not remove compile draft file %s", path, exc_info=True)

    # ── snapshots and diffing ────────────────────────────────────────────

    @staticmethod
    async def _snapshot(session) -> dict[str, Any]:
        """Flatten the hierarchy into an id-keyed shape that diffs cleanly."""
        tree = await HierarchyService(session).get_hierarchy(max_depth=4)
        nodes: dict[str, Any] = {}

        def walk(node: dict[str, Any], parent_id: Optional[int]) -> None:
            nodes[str(node["id"])] = {
                "id": node["id"],
                "type": node["type"],
                "text": node["text"],
                "parent_id": parent_id,
                "child_ids": [child["id"] for child in node["children"]],
            }
            for child in node["children"]:
                walk(child, node["id"])

        for root in tree["roots"]:
            walk(root, None)
        return {"roots": tree["roots"], "nodes": nodes, "count": tree["count"]}

    @staticmethod
    def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        """Compare two snapshots into per-entity highlights for the preview."""
        before_nodes: dict[str, Any] = before["nodes"]
        after_nodes: dict[str, Any] = after["nodes"]

        highlights: dict[str, str] = {}
        added: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []

        for key, node in after_nodes.items():
            prior = before_nodes.get(key)
            if prior is None:
                highlights[key] = "added"
                added.append({"id": node["id"], "type": node["type"], "text": node["text"]})
            elif prior["text"] != node["text"] or prior["parent_id"] != node["parent_id"]:
                highlights[key] = "modified"
                modified.append({
                    "id": node["id"],
                    "type": node["type"],
                    "text": node["text"],
                    "previous_text": prior["text"],
                    "previous_parent_id": prior["parent_id"],
                    "parent_id": node["parent_id"],
                })

        for key, node in before_nodes.items():
            if key not in after_nodes:
                removed.append({"id": node["id"], "type": node["type"], "text": node["text"]})

        return {
            "highlights": highlights,
            "added": added,
            "modified": modified,
            "removed": removed,
            "counts": {
                "added": len(added),
                "modified": len(modified),
                "removed": len(removed),
            },
        }

    # ── applying the edit set ────────────────────────────────────────────

    async def _apply_edits(self, session, edits: HierarchyEdits) -> dict[str, int]:
        """Write the user's markup onto the working copy.

        Applied directly rather than through ``HierarchyService`` because the
        editor stages a whole batch at once: revision checks belong to the
        interactive single-edit path, and half of this markup (rejections,
        annotations) has no service equivalent.
        """
        store = Store(session)
        applied = {
            "text_edits": 0,
            "rejections": 0,
            "locks": 0,
            "merges": 0,
            "action_removals": 0,
            "reparents": 0,
            "annotations": 0,
        }

        async def _load(entity_id: int) -> Optional[Entity]:
            try:
                return await store.entities.get(entity_id)
            except Exception:
                return None

        async def _patch(entity: Entity, **values: Any) -> None:
            metadata = dict(entity.metadata_dict or {})
            metadata.update(values)
            metadata["edited_at"] = _now()
            await store.entities.update(entity.id, metadata=metadata)

        # 1. Text overrides — the label the user wants kept verbatim.
        for entity_id, text in edits.text_overrides.items():
            entity = await _load(entity_id)
            if entity is None:
                continue
            metadata = dict(entity.metadata_dict or {})
            metadata.setdefault("original_text", entity.text)
            metadata["user_edited"] = True
            metadata["user_text_override"] = text
            metadata["edited_at"] = _now()
            await store.entities.update(entity_id, text=text, metadata=metadata)
            applied["text_edits"] += 1

        # 2. Locks — pin an entity so synthesis leaves it intact.
        for entity_id in edits.locked_ids:
            entity = await _load(entity_id)
            if entity is None:
                continue
            await _patch(entity, user_locked=True)
            applied["locks"] += 1

        # 3. Annotations — privileged context the prompts weight above
        #    behavioral inference.
        grouped: dict[int, list[dict[str, Any]]] = {}
        for annotation in edits.annotations:
            grouped.setdefault(annotation["entity_id"], []).append({
                "type": annotation["type"],
                "text": annotation["text"],
                "at": _now(),
            })
        for entity_id, notes in grouped.items():
            entity = await _load(entity_id)
            if entity is None:
                continue
            metadata = dict(entity.metadata_dict or {})
            existing = list(metadata.get("user_annotations") or [])
            existing.extend(notes)
            metadata["user_annotations"] = existing
            metadata["edited_at"] = _now()
            await store.entities.update(entity_id, metadata=metadata)
            applied["annotations"] += len(notes)

        # 4. Merges — fold the absorbed entity's children onto the survivor.
        for keep_id, absorbed_id in (*edits.goal_merges, *edits.activity_merges):
            survivor = await _load(keep_id)
            absorbed = await _load(absorbed_id)
            if survivor is None or absorbed is None or survivor.type != absorbed.type:
                continue
            child_relations = await store.relations.get_by_target(
                absorbed_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            for relation in child_relations or []:
                already = await session.execute(
                    select(Relation.id).where(
                        Relation.source_id == relation.source_id,
                        Relation.target_id == keep_id,
                        Relation.relation_type == RelationType.STRUCTURAL,
                        Relation.relation_subtype == RelationSubtype.PART_OF,
                    )
                )
                if already.scalar_one_or_none() is None:
                    await store.relations.create(
                        source_id=relation.source_id,
                        target_id=keep_id,
                        relation_type=RelationType.STRUCTURAL,
                        relation_subtype=RelationSubtype.PART_OF,
                    )
                await store.relations.delete(relation.id)

            await _patch(
                absorbed,
                removed_by_user=True,
                merged_into=keep_id,
                user_edited=True,
            )
            await _patch(survivor, user_edited=True, merged_from=[
                *(list((survivor.metadata_dict or {}).get("merged_from") or [])),
                absorbed_id,
            ])
            applied["merges"] += 1

        # 5. Action removals — either one parent link, or the action entirely.
        for key in edits.removed_action_relations:
            action_id_text, activity_id_text = key.split(":")
            action_id, activity_id = int(action_id_text), int(activity_id_text)
            relations = await store.relations.get_by_source(
                action_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            for relation in relations or []:
                if relation.target_id == activity_id:
                    await store.relations.delete(relation.id)
                    applied["action_removals"] += 1
                    break
            action = await _load(action_id)
            if action is not None:
                detached = list((action.metadata_dict or {}).get("removed_from_activities") or [])
                if activity_id not in detached:
                    detached.append(activity_id)
                await _patch(action, removed_from_activities=detached)

        for action_id in edits.removed_action_ids:
            action = await _load(action_id)
            if action is None:
                continue
            relations = await store.relations.get_by_source(
                action_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            for relation in relations or []:
                await store.relations.delete(relation.id)
            await _patch(action, removed_by_user=True, user_edited=True)
            applied["action_removals"] += 1

        # 6. Reparents — the user moved a node under a different parent.
        for entity_id, new_parent_id in edits.reparents:
            entity = await _load(entity_id)
            parent = await _load(new_parent_id)
            if entity is None or parent is None:
                continue
            old_relations = await store.relations.get_by_source(
                entity_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            old_parent_ids = [relation.target_id for relation in old_relations or []]
            for relation in old_relations or []:
                await store.relations.delete(relation.id)
            await store.relations.create(
                source_id=entity_id,
                target_id=new_parent_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            await _patch(
                entity,
                user_reassigned=True,
                reassigned_from=old_parent_ids,
                reassigned_to=new_parent_id,
            )
            applied["reparents"] += 1

        # 7. Rejections last, so a rejected entity can still have donated its
        #    children to a survivor above.
        for entity_id in edits.rejected_ids:
            entity = await _load(entity_id)
            if entity is None:
                continue
            await _patch(entity, removed_by_user=True, rejected_by_user=True, user_edited=True)
            applied["rejections"] += 1

        return applied

    # ── re-synthesis ─────────────────────────────────────────────────────

    @staticmethod
    async def _resynthesize(
        session,
        provider,
        *,
        user_name: Optional[str],
        progress: Optional["_SynthesisProgress"] = None,
    ) -> dict[str, Any]:
        """Re-run activity and goal synthesis over the edited copy.

        Both jobs read the user-edit metadata written above when they build
        their prompts, so this is where the user's feedback actually reaches
        the model.
        """
        from tempo.pipelines.action_to_activities import ActivityProposeJob
        from tempo.pipelines.goal_synthesis import GoalSynthesisJob

        store = Store(session)
        results: dict[str, Any] = {}

        # Wrapped so the caller can report model calls as they happen. The two
        # jobs make an unpredictable number of calls, and together they account
        # for effectively all of a compile's runtime — without this the UI has
        # nothing to show for minutes at a stretch.
        counted = _CountingProvider(provider, progress)

        if progress is not None:
            progress.stage = "activities"
        activity_job = ActivityProposeJob(counted, store, user_name=user_name)
        results["activities"] = await activity_job.run()

        if progress is not None:
            progress.stage = "goals"
        goal_job = GoalSynthesisJob(counted, store, user_name=user_name, mode="full")
        results["goals"] = await goal_job.run()

        if progress is not None:
            progress.stage = "done"
        return results

    # ── compile ──────────────────────────────────────────────────────────

    async def compile(
        self,
        edits: HierarchyEdits,
        *,
        provider: Any = None,
        user_name: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a compile, yielding progress events then a final result.

        Yields ``{"type": "progress", ...}`` events throughout and exactly one
        terminal ``{"type": "complete", ...}`` or ``{"type": "error", ...}``.
        """
        self._sweep_stale_drafts()
        token = uuid.uuid4().hex
        draft: Optional[Path] = None

        try:
            yield {"type": "progress", "step": "snapshot", "detail": "Creating working copy…"}
            draft = await asyncio.to_thread(self._copy_live_database, token)

            db = Database(db_name=draft.name, data_directory=str(self.drafts_dir))
            await db.connect()
            try:
                async with db.session() as session:
                    before = await self._snapshot(session)

                yield {"type": "progress", "step": "edits", "detail": "Applying your edits…"}
                async with db.session() as session:
                    applied = await self._apply_edits(session, edits)

                if provider is not None:
                    progress = _SynthesisProgress()
                    started = time.monotonic()
                    yield {
                        "type": "progress", "step": "synthesize",
                        "detail": progress.label(), "llm_calls": 0, "elapsed_seconds": 0,
                    }
                    async with db.session() as session:
                        task = asyncio.create_task(
                            self._resynthesize(
                                session, provider, user_name=user_name, progress=progress
                            )
                        )
                        # Poll rather than await outright: this is the only part
                        # of a compile long enough for the user to wonder whether
                        # anything is happening.
                        while not task.done():
                            done, _ = await asyncio.wait({task}, timeout=1.0)
                            if done:
                                break
                            yield {
                                "type": "progress", "step": "synthesize",
                                "detail": progress.label(),
                                "llm_calls": progress.calls,
                                "elapsed_seconds": round(time.monotonic() - started, 1),
                            }
                        await task  # re-raise anything the jobs failed with
                    synthesized = True
                    total_calls = progress.calls
                else:
                    # Without a provider the edits still compile — the tree just
                    # is not re-thought. Say so rather than failing the compile.
                    yield {
                        "type": "progress",
                        "step": "synthesize",
                        "detail": "No model configured — applying edits only.",
                    }
                    synthesized = False
                    total_calls = 0

                yield {"type": "progress", "step": "diff", "detail": "Comparing before and after…"}
                async with db.session() as session:
                    after = await self._snapshot(session)
            finally:
                await db.close()

            diff = self._diff(before, after)
            meta = {
                "token": token,
                "created_at": _now(),
                "db_name": self.db_name,
                "applied": applied,
                "requested": edits.summary(),
                "synthesized": synthesized,
                "llm_calls": total_calls,
                "counts": diff["counts"],
            }
            self._write_meta(token, meta)

            yield {
                "type": "complete",
                "data": {
                    "token": token,
                    "synthesized": synthesized,
                    "llm_calls": total_calls,
                    "applied": applied,
                    "before": before["roots"],
                    "after": after["roots"],
                    **diff,
                },
            }
        except (CompileError, HierarchyError) as exc:
            if draft is not None:
                self._discard_draft(token)
            yield {"type": "error", "error": exc.message}
        except Exception as exc:
            logger.exception("Hierarchy compile failed")
            if draft is not None:
                self._discard_draft(token)
            yield {"type": "error", "error": f"Compile failed: {exc}"}

    # ── accept / revert ──────────────────────────────────────────────────

    async def accept(self, token: str, *, on_quiesce=None) -> dict[str, Any]:
        """Promote a draft to be the live database.

        ``on_quiesce`` is awaited with every live database handle closed before
        the swap and again after it, so the caller can release and rebuild its
        own connections around the file move.
        """
        meta = self.read_meta(token)
        draft = self.draft_path(token)
        if not draft.exists():
            raise CompileNotFound(f"Compile draft {token!r} is no longer on disk")

        async with self._swap_lock:
            if on_quiesce is not None:
                await on_quiesce("before")
            try:
                backup = await asyncio.to_thread(self._swap_in_draft, draft)
            finally:
                if on_quiesce is not None:
                    await on_quiesce("after")

        self._discard_draft(token)
        meta["accepted_at"] = _now()
        meta["backup"] = str(backup)
        return meta

    def _swap_in_draft(self, draft: Path) -> Path:
        """Move a draft over the live database, keeping a timestamped backup."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.live_path.with_name(f"{self.live_path.name}.pre-compile-{stamp}")

        # Fold each database's WAL into its main file before moving anything,
        # so neither ends up paired with the other's sidecars.
        for path in (self.live_path, draft):
            _checkpoint_sqlite(path)

        if self.live_path.exists():
            shutil.move(str(self.live_path), str(backup))
        for suffix in ("-wal", "-shm"):
            stale = self.live_path.with_name(self.live_path.name + suffix)
            if stale.exists():
                stale.unlink()

        shutil.move(str(draft), str(self.live_path))
        for suffix in ("-wal", "-shm"):
            sidecar = draft.with_name(draft.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        return backup

    async def revert(self, token: str) -> dict[str, Any]:
        """Throw a draft away. The live database was never touched."""
        meta = self.read_meta(token)
        async with self._swap_lock:
            self._discard_draft(token)
        meta["reverted_at"] = _now()
        return meta


def _checkpoint_sqlite(path: Path) -> None:
    """Fold a SQLite file's WAL back into it, ignoring a missing database."""
    if not path.exists():
        return
    import sqlite3

    try:
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error:
        logger.warning("Could not checkpoint %s before swap", path, exc_info=True)
