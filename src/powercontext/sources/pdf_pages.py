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
import zlib
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
    from pypdf.generic import ContentStream, DictionaryObject, StreamObject

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


def _normalize_text_positions(stream: ContentStream) -> bool:
    from pypdf.generic import FloatObject, NumberObject

    operations = stream.operations
    normalized = []
    changed = False
    unpositioned = False
    for index, (operands, operator) in enumerate(operations):
        following = operations[index + 1][1] if index + 1 < len(operations) else None
        if operator == b"Td" and normalized and normalized[-1][1] == b"Tm":
            matrix = normalized[-1][0]
            if len(matrix) == 6 and len(operands) == 2:
                # Neither positioning instruction paints a glyph. Fold their
                # equivalent final matrix so the transient origin cannot be
                # mistaken for a line break inside a word spanning BT objects.
                tx, ty = map(float, operands)
                normalized[-1] = (
                    [
                        *matrix[:4],
                        FloatObject(float(matrix[4]) + tx * float(matrix[0]) + ty * float(matrix[2])),
                        FloatObject(float(matrix[5]) + tx * float(matrix[1]) + ty * float(matrix[3])),
                    ],
                    b"Tm",
                )
                changed = True
                continue
        if operator == b"BT":
            unpositioned = True
        elif operator in (b"Tm", b"Td", b"TD", b"T*", b"'", b'"', b"ET"):
            unpositioned = False
        elif unpositioned and operator in (b"Tj", b"TJ"):
            # BT already resets both text matrices to identity. An explicit,
            # equivalent Tm lets pypdf process the position before the first
            # glyph run; otherwise it may insert a column/line separator in
            # the middle of the first kerned word instead.
            normalized.append(([NumberObject(n) for n in (1, 0, 0, 1, 0, 0)], b"Tm"))
            changed = True
            unpositioned = False
        normalized.append((operands, operator))
        if operator == b"TJ" and following == b"ET":
            array = operands[0]
            # ET ends the text object, so a final numeric advance has no
            # visible glyph. pypdf otherwise turns it into spaces.
            while array and isinstance(array[-1], (NumberObject, FloatObject)):
                array.pop()
                changed = True
    if changed:
        stream.operations = normalized
    return changed


def _normalize_glyph_names(resources: DictionaryObject) -> DictionaryObject:
    from pypdf._codecs import adobe_glyphs
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject

    fonts_ref = resources.get("/Font")
    fonts = fonts_ref.get_object() if fonts_ref is not None else None
    if not isinstance(fonts, DictionaryObject):
        return resources
    replacements = DictionaryObject()
    for name, ref in fonts.items():
        font = ref.get_object()
        if not isinstance(font, DictionaryObject) or "/Encoding" not in font:
            continue
        encoding = font["/Encoding"]
        if not isinstance(encoding, DictionaryObject) or "/Differences" not in encoding:
            continue
        differences = encoding["/Differences"]
        if not isinstance(differences, ArrayObject):
            continue
        # Adobe Glyph List treats the suffix as a visual variant.
        changes = {
            index: NameObject(base)
            for index, glyph in enumerate(differences)
            if isinstance(glyph, NameObject) and (base := glyph.partition(".")[0]) != glyph and base in adobe_glyphs
        }
        if not changes:
            continue
        names = ArrayObject(differences)
        for index, glyph_name in changes.items():
            names[index] = glyph_name
        normalized_encoding = DictionaryObject(encoding)
        normalized_encoding[NameObject("/Differences")] = names
        normalized_font = DictionaryObject(font)
        normalized_font[NameObject("/Encoding")] = normalized_encoding
        replacements[name] = normalized_font

    if not replacements:
        return resources
    normalized_fonts = DictionaryObject(fonts)
    normalized_fonts.update(replacements)
    normalized_resources = DictionaryObject(resources)
    normalized_resources[NameObject("/Font")] = normalized_fonts
    return normalized_resources


