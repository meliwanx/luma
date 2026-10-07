"""Owner-filtered discovery and bounded, revision-bound capability loading.

MCP provides the transport; search and trusted deployment skills are Luma
application features. Nothing in this module treats remote text as policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .. import mcp
from ..db import get_connection
from ..mappers import parse_json

MAX_TOOLS = 8
MAX_SKILLS = 2
MAX_LOAD_CHARS = 32_000
MAX_SKILL_CHARS = 8_000
MAX_MANIFESTS = 256
MAX_RESULT_CHARS = 12_000
MAX_TURN_RESULT_CHARS = 24_000


class CapabilityError(mcp.MCPError):
    """A safe, public error with no endpoint, credential or raw payload."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()[:24]


def tool_fingerprint(tool: dict[str, Any]) -> str:
    """Fingerprint normalized schema and hints, not display/transport state."""
    return _hash({key: tool.get(key) for key in
                  ("name", "title", "description", "input_schema", "annotations", "annotations_declared")})


def _rows(user_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT c.*, s.updated_at AS credential_revision FROM connectors c "
            "LEFT JOIN connector_secrets s ON s.connector_id = c.id AND s.user_id = c.user_id "
            "WHERE c.user_id = ? AND c.kind = 'mcp' "
            "AND c.enabled = 1 ORDER BY c.created_at, c.id", (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _tools(user_id: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in _rows(user_id):
        metadata = parse_json(record.get("metadata_json", "{}"))
        if not isinstance(metadata, dict) or metadata.get("status", "ok") != "ok":
            continue
        server = metadata.get("server", {})
        server = server if isinstance(server, dict) else {}
        raw = metadata.get("tools", [])
        for tool in raw if isinstance(raw, list) else []:
            if not isinstance(tool, dict) or not tool.get("enabled", True):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name or len(name) > 200:
                continue
            connector_id = str(record["id"])
            capability_id = "mcp:" + connector_id + ":" + name
            fingerprint = tool_fingerprint(tool)
            version = _hash({"endpoint": record["endpoint"], "server": server,
                             "tool": fingerprint, "credential": record.get("credential_revision")})
            function_name = "mcp_" + _hash(capability_id)
            info = {
                "capability_id": capability_id, "version": version,
                "remote_schema_version": fingerprint,
                "remote_server_version": str(server.get("version") or ""),
                "connector_id": connector_id, "connector": record["name"],
                "endpoint": record["endpoint"], "tool": name,
                "mcp_name": function_name, "name": function_name,
                "title": str(tool.get("title") or name)[:200],
                "description": str(tool.get("description") or "")[:mcp.MCP_TOOL_DESCRIPTION_MAX_CHARS],
                "input_schema": tool.get("input_schema") or {"type": "object"},
                "read_only": mcp.tool_risk(tool) == "read",
                "annotations": tool.get("annotations", {}),
                "annotations_declared": bool(tool.get("annotations_declared")),
                "requires_confirmation": mcp.tool_risk(tool) != "read",
            }
            result[capability_id] = info
    return result


def _manifest_files() -> list[Path]:
    """Only deployment-owned local paths; never paths supplied by a model."""
    files: list[Path] = []
    for item in os.getenv("LUMA_SKILL_PATHS", "").split(os.pathsep):
        if not item.strip():
            continue
        path = Path(item).expanduser().resolve()
        if path.is_file() and path.suffix == ".json":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            raise CapabilityError("Skill configuration is unavailable")
        if len(files) > MAX_MANIFESTS:
            raise CapabilityError("Too many skill manifests")
    return files


def _skills(user_id: str, tools: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in _manifest_files():
        try:
            if path.stat().st_size > 64_000:
                raise ValueError()
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError()
            skill_id = str(manifest.get("id") or "")
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", skill_id):
                raise ValueError()
            instructions = manifest.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > MAX_SKILL_CHARS:
                raise ValueError()
            allowed_users = manifest.get("allowed_users")
            if allowed_users is not None:
                if not isinstance(allowed_users, list) or not all(isinstance(x, str) for x in allowed_users):
                    raise ValueError()
                if user_id not in allowed_users:
                    continue
            references = manifest.get("tools", [])
            if not isinstance(references, list) or len(references) > MAX_TOOLS:
                raise ValueError()
            keywords = manifest.get("keywords", [])
            if not isinstance(keywords, list) or not all(isinstance(x, str) for x in keywords):
                raise ValueError()
            tool_ids: list[str] = []
            permitted = True
            for reference in references:
                if not isinstance(reference, dict):
                    raise ValueError()
                if reference.get("capability_id"):
                    matches = [str(reference["capability_id"])] if reference["capability_id"] in tools else []
                else:
                    matches = [key for key, value in tools.items()
                               if value["connector"] == reference.get("connector_name")
                               and value["tool"] == reference.get("tool")]
                # Ambiguous connector names never choose an arbitrary identity.
                if len(matches) != 1:
                    permitted = False
                    break
                if matches[0] not in tool_ids:
                    tool_ids.append(matches[0])
            if not permitted:
                continue
            identifier = "skill:" + skill_id
            if identifier in result:
                raise ValueError()
            result[identifier] = {
                "id": identifier, "title": str(manifest.get("title") or skill_id)[:200],
                "summary": str(manifest.get("summary") or "")[:240],
                "keywords": [x[:80] for x in keywords][:30],
                "instructions": instructions, "tool_ids": tool_ids,
                "exclude_from_memory": manifest.get("exclude_from_memory") is True,
                "version": _hash({"manifest": manifest,
                                  "tools": {key: tools[key]["version"] for key in tool_ids}}),
            }
        except (OSError, ValueError, TypeError, KeyError):
            raise CapabilityError("Invalid trusted skill configuration") from None
    return result


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9_]+", lowered))
    for span in re.findall(r"[\u3400-\u9fff]+", lowered):
        terms.add(span)
        terms.update(span[index:index + 2] for index in range(len(span) - 1))
    return terms


def search(user_id: str, query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Return summaries only, without schemas, instructions or endpoints."""
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise CapabilityError("A discovery query of 1 to 2000 characters is required")
    limit = max(1, min(12, int(limit)))
    tools = _tools(user_id)
    skills = _skills(user_id, tools)
    candidates = []
    for key, tool in tools.items():
        candidates.append({"capability_id": key, "title": tool["title"],
                           "summary": tool["description"][:240], "kind": "tool",
                           "connector_id": tool["connector_id"], "version": tool["version"]})
    for key, skill in skills.items():
        candidates.append({"capability_id": key, "title": skill["title"],
                           "summary": skill["summary"], "kind": "skill",
                           "version": skill["version"], "_keywords": skill["keywords"]})
    terms = _terms(query)
    ranked = []
    for candidate in candidates:
        title = candidate["title"].lower()
        haystack = " ".join([title, candidate["summary"], candidate["capability_id"],
                             " ".join(candidate.pop("_keywords", []))]).lower()
        score = sum(3 if term in title else 1 for term in terms if term in haystack)
        if score:
            ranked.append((score + (0.25 if candidate["kind"] == "skill" else 0), candidate))
    ranked.sort(key=lambda item: (-item[0], item[1]["capability_id"]))
    return [item for _, item in ranked[:limit]]


def load(user_id: str, capability_ids: list[str]) -> dict[str, Any]:
    if not isinstance(capability_ids, list) or not capability_ids or len(capability_ids) > MAX_TOOLS + MAX_SKILLS:
        raise CapabilityError("Load between 1 and 10 capabilities")
    if not all(isinstance(key, str) for key in capability_ids):
        raise CapabilityError("Invalid capability IDs")
    tools = _tools(user_id)
    skills = _skills(user_id, tools)
    tool_ids: list[str] = []
    selected_skills = []
    for key in dict.fromkeys(capability_ids):
        if key in tools:
            tool_ids.append(key)
        elif key in skills:
            selected_skills.append(skills[key])
            tool_ids.extend(skills[key]["tool_ids"])
        else:
            raise CapabilityError("Capability is unavailable or not authorized")
    tool_ids = list(dict.fromkeys(tool_ids))
    if len(tool_ids) > MAX_TOOLS or len(selected_skills) > MAX_SKILLS:
        raise CapabilityError("Active budget exceeded: 8 tools and 2 skills")
    definitions = []
    mapping = {}
    for key in tool_ids:
        info = tools[key]
        name = info["mcp_name"]
        definitions.append({"type": "function", "function": {
            "name": name, "description": ("[" + str(info["connector"]) + "] " + info["description"])[:1024],
            "parameters": info["input_schema"],
        }})
        mapping[name] = info
    selected_skills = [{key: value for key, value in skill.items() if key != "keywords"}
                       for skill in selected_skills]
    if len(json.dumps({"definitions": definitions, "skills": selected_skills}, ensure_ascii=False)) > MAX_LOAD_CHARS:
        raise CapabilityError("Capability context budget exceeded; load fewer capabilities")
    return {"definitions": definitions, "mapping": mapping, "skills": selected_skills}


def validate(user_id: str, capability_id: str, version: str) -> dict[str, Any]:
    """Recheck the owner, enabled state and revision on every invocation."""
    info = _tools(user_id).get(capability_id)
    if info is None:
        raise CapabilityError("Capability is unavailable or not authorized")
    if not version or info["version"] != version:
        raise CapabilityError("Capability changed; discover and load it again")
    return info


def verify_remote_schema(client: Any, info: dict[str, Any]) -> None:
    """Fail closed on live schema drift. Never retry a business write."""
    raw_tools, server = client.list_tools()
    remote_version = str(server.get("version") or "") if isinstance(server, dict) else ""
    if remote_version and info.get("remote_server_version") and remote_version != info["remote_server_version"]:
        raise CapabilityError("Remote connector version changed; synchronize and load again")
    matches = [item for item in mcp.normalize_tools(raw_tools) if item["name"] == info["tool"]]
    if len(matches) != 1 or tool_fingerprint(matches[0]) != info["remote_schema_version"]:
        raise CapabilityError("Remote schema changed; synchronize the connector and load again")


def validate_arguments(info: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate locally without resolving remote JSON Schema references."""
    from jsonschema import validators

    def remote_reference(value: Any) -> bool:
        if isinstance(value, dict):
            if any(key in {"$ref", "$dynamicRef"} and isinstance(item, str) and not item.startswith("#")
                   for key, item in value.items()):
                return True
            return any(remote_reference(item) for item in value.values())
        return isinstance(value, list) and any(remote_reference(item) for item in value)

    schema = info.get("input_schema") or {"type": "object"}
    try:
        if remote_reference(schema):
            raise ValueError()
        validator = validators.validator_for(schema)
        validator.check_schema(schema)
        if next(validator(schema).iter_errors(arguments), None) is not None:
            raise ValueError()
    except Exception:
        raise CapabilityError("Arguments do not match the loaded tool schema") from None


def bound_result(ctx: Any, value: Any) -> str:
    """Limit model-visible remote output without changing the owner's data."""
    from ..tool_results import tool_result_text
    used = int(getattr(ctx, "capability_result_chars", 0))
    remaining = MAX_TURN_RESULT_CHARS - used
    if remaining < 256:
        return "本轮外部工具结果预算已用完；请缩小查询范围。不能从截断内容猜测结果。"
    text = tool_result_text(value, max_chars=min(MAX_RESULT_CHARS, remaining))
    ctx.capability_result_chars = used + len(text)
    return text
