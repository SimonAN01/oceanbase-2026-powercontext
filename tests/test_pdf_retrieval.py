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
from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCandidateError
from powercontext.builtin.persistence.sqlite import SQLiteConfig
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


def _tagged_glyph_pdf(*, mapping="0000", nested=False, conflicting=False):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    cmap = DecodedStreamObject()
    cmap.set_data(
        (
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            "6 beginbfchar <0001> <0053> <0002> <0065> <0003> <0063> "
            f"<0004> <{mapping}> <0005> <006F> <0006> <006E> endbfchar\nendcmap end end"
        ).encode()
    )
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/Synthetic"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/ToUnicode"): cmap,
        NameObject("/DescendantFonts"): ArrayObject([
            DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/CIDFontType2"),
                NameObject("/BaseFont"): NameObject("/Synthetic"),
                NameObject("/DW"): NumberObject(500),
            })
        ]),
    })
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    commands = (
        b"BT /F1 12 Tf 1 0 0 1 36 700 Tm <000100020003> Tj "
        b"/Span << /ActualText <FEFF00740069> >> BDC <0004> Tj EMC <00050006> Tj "
    )
    if conflicting:
        commands += b"/Span << /ActualText <FEFF00660074> >> BDC <0004> Tj EMC "
    commands += b"ET"
    stream = DecodedStreamObject()
    stream.set_data(commands)
    if nested:
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 612, 792)]),
            NameObject("/Resources"): resources,
        })
        resources = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): stream})})
        stream = DecodedStreamObject()
        stream.set_data(b"q /Fm Do Q")
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("nested", [False, True])
def test_actual_text_recovers_null_mapped_ligature(nested):
    content = _tagged_glyph_pdf(nested=nested)
    pages = parse_pdf_document(PDFDocumentInput(document_id="constructed-actual-text", content=content))
    assert "Section" in pages[0].text
    assert "\0" not in pages[0].text


def test_actual_text_does_not_overwrite_valid_or_ambiguous_font_mapping():
    valid = parse_pdf_document(PDFDocumentInput(document_id="valid-map", content=_tagged_glyph_pdf(mapping="0051")))
    assert "SecQon" in valid[0].text
    ambiguous = parse_pdf_document(
        PDFDocumentInput(document_id="ambiguous-map", content=_tagged_glyph_pdf(conflicting=True))
    )
    assert ambiguous[0].text.count("\0") == 2


def test_latin_presentation_ligature_expands_without_altering_other_unicode():
    pages = parse_pdf_document(
        PDFDocumentInput(document_id="presentation-ligature", content=_tagged_glyph_pdf(mapping="FB01"))
    )
    assert "Secfion" in pages[0].text


@pytest.mark.parametrize("rotation", ["1 0 0 1", "0 1 -1 0"])
def test_new_text_object_separates_columns_before_showing_kerned_words(rotation):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
    })
    stream = DecodedStreamObject()
    stream.set_data(
        (
            f"q {rotation} 36 400 cm BT /F1 12 Tf (Column) Tj ET Q "
            f"q {rotation} 240 600 cm BT /F1 12 Tf [(Financi) -10 (al data)] TJ ET Q "
            "q 1 0 0 1 36 300 cm BT /F1 12 Tf [(Net) -600 (income)] TJ ET Q"
        ).encode()
    )
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="constructed-text-objects", content=output.getvalue()))
    assert "Financial data" in pages[0].text
    assert "Net income" in pages[0].text
    assert "Column" in pages[0].text


@pytest.mark.parametrize(
    ("matrix", "nested"), [("1 0 0 1", False), ("0 1 -1 0", False), ("-1 0 0 -1", True), ("0 -1 1 0", True)]
)
def test_repainting_a_positioned_word_does_not_merge_its_last_and_first_letters(matrix, nested):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
    })
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font, NameObject("/F2"): font})
    })
    a, b, _, _ = map(float, matrix.split())
    commands = ["BT"]
    for _ in range(2):
        for position, letter in enumerate("ALPHA"):
            commands.append(
                f"/F{position % 2 + 1} 12 Tf {matrix} {180 + position * 7.2 * a} {400 + position * 7.2 * b} Tm ({letter}) Tj"
            )
    commands.append("ET")
    stream = DecodedStreamObject()
    stream.set_data(" ".join(commands).encode())
    if nested:
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(n) for n in (0, 0, 612, 792)]),
            NameObject("/Resources"): resources,
        })
        resources = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): stream})})
        stream = DecodedStreamObject()
        stream.set_data(b"q /Fm Do Q")
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="constructed-repainting", content=output.getvalue()))
    assert pages[0].text.split() == ["ALPHA", "ALPHA"]