def _restore_marked_glyphs(  # noqa: C901 - walk the PDF text and marked-content state together
    stream: ContentStream, resources: DictionaryObject
) -> DictionaryObject:
    """Recover null Unicode glyph mappings from explicit single-glyph ActualText.

    A marked replacement can cover an entire phrase. Only an unambiguous,
    single encoded glyph with a null mapping can repair a font-wide mapping;
    other marked spans do not justify changing unrelated text.
    """
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, create_string_object

    fonts_ref = resources.get("/Font")
    if fonts_ref is None:
        return resources
    fonts = fonts_ref.get_object()
    font_name: str | None = None
    saved_fonts: list[str | None] = []
    spans: list[tuple[str | None, list[tuple[str | None, bytes]]]] = []
    candidates: dict[tuple[str, bytes], str | None] = {}
    for operands, operator in stream.operations:
        if operator == b"Tf":
            font_name = str(operands[0])
        elif operator == b"q":
            saved_fonts.append(font_name)
        elif operator == b"Q":
            font_name = saved_fonts.pop() if saved_fonts else None
        elif operator in (b"BDC", b"BMC"):
            actual: str | None = None
            if operator == b"BDC":
                properties = operands[1]
                if isinstance(properties, NameObject):
                    properties = resources.get("/Properties", {}).get(properties, {})
                if hasattr(properties, "get_object"):
                    properties = properties.get_object()
                if isinstance(properties, dict):
                    value = properties.get("/ActualText")
                    if isinstance(value, bytes):
                        value = create_string_object(value)
                    if isinstance(value, str):
                        actual = value
            spans.append((actual, []))
        elif operator in (b"Tj", b"TJ", b"'", b'"') and spans:
            values = operands[0] if operator == b"TJ" else (operands[-1],)
            for value in values:
                if isinstance(value, bytes) and value:
                    for _, glyphs in spans:
                        glyphs.append((font_name, value))
        elif operator == b"EMC" and spans:
            actual, glyphs = spans.pop()
            if actual and glyphs:
                name = glyphs[0][0]
                if name is None or name not in fonts or any(font != name for font, _ in glyphs):
                    continue
                font = fonts[name].get_object()
                width = 2 if font.get("/Encoding") in ("/Identity-H", "/Identity-V") else 1
                encoded = b"".join(value for _, value in glyphs)
                if len(encoded) != width:
                    continue
                key = (name, encoded)
                candidates[key] = actual if key not in candidates or candidates[key] == actual else None

    replacements = DictionaryObject(fonts)
    changed = False
    for (name, encoded), actual in candidates.items():
        font = replacements[name].get_object()
        if not isinstance(font, DictionaryObject):
            continue
        cmap_ref = font.get("/ToUnicode")
        if actual is None or cmap_ref is None:
            continue
        cmap = cmap_ref.get_object().get_data()
        # Repair only explicitly null bfchar mappings. ActualText does not
        # override a valid character map or an unrelated occurrence of a glyph.
        pattern = rb"<" + encoded.hex().encode() + rb">\s*<0+>"
        bfchars = re.findall(rb"beginbfchar(.*?)endbfchar", cmap, flags=re.DOTALL)
        if not any(re.search(pattern, block, flags=re.IGNORECASE) for block in bfchars) or b"endcmap" not in cmap:
            continue
        replacement = (
            b"1 beginbfchar\n<"
            + encoded.hex().encode()
            + b"> <"
            + actual.encode("utf-16-be").hex().encode()
            + b">\nendbfchar\n"
        )
        head, tail = cmap.rsplit(b"endcmap", 1)
        mapping = DecodedStreamObject()
        mapping.set_data(head + replacement + b"endcmap" + tail)
        repaired = DictionaryObject(font)
        repaired[NameObject("/ToUnicode")] = mapping
        replacements[NameObject(name)] = repaired
        changed = True
    if not changed:
        return resources
    result = DictionaryObject(resources)
    result[NameObject("/Font")] = replacements
    return result


