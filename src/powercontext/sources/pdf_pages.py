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

"""Complete physical PDF pages and offline retrieval through scoped Memory."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, Field, model_validator

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.search import analyze_fts_query
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryHit
    from powercontext.builtin.runtime.application import ScopedMemoryApplication


class PDFDocumentInput(BaseModel):
    """The complete original file, independent of paths and network services."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1, strict=True)
    text: str

    @model_validator(mode="after")
    def validate_document_id(self) -> Self:
        _validate_document_id(self.document_id)
        return self


def _validate_document_id(value: str) -> None:
    if not value.strip():
        raise ValueError("blank-document-id")


def pdf_page_source_id(value: PDFPageInput) -> str:
    """Hash the unambiguous complete identity, including the physical page."""
    encoded = json.dumps(
        [value.document_id, value.document_sha256, value.page_number], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return f"pdf:{hashlib.sha256(encoded).hexdigest()}"


class PDFPageSource(Source, PDFPageInput):
    source_id: str
    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.name != pdf_page_source_id(self) or self.source_id != self.name:
            raise ValueError("invalid-pdf-page-identity")
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every page before publishing any results, including empty pages."""
    # pypdf is an optional source dependency; other Source types do not require it.
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    _validate_document_id(value.document_id)
    if not value.content or b"%PDF-" not in value.content[:1024]:
        raise ValueError("invalid-pdf-document")
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("password-required")  # noqa: TRY301 - normalize all unreadable files below
        pages = tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=index,
                text=page.extract_text() or "",
            )
            for index, page in enumerate(reader.pages, start=1)
        )
    except (PyPdfError, OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        raise ValueError("unreadable-pdf-document") from error
    if not pages:
        raise ValueError("pdf-has-no-pages")
    return pages


class _PDFPageAdapter:
    input_class = PDFPageInput
    name = "pdf-page"
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value)
        return PDFPageSource(name=identity, source_id=identity, **value.model_dump())

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput.model_validate(source.model_dump())


class _PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageAdapter(), projections=(_PDFPageProjection(),)
)

_WORDS = re.compile(r"[a-z]+|\d+")
_FINANCIAL_EXPANSIONS = {
    "ebitda": "operating income depreciation amortization earnings interest taxes",
    "capex": "capital expenditures property plant equipment payments additions",
    "margin": "income revenue sales earnings",
    "revenue": "sales revenues",
    "sales": "revenue revenues",
    "restructuring": "restructuring impairment charges expenses",
    "geography": "geographic geographical countries regions international",
    "geographies": "geographic geographical countries regions international",
    "legal": "litigation contingencies proceedings settlements",
}


def _terms(text: str) -> Counter[str]:
    return Counter(_WORDS.findall(text.casefold()))


def _rank_pages(hits: tuple[MemoryHit, ...], query: str, expansion: str) -> tuple[MemoryHit, ...]:
    """BM25 over actual retrieved page text, with lower-weight concept expansion."""
    documents = [_terms(hit.text) for hit in hits]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 1.0
    frequency: Counter[str] = Counter(term for document in documents for term in document)
    weights = dict.fromkeys(_terms(expansion), 0.35)
    weights.update(dict.fromkeys(_terms(query), 1.0))

    def score(index: int) -> float:
        document = documents[index]
        normalization = 1.2 * (0.25 + 0.75 * lengths[index] / max(average, 1))
        return sum(
            weight
            * math.log1p((len(hits) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            * document[term]
            * 2.2
            / (document[term] + normalization)
            for term, weight in weights.items()
            if document[term]
        )

    return tuple(hits[index] for index in sorted(range(len(hits)), key=lambda index: (-score(index), index)))


def _queries(question: str) -> tuple[str, str, tuple[str, ...]]:
    # The first question carries the information need; later sentences often
    # specify answer formatting. Financial definitions still contribute concepts.
    primary = analyze_fts_query(question.partition("?")[0])
    concepts = tuple(
        dict.fromkeys(_FINANCIAL_EXPANSIONS[term] for term in _terms(question) if term in _FINANCIAL_EXPANSIONS)
    )
    phrases = re.findall(
        r"\b(?:operating income|net income|cash flows?|balance sheets?|income statements?|capital expenditures)\b",
        question.casefold(),
    )
    # Separate short concept queries prevent a long instruction from imposing a
    # lexical coverage floor that suppresses a table containing only one input.
    queries = tuple(dict.fromkeys(value for value in (primary, *concepts, *phrases) if value))
    return primary, " ".join(concepts), queries


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Retrieve real immutable entry citations; no document paths or labels are read."""
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if type(limit) is not int or limit < 1 or not question.strip():
        raise ValueError("invalid-pdf-search")
    query, expansion, queries = _queries(question)
    candidates: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    for text in queries:
        page = await memory.search(SearchMemoryRequest(query=text, limit=max(200, limit * 10), mode="fts"))
        for hit in page.hits:
            key = (
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            candidates.setdefault(key, hit)
    return tuple(
        MemoryCitation(memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id)
        for hit in _rank_pages(tuple(candidates.values()), query, expansion)[:limit]
    )
