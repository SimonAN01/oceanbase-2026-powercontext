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

"""Check durable CSV evidence and exact Memory citations across process exit."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.artifacts.memory.protocols import MemoryCandidateRequest
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, parse_csv_document
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

_DOCUMENT = CSVDocumentInput(
    document_id="shipment:账单/2026",
    primary_key=" id ",
    content=(
        '\ufeff" id ",city,amount,active,missing,note\r\n'
        ' 001 ,北京,0007,false,,"shipment first\r\nsecond ""quoted"" line"\r\n'
        '002,東京,1.00,true,,"shipment, café"\r\n'
    ).encode(),
)


class _RowPipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            entries.append(
                MemoryEntryInput(
                    kind="csv-row",
                    text=TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content,
                    sources=(source,),
                )
            )
        return tuple(entries)


async def _run_process(phase: str, database: Path) -> int:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    state_file = database.with_suffix(".citations.json")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    observations = []
    for row in parse_csv_document(_DOCUMENT):
        observations.append(project_source_for_transport(registry, await registry.resolve(row)))
    expected = {source.name: source.projection(TEXT_EVIDENCE_PROJECTION_KEY) for source in observations}

    if phase == "write":
        async with open_builtin_runtime(config, candidate_pipeline=_RowPipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="CSV process restart", summary="CSV process restart", idempotency_key="csv-restart")
            )
            await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            flushed = await memory.flush()
            assert flushed.processed and flushed.source_count == len(expected)
            search = await memory.search(SearchMemoryRequest(query="shipment", mode="fts", limit=10))
            assert len(search.hits) == len(expected)
            citations = [
                MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                ).model_dump(mode="json")
                for hit in search.hits
            ]
            state_file.write_text(json.dumps({"scope_id": scope.scope_id, "citations": citations}), encoding="utf-8")
    else:
        assert phase == "read"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        async with open_builtin_contexts(config) as contexts:
            entries = await (await contexts.get(state["scope_id"])).sources.journal.entries()
            persisted = {}
            for entry in entries:
                assert isinstance(entry.source, SourceObservation)
                persisted[entry.source.name] = entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY)
            assert persisted == expected
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(state["scope_id"])
            restored_ids = set()
            for serialized in state["citations"]:
                citation = MemoryCitation.model_validate(serialized)
                result = await memory.get(GetMemoryEntryRequest(citation=citation))
                assert len(result.entry.sources) == 1
                source_ref = result.entry.sources[0]
                assert source_ref.source_type == "csv-row"
                evidence = TextEvidence.model_validate(expected[source_ref.source_id])
                assert result.entry.text == evidence.content
                restored_ids.add(citation.entry_id)
            assert len(restored_ids) == len(expected)
            search = await memory.search(SearchMemoryRequest(query="shipment", mode="fts", limit=10))
            assert {hit.entry_id for hit in search.hits} == restored_ids
    return len(expected)


def test_csv_evidence_and_exact_citations_survive_a_new_python_process(tmp_path: Path) -> None:
    database = tmp_path / "csv-process-restart.db"
    for phase in ("write", "read"):
        result = subprocess.run(
            [sys.executable, str(Path(__file__)), phase, str(database)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {"phase": phase, "rows": 2}


if __name__ == "__main__":
    phase, database = sys.argv[1:]
    print(json.dumps({"phase": phase, "rows": asyncio.run(_run_process(phase, Path(database)))}))
