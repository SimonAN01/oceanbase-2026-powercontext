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

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources.pdf_pages import search_pdf_pages


def test_answer_style_instructions_do_not_hide_separate_financial_statements():
    async def scenario():
        statements = (
            "ACME_2026\nConsolidated income statement. Operating income 300. Net income 200. Revenue 900.",
            "ACME_2026\nConsolidated statement of cash flows. Depreciation and amortization 100. Capital expenditures 40.",
        )
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Financial search", summary="Statement retrieval", idempotency_key="financial")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await memory.remember(
                RememberMemoryRequest(
                    entries=tuple(
                        MemoryEntryInput(kind="document", text=text)
                        for text in (
                            *statements,
                            "ACME_2026\nRespond as an investment analyst. Give a response using the details shown in this question.",
                        )
                    )
                )
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


def test_an_explicit_statement_remains_retrievable_when_a_named_cost_is_absent():
    async def scenario():
        statement = "ACME_2026\nConsolidated Statement of Income\nNet sales 900. Operating income 300. Net income 200."
        notes = tuple(
            f"ACME_2026\nNote {number}\nRestructuring costs directly outlined in income statements for 2026. "
            "Restructuring costs and income are discussed in these detailed notes.\n"
            "Hedging instruments\nLosses and gains\nRecognized in\nIncome Statement\nOther columns"
            for number in range(30)
        )
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Statement focus", summary="Primary statement retrieval", idempotency_key="statement-focus"
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await memory.remember(
                RememberMemoryRequest(
                    entries=tuple(MemoryEntryInput(kind="document", text=text) for text in (statement, *notes))
                )
            )
            citations = await search_pdf_pages(
                memory, "What restructuring costs are directly outlined in ACME's income statements for 2026?", limit=1
            )
            assert len(citations) == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry.text == statement

    asyncio.run(scenario())
