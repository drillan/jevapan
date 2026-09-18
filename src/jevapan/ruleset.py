from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator


class Severity(StrEnum):
    error = "error"
    warning = "warning"
    info = "info"


class Scope(StrEnum):
    block = "block"
    document = "document"
    both = "both"


class Category(BaseModel):
    name: str
    enabled: bool = True
    scope: Scope = Scope.block
    severity: Severity = Severity.warning
    threshold: float = 1.5
    description: str
    levels: list[str]
    levels_document: list[str] | None = None
    locate: str | None = None

    @field_validator("levels")
    @classmethod
    def _levels_len(cls, v: list[str]) -> list[str]:
        if not 2 <= len(v) <= 5:
            raise ValueError("levels must have 2-5 entries")
        return v


class Ruleset(BaseModel):
    version: int = 1
    extends: str | None = None
    categories: list[Category] = Field(default_factory=list)


def parse_ruleset(data: dict[str, Any], source: str) -> Ruleset:
    try:
        return Ruleset.model_validate(data)
    except ValidationError as e:
        cats = data.get("categories")
        cats = cats if isinstance(cats, list) else []

        def _loc(part: object) -> str:
            if (
                isinstance(part, int)
                and part < len(cats)
                and isinstance(cats[part], dict)
            ):
                return str(cats[part].get("name", part))
            return str(part)

        lines = []
        for err in e.errors():
            loc = ".".join(_loc(part) for part in err["loc"])
            lines.append(f"  {loc}: {err['msg']}")
        raise ValueError(f"invalid ruleset {source}:\n" + "\n".join(lines)) from e
