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

"""Portable, self-verifying UTF-8 context packs and prefix deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Self

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Malformed context bytes, protocol fields or reconstruction identity."""


def _encode(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _items(items: tuple[bytes, ...]) -> list[str]:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("invalid-items")
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decoded_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("invalid-items-array")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError("invalid-item-type")
        try:
            decoded = base64.b64decode(item, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ContextPackError("invalid-base64") from error
        if base64.b64encode(decoded).decode("ascii") != item:
            raise ContextPackError("noncanonical-base64")
        result.append(decoded)
    return tuple(result)


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ContextPackError("invalid-digest")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ContextPackError("duplicate-field")
    return value


def _payload(raw: bytes, schema: str, fields: set[str]) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, AttributeError) as error:
        raise ContextPackError("invalid-json") from error
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != schema:
        raise ContextPackError("invalid-schema")
    return value


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Immutable original chunks with a digest and exact canonical wire size."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        unsigned: dict[str, object] = {"schema": PACK_SCHEMA, "items": _items(self.items)}
        expected = "sha256:" + hashlib.sha256(_encode(unsigned)).hexdigest()
        if _digest(self.digest) != expected:
            raise ContextPackError("digest-mismatch")
        if type(self.byte_count) is not int or self.byte_count != len(self.to_bytes()):
            raise ContextPackError("byte-count-mismatch")

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> Self:
        unsigned: dict[str, object] = {"schema": PACK_SCHEMA, "items": _items(items)}
        digest = "sha256:" + hashlib.sha256(_encode(unsigned)).hexdigest()
        count = 0
        while True:
            size = len(_encode({**unsigned, "digest": digest, "byte_count": count}))
            if count == size:
                return cls(items, digest, count)
            count = size

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> Self:
        if not isinstance(content, str) or type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("invalid-content-or-limit")
        try:
            raw = content.encode("utf-8")
        except UnicodeError as error:
            raise ContextPackError("invalid-utf8") from error
        chunks: list[bytes] = []
        start = 0
        while start < len(raw):
            end = min(len(raw), start + chunk_bytes)
            if end < len(raw):
                while end > start and raw[end] & 0xC0 == 0x80:
                    end -= 1
            if end == start:
                raise ContextPackError("chunk-limit-too-small")
            chunks.append(raw[start:end])
            start = end
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        return _encode({
            "schema": PACK_SCHEMA,
            "items": _items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        value = _payload(raw, PACK_SCHEMA, {"schema", "items", "digest", "byte_count"})
        result = cls.build(_decoded_items(value["items"]))
        if (
            _digest(value["digest"]) != result.digest
            or type(value["byte_count"]) is not int
            or value["byte_count"] != result.byte_count
        ):
            raise ContextPackError("pack-integrity-mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A complete reconstruction recipe requiring only the receiver's base."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _digest(self.base_digest)
        _digest(self.target_digest)
        _items(self.appended)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("invalid-prefix")

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> Self:
        prefix = 0
        for old, new in zip(base.items, target.items, strict=False):
            if old != new:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _encode({
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _items(self.appended),
        })

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        value = _payload(raw, DELTA_SCHEMA, {"schema", "base_digest", "target_digest", "prefix_items", "appended"})
        prefix = value["prefix_items"]
        if type(prefix) is not int:
            raise ContextPackError("invalid-prefix")
        return cls(
            _digest(value["base_digest"]), _digest(value["target_digest"]), prefix, _decoded_items(value["appended"])
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Reject a wrong base, invalid prefix or corrupted target before use."""
    if base.digest != delta.base_digest or not 0 <= delta.prefix_items <= len(base.items):
        raise ContextPackError("base-or-prefix-mismatch")
    result = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if result.digest != delta.target_digest:
        raise ContextPackError("target-digest-mismatch")
    return result
