"""Ground recognized tool syntax in declared solver contracts and reject case literals."""

import ast
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from unicodedata import normalize

from enterprise_ops.agent.model import strict_json


_WORD = r"[A-Za-z_][A-Za-z_0-9]*"
_REFERENCE = re.compile(rf"\{{\s*(?=[\"}}])|(?<![\w.]){_WORD}(?:\.{_WORD})+|(?<![\w.]){_WORD}(?=\()")
_EXPLICIT_TOOL = re.compile(r"\b(?:call|invoke|run|use|execute|tool)\s+(?:the\s+)?[`\"]?$", re.I)
_ARGUMENT_START = re.compile(r"[`]*\s*(?:(?:with|using)\s+(?:arguments?\s+)?[`]*)?", re.I)
_ASSIGNMENT = re.compile(
    rf"""{_WORD}\s*=\s*(?:\[[^\]\n]*\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[+-]?\w+(?:\.\w+)*)"""
)
_ASSIGNMENT_SEPARATOR = re.compile(r"(?:\s*,\s*|\s+and\s+)", re.I)
_PROSE_ARGUMENT = re.compile(
    rf"\b(?:argument|parameter)\s+named\s+`?({_WORD})`?|"
    rf"\b(?:argument|parameter)\s+`({_WORD})`|"
    rf"`({_WORD})`\s+(?:argument|parameter)\b|"
    rf"`({_WORD})`\s+(?:set to|equal to)\b|"
    r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z_0-9]+)\s+(?:argument|parameter)\b",
    re.I,
)
_PROSE_RESULT = re.compile(rf"\breturns?\s+(?:the\s+)?(?:`({_WORD})`|([a-z][a-z0-9]*_[a-z_0-9]+))", re.I)
_CALL = re.compile(r"""\((?:[^()'"\\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')*\)""")
_JSON = json.JSONDecoder()
_REVIEW_FEEDBACK = re.compile(
    r"(?<![\w-])(?:reviewers?|criteria|criterions?|criterion|verdicts?|criterion[_-]ids?|"
    r"task[_-]ids?|req_\d+|eoai_[a-z0-9_]+|unreliable_review|grader(?:s)?|rubric(?:s)?|"
    r"reference[_-](?:grades?|answers?))(?![\w-])",
    re.I,
)
_ENTITY_ID = re.compile(
    r"(?<![\w-])(?:inv|pmt|dsp|tck|c|a)-[A-Z]*\d[\w-]*\b"
    r"|(?<![\w-])d-(?-i:[A-Z])[\w-]+\b"
    r"|(?<![\w-])(?:call|ev|audit|approval|ticket|preview)_[0-9A-F]{8,}\b",
    re.I,
)
_OPAQUE_ARGS = {"account_ref", "cursor", "page_cursor", "preview_token", "idempotency_key"}
_LITERAL_NAMES = {"true": True, "false": False, "null": None}
_RUNTIME_ALIASES = {
    "customer_id": {"account_ref"}, "account_ref": {"customer_id"},
    "cursor": {"next_cursor"}, "page_cursor": {"next_cursor", "cursor"},
    "ticket_id": {"result_id"}, "approval_id": {"result_id"},
    "expected_tenant_revision": {"tenant_revision", "state_revision"},
    "expected_invoice_version": {"invoice_version"}, "expected_policy_version": {"policy_version"},
}
_JSON_TYPES = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "null": (type(None),), "array": (list,),
}


def compact_declarations(declarations: Iterable[Mapping]) -> list[dict]:
    """Return argument contracts and the names of top-level solver result fields."""
    compact = []
    for declaration in declarations:
        arguments = deepcopy(declaration["arguments"])
        arguments.pop("title", None)
        for schema in arguments["properties"].values():
            schema.pop("title", None)
        result = declaration.get("result", {})
        compact.append({
            "name": declaration["name"],
            "schema_version": declaration["schema_version"],
            "arguments": arguments,
            "result_fields": list(declaration.get("result_fields", result.get("properties", ()))),
        })
    return compact