def _extract_page_text(page: PageObject) -> str:  # noqa: C901 - keep per-page extraction state and callbacks together
    from types import FunctionType, MethodType

    from pypdf import PageObject
    from pypdf._text_extraction import mult
    from pypdf._text_extraction._text_extractor import TextExtraction
    from pypdf.generic import ArrayObject, NameObject
    from typing_extensions import override

    saved = []
    advances: list[float] = []
    active: list[PositionedExtraction | None] = []
    outer_matrices: list[list[float] | None] = []
    identity = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    crop_left = max(float(page.cropbox.left), float(page.mediabox.left))
    crop_right = min(float(page.cropbox.right), float(page.mediabox.right))
    cropped = crop_left < crop_right and (crop_left > page.mediabox.left or crop_right < page.mediabox.right)

    class PositionedExtraction(TextExtraction):
        def __init__(self):
            super().__init__()
            parent = active[-2] if len(active) > 1 else None
            self.character_spacing = parent.character_spacing if parent is not None else 0.0
            self.saved_character_spacing: list[float] = []
            self.outer_matrix = outer_matrices[-1]
            active[-1] = self
            self._last_text_position: tuple[list[float], list[float], float] | None = None

        def _outside_horizontal_crop(self, values):
            if self.outer_matrix is None or self.font.sub_type not in ("Type1", "TrueType", "MMType1"):
                return False
            a, b, c, _, x, _ = mult(mult(self.tm_matrix, self.cm_matrix), self.outer_matrix)
            scale = a * self.char_scale
            if b or c or scale <= 0 or self.font_size <= 0 or crop_left <= x <= crop_right:
                return False
            units = self.font_size / 1000
            length = sum(len(value) for value in values if isinstance(value, (str, bytes)))
            widths = self.font.character_widths.values()
            low, _, high, _ = self.font.font_descriptor.bbox
            spacing = abs(self.character_spacing) + abs(self.space_scale - 1)
            shifts = [-float(value) * units for value in values if isinstance(value, (int, float))]
            # Use worst-case glyph advances and ink overhang. A run that
            # might touch the crop is kept intact.
            left = low * units + length * (min(0, min(widths)) * units - spacing) + sum(min(0, v) for v in shifts)
            right = high * units + length * (max(0, max(widths)) * units + spacing) + sum(max(0, v) for v in shifts)
            return x + right * scale < crop_left or x + left * scale > crop_right

        def _separate_rewound_word(self):
            if self._last_text_position is None:
                return
            cm, matrix, font_size = self._last_text_position
            if cm != self.cm_matrix or matrix[:4] != self.tm_matrix[:4]:
                return
            a, b, _, _, x, y = matrix
            scale = a * a + b * b
            if not scale:
                return
            dx, dy = self.tm_matrix[4] - x, self.tm_matrix[5] - y
            advance = (dx * a + dy * b) / scale
            lift = (-dx * b + dy * a) / scale
            height = max(abs(font_size), abs(self.font_size))
            # A substantial rewind along the same baseline starts another
            # run. Tf may already have flushed the preceding glyphs to output.
            # Small backwards kerning and genuine line changes stay untouched.
            if advance < -2 * height and abs(lift) <= height / 2:
                if self.text and not self.text[-1].isspace():
                    self.text += " "
                elif not self.text and self.output and not self.output[-1].isspace():
                    self.output += " "

        @override
        def process_operation(self, operator, operands):
            if operator == b"Tc" and operands:
                self.character_spacing = float(operands[0])
            elif operator == b"q":
                self.saved_character_spacing.append(self.character_spacing)
            elif operator == b"Q" and self.saved_character_spacing:
                self.character_spacing = self.saved_character_spacing.pop()
            # pypdf inserts a plain Python string for a numeric TJ gap. Its
            # visible separator is useful, but its font width must not count
            # again: the actual numeric advance is accounted for below.
            synthetic_space = operator == b"Tj" and operands == [" "] and type(operands[0]) is str
            paints_text = operator == b"Tj" and not synthetic_space and bool(operands) and bool(operands[0])
            if paints_text:
                self._separate_rewound_word()
            super().process_operation(operator, operands)
            if paints_text:
                self._last_text_position = (self.cm_matrix.copy(), self.tm_matrix.copy(), self.font_size)
            if synthetic_space:
                self._actual_str_size["str_widths"] -= self.font.space_width * self.font_size

    def before(operator, operands, _cm, _tm):
        if not operands or (operator != b"TJ" and (not cropped or operator != b"Tj")):
            return
        extractor = active[-1]
        if extractor is None:
            raise ValueError("missing-pdf-text-extractor")
        original = operands[0]
        if operator == b"Tj":
            if extractor._outside_horizontal_crop(operands):
                saved.append((operands, original))
                operands[0] = ""
            return
        advances.append(-sum(float(value) for value in original if isinstance(value, (int, float))))
        spacing = 1000 * extractor.character_spacing / extractor.font_size if extractor.font_size > 0 else 0.0
        threshold = extractor._space_width * 0.95
        # Backwards kerning cannot separate words. Forward adjustments can
        # also be cancelled by negative character spacing, in the same units.
        # Retain the raw advances for cursor accounting after extraction.
        visible = (
            []
            if extractor._outside_horizontal_crop(original)
            else [
                value
                for value in original
                if not isinstance(value, (int, float))
                or (value <= 0 and (spacing >= 0 or -value < threshold or -value + spacing >= threshold))
            ]
        )
        if len(visible) != len(original):
            saved.append((operands, original))
            operands[0] = ArrayObject(visible)

    def after(operator, operands, _cm, _tm):
        if saved and saved[-1][0] is operands:
            _, original = saved.pop()
            operands[0] = original
        if operator == b"TJ" and operands:
            extractor = active[-1]
            if extractor is None:
                raise ValueError("missing-pdf-text-extractor")
            # pypdf counts glyph widths but omits TJ cursor adjustments. A
            # subsequent Td/Tm can then mistake accumulated letter spacing
            # for a word boundary in the middle of a word or number.
            extractor._actual_str_size["str_widths"] += advances.pop() * extractor.font_size

    base = PageObject._extract_text
    extract = FunctionType(
        base.__code__,
        {**base.__globals__, "TextExtraction": PositionedExtraction},
        base.__name__,
        base.__defaults__,
        base.__closure__,
    )
    extract.__kwdefaults__ = base.__kwdefaults__

    def invoke(self, obj, *args, **kwargs):
        # Legacy forms without a resource dictionary use the invoking page's
        # resources. The same indirect form may be drawn on several pages.
        resources = page.get("/Resources") if active and "/Resources" not in obj else None
        outer = None
        if cropped:
            parent = active[-1] if active else None
            if parent is None:
                outer = identity
            elif parent.outer_matrix is not None and parent.char_scale == parent.space_scale == 1.0:
                form = obj.get("/Matrix", identity)
                if isinstance(form, list) and len(form) == 6 and all(isinstance(v, (int, float)) for v in form):
                    outer = mult(form, mult(parent.cm_matrix, parent.outer_matrix))
        outer_matrices.append(outer)
        active.append(None)
        try:
            if resources is not None:
                obj[NameObject("/Resources")] = resources
            return extract(self, obj, *args, **kwargs)
        finally:
            active.pop()
            outer_matrices.pop()
            if resources is not None:
                obj.pop("/Resources", None)

    # Bind the loop to this reader-owned page. Nested Forms use the same
    # per-page factory; other pages and pypdf callers keep their own methods.
    previous = page.__dict__.get("_extract_text")
    page._extract_text = MethodType(invoke, page)  # ty: ignore[invalid-assignment]

    try:
        return page.extract_text(visitor_operand_before=before, visitor_operand_after=after) or ""
    finally:
        for operands, original in reversed(saved):
            operands[0] = original
        if previous is None:
            del page._extract_text
        else:
            page._extract_text = previous


