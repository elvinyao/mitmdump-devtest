"""Bounded regular-expression matching using Pydantic's Rust regex engine."""

from dataclasses import dataclass, field

from pydantic_core import SchemaError, SchemaValidator, ValidationError, core_schema

REGEX_LENGTH_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class PathPattern:
    _validator: SchemaValidator = field(repr=False)

    def fullmatch(self, path: str) -> bool:
        try:
            self._validator.validate_python(path)
        except ValidationError:
            return False
        return True


def compile_path_pattern(pattern: str) -> PathPattern:
    """Preserve full-match semantics without backtracking on request paths."""
    if len(pattern) > REGEX_LENGTH_LIMIT:
        raise ValueError(f"path_regex exceeds {REGEX_LENGTH_LIMIT} characters")
    try:
        # Validate the original expression before interpolation: unmatched parentheses
        # must not escape the wrapper and turn full matching into an alternative.
        SchemaValidator(core_schema.str_schema(pattern=pattern, regex_engine="rust-regex"))
        validator = SchemaValidator(
            core_schema.str_schema(pattern=rf"\A(?:{pattern})\z", regex_engine="rust-regex")
        )
    except SchemaError:
        # The underlying error includes the supplied pattern, which may contain secrets.
        raise ValueError(
            "invalid or unsupported path_regex; lookaround and backreferences are not supported"
        ) from None
    return PathPattern(validator)