@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize("rotation", ["1 0 0 1", "0 1 -1 0"])
@pytest.mark.parametrize("matrix_translation", [False, True])
def test_positioned_chunks_preserve_complete_words(depth, rotation, matrix_translation):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Courier"),
            })
        })
    })
    if matrix_translation:
        commands = (
            "BT /F1 12 Tf 1 0 0 1 40 700 Tm (Execu) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 0 0 Tm 76 700 Td (tive) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 0 0 Tm 112 700 Td (officer) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 40 650 Tm (201) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 0 0 Tm 61.6 650 Td (5) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 0 0 Tm 40 600 Td (Net income) Tj ET"
        )
    else:
        commands = (
            # Courier glyphs advance 600 units. Include the TJ adjustments in
            # the relative position, leaving no word space before 'tive'.
            "BT /F1 12 Tf 1 0 0 1 36 700 Tm "
            "[(E) -80 (x) -80 (e) -80 (c) -80 (u) -80] TJ "
            "40.8 0 Td (tive) Tj 36 0 Td (officer) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 36 650 Tm "
            "[(2) -120 (0) -120 (1) -120] TJ 25.92 0 Td (5) Tj ET "
            "BT /F1 12 Tf 1 0 0 1 36 600 Tm [(Net) -600 (income)] TJ ET"
        )
    stream = DecodedStreamObject()
    stream.set_data((f"q {rotation} 0 0 cm " + commands + " Q").encode())
    for _ in range(depth):
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(n) for n in (0, 0, 612, 792)]),
            NameObject("/Resources"): resources,
        })
        resources = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): stream})})
        stream = DecodedStreamObject()
        stream.set_data(b"q /Fm Do Q")
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = stream
    content = io.BytesIO()
    writer.write(content)
    parsed = parse_pdf_document(PDFDocumentInput(document_id="constructed-cursor-width", content=content.getvalue()))
    assert "Executive officer" in parsed[0].text
    assert "2015" in parsed[0].text
    assert "Net income" in parsed[0].text


@pytest.mark.parametrize("inside_nested_forms", [False, True])
def test_unused_trailing_text_advance_does_not_split_words_or_drop_rotated_text(inside_nested_forms):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Courier"),
                NameObject("/Encoding"): DictionaryObject({
                    NameObject("/BaseEncoding"): NameObject("/WinAnsiEncoding"),
                    NameObject("/Differences"): ArrayObject([NumberObject(65), NameObject("/D")]),
                }),
            })
        })
    })
    # Each text object ends after its final advance. The next absolute position
    # places the next letter directly beside it, with no visual word space.
    commands = [
        f"BT /F1 12 Tf 1 0 0 1 {36 + index * 7.2:.2f} 700 Tm [({character}) -1000] TJ ET"
        # Character code A maps to glyph D; normalization must retain the
        # encoded bytes so that the font still yields the word December.
        for index, character in enumerate("Aecember 31, 2022")
    ]
    commands.append("BT /F1 12 Tf 1 0 0 1 36 650 Tm [(Net) -600 (income 300) -1000] TJ ET")
    commands.append("BT /F1 12 Tf 1 0 0 1 36 620 Tm [(Recei) 350 (vables) -600 (balance)] TJ ET")
    commands.append("BT /F1 12 Tf 0 1 -1 0 400 400 Tm (Revenue 900) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    if inside_nested_forms:
        # Forms may contain other Forms; both upright and rotated text still
        # belong to this physical page, including unused text advances.
        for _ in range(2):
            stream.update({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject([NumberObject(value) for value in (0, 0, 612, 792)]),
                NameObject("/Resources"): page["/Resources"],
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): stream})
            })
            stream = DecodedStreamObject()
            stream.set_data(b"q /Fm Do Q")
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="constructed-advances", content=output.getvalue()))
    assert len(pages) == 1
    assert "December 31, 2022" in pages[0].text
    assert "Net income 300" in pages[0].text
    assert "Receivables balance" in pages[0].text
    assert "Revenue 900" in pages[0].text


