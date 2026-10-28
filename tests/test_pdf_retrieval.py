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

"""Natural financial questions retrieve their separate literal table inputs."""

import asyncio
import io

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class _PagePipeline:
    async def extract(self, request, /):
        # Distinct Memory entries can legitimately preserve the same Source.
        return tuple(
            MemoryEntryInput(
                kind=f"literal-page-{copy}",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
            for copy in range(2)
        )


async def _store_pages(runtime, scope_id, texts):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
    for number, text in enumerate(texts, start=1):
        document_id, _, body = text.partition("\n")
        source = await registry.resolve(
            PDFPageInput(document_id=document_id, document_sha256="a" * 64, page_number=number, text=body)
        )
        await runtime.ingestion.submit(
            SubmitSourceObservation(scope_id=scope_id, observation=project_source_for_transport(registry, source))
        )
    memory = runtime.memory.for_scope(scope_id)
    for _ in range(len(texts) + 1):
        if not (await memory.flush()).processed:
            break
    else:
        pytest.fail("source flush did not drain")


def test_unused_trailing_text_advance_does_not_split_words_or_drop_rotated_text():
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Courier"),
            })
        })
    })
    # Each text object ends after its final advance. The next absolute position
    # places the next letter directly beside it, with no visual word space.
    commands = [
        f"BT /F1 12 Tf 1 0 0 1 {36 + index * 7.2:.2f} 700 Tm [({character}) -1000] TJ ET"
        for index, character in enumerate("December 31, 2022")
    ]
    commands.append("BT /F1 12 Tf 0 1 -1 0 400 400 Tm (Revenue 900) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="constructed-advances", content=output.getvalue()))
    assert len(pages) == 1
    assert "December 31, 2022" in pages[0].text
    assert "Revenue 900" in pages[0].text


def test_answer_style_instructions_do_not_hide_separate_financial_statements():
    async def scenario():
        statements = (
            "ACME_2026\nConsolidated income statement. Operating income 300. Net income 200. Revenue 900.",
            "ACME_2026\nConsolidated statement of cash flows. Depreciation and amortization 100. Capital expenditures 40.",
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Financial search", summary="Statement retrieval", idempotency_key="financial")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(
                runtime,
                scope.scope_id,
                (
                    *statements,
                    "ACME_2026\nRespond as an investment analyst. Give a response using the details shown in this question.",
                ),
            )
            citations = await search_pdf_pages(
                memory,
                "What is the FY2026 unadjusted EBITDA less capex for ACME? Define EBITDA as operating income plus "
                "depreciation and amortization. Answer in USD millions. Respond by assuming the perspective of an "
                "investment analyst who can only use the details shown within the cash flow and income statement.",
                limit=2,
            )
            returned = {
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text for citation in citations
            }
            assert returned == set(statements)

    asyncio.run(scenario())


@pytest.mark.parametrize("statement_name", ["income statements", "P&L", "statements of operations"])
@pytest.mark.parametrize("heading", ["Consolidated Statement of Income", "Statements of\nConsolidated Income"])
def test_an_explicit_statement_remains_retrievable_when_a_named_cost_is_absent(statement_name, heading):
    async def scenario():
        statement = f"ACME_2026\n{heading}\nNet sales 900. Operating income 300. Net income 200."
        notes = tuple(
            f"ACME_2026\nNote {number}\nRestructuring costs directly outlined in income statements for 2026. "
            "Restructuring costs and income are discussed in these detailed notes.\n"
            "Hedging instruments\nLosses and gains\nRecognized in\nIncome Statement\nOther columns"
            for number in range(30)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Statement focus", summary="Primary statement retrieval", idempotency_key="statement-focus"
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (statement, *notes))
            citations = await search_pdf_pages(
                memory,
                f"What restructuring costs are directly outlined in ACME's {statement_name} for FY2026?",
                limit=1,
            )
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == statement

    asyncio.run(scenario())


def test_numeric_abbreviation_retrieves_formula_inputs_without_an_explicit_statement_name():
    async def scenario():
        cash_pages = (
            "ACME_2026\nConsolidated Statements of Cash Flows\nNet cash from operating activities 300.",
            "ACME_2026\nConsolidated Statements of Cash Flows (continued)\nPurchases of property and equipment 40.",
        )
        other_statements = (
            "ACME_2026\nConsolidated Balance Sheets\nAssets 1000. Liabilities 400.",
            "ACME_2026\nConsolidated Statements of Income\nNet income 200. Sales 900.",
        )
        notes = tuple(f"ACME_2026\nNote {number}\nFCF for ACME FY2026." for number in range(30))
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Numeric query", summary="Raw metric abbreviation", idempotency_key="numeric")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*cash_pages, *other_statements, *notes))
            citations = await search_pdf_pages(memory, "What is the FY2026 FCF for ACME?", limit=20)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            assert set(cash_pages).issubset({entry.text for entry in entries})
            assert len({entry.sources[0].source_id for entry in entries}) == len(citations) == 20

    asyncio.run(scenario())


def test_geographic_question_can_retrieve_a_reportable_divisions_page():
    async def scenario():
        divisions = (
            "ACME_2026\nOperating Divisions\nWe are organized into three reportable segments. "
            "North serves Canada. East serves Japan. West serves France."
        )
        notes = tuple(f"ACME_2026\nNote {number}\nACME FY2026 accounting policies." for number in range(30))
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Geographies", summary="Literal reporting segments", idempotency_key="geographies")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (divisions, *notes))
            citations = await search_pdf_pages(
                memory, "What are the geographies that ACME primarily operates in as of FY2026?", limit=20
            )
            returned = {
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text for citation in citations
            }
            assert divisions in returned

    asyncio.run(scenario())


def test_statement_title_drawn_after_table_is_still_recognized():
    async def scenario():
        rows = "\n".join(f"Revenue category {number} Amount 100" for number in range(18))
        statement = (
            f"ACME_2026\nNet sales 900. Operating income 300. Net income 200.\n{rows}\nConsolidated Statement of Income"
        )
        notes = tuple(
            f"ACME_2026\nNote {number}\nRestructuring costs directly outlined in income statements FY2026."
            for number in range(30)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Drawing order", summary="Title after table", idempotency_key="drawing-order")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (statement, *notes))
            citations = await search_pdf_pages(
                memory,
                "What restructuring costs are directly outlined in ACME's income statements for FY2026?",
                limit=1,
            )
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == statement

    asyncio.run(scenario())


def test_a_note_caption_about_statement_locations_is_not_a_primary_statement_title():
    async def scenario():
        statement = "ACME_2026\nConsolidated Statement of Income\nNet sales 900. Operating income 300."
        notes = tuple(
            f"ACME_2026\nNote {number}\nConsolidated statement of income location and impact of derivative instruments\n"
            "Restructuring costs directly outlined in income statements FY2026."
            for number in range(30)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Table captions", summary="Full formal titles", idempotency_key="table-captions")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (statement, *notes))
            citations = await search_pdf_pages(
                memory,
                "What restructuring costs are directly outlined in ACME's income statements for FY2026?",
                limit=1,
            )
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == statement

    asyncio.run(scenario())


def test_ratio_definitions_retrieve_distinct_pages_among_commentary_and_non_pdf_entries():
    async def scenario():
        statements = (
            "ACME_2026\nConsolidated Statements of Income\nNet income 200. Revenue 900.",
            "ACME_2026\nConsolidated Balance Sheets\nTotal assets 1000. Stockholders' equity 500.",
        )
        notes = tuple(
            f"ACME_2026\nNote {number}\nROA and ROE comparison for FY2026. Return on assets and equity change."
            for number in range(30)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Ratios", summary="Separate formula inputs", idempotency_key="ratios")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*statements, *notes))
            await memory.remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="commentary", text="ROA ROE FY2026 ACME comparison"),)
                )
            )
            citations = await search_pdf_pages(
                memory,
                "How do ACME's ROA and ROE compare for FY2026? Use net income, total assets, and stockholders' equity.",
                limit=2,
            )
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            assert {entry.text for entry in entries} == set(statements)
            assert len({entry.sources[0].source_id for entry in entries}) == 2

    asyncio.run(scenario())
