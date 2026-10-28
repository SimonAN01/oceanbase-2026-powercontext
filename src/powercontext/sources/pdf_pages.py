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
    from pypdf import PageObject
    from pypdf.generic import ContentStream

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


def _strip_unused_text_advances(stream: ContentStream) -> bool:
    from pypdf.generic import FloatObject, NumberObject

    operations = stream.operations
    changed = False
    for index, (operands, operator) in enumerate(operations[:-1]):
        if operator == b"TJ" and operations[index + 1][1] == b"ET":
            array = operands[0]
            # ET ends the text object, so a final numeric advance has no
            # visible glyph. pypdf otherwise turns it into spaces.
            while array and isinstance(array[-1], (NumberObject, FloatObject)):
                array.pop()
                changed = True
    if changed:
        stream.operations = operations
    return changed


def _pdf_page_text(page: PageObject) -> str:
    from pypdf import PageObject
    from pypdf.generic import ContentStream, DecodedStreamObject, DictionaryObject, NameObject

    normalized: dict[int, DictionaryObject] = {}

    def normalize(owner: DictionaryObject) -> DictionaryObject:
        identity = id(owner)
        if identity in normalized:
            return normalized[identity]
        normalized[identity] = owner
        is_page = isinstance(owner, PageObject)
        contents = owner.get("/Contents") if is_page else owner
        if contents is not None:
            # Keep encoded glyph bytes: decoded PDF strings would bypass the
            # font's character map when pypdf reads this ContentStream again.
            stream = ContentStream(contents, page.pdf, "bytes")
            if _strip_unused_text_advances(stream):
                if is_page:
                    owner[NameObject("/Contents")] = stream
                else:
                    replacement = DecodedStreamObject()
                    replacement.update({
                        key: value for key, value in owner.items() if key not in ("/Length", "/Filter", "/DecodeParms")
                    })
                    replacement.set_data(stream.get_data())
                    owner = replacement
                    normalized[identity] = owner
        resources = owner.get("/Resources")
        xobjects = resources.get_object().get("/XObject") if resources is not None else None
        if xobjects is not None:
            entries = xobjects.get_object()
            for name, reference in entries.items():
                form = reference.get_object()
                if form.get("/Subtype") == "/Form":
                    entries[name] = normalize(form)
        return owner

    normalize(page)
    return page.extract_text() or ""


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
                text=_pdf_page_text(page),
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
    "roa": "net income total assets",
    "roe": "net income shareholders stockholders equity",
    "margin": "income revenue sales earnings",
    "revenue": "sales revenues",
    "sales": "revenue revenues",
    "restructuring": "restructuring impairment charges expenses",
    "geography": "geographic geographical countries regions international worldwide segments divisions",
    "geographies": "geographic geographical countries regions international worldwide segments divisions",
    "legal": "litigation contingencies proceedings settlements",
}


def _terms(text: str) -> Counter[str]:
    return Counter(_WORDS.findall(text.casefold()))


_STATEMENT_QUERIES = {
    "income": re.compile(
        r"\bincome statements?\b|\bstatements? of (?:income|operations|earnings)\b|\bp\s*&\s*l\b"
        r"|\breturn on (?:assets|equity)\b|\bro[ae]\b"
    ),
    "cash": re.compile(r"\bcash\s*flows?\b"),
    "balance": re.compile(
        r"\bbalance sheets?\b|\bstatements? of financial position\b|\breturn on (?:assets|equity)\b|\bro[ae]\b"
        r"|\b(?:current|quick) ratio\b|\bworking capital\b"
    ),
}
_STATEMENT_SEARCH_TERMS = {"income": "income earnings", "cash": "cash flows", "balance": "assets liabilities"}
_NUMERIC_QUESTION = re.compile(
    r"\b(?:calculate|compute|ratio|margin|growth|turnover|percent(?:age)?|amount|quantity|costs?|expenses?"
    r"|ebitda?|capex|fcf|eps|nwc|roic|revenue|sales|profit|income|earnings|debt|liquidity|leverage"
    r"|assets|liabilities|equity|inventory|dividends)\b|%|\b(?:how much|return on)\b"
)
_STATEMENT_PREFIX = r"^(?:(?:consolidated|combined|condensed)\s+){0,2}"
_INNER_QUALIFIER = r"(?:(?:consolidated|combined|condensed)\s+)?"
_STATEMENT_HEADINGS = {
    "income": re.compile(
        _STATEMENT_PREFIX
        + r"(?:statements?\s+of\s+"
        + _INNER_QUALIFIER
        + r"(?:income|operations|earnings)|income\s+statements?)\b"
    ),
    "cash": re.compile(
        _STATEMENT_PREFIX + r"(?:statements?\s+of\s+" + _INNER_QUALIFIER + r"cash\s*flows?|cash\s*flow\s*statements?)\b"
    ),
    "balance": re.compile(
        _STATEMENT_PREFIX + r"(?:balance\s*sheets?|statements?\s+of\s+" + _INNER_QUALIFIER + r"financial\s+position)\b"
    ),
}