def _validate_flate_eof(stream: StreamObject) -> None:
    from pypdf import get_configuration
    from pypdf.generic import ArrayObject, EncodedStreamObject

    if not isinstance(stream, EncodedStreamObject) or "/Filter" not in stream:
        return
    filters = stream["/Filter"]
    first = filters[0].get_object() if isinstance(filters, ArrayObject) and filters else filters
    if first not in ("/FlateDecode", "/Fl"):
        return
    # pypdf's recovery decoder can return a partial prefix without a zlib EOF.
    # Check the trailer before any page text is accepted, using its output limit.
    decoder = zlib.decompressobj()
    try:
        decoder.decompress(stream._data, max_length=get_configuration().zlib_maximum_output_length)
    except zlib.error as error:
        raise ValueError("invalid-pdf-flate-stream") from error
    if not decoder.eof:
        raise ValueError("incomplete-pdf-flate-stream")


def _resolve_pdf_contents(contents):
    from pypdf.generic import ArrayObject, NullObject, StreamObject

    resolved = contents.get_object()
    if isinstance(resolved, NullObject):
        return None
    if isinstance(resolved, ArrayObject):
        streams = [item.get_object() for item in resolved]
        if all(isinstance(stream, (StreamObject, NullObject)) for stream in streams):
            for stream in streams:
                if isinstance(stream, StreamObject):
                    _validate_flate_eof(stream)
            return ArrayObject(streams)
    elif isinstance(resolved, StreamObject):
        _validate_flate_eof(resolved)
        return resolved
    # pypdf can skip missing references in an array and return partial text.
    raise ValueError("invalid-pdf-content-stream")


