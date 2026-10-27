# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Durable evidence dependencies and recoverable native Memory state changes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE as NODES
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Invalid graph identity, event ordering, or generation eligibility."""


def _nonblank(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("blank-lifecycle-value")
    return value


class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    reason: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _nonblank(value)


class _Node(BaseModel):
    node_id: str
    kind: Literal["source", "memory", "topic"]
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    revision: int = 1
    available: bool = True
    last_event: SourceLifecycleEvent | None = None


class SourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None = None) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        if self._database is None:
            raise SourceLifecycleError("lifecycle-storage-unavailable")
        return ScopedSourceLifecycle(self._runtime, self._database, validate_scope_id(scope_id))


class ScopedSourceLifecycle:
    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = scope_id

    async def _load(self) -> dict[str, _Node]:
        async with self._database.transaction() as connection:
            rows = await connection.scalars(select(NODES.c.payload).where(NODES.c.scope_id == self.scope_id))
            nodes = (_Node.model_validate_json(bytes(payload)) for payload in rows)
            return {node.node_id: node for node in nodes}

    async def _save(self, node: _Node, *, new: bool = False) -> None:
        reference = node.reference
        artifact_id = (
            reference.memory_ref.artifact_id
            if isinstance(reference, MemoryCitation)
            else reference.artifact_id
            if isinstance(reference, ArtifactRef)
            else None
        )
        values = {
            "kind": node.kind,
            "artifact_id": artifact_id,
            "available": node.available,
            "payload": node.model_dump_json().encode("utf-8"),
        }
        async with self._database.transaction() as connection:
            statement = (
                insert(NODES).values(scope_id=self.scope_id, node_id=node.node_id, **values)
                if new
                else update(NODES)
                .where(NODES.c.scope_id == self.scope_id, NODES.c.node_id == node.node_id)
                .values(**values)
            )
            await connection.execute(statement)

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int = 1) -> None:
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("invalid-lifecycle-revision")
        await self._runtime.records.for_scope(self.scope_id).get_source(source_ref.source_type, source_ref.source_id)
        await self._register(_Node(node_id=_nonblank(node_id), kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: Literal["all", "any"] = "all"
    ) -> None:
        await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        await self._register(
            _Node(node_id=_nonblank(node_id), kind="memory", reference=citation, parents=parents, policy=policy)
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"] = "all",
    ) -> None:
        if artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("not-a-topic-reference")
        await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
            artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
        )
        await self._register(
            _Node(node_id=_nonblank(node_id), kind="topic", reference=artifact_ref, parents=parents, policy=policy)
        )

    async def _register(self, node: _Node) -> None:
        async with self._runtime._context(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load()
            existing = nodes.get(node.node_id)
            if existing is not None:
                binding = {"node_id", "kind", "reference", "parents", "policy", "revision"}
                if existing.model_dump(include=binding) != node.model_dump(include=binding):
                    raise SourceLifecycleError("conflicting-lifecycle-binding")
                await self._sync_memory(existing)
                return
            if node.kind != "source":
                if not node.parents or len(set(node.parents)) != len(node.parents):
                    raise SourceLifecycleError("invalid-lifecycle-parents")
                if any(parent not in nodes or parent == node.node_id for parent in node.parents):
                    raise SourceLifecycleError("unknown-or-cyclic-parent")
                support = [nodes[parent].available for parent in node.parents]
                node.available = all(support) if node.policy == "all" else any(support)
            await self._save(node, new=True)
            await self._sync_memory(node)

    async def _sync_memory(self, node: _Node) -> None:
        if node.kind != "memory" or not isinstance(node.reference, MemoryCitation):
            return
        citation = node.reference
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            head = await service.head(citation.memory_ref.artifact_id)
            manifest = next(
                (entry for entry in head.content.manifest.entries if entry.entry_id == citation.entry_id), None
            )
            if manifest is None or manifest.entry_version_id != citation.entry_version_id:
                raise SourceLifecycleError("lifecycle-body-version-changed")
            if (manifest.state == "active") == node.available:
                return
            entry = next(entry for entry in await service.entries(head) if entry.entry_id == citation.entry_id)
            operation = service.reactivate if node.available else service.forget
            await operation(head, entries=(entry,), reason="source-lifecycle")

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        async with self._runtime._context(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load()
            node = nodes.get(event.node_id)
            if node is None or node.kind != "source" or node.revision != event.revision:
                raise SourceLifecycleError("unknown-source-or-revision")
            if node.last_event is not None and (
                event.sequence < node.last_event.sequence
                or (event.sequence == node.last_event.sequence and event != node.last_event)
            ):
                raise SourceLifecycleError("obsolete-or-conflicting-event")
            # Persist the accepted event before native writes. An interrupted replay
            # rechecks physical state and completes every missing transition.
            node.available, node.last_event = event.available, event
            await self._save(node)
            resolved: set[str] = {key for key, value in nodes.items() if value.kind == "source"}
            while len(resolved) < len(nodes):
                ready = [
                    value for key, value in nodes.items() if key not in resolved and set(value.parents) <= resolved
                ]
                if not ready:
                    raise SourceLifecycleError("cyclic-lifecycle-graph")
                for value in ready:
                    support = [nodes[parent].available for parent in value.parents]
                    value.available = all(support) if value.policy == "all" else any(support)
                    await self._save(value)
                    resolved.add(value.node_id)
            for value in nodes.values():
                await self._sync_memory(value)
            return sorted(key for key, value in nodes.items() if value.available)

    async def resolve(self, node_id: str, *, purpose: str = "generation") -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown-lifecycle-purpose")
        async with self._runtime._context(self.scope_id):
            node = (await self._load()).get(node_id)
            if node is None or (purpose == "generation" and not node.available):
                raise SourceLifecycleError("unavailable-lifecycle-reference")
            return node.reference