def test_long_captured_page_remains_complete_after_persistence_and_restart(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'long-page.db'}"))
        text = "ACME_2026\n" + "Annual report financial performance and cash flow 利润 2026. " * 200
        assert len(text.encode("utf-8")) > 8192
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Long captured page", summary="Full page evidence", idempotency_key="long-page")
            )
            await _store_pages(runtime, scope.scope_id, (text,))
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "ACME financial performance 2026", limit=1)
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == text.strip()
            # An unbacked ordinary Memory entry retains its existing quota.
            with pytest.raises(InvalidMemoryCandidateError, match="8192 UTF-8 bytes"):
                await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="note", text=text),)))
        async with open_builtin_runtime(config) as runtime:
            restored = await runtime.memory.for_scope(scope.scope_id).get(GetMemoryEntryRequest(citation=citations[0]))
            assert restored.entry.text == text.strip()
            assert len(restored.entry.sources) == 1
            assert restored.entry.sources[0].source_type == "pdf-page"

    asyncio.run(scenario())


def test_many_financial_notes_cannot_hide_the_named_documents_primary_statement():
    async def scenario():
        statement = "ACME_2026\nConsolidated Balance Sheets\nAssets 100. Liabilities 40."
        notes = tuple(
            f"ACME_2026\nNote {number}\n"
            + "ACME FY2026 current ratio balance sheet assets liabilities financial discussion. " * 12
            for number in range(220)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Long filing", summary="Candidate coverage", idempotency_key="long-filing")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*notes, statement))
            citations = await search_pdf_pages(memory, "What is ACME's FY2026 current ratio?", limit=1)
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == statement

    asyncio.run(scenario())


@pytest.mark.parametrize("company", ["ACME", "3M", "A&T"])
@pytest.mark.parametrize(
    ("metric", "heading"),
    [("current ratio", "Consolidated Balance Sheets"), ("Cash Flow", "Consolidated Statements of Cash Flows")],
)
def test_opaque_document_id_keeps_the_named_companys_statement(company, metric, heading):
    async def scenario():
        target = f"d93ac471c2b6408a\n{heading}\nAssets 100. Liabilities 40. Operating cash flows 60."
        company_pages = (
            f"d93ac471c2b6408a\n{company} annual report. Our business and operations.",
            f"d93ac471c2b6408a\n{company} management report. Corporate governance and directors.",
        )
        other_pages = tuple(
            f"b74ce8f021a69d30\n{heading}\nDivision {number}. "
            + (f"An investment in {company}. " if number == 0 else "")
            + f"FY2026 {metric} balance sheet assets liabilities. " * 3
            for number in range(25)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Opaque filing", summary="Company from page text", idempotency_key="opaque")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*other_pages, *company_pages, target))
            citations = await search_pdf_pages(memory, f"What is {company}'s FY2026 {metric}?", limit=1)
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == target

    asyncio.run(scenario())


def test_many_primary_statements_cannot_hide_a_querys_narrative_evidence():
    async def scenario():
        target = "ACME_2026\nRevenue by sales channel\nDigital channels contributed 35 percent of revenue."
        statements = tuple(
            f"ACME_2026\nConsolidated Statements of Income\nSubsidiary {number}. Revenue 100. Net income 20."
            for number in range(32)
        )
        notes = tuple(f"ACME_2026\nAccounting policies {number}. Revenue recognition." for number in range(20))
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Statement diversity", summary="Narrative metric evidence", idempotency_key="diversity"
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*statements, target, *notes))
            citations = await search_pdf_pages(
                memory, "What percentage of ACME's FY2026 revenue came from digital channels?", limit=20
            )
            entries = [(await memory.get(GetMemoryEntryRequest(citation=value))).entry for value in citations]
            assert target in {entry.text for entry in entries}
            assert len(entries) == len({entry.sources[0].source_id for entry in entries}) == 20

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("question", "target", "background", "distractor"),
    [
        (
            "Has ACME reported any materially important ongoing legal battles in FY2026 and FY2025?",
            "Legal proceedings\nLitigation and regulatory proceedings have no material adverse effect.",
            "Routine legal policies and settlements discussion.",
            "ACME reported materially important ongoing disclosures in FY2026 and FY2025.",
        ),
        (
            "What are the geographies that ACME primarily operates in as of FY2026?",
            "Business operations\nOur segments and divisions serve countries and regions worldwide.",
            "International geographic policy discussion.",
            "ACME primarily operates in FY2026. These are its latest corporate disclosures.",
        ),
    ],
)
def test_short_topic_rankings_preserve_narrative_pages(question, target, background, distractor):
    async def scenario():
        evidence = "ACME_2026\n" + target
        notes = tuple(f"ACME_2026\nAccounting note {i}\n{distractor * 3}" for i in range(28))
        routine = tuple(
            f"ACME_2026\nGeneral policy {i}\n{background} " + "Unrelated disclosure detail. " * 90 for i in range(80)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Narrative ranks", summary="Topic coverage", idempotency_key="topic-ranks")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*notes, *routine, evidence))
            citations = await search_pdf_pages(memory, question, limit=20)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=value))).entry for value in citations]
            assert evidence in {entry.text for entry in entries}
            assert len(entries) == len({entry.sources[0].source_id for entry in entries}) == 20

    asyncio.run(scenario())


