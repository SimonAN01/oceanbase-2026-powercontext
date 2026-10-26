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

from __future__ import annotations

import asyncio
import json

import pytest

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


@pytest.mark.parametrize("document_id", ["", " ", "\tdoc", "doc\n"])
def test_invalid_document_ids_are_rejected_even_without_data(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key="id", content=b"id\n"))


@pytest.mark.parametrize("primary_key", ["", "id", " id"])
def test_primary_key_must_match_the_untrimmed_header(primary_key: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key=primary_key, content=b'" id "\n001\n'))


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf",
        b"\nid,name\n1,ok\n",
        b'""\n1\n',
        b'id,name\n1,ok\n2,"bad"x\n',
        b"id,name\n1,ok\n2,too,many\n",
        b'id,name\n1,ok\n"",empty\n',
        b"id,name\n1,ok\n  ,blank\n",
        b"id,name\n1,ok\n \n",
        b"id,name\n1,ok\n2,\xff\n",
    ],
)
def test_late_errors_and_nonempty_blank_records_reject_the_document(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))


def test_quoted_empty_key_is_not_an_ignored_physical_record() -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b'id\n\n""\n'))


def test_identity_limit_applies_after_percent_encoding() -> None:
    assert len(csv_row_source_id("doc", "x" * 248)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "x" * 249)
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "中" * 28)
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a%3Ab", "c") != csv_row_source_id("a:b", "c")


def test_unicode_and_original_scalar_strings_survive_source_serialization() -> None:
    row = parse_csv_document(
        CSVDocumentInput(
            document_id="账单/2026",
            primary_key=" id ",
            content='\ufeff" id ",城市,amount,active,missing,note\r\n 001 ,北京,0007,false,,"first\r\nsecond"\r\n'.encode(),
        )
    )[0]
    assert row.values == (" 001 ", "北京", "0007", "false", "", "first\r\nsecond")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert restored.materialization is SourceMaterialization.CAPTURED
    assert asyncio.run(registry.read(restored)) == row
    evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_id == "csv:%E8%B4%A6%E5%8D%95%2F2026:%20001%20"
    assert evidence.content == (
        '{" id ":" 001 ","城市":"北京","amount":"0007","active":"false","missing":"","note":"first\\r\\nsecond"}'
    )
    assert list(json.loads(evidence.content)) == list(row.columns)


@pytest.mark.parametrize(
    "changes",
    [
        {"values": ("001",)},
        {"values": ("002", "name")},
        {"columns": ("id", "id")},
        {"columns": ("id", " ")},
        {"primary_key_value": " "},
    ],
)
def test_direct_row_inputs_cannot_produce_lossy_or_misidentified_sources(changes: dict[str, object]) -> None:
    data: dict[str, object] = {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "001",
        "columns": ("id", "name"),
        "values": ("001", "name"),
    }
    with pytest.raises(ValueError):
        CSVRowInput.model_validate(data | changes)


def test_captured_source_rejects_a_name_that_points_at_a_different_row() -> None:
    row = parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id,name\n001,name\n"))[0]
    with pytest.raises(ValueError):
        CSVRowSource(name="csv:doc:002", source_id="csv:doc:001", **row.model_dump())
