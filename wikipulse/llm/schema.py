"""Check a JSON value against a JSON schema (the subset WikiPulse uses).

Ollama constrains the model's output with the schema, but a model can still stop early
(hitting `max_tokens`), and the Anthropic backend only asks for the schema in the prompt.
So every structured answer is checked here before the caller sees it.

Supported keywords: `type` (a name or a list of names), `enum`, `const`, `properties`,
`required`, `additionalProperties`, `items`, `minItems`, `maxItems`, `minLength`,
`maxLength`, `minimum`, `maximum`, `anyOf`. Other keywords are ignored, so a schema using
them is checked less strictly, never more.
"""

from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python, but not a number in JSON
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Every way `value` breaks `schema`, as readable messages (empty list: valid)."""
    if "anyOf" in schema:
        if all(schema_errors(value, option, path) for option in schema["anyOf"]):
            return [f"{path}: matches none of the allowed forms"]
    errors = _type_errors(value, schema, path)
    if errors:
        return errors  # the other checks assume the right type
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} is not one of {schema['enum']!r}"]
    if "const" in schema and value != schema["const"]:
        return [f"{path}: must be {schema['const']!r}"]
    if isinstance(value, dict):
        errors += _object_errors(value, schema, path)
    elif isinstance(value, list):
        errors += _array_errors(value, schema, path)
    elif isinstance(value, str):
        errors += _bounds(len(value), schema, path, "minLength", "maxLength", "length")
    elif _TYPE_CHECKS["number"](value):
        errors += _bounds(value, schema, path, "minimum", "maximum", "value")
    return errors


def _type_errors(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    expected = schema.get("type")
    if expected is None:
        return []
    names = expected if isinstance(expected, list) else [expected]
    if any(_TYPE_CHECKS.get(name, lambda v: True)(value) for name in names):
        return []
    return [f"{path}: expected {' or '.join(names)}, got {type(value).__name__}"]


def _object_errors(value: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    errors = [f"{path}: missing {key!r}" for key in schema.get("required", []) if key not in value]
    properties: dict[str, Any] = schema.get("properties", {})
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        if key in properties:
            errors += schema_errors(item, properties[key], f"{path}.{key}")
        elif extra is False:
            errors.append(f"{path}: unexpected key {key!r}")
        elif isinstance(extra, dict):
            errors += schema_errors(item, extra, f"{path}.{key}")
    return errors


def _array_errors(value: list[Any], schema: dict[str, Any], path: str) -> list[str]:
    errors = _bounds(len(value), schema, path, "minItems", "maxItems", "length")
    if isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{index}]")
    return errors


def _bounds(
    amount: float, schema: dict[str, Any], path: str, low: str, high: str, what: str
) -> list[str]:
    if low in schema and amount < schema[low]:
        return [f"{path}: {what} {amount} is below {schema[low]}"]
    if high in schema and amount > schema[high]:
        return [f"{path}: {what} {amount} is above {schema[high]}"]
    return []