@pytest.mark.parametrize("year", [2026, 2027])
def test_report_year_distinguishes_filings_of_the_same_company(year):
    async def scenario():
        target = f"ACME_{year}\nConsolidated Statement of Income\nNet income 42."
        previous = f"ACME_{year - 1}\nConsolidated Statement of Income\n" + (
            f"ACME net income directly outlined in its FY{year} income statement outlook. " * 3
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Filing years", summary="Same company, different reports", idempotency_key="years")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (previous, target))
            citations = await search_pdf_pages(
                memory, f"What net income is directly outlined in ACME's FY{year} income statement?", limit=1
            )
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == target

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "question",
    ["What was ACME's FY2026 net income?", "In ACME_2027, what was its restated FY2026 net income?"],
)
def test_comparative_figures_remain_available_when_the_requested_filing_is_absent(question):
    async def scenario():
        target = "ACME_2027\nConsolidated Statement of Income\nRestated FY2026 net income 42."
        other = "BETA_2026\nConsolidated Statement of Income\nACME FY2026 net income outlook and restated figures."
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Comparative figures", summary="Available filing evidence", idempotency_key="comparatives"
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (other, target))
            citations = await search_pdf_pages(memory, question, limit=1)
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == target

    asyncio.run(scenario())


@pytest.mark.parametrize("opaque", [False, True])
def test_one_companys_matching_year_does_not_hide_another_companys_report(opaque):
    async def scenario():
        first_id, second_id = ("d93ac471c2b6408a", "b74ce8f021a69d30") if opaque else ("ACME_2026", "BETA_2027")
        target = f"{second_id}\nBusiness segments\nACME and BETA FY2026 reportable business segments comparison."
        other_pages = tuple(f"{first_id}\nBusiness segments {i}\nACME reports its FY2026 segments." for i in range(22))
        headers = (f"{second_id}\nBETA annual report.", f"{second_id}\nBETA corporate governance report.")
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Two companies", summary="Distinct available filing years", idempotency_key="companies"
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (*other_pages, *headers, target))
            citations = await search_pdf_pages(
                memory, "From FY2026 reports, what business segments do ACME and BETA discuss?", limit=20
            )
            assert target in {
                (await memory.get(GetMemoryEntryRequest(citation=value))).entry.text for value in citations
            }

    asyncio.run(scenario())


def test_statement_diversity_backfills_when_only_statement_pages_are_available():
    async def scenario():
        statements = tuple(
            f"ACME_2026\nConsolidated Statements of Income\nRevenue category {number}. Net income {number}."
            for number in range(8)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Statement backfill", summary="Preserve result count", idempotency_key="backfill")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, statements)
            citations = await search_pdf_pages(memory, "ACME FY2026 income statement", limit=6)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=value))).entry for value in citations]
            assert len(entries) == len({entry.sources[0].source_id for entry in entries}) == 6

    asyncio.run(scenario())


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


@pytest.mark.parametrize(
    "prefix",
    (
        "Depreciation expense is included in other income in the",
        "Expense is included in the Consolidated",
        "The following table presents the related depreciation expense on the",
    ),
)
def test_wrapped_statement_mentions_in_prose_are_not_primary_titles(prefix):
    async def scenario():
        statement = "ACME_2026\nACME Inc.\nConsolidated Statements of Income\nNet income 100."
        notes = tuple(
            f"ACME_2026\nNote {number}\n{prefix}\nConsolidated statements of income.\n"
            "Depreciation and amortization expense."
            for number in range(8)
        )
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Wrapped prose", summary="Full formal titles", idempotency_key="wrapped-prose")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await _store_pages(runtime, scope.scope_id, (statement, *notes))
            citations = await search_pdf_pages(
                memory, "Which ACME FY2026 income statement contains depreciation and amortization?", limit=1
            )
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