def _id_error(text: str, forbidden: tuple[re.Pattern, ...]) -> str | None:
    if _REVIEW_FEEDBACK.search(text):
        return "Rule refers to evaluation feedback or identifiers"
    if _ENTITY_ID.search(text) or any(pattern.search(text) for pattern in forbidden):
        return "Rule contains a case-specific identifier or literal"
    return None


@dataclass(frozen=True)
class _RuntimeReference:
    """An unquoted named value in a call sketch, to be supplied at execution time."""

    name: str


def _call_value(node: ast.expr) -> object:
    if isinstance(node, ast.Name):
        return _LITERAL_NAMES.get(node.id, _RuntimeReference(node.id))
    if isinstance(node, ast.List):
        return [_call_value(item) for item in node.elts]
    return ast.literal_eval(node)


def _call_arguments(source: str) -> dict:
    call = ast.parse(f"tool{source}", mode="eval").body
    if call.args:
        if len(call.args) == 1 and not call.keywords:
            fields, _ = _json_object(ast.get_source_segment(f"tool{source}", call.args[0]))
            return fields
        raise ValueError("Use named tool arguments")
    args = {item.arg: _call_value(item.value) for item in call.keywords}
    if None in args or len(args) != len(call.keywords):
        raise ValueError("Tool arguments must have distinct names")
    return args


def _json_object(source: str) -> tuple[dict, int]:
    _, end = _JSON.raw_decode(source)
    value = strict_json(source[:end])
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object for tool arguments")
    return value, end


def _value_valid(value: object, schema: Mapping) -> bool:
    if isinstance(value, _RuntimeReference):
        return True
    if "anyOf" in schema:
        return any(_value_valid(value, variant) for variant in schema["anyOf"])
    if type(value) not in _JSON_TYPES[schema["type"]]:
        return False
    if (
        ("const" in schema and value != schema["const"])
        or ("enum" in schema and value not in schema["enum"])
    ):
        return False
    if isinstance(value, str):
        return schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
    if isinstance(value, list):
        return (
            schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value))
            and all(_value_valid(item, schema["items"]) for item in value)
        )
    if type(value) in (int, float):
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    return True


def _argument_error(
    name: str, args: dict, variants: list[Mapping], complete: bool,
    forbidden: tuple[re.Pattern, ...], runtime_fields: set[str],
) -> str | None:
    for variant in variants:
        schema = variant["arguments"]
        properties = schema["properties"]
        version = variant["schema_version"]
        for key, value in args.items():
            if key not in properties:
                return f"{name} does not declare argument {key} in {version}"
            if not _value_valid(value, properties[key]):
                return f"{name} argument {key} does not match its declaration in {version}"
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, _RuntimeReference):
                    allowed_prefixes = ("observed_", "requested_", "returned_", "current_")
                    symbol = item.name.split("_", 1)[1] if item.name.startswith(allowed_prefixes) else item.name
                    if symbol not in runtime_fields or symbol not in {key, *_RUNTIME_ALIASES.get(key, ())}:
                        return f"{name} argument {key} refers to an unknown or incompatible runtime field"
                if isinstance(item, str):
                    if key in _OPAQUE_ARGS or key.endswith(("_id", "_ids")):
                        return f"{name} argument {key} must come from the current request or tool results"
                    error = _id_error(normalize("NFKC", item), forbidden)
                    if error is not None:
                        return error
        if complete and set(schema.get("required", ())) - args.keys():
            return f"{name} is missing required arguments in {version}"
    return None


