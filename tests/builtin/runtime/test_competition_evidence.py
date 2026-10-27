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

"""Regression checks for literal evidence identity and whole-envelope budgets."""

import json

import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates


def _group(scope, text):
    reference = ArtifactRef(family="memory", artifact_id="shared-memory-id", revision=2)
    return PreparedMemoryCandidates(
        scope_id=scope,
        memory_ref=reference,
        hits=(
            MemoryHit(
                memory_ref=reference,
                entry_id="same-entry",
                entry_version_id="version-1",
                text=text,
                score=1.0,
                matched_by=("fts",),
            ),
        ),
    )


@pytest.mark.parametrize("budget", [512, 800, 1200, 1800])
def test_selected_evidence_charges_json_escaping_and_unicode_citations(budget):
    original = (
        "Background administrative notes. " * 80 + 'The cobalt sensor records "海洋温度" at 37 degrees.\n'
        "Cobalt calibration remains stable at 37 degrees."
    )
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibration 37", max_bytes=budget, evidence_selection=True),
        current_scope_id="scope-海洋",
        memory_candidates=(_group("scope-海洋", original),),
    )
    assert build.context.content_bytes == len((build.context.content or "").encode("utf-8")) <= budget
    if build.context.content:
        items = json.loads(build.context.content.splitlines()[-2])["items"]
        assert len(build.fragments) == len(build.origins) == len(items)
        for item, fragment in zip(items, build.fragments, strict=True):
            assert item["content"] == fragment.text == original[fragment.start : fragment.end]
            assert item["citation"]["scope_id"] == "scope-海洋"
            assert fragment.start > 80
    if budget == 1800:
        assert build.context.content is not None
        assert "Cobalt calibration remains stable" in build.context.content
        assert "海洋温度" in build.context.content
        assert len(build.fragments) == 1


def test_equal_ids_and_text_in_two_scopes_keep_independent_addresses():
    text = "Cobalt sensor calibration is stable at 37 degrees."
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt calibration", max_bytes=2400, evidence_selection=True),
        current_scope_id="left",
        memory_candidates=(_group("left", text), _group("right", text)),
    )
    assert {fragment.address.memory.scope_id for fragment in build.fragments} == {"left", "right"}
    assert tuple(fragment.address for fragment in build.fragments) == build.origins
