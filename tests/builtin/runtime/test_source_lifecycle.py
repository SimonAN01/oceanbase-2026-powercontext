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

"""Interrupted native state propagation survives reopening and event replay."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.memory import RelationalMemoryBackend
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


def test_interrupted_withdrawal_replays_without_rewriting_body_or_completed_states(tmp_path, monkeypatch):
    asyncio.run(_interrupted_withdrawal(tmp_path, monkeypatch))


async def _interrupted_withdrawal(tmp_path, monkeypatch):
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'graph.db'}"))
    withdrawal = SourceLifecycleEvent(node_id="origin", revision=3, sequence=1, available=False, reason="withdrawn")
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Recovery", summary="Evidence recovery", idempotency_key="recovery")
        )
        service = runtime.lifecycle.for_scope(scope.scope_id)
        receipt = await runtime.sources.for_scope(scope.scope_id).capture(
            CaptureSource(source_id="origin", content="Evidence", metadata={})
        )
        await service.register_source("origin", receipt.source_ref, revision=3)
        memory = runtime.memory.for_scope(scope.scope_id)
        citations = []
        for number in range(2):
            result = await memory.remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"Cobalt fact {number}"),))
            )
            assert result.entry is not None
            citations.append(result.entry.citation)
            await service.register_memory(f"fact{number}", result.entry.citation, parents=("origin",), policy="all")
        commit = RelationalMemoryBackend._commit
        calls = 0

        async def interrupt(backend, connection, value, /):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("controlled-storage-failure")
            return await commit(backend, connection, value)

        with monkeypatch.context() as patch:
            patch.setattr(RelationalMemoryBackend, "_commit", interrupt)
            with pytest.raises(OSError, match="controlled-storage-failure"):
                await service.apply(withdrawal)
        assert len((await memory.list()).entries) == 1
    async with open_builtin_runtime(config) as runtime:
        service = runtime.lifecycle.for_scope(scope.scope_id)
        memory = runtime.memory.for_scope(scope.scope_id)
        assert await service.apply(withdrawal) == []
        assert not (await memory.list()).entries
        completed = (await memory.list(include_inactive=True)).memory_ref
        assert await service.apply(withdrawal) == []
        assert (await memory.list(include_inactive=True)).memory_ref == completed
        for number, citation in enumerate(citations):
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == f"Cobalt fact {number}"
            assert await service.resolve(f"fact{number}", purpose="audit") == citation
            with pytest.raises(SourceLifecycleError):
                await service.resolve(f"fact{number}", purpose="generation")
        assert await service.apply(withdrawal.model_copy(update={"sequence": 2, "available": True})) == [
            "fact0",
            "fact1",
            "origin",
        ]
        assert {entry.entry.entry_version_id for entry in (await memory.list()).entries} == {
            citation.entry_version_id for citation in citations
        }