def _referenced_arguments(text: str, end: int) -> tuple[dict | None, bool, int]:
    start = _ARGUMENT_START.match(text, end).end()
    if text.startswith("(", start):
        call = _CALL.match(text, start)
        if call is None:
            raise ValueError("Use a simple named tool call")
        return _call_arguments(call[0]), True, call.end()
    if text.startswith("{", start):
        value, size = _json_object(text[start:])
        return value, False, start + size
    if re.match(rf"{_WORD}\s*=", text[start:]):
        assignments = []
        while assignment := _ASSIGNMENT.match(text, start):
            assignments.append(assignment[0])
            start = assignment.end()
            separator = _ASSIGNMENT_SEPARATOR.match(text, start)
            if separator is None or not re.match(rf"{_WORD}\s*=", text[separator.end():]):
                return _call_arguments(f"({', '.join(assignments)})"), False, start
            start = separator.end()
        raise ValueError("Use a literal or named runtime value for each tool argument")
    return None, False, end


def _tool_envelope(envelope: dict) -> tuple[str, dict]:
    if envelope.keys() == {"type", "function"} and envelope["type"] == "function":
        envelope = envelope["function"]
    if not isinstance(envelope, dict):
        raise ValueError("Invalid JSON tool call")
    name_key = "name" if "name" in envelope else "tool"
    if envelope.keys() != {name_key, "arguments"}:
        raise ValueError("Associate JSON arguments with a named tool")
    name, args = envelope[name_key], envelope["arguments"]
    if not isinstance(name, str) or not isinstance(args, dict):
        raise ValueError("Invalid JSON tool name or arguments")
    return name, args


def validate_rule(text: str, declarations: Iterable[Mapping], *, forbidden: Iterable[str] = ()) -> str | None:
    """Return a rejection reason for recognized tool syntax or case identifiers.

    Tool syntax includes dotted names in the declared namespaces, explicit
    call phrases, named call sketches, and JSON argument objects or envelopes.
    Each provided contract for a tool must allow the reference. Unstructured
    prose can describe where to obtain runtime values without using call syntax.
    """
    text = normalize("NFKC", text)
    blocked = tuple(
        re.compile(rf"(?<![\w-]){re.escape(normalize('NFKC', item))}(?![\w-])", re.I)
        for item in forbidden if item
    )
    error = _id_error(text, blocked)
    if error is not None:
        return error

    by_name = defaultdict(list)
    runtime_fields = set()
    for declaration in declarations:
        by_name[declaration["name"]].append(declaration)
        runtime_fields.update(declaration["arguments"]["properties"])
        runtime_fields.update(declaration.get("result_fields", declaration.get("result", {}).get("properties", ())))
    namespaces = {name.split(".")[0].casefold() for name in by_name}
    end = 0
    while match := _REFERENCE.search(text, end):
        end = match.end()
        try:
            if match[0].startswith("{"):
                envelope, size = _json_object(text[match.start():])
                name, args = _tool_envelope(envelope)
                end, complete = match.start() + size, True
            else:
                name = match[0]
                root, _, _ = name.partition(".")
                explicit = _EXPLICIT_TOOL.search(text[:match.start()]) or text[match.start() - 1:match.start()] == "`"
                args, complete, end = _referenced_arguments(text, end)
                if name not in by_name and root.casefold() not in namespaces and not (explicit or args is not None):
                    continue
            if name not in by_name:
                return f"Rule references undeclared tool {name}"
            if args is not None:
                error = _argument_error(name, args, by_name[name], complete, blocked, runtime_fields)
                if error is not None:
                    return error
            elif not match[0].startswith("{"):
                clause = re.split(r"[.;\n]", text[end:end + 180], maxsplit=1)[0]
                for reference in _PROSE_ARGUMENT.finditer(clause):
                    key = next(value for value in reference.groups() if value is not None)
                    if any(key not in variant["arguments"]["properties"] for variant in by_name[name]):
                        return f"{name} does not declare argument {key}"
                for reference in _PROSE_RESULT.finditer(clause):
                    key = next(value for value in reference.groups() if value is not None)
                    if any(key not in variant.get("result_fields", variant.get("result", {}).get("properties", ()))
                           for variant in by_name[name]):
                        return f"{name} does not declare result field {key}"
        except (SyntaxError, ValueError, RecursionError):
            return "Rule has malformed tool arguments"
    return None
