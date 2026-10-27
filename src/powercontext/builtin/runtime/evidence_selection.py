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

"""Offline sentence evidence selection with exact citations and byte accounting."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedEvidenceFragment,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")  # noqa: RUF001 - multilingual sentence delimiters


def _stem(term: str) -> str:
    if len(term) <= 4:
        return term
    if term.endswith("ies"):
        term = term[:-3] + "y"
    elif term.endswith("s") and not term.endswith("ss"):
        term = term[:-1]
    for suffix in ("ing", "ed", "ion", "ory", "ive", "ly"):
        if len(term) - len(suffix) > 4 and term.endswith(suffix):
            term = term[: -len(suffix)]
            break
    return term.removesuffix("e") if len(term) > 5 else term


def _terms(text: str) -> Counter[str]:
    # Normalize common inflections without adding a model or rewriting evidence.
    return Counter(_stem(term) for term in analyze_text(text).split())


def _sentences(text: str) -> tuple[tuple[int, int], ...]:
    start = 0
    spans = []
    for match in _BOUNDARY.finditer(text):
        if text[start : match.start()].strip():
            spans.append((start, match.start()))
        start = match.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return tuple(spans)


@dataclass(frozen=True)
class _ScoredSpan:
    fragment: PreparedEvidenceFragment
    score: float
    source_text: str


def _candidates(groups: Sequence[PreparedMemoryCandidates], query: str) -> tuple[_ScoredSpan, ...]:
    entries = []
    seen: set[tuple[str, str, str, str]] = set()
    for group in groups:
        for hit in group.hits:
            if group.memory_ref is None or group.memory_ref != hit.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            key = (group.scope_id, hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id)
            if key not in seen:
                seen.add(key)
                entries.append((group, hit))
    queries = _terms(analyze_fts_query(query))
    frequency = Counter(term for _group, hit in entries for term in _terms(hit.text))
    weights = {term: math.log1p((len(entries) + 1) / (frequency[term] + 0.5)) for term in queries}
    result = []
    for group, hit in entries:
        address = MemoryEntryAddress(
            memory=ArtifactAddress(scope_id=group.scope_id, artifact=hit.memory_ref),
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        for start, end in _sentences(hit.text):
            terms = _terms(hit.text[start:end])
            matched = sum(weight for term, weight in weights.items() if term in terms)
            if not matched:
                continue
            # Saturation avoids preferring repeated boilerplate over a concise sentence.
            value = sum(weight * min(terms[term], 2) for term, weight in weights.items())
            value *= (0.5 + matched / max(sum(weights.values()), 1)) / (0.65 + 0.35 * len(terms) / 35)
            result.append(
                _ScoredSpan(PreparedEvidenceFragment(address, start, end, hit.text[start:end]), value, hit.text)
            )
    return tuple(sorted(result, key=lambda candidate: -candidate.score))


def _render(fragments: Sequence[PreparedEvidenceFragment]) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    body = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return f"{TRUST_POLICY}\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{body}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"


def _merge(selected: Sequence[PreparedEvidenceFragment], candidate: _ScoredSpan) -> list[PreparedEvidenceFragment]:
    """Adjacent sentences share citation overhead while retaining original spacing."""
    fragment = candidate.fragment
    result = []
    position = len(selected)
    for previous in selected:
        left, right = sorted((previous, fragment), key=lambda value: value.start)
        if previous.address == fragment.address and (
            left.end >= right.start or not candidate.source_text[left.end : right.start].strip()
        ):
            position = min(position, len(result))
            start, end = min(previous.start, fragment.start), max(previous.end, fragment.end)
            fragment = PreparedEvidenceFragment(fragment.address, start, end, candidate.source_text[start:end])
        else:
            result.append(previous)
    result.insert(position, fragment)
    return result


def select_evidence(
    request: PrepareContextRequest, groups: Sequence[PreparedMemoryCandidates], entry_limit: int
) -> PreparedContextBuild:
    """Greedily retain relevant complete sentences; charge all rendered overhead."""
    selected: list[PreparedEvidenceFragment] = []
    for candidate in _candidates(groups, request.query):
        trial = _merge(selected, candidate)
        if len(trial) <= entry_limit and len(_render(trial).encode("utf-8")) <= request.max_bytes:
            selected = trial
    if not selected:
        return PreparedContextBuild(context=PreparedContext(status="empty", content=None, content_bytes=0), origins=())
    content = _render(selected)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in selected),
        fragments=tuple(selected),
    )
