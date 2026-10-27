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

"""Lossless visible Pi events, branch selection and durable incremental capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    content: bytes
    leaf_id: str | None = None


def pi_event_source_id(session_id: str, event_id: str) -> str:
    if not session_id.strip() or not event_id.strip():
        raise ValueError("Pi identity must contain a nonblank session and event")  # noqa: TRY003
    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("Pi source identity exceeds the source length limit")  # noqa: TRY003
    return identity


class PiEventInput(BaseModel):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        pi_event_source_id(self.session_id, self.event_id)
        return self


class PiEventSource(Source, PiEventInput):
    source_id: str
    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.source_id != pi_event_source_id(self.session_id, self.event_id) or self.name != self.source_id:
            raise ValueError("Pi source identity does not match its original event")  # noqa: TRY003
        return self


def _invalid_number(value: str) -> None:
    raise ValueError("Pi JSON must not contain non-finite numbers")  # noqa: TRY003


def _archive(value: bytes) -> tuple[str, dict[str, dict[str, JsonValue]]]:
    rows = [
        json.loads(line, parse_constant=_invalid_number)
        for line in value.decode("utf-8-sig").splitlines()
        if line.strip()
    ]
    if not rows or not isinstance(rows[0], dict):
        raise ValueError("Pi archive has no session header")  # noqa: TRY003
    header = rows[0]
    session_id = header.get("id")
    if (
        header.get("type") != "session"
        or type(header.get("version")) is not int
        or header["version"] != 3
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        raise ValueError("Pi archive must start with a version 3 session")  # noqa: TRY003
    records: dict[str, dict[str, JsonValue]] = {}
    for record in rows[1:]:
        if not isinstance(record, dict) or "parentId" not in record or not isinstance(record.get("type"), str):
            raise ValueError("Pi event is missing its type or parent")  # noqa: TRY003
        identity, parent = record.get("id"), record["parentId"]
        if not isinstance(identity, str) or not identity.strip() or identity in records:
            raise ValueError("Pi event IDs must be nonblank and unique")  # noqa: TRY003
        if parent is not None and (not isinstance(parent, str) or (parent and parent not in records)):
            raise ValueError("Pi parent must precede the event")  # noqa: TRY003
        records[identity] = record
    return session_id, records


def _visible(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        raise ValueError("Pi content must be a string or content blocks")  # noqa: TRY003, TRY004
    pieces = []
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Pi content blocks must be objects")  # noqa: TRY003, TRY004
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Pi text block has no text")  # noqa: TRY003
            pieces.append(text)
        elif block.get("type") == "toolCall":
            name, identity = block.get("name"), block.get("id")
            if not isinstance(name, str) or not isinstance(identity, str):
                raise ValueError("Pi tool call has no name or ID")  # noqa: TRY003
            arguments = json.dumps(
                block.get("arguments", {}), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            pieces.append(f"[tool_call {name} {identity}] {arguments}")
    return "\n".join(pieces)


def _event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    kind = record["type"]
    if kind == "message":
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ValueError("Pi message must have a role")  # noqa: TRY003
        role = message["role"]
        body = _visible(message.get("content"))
        if role == "toolResult" and body:
            body = f"[tool_result {message.get('toolName', '')} {message.get('toolCallId', '')}] {body}"
        elif role == "bashExecution":
            command, output = message.get("command", ""), message.get("output", "")
            if not isinstance(command, str) or not isinstance(output, str):
                raise ValueError("Pi shell evidence must contain text")  # noqa: TRY003
            body = f"$ {command}\n{output}"
    elif kind in ("compaction", "branch_summary"):
        role, body = kind, record.get("summary", "")
        if not isinstance(body, str):
            raise ValueError("Pi summary must contain text")  # noqa: TRY003
    elif kind == "custom_message":
        role, body = "custom", _visible(record.get("content"))
    else:
        return None
    if not body:
        return None
    return PiEventInput.model_validate({
        "session_id": session_id,
        "event_id": record["id"],
        "parent_id": record["parentId"],
        "event_type": kind,
        "role": role,
        "content": f"{role}: {body}",
        "record": record,
    })


def _branch(session_id: str, records: dict[str, dict[str, JsonValue]], leaf_id: str | None) -> tuple[PiEventInput, ...]:
    # Validate every event before returning any part of the chosen branch.
    events = {key: _event(session_id, record) for key, record in records.items()}
    leaf = leaf_id if leaf_id is not None else next(reversed(records), None)
    if leaf is not None and leaf not in records:
        raise ValueError("Pi leaf does not exist")  # noqa: TRY003
    chain: list[PiEventInput] = []
    while leaf:
        event = events[leaf]
        if event is not None:
            chain.append(event)
        parent = records[leaf]["parentId"]
        leaf = parent if isinstance(parent, str) else None
    return tuple(reversed(chain))


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    session_id, records = _archive(value.content)
    return _branch(session_id, records, value.leaf_id)


class _PiEventAdapter:
    name = "pi-event"
    input_class = PiEventInput
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(name=identity, source_id=identity, **value.model_dump())

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput.model_validate(source.model_dump(include=set(PiEventInput.model_fields)))


class _PiEventProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pi-event",
            source_id=source.source_id,
            content=source.content,
            metadata={
                key: getattr(source, key) for key in ("session_id", "event_id", "parent_id", "event_type", "role")
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    _PiEventAdapter(), projections=(_PiEventProjection(),)
)


class PiSessionConnector:
    """Submit only new selected events; let ConnectorLifecycle commit progress."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, records = _archive(await asyncio.to_thread(self.path.read_bytes))
        events = _branch(session_id, records, self.leaf_id)
        fingerprints: dict[str, JsonValue] = {
            key: hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest()
            for key, record in records.items()
        }
        previous = session.checkpoint
        imported: dict[str, JsonValue] = {}
        if previous is not None:
            if (
                not isinstance(previous, dict)
                or previous.get("schema") != 1
                or previous.get("session_id") != session_id
                or not isinstance(previous.get("imported"), dict)
            ):
                raise ValueError("Pi checkpoint does not belong to this archive")  # noqa: TRY003
            imported = dict(previous["imported"])
            if any(key not in fingerprints or fingerprints[key] != digest for key, digest in imported.items()):
                raise ValueError("Previously imported Pi history was changed or removed")  # noqa: TRY003
        for event in events:
            if event.event_id in imported:
                continue
            outcome = await session.submit(pi_event_source_id(session_id, event.event_id), "pi-event", event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                imported[event.event_id] = fingerprints[event.event_id]
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE, checkpoint={"schema": 1, "session_id": session_id, "imported": imported}
        )
