from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Optional

from fastapi import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    conversation_uuid: Optional[str] = None
    table: Optional[str] = None
    model: Optional[str] = None
    fields: Optional[str] = None
    custom_prompt: Optional[str] = None


def parse_chat_result(payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        if payload.get("error"):
            raise ValueError(str(payload["error"]))
        if "hits" in payload:
            hits = payload.get("hits", {}).get("hits", [])
            if hits:
                return hits[0].get("_source", {})
        return payload

    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict) and first.get("error"):
            raise ValueError(str(first["error"]))
        if isinstance(first, dict) and "columns" in first and "data" in first:
            columns = []
            for col in first.get("columns", []):
                if isinstance(col, dict) and col:
                    columns.append(next(iter(col.keys())))
            data = first.get("data", [])
            if data and columns:
                row0 = data[0]
                if isinstance(row0, list):
                    return dict(zip(columns, row0))
                if isinstance(row0, dict):
                    return row0
        if isinstance(first, dict):
            return first

    return {}


def parse_sources(value: Any) -> list[Any]:
    def stringify_reference_ids(source: Any) -> Any:
        if not isinstance(source, dict):
            return source
        normalized = dict(source)
        for key in ("id", "item_id", "document_id"):
            if key in normalized and normalized[key] is not None:
                normalized[key] = str(normalized[key])
        return normalized

    if isinstance(value, list):
        return [stringify_reference_ids(source) for source in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [stringify_reference_ids(source) for source in parsed]
        except Exception:
            return []
    return []


def build_chat_sql(
    message: str,
    table: str,
    model: str,
    conversation_uuid: str | None,
    fields: str | None,
    quote: Callable[[str], str],
) -> str:
    args = [quote(message), quote(table), quote(model)]
    if conversation_uuid is not None or fields is not None:
        args.append(quote(conversation_uuid or ""))
    if fields is not None:
        args.append(quote(fields))
    return f"CALL CHAT({', '.join(args)})"


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def custom_prompt_model_name(base_model: str, prompt: str) -> str:
    return f"{base_model}_{prompt_hash(prompt)}"


def build_create_chat_model_sql(
    *,
    model_name: str,
    prompt: str,
    chat_model_options: dict[str, Any],
    quote: Callable[[str], str],
) -> str:
    options = []
    for key, value in chat_model_options.items():
        if isinstance(value, str):
            options.append(f"{key}={quote(value)}")
        else:
            options.append(f"{key}={value}")
    options.append(f"custom_prompt={quote(prompt)}")
    return f"CREATE CHAT MODEL {model_name} (\n    " + ",\n    ".join(options) + "\n)"


def create_chat_handler(
    *,
    manticore_sql: Callable[[str], dict[str, Any] | list[Any]],
    sql_quote: Callable[[str], str],
    default_table: str,
    default_model: str,
    vector_fields: str,
    init_message: str,
    default_prompt: str,
    chat_model_options: dict[str, Any],
):
    def assistant_chat(req: ChatRequest) -> dict[str, Any]:
        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        table = (req.table or default_table).strip() or default_table
        base_model = (req.model or default_model).strip() or default_model
        custom_prompt = req.custom_prompt.strip() if isinstance(req.custom_prompt, str) else ""
        prompt = custom_prompt or default_prompt
        model = custom_prompt_model_name(base_model, custom_prompt) if custom_prompt else base_model
        fields = req.fields.strip() if isinstance(req.fields, str) else (vector_fields or None)
        if isinstance(fields, str) and not fields.strip():
            fields = None
        conversation_uuid = req.conversation_uuid.strip() if req.conversation_uuid else None

        create_model_sql = build_create_chat_model_sql(
            model_name=model,
            prompt=prompt,
            chat_model_options=chat_model_options,
            quote=sql_quote,
        )

        try:
            parse_chat_result(manticore_sql(create_model_sql))
        except ValueError as exc:
            error_text = str(exc)
            if not is_chat_model_exists_error(error_text):
                raise HTTPException(status_code=400, detail=error_text) from exc
        except Exception as exc:
            logger.exception("Chat model creation failed: %s", exc)
            if init_message in str(exc):
                raise HTTPException(status_code=503, detail=init_message) from exc
            raise HTTPException(status_code=502, detail=f"Conversational search backend unavailable: {exc}") from exc

        def execute_chat(call_fields: str | None) -> dict[str, Any]:
            sql = build_chat_sql(
                message=message,
                table=table,
                model=model,
                conversation_uuid=conversation_uuid,
                fields=call_fields,
                quote=sql_quote,
            )
            last_error: ValueError | None = None
            for attempt in range(2):
                try:
                    return parse_chat_result(manticore_sql(sql))
                except ValueError as exc:
                    if attempt == 0 and is_transient_llm_error(str(exc)):
                        logger.warning("Retrying transient CALL CHAT LLM error: %s", exc)
                        last_error = exc
                        continue
                    raise
            if last_error is not None:
                raise last_error
            return {}

        try:
            row = execute_chat(fields)
        except ValueError as exc:
            error_text = str(exc)
            if fields is not None and "expects query, table, model, optional conversation_uuid" in error_text:
                row = execute_chat(None)
            elif is_uninitialized_error(error_text):
                raise HTTPException(status_code=503, detail=init_message) from exc
            else:
                raise HTTPException(status_code=400, detail=error_text) from exc
        except Exception as exc:
            logger.exception("Conversational search call failed: %s", exc)
            if init_message in str(exc):
                raise HTTPException(status_code=503, detail=init_message) from exc
            raise HTTPException(status_code=502, detail=f"Conversational search backend unavailable: {exc}") from exc

        if not row:
            raise HTTPException(status_code=500, detail="Empty conversational search response")

        return {
            "conversation_uuid": row.get("conversation_uuid") or conversation_uuid,
            "user_query": row.get("user_query") or message,
            "search_query": row.get("search_query") or "",
            "response": row.get("response") or "",
            "response_with_refs": row.get("response_with_refs") or "",
            "sources": parse_sources(row.get("sources")),
            "items": [],
            "model": model,
            "table": table,
        }

    return assistant_chat


def is_uninitialized_error(error_text: str) -> bool:
    text = error_text.lower()
    return any(
        needle in text
        for needle in (
            "unknown table",
            "unknown local table",
            "unknown index",
            "no such table",
            "table not found",
            "index not found",
            "model not found",
            "unknown chat model",
        )
    )


def is_chat_model_exists_error(error_text: str) -> bool:
    text = error_text.lower()
    return "chat model" in text and any(
        needle in text
        for needle in (
            "already exists",
            "exists already",
            "duplicate",
        )
    )


def is_transient_llm_error(error_text: str) -> bool:
    text = error_text.lower()
    return (
        "llm" in text
        and any(
            needle in text
            for needle in (
                "missing field `id`",
                "missing field 'id'",
                "tool call failed",
                "response generation failed",
                "request failed",
            )
        )
    )