def _statement_focus(question: str) -> tuple[str, ...]:
    question = question.casefold()
    explicit = tuple(kind for kind, pattern in _STATEMENT_QUERIES.items() if pattern.search(question))
    # A numeric metric may name no statement at all. Its formula inputs still
    # come from the primary statements, rather than notes repeating its acronym.
    return explicit or (tuple(_STATEMENT_QUERIES) if _NUMERIC_QUESTION.search(question) else ())


def _statement_page(text: str, focus: tuple[str, ...]) -> bool:
    if not focus:
        return False
    # PDF drawing order can put a visual page heading after the entire table.
    # Formal statement titles must remain recognizable anywhere on the page.
    headings = [line for line in text.splitlines()[1:] if line.strip()]
    for position in range(len(headings)):
        for width in (1, 2):
            heading = " ".join(" ".join(headings[position : position + width]).casefold().split())
            if len(heading.split()) > 12 or re.search(r"\.(?:\s*\.)+", heading):
                continue
            # A column labelled "Income Statement" in a later footnote is not a
            # primary title. Formal statement titles can follow longer headers.
            if position > 3 and not re.match(r"(?:consolidated|combined|condensed|statements?)\b", heading):
                continue
            for kind in focus:
                match = _STATEMENT_HEADINGS[kind].search(heading)
                if match and re.fullmatch(
                    r"(?:\s+and\s+(?:comprehensive\s+(?:income|loss)|retained\s+earnings))?"
                    r"(?:\s*\([^)]*\))*\s*(?:[-\u2013\u2014]?\s*continued)?[.:]?",
                    heading[match.end() :],
                ):
                    return True
    return False


def _rank_pages(hits: tuple[MemoryHit, ...], query: str, expansion: str, question: str) -> tuple[MemoryHit, ...]:
    """BM25 over actual retrieved page text, with lower-weight concept expansion."""
    documents = [_terms(hit.text) for hit in hits]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 1.0
    frequency: Counter[str] = Counter(term for document in documents for term in document)
    weights = dict.fromkeys(_terms(expansion), 0.35)
    weights.update(dict.fromkeys(_terms(query), 1.0))
    focus = _statement_focus(question)
    query_words = re.findall(r"[a-z0-9]+", question.casefold())
    query_identity = {
        "".join(query_words[start : start + width])
        for start in range(len(query_words))
        for width in range(1, min(4, len(query_words) - start) + 1)
    }
    filing_words = {"10k", "10q", "8k", "annual", "annualreport", "report", "earnings", "dated"}
    named_documents = tuple(
        any(
            len(word) > 1 and re.search(r"[a-z]", word) and word not in filing_words and word in query_identity
            for word in re.findall(r"[a-z0-9]+", hit.text.partition("\n")[0].casefold())
        )
        for hit in hits
    )

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

    return tuple(
        hits[index]
        for index in sorted(
            range(len(hits)),
            key=lambda index: (
                -int(named_documents[index]),
                -int(_statement_page(hits[index].text, focus)),
                -score(index),
                index,
            ),
        )
    )


def _queries(question: str) -> tuple[str, str, tuple[str, ...]]:
    # The first question carries the information need; later sentences often
    # specify answer formatting. Financial definitions still contribute concepts.
    primary = analyze_fts_query(question.partition("?")[0])
    concepts = tuple(
        dict.fromkeys(_FINANCIAL_EXPANSIONS[term] for term in _terms(question) if term in _FINANCIAL_EXPANSIONS)
    )
    focus = _statement_focus(question)
    statement_terms = tuple(_STATEMENT_SEARCH_TERMS[kind] for kind in focus)
    concept_queries: list[str] = []
    for concept in concepts:
        if focus:
            concept_queries.append(concept)
        else:
            # A narrative evidence page may use just one of the topic synonyms.
            # Keep these queries short enough for Memory's lexical admission.
            words = concept.split()
            concept_queries.extend(" ".join(words[index : index + 2]) for index in range(0, len(words), 2))
    phrases = re.findall(
        r"\b(?:operating income|net income|cash flows?|balance sheets?|income statements?|capital expenditures"
        r"|(?:current|total) (?:assets|liabilities|equity)|(?:shareholders?|stockholders?)[\s\u2019']+equity"
        r"|cost of (?:goods sold|sales|revenues?))\b",
        question.casefold(),
    )
    # Separate short concept queries prevent a long instruction from imposing a
    # lexical coverage floor that suppresses a table containing only one input.
    queries = tuple(dict.fromkeys(value for value in (primary, *concept_queries, *phrases, *statement_terms) if value))
    return primary, " ".join((*concepts, *phrases, *statement_terms)), queries


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Retrieve real immutable entry citations; no document paths or labels are read."""
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

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
    citations: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    for hit in _rank_pages(tuple(candidates.values()), query, expansion, question):
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        # Different Memory entries can cite the same page. An entry combining
        # several pages cannot serve as an exact, single-page citation either.
        if len(entry.sources) != 1 or entry.sources[0].source_type != "pdf-page":
            continue
        page_id = entry.sources[0].source_id
        if page_id in seen_pages:
            continue
        seen_pages.add(page_id)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)
