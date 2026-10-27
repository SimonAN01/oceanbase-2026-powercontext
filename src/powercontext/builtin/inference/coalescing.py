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

"""Share in-flight embedding work and cache only successful vectors."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from itertools import islice

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

_Resolved = tuple[EmbeddingVector, int, InferenceUsage]


def _observe(future: asyncio.Future[_Resolved]) -> None:
    if not future.cancelled():
        future.exception()


def _validate_batch(result: EmbeddingResult, expected: int) -> None:
    if len(result.vectors) != expected:
        raise ValueError("embedding vector count does not match the requested batch")  # noqa: TRY003


class CoalescingEmbeddingModel:
    """Coordinate one loop's callers without transferring provider ownership."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a nonnegative integer")  # noqa: TRY003
        if not isinstance(coalesce_ms, (int, float)) or not math.isfinite(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be finite and nonnegative")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._capacity = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: dict[str, asyncio.Future[_Resolved]] = {}
        self._queued: dict[str, asyncio.Future[_Resolved]] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._batches: set[asyncio.Task[None]] = set()
        self._batch_id = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        loop = asyncio.get_running_loop()
        cached: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_Resolved]] = {}
        for text in texts:
            if not isinstance(text, str):
                raise ValueError("embedding inputs must be strings")  # noqa: TRY003, TRY004
            if text in self._cache:
                cached[text] = self._cache[text]
                self._cache.move_to_end(text)
            else:
                future = self._pending.get(text)
                if future is None:
                    future = loop.create_future()
                    future.add_done_callback(_observe)
                    self._pending[text] = future
                    self._queued[text] = future
                waiting[text] = future
        if self._queued and self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch())
        # Shield each shared Future. Cancelling gather only abandons this caller.
        values = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        usage: dict[int, InferenceUsage] = {}
        vectors = dict(cached)
        for text, (vector, batch_id, batch_usage) in zip(waiting, values, strict=True):
            vectors[text] = vector
            usage[batch_id] = batch_usage
        tokens = [item.input_tokens for item in usage.values()]
        output = [item.output_tokens for item in usage.values()]
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(item.requests for item in usage.values()),
                input_tokens=sum(tokens) if tokens and all(item is not None for item in tokens) else None,
                output_tokens=sum(output) if output and all(item is not None for item in output) else None,
            ),
        )

    async def _dispatch(self) -> None:
        try:
            await asyncio.sleep(self._delay)
            while self._queued:
                texts = tuple(islice(self._queued, self._batch_size))
                futures = tuple(self._queued.pop(text) for text in texts)
                self._batch_id += 1
                task = asyncio.create_task(self._compute(texts, futures, self._batch_id))
                self._batches.add(task)
                task.add_done_callback(self._batches.discard)
        finally:
            self._dispatcher = None

    async def _compute(
        self,
        texts: tuple[str, ...],
        futures: tuple[asyncio.Future[_Resolved], ...],
        batch_id: int,
    ) -> None:
        try:
            result = await self._model.embed(texts)
            _validate_batch(result, len(texts))
            self._complete(texts, futures, batch_id, result)
        except asyncio.CancelledError:
            for future in futures:
                if not future.done():
                    future.cancel()
            raise
        except Exception as error:
            for future in futures:
                if not future.done():
                    future.set_exception(error)
        finally:
            for text, future in zip(texts, futures, strict=True):
                if self._pending.get(text) is future:
                    self._pending.pop(text)

    def _complete(
        self,
        texts: tuple[str, ...],
        futures: tuple[asyncio.Future[_Resolved], ...],
        batch_id: int,
        result: EmbeddingResult,
    ) -> None:
        for text, vector, future in zip(texts, result.vectors, futures, strict=True):
            if self._capacity:
                self._cache[text] = vector
                self._cache.move_to_end(text)
                while len(self._cache) > self._capacity:
                    self._cache.popitem(last=False)
            if not future.done():
                future.set_result((vector, batch_id, result.usage))