def _prepare_page_streams(page: PageObject) -> None:
    from pypdf import PageObject
    from pypdf.generic import ContentStream, DecodedStreamObject, NameObject

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
            stream = ContentStream(_resolve_pdf_contents(contents), page.pdf, "bytes")
            resources = owner.get("/Resources")
            if resources is not None:
                owner[NameObject("/Resources")] = _restore_marked_glyphs(
                    stream, _normalize_glyph_names(resources.get_object())
                )
            changed = _normalize_text_positions(stream)
            if is_page:
                # Reuse the parsed operators even when their positions needed
                # no repair. Reopening the original stream parses every glyph
                # a second time without changing the extracted text.
                owner[NameObject("/Contents")] = stream
            elif changed:
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


def _pdf_page_text(page: PageObject) -> str:
    from pypdf.generic import NameObject

    original_contents = page.get("/Contents")
    try:
        _prepare_page_streams(page)
        # These presentation-form ligatures represent ordinary Latin letter
        # sequences. Expanding them preserves words for the specified ASCII tokens.
        return _extract_page_text(page).translate({
            0xFB00: "ff",
            0xFB01: "fi",
            0xFB02: "fl",
            0xFB03: "ffi",
            0xFB04: "ffl",
            0xFB05: "st",
            0xFB06: "st",
        })
    finally:
        # Reader retains every PageObject. Keep parsed operators only for this
        # extraction, rather than accumulating every page's arrays in memory.
        if original_contents is None:
            page.pop("/Contents", None)
        else:
            page[NameObject("/Contents")] = original_contents


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every page before publishing any results, including empty pages."""
    # pypdf is an optional source dependency; other Source types do not require it.
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    _validate_document_id(value.document_id)
    if not value.content or b"%PDF-" not in value.content[:1024]:
        raise ValueError("invalid-pdf-document")
    # pypdf can recover pages even when the final EOF marker is incomplete.
    if not value.content.rstrip(b"\x00\t\n\f\r ").endswith((b"\n%%EOF", b"\r%%EOF")):
        raise ValueError("unreadable-pdf-document")
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
_REPORT_YEARS = re.compile(r"(?<![a-z0-9])(?:fy)?((?:19|20)\d{2})(?![a-z0-9])", re.IGNORECASE)
_REGISTRANT_NAME = re.compile(r"^([^\n]+)\n\s*\(?exact name of (?:the )?registrant\b", re.IGNORECASE | re.MULTILINE)
_FINANCIAL_EXPANSIONS = {
    "ebitda": "operating income depreciation amortization earnings interest taxes",
    "capex": "capital expenditures property plant equipment payments additions",
    "roa": "net income total assets",
    "roe": "net income shareholders stockholders equity",
    "margin": "income revenue sales earnings",
    "revenue": "sales revenues",
    "sales": "revenue revenues",
    "restructuring": "restructuring impairment charges expenses",
    "acquire": "acquisition purchase consideration",
    "divestiture": "divest divestment sale disposal transaction",
    "lending": "loan loans portfolio credit",
    "repurchase": "repurchase repurchases authorization",
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
        # Wrapped prose and supplementary headings can put an exact statement
        # name on its own line. Keep the preceding connective or qualifier.
        if position and re.search(
            r"\b(?:the|a|an|of|in|on|to|for|and|or|our|its|their|consolidated|combined|condensed|consolidating)\s*$"
            r"|^\s*off[-\u2010-\u2015]?\s*$",
            headings[position - 1],
            re.IGNORECASE,
        ):
            continue
        first = headings[position].strip().casefold()
        formal = re.match(r"(?:consolidated|combined|condensed|statements?)\b", first)
        # A short column label in a later footnote cannot start a formal title.
        if position > 3 and not formal:
            continue
        # Some producers draw each word of a formal title on a separate line.
        width = 6 if formal else 2
        heading = ""
        for line in headings[position : position + width]:
            heading = f"{heading} {' '.join(line.casefold().split())}".lstrip()
            if len(heading.split()) > 12 or re.search(r"\.(?:\s*\.)+", heading):
                break
            for kind in focus:
                match = _STATEMENT_HEADINGS[kind].search(heading)
                if match and re.fullmatch(
                    r"(?:\s+and\s+(?:comprehensive\s+(?:income|loss)|retained\s+earnings))?"
                    r"(?:\s*\([^)]*\))*\s*(?:[-\u2013\u2014]?\s*continued)?[.:]?",
                    heading[match.end() :],
                ):
                    return True
    return False


def _named_documents(hits: tuple[MemoryHit, ...], question: str) -> frozenset[str]:
    query_words = re.findall(r"[a-z0-9]+", question.casefold())
    query_identity = {
        "".join(query_words[start : start + width])
        for start in range(len(query_words))
        for width in range(1, min(4, len(query_words) - start) + 1)
    }
    filing_words = {"10k", "10q", "8k", "annual", "annualreport", "report", "earnings", "dated"}
    documents = {
        identity: company
        for identity in {hit.text.partition("\n")[0] for hit in hits}
        if (
            company := frozenset(
                word
                for word in re.findall(r"[a-z0-9]+", identity.casefold())
                if len(word) > 1 and re.search(r"[a-z]", word) and word not in filing_words and word in query_identity
            )
        )
    }
    matched = frozenset(documents)
    years = set(_REPORT_YEARS.findall(question))
    if len(years) == 1 and len(set(documents.values())) == 1:
        matching_year = frozenset(
            identity for identity in matched if years.intersection(_REPORT_YEARS.findall(identity))
        )
        # A later report may contain the requested year's comparative figures.
        # Retain all named reports if none has that year, or several years are
        # explicitly requested, rather than discarding real evidence.
        return matching_year or matched
    return matched


def _hit_key(hit: MemoryHit) -> tuple[str, str, int, str, str]:
    return (
        hit.memory_ref.family,
        hit.memory_ref.artifact_id,
        hit.memory_ref.revision,
        hit.entry_id,
        hit.entry_version_id,
    )


def _rank_pages(
    hits: tuple[MemoryHit, ...],
    query: str,
    expansion: str,
    question: str,
    query_ranks: dict[tuple[str, str, int, str, str], float],
    inferred_documents: frozenset[str],
) -> tuple[MemoryHit, ...]:
    """Fuse topic retrieval ranks with BM25 over the actual retrieved page text."""
    documents = [_terms(hit.text) for hit in hits]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 1.0
    frequency: Counter[str] = Counter(term for document in documents for term in document)
    weights = dict.fromkeys(_terms(expansion), 0.35)
    weights.update(dict.fromkeys(_terms(query), 1.0))
    focus = _statement_focus(question)
    named_documents = _named_documents(hits, question) or inferred_documents
    priorities = [
        (-int(hit.text.partition("\n")[0] in named_documents), -int(_statement_page(hit.text, focus))) for hit in hits
    ]

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

    ranking = sorted(range(len(hits)), key=lambda index: (*priorities[index], -score(index), index))
    # RRF preserves a short topic query's signal without requiring the Memory
    # and page-level BM25 scores to share a numerical scale. The reranker is
    # one additional ranking; named-document pool expansion casts no vote.
    fused = {
        index: query_ranks.get(_hit_key(hits[index]), 0.0) + 1.0 / (60 + rank) for rank, index in enumerate(ranking, 1)
    }
    # sorted is stable, so BM25 order resolves equal fused scores.
    return tuple(hits[index] for index in sorted(ranking, key=lambda index: (*priorities[index], -fused[index])))


def _question_clauses(question: str) -> tuple[str, ...]:
    # Keep each explicit question and omit trailing answer-style instructions.
    body, separator, _ = question.rpartition("?")
    if not separator:
        return (question,)
    return tuple(clause.strip() for clause in body.split("?") if clause.strip())


def _queries(question: str) -> tuple[str, str, tuple[str, ...]]:
    primary_queries = tuple(analyze_fts_query(clause) for clause in _question_clauses(question))
    primary = " ".join(primary_queries)
    financial_text = re.sub(r"\bbuy\s+back\b", "repurchase", question.casefold())
    concepts = tuple(
        dict.fromkeys(_FINANCIAL_EXPANSIONS[term] for term in _terms(financial_text) if term in _FINANCIAL_EXPANSIONS)
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
    queries = tuple(
        dict.fromkeys(value for value in (*primary_queries, *concept_queries, *phrases, *statement_terms) if value)
    )
    return primary, " ".join((*concepts, *phrases, *statement_terms)), queries


async def _search_document_mentions(
    memory: ScopedMemoryApplication, question: str, limit: int
) -> tuple[tuple[MemoryHit, ...], frozenset[str]]:
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    inferred_documents: set[str] = set()
    hits: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    question_text = " ".join(_question_clauses(question))
    primary_terms = set(analyze_fts_query(question_text).split())
    headings = {
        "item",
        "section",
        "note",
        "legal",
        "proceedings",
        "report",
        "annual",
        "quarterly",
        "statement",
        "statements",
        "cash",
        "flow",
        "flows",
        "balance",
        "sheet",
        "sheets",
        "consolidated",
        "combined",
        "condensed",
        "financial",
    }
    mentions = {
        match[0]
        for match in re.finditer(r"\b[A-Za-z0-9]+(?:[.&-][A-Za-z0-9]+)*", question_text)
        if len(match[0]) > 1
        and any(character.isupper() for character in match[0])
        and set(analyze_fts_query(match[0]).split()).intersection(primary_terms)
        and match[0].casefold() not in headings
        and not _REPORT_YEARS.fullmatch(match[0])
        and not _NUMERIC_QUESTION.fullmatch(match[0].casefold())
    }
    literals = {
        mention: re.compile(r"(?<!\w)" + re.escape(mention) + r"(?!\w)", re.IGNORECASE) for mention in sorted(mentions)
    }
    for mention, literal in literals.items():
        page = await memory.search(
            SearchMemoryRequest(query=analyze_fts_query(mention), limit=max(200, limit * 10), mode="fts")
        )
        texts: dict[str, set[str]] = {}
        for hit in page.hits:
            hits.setdefault(_hit_key(hit), hit)
            identity, _, body = hit.text.partition("\n")
            if literal.search(body):
                texts.setdefault(identity, set()).add(body)
        counts = sorted(((len(values), identity) for identity, values in texts.items()), reverse=True)
        # A reference in another report must not become its identity. Count
        # distinct page texts, rather than duplicate Memory entries, and
        # retain ordinary topic ranking when the association is ambiguous.
        if counts and counts[0][0] >= max(2, 2 * counts[1][0] if len(counts) > 1 else 2):
            inferred_documents.add(counts[0][1])
    if len(inferred_documents) > 1:
        # A common issuer name can fill its FTS pool with commentary. Query
        # the filing label separately to find covers and disambiguate names
        # that recur in the discussion, such as a geographic market.
        covers = await memory.search(SearchMemoryRequest(query="exact name registrant", limit=200, mode="fts"))
        registered_documents = set()
        for hit in covers.hits:
            identity, _, body = hit.text.partition("\n")
            registrant = _REGISTRANT_NAME.search(body)
            if registrant and any(literal.search(registrant[1]) for literal in literals.values()):
                registered_documents.add(identity)
        if registered_documents:
            inferred_documents = registered_documents
    return tuple(hits.values()), frozenset(inferred_documents)


async def _search_candidates(
    memory: ScopedMemoryApplication, queries: tuple[str, ...], question: str, limit: int
) -> tuple[tuple[MemoryHit, ...], dict[tuple[str, str, int, str, str], float], frozenset[str]]:
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    candidates: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    query_ranks: dict[tuple[str, str, int, str, str], float] = {}
    for text in queries:
        page = await memory.search(SearchMemoryRequest(query=text, limit=max(200, limit * 10), mode="fts"))
        for rank, hit in enumerate(page.hits, 1):
            key = _hit_key(hit)
            candidates.setdefault(key, hit)
            query_ranks[key] = query_ranks.get(key, 0.0) + 1.0 / (60 + rank)
    named_documents = _named_documents(tuple(candidates.values()), question)
    inferred_documents = frozenset()
    if not named_documents:
        mention_hits, inferred_documents = await _search_document_mentions(memory, question, limit)
        for hit in mention_hits:
            candidates.setdefault(_hit_key(hit), hit)
    # Short topic queries have a global top-N cutoff. In a long filing,
    # numerous matching notes can crowd a primary statement out of every
    # topic pool. Retrieve the named filing through the same public API before
    # reranking, expanding the request until Memory has no further hits.
    for document in sorted(named_documents or inferred_documents):
        pool_limit = max(1000, limit * 10)
        while True:
            page = await memory.search(
                SearchMemoryRequest(query=analyze_fts_query(document), limit=pool_limit, mode="fts")
            )
            for hit in page.hits:
                candidates.setdefault(_hit_key(hit), hit)
            if len(page.hits) < pool_limit:
                break
            pool_limit *= 2
    return tuple(candidates.values()), query_ranks, frozenset(inferred_documents)


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Retrieve real immutable entry citations; no document paths or labels are read."""
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest

    if type(limit) is not int or limit < 1 or not question.strip():
        raise ValueError("invalid-pdf-search")
    query, expansion, queries = _queries(question)
    candidates, query_ranks, inferred_documents = await _search_candidates(memory, queries, question, limit)
    citations: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    deferred: list[MemoryCitation] = []
    statement_counts: Counter[tuple[str, str]] = Counter()
    focus = _statement_focus(question)
    # Main statements supply numeric inputs, but a long filing can also
    # contain many subsidiary statements. Leave room for narrative evidence;
    # unused slots are backfilled from the deferred statements below.
    statement_slots = max(1, limit // 5)
    for hit in _rank_pages(candidates, query, expansion, question, query_ranks, inferred_documents):
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
        document = hit.text.partition("\n")[0]
        kinds = tuple(kind for kind in focus if _statement_page(hit.text, (kind,)))
        if kinds and all(statement_counts[document, kind] >= statement_slots for kind in kinds):
            deferred.append(citation)
            continue
        for kind in kinds:
            statement_counts[document, kind] += 1
        citations.append(citation)
        if len(citations) == limit:
            break
    citations.extend(deferred[: limit - len(citations)])
    return tuple(citations)
