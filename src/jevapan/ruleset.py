from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class Severity(StrEnum):
    error = "error"
    warning = "warning"
    info = "info"


class Scope(StrEnum):
    block = "block"
    document = "document"
    both = "both"


class Category(BaseModel):
    """ルールカテゴリ。`levels` を持つものは完全定義(description 必須)、
    持たないものは extends 元への部分上書き(patch)として解釈する。"""

    name: str
    enabled: bool = True
    scope: Scope = Scope.block
    severity: Severity = Severity.warning
    threshold: float = 1.5
    description: str = ""
    levels: list[str] = Field(default_factory=list)
    levels_document: list[str] | None = None
    locate: str | None = None

    @field_validator("levels")
    @classmethod
    def _levels_len(cls, v: list[str]) -> list[str]:
        if v and not 2 <= len(v) <= 5:
            raise ValueError("levels must have 2-5 entries")
        return v

    @model_validator(mode="after")
    def _description_required_with_levels(self) -> "Category":
        if self.levels and not self.description:
            raise ValueError("description is required when levels are defined")
        return self

    @property
    def is_complete(self) -> bool:
        """完全定義(親を必要としない)か。"""
        return bool(self.levels) and bool(self.description)


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


PRESET_DIR = Path(__file__).parent / "rulesets"


def resolve_preset(name: str) -> Path:
    """プリセット名またはファイルパスを Path に解決する。"""
    p = Path(name)
    if p.suffix in (".yaml", ".yml") or p.exists():
        if not p.exists():
            raise ValueError(f"ruleset not found: {name}")
        return p.resolve()
    cand = PRESET_DIR / f"{name}.yaml"
    if not cand.exists():
        raise ValueError(f"unknown ruleset preset: {name}")
    return cand


def merge_rulesets(parent: Ruleset, child: Ruleset) -> Ruleset:
    """カテゴリを name でキーにマージする。子は明示したフィールドのみで親を上書きする。
    親に存在しない不完全カテゴリ(patch)はエラー。"""
    merged = {c.name: c for c in parent.categories}
    for c in child.categories:
        if c.name in merged:
            merged[c.name] = merged[c.name].model_copy(
                update=c.model_dump(exclude_unset=True)
            )
        else:
            if not c.is_complete:
                raise ValueError(
                    f"category '{c.name}' is incomplete: "
                    "new categories require description and levels"
                )
            merged[c.name] = c
    return Ruleset(categories=list(merged.values()))


def _resolve_extends(name: str, base_dir: Path) -> Path:
    """extends 参照を解決する。まず base_dir 相対のファイル、次にプリセット名。"""
    cand = Path(name)
    if not cand.is_absolute():
        cand = base_dir / cand
    if not cand.exists() and cand.suffix not in (".yaml", ".yml"):
        cand = cand.with_suffix(".yaml")
    if cand.exists():
        return cand.resolve()
    return resolve_preset(name)


def load_ruleset(path: Path, _seen: frozenset[Path] = frozenset()) -> Ruleset:
    path = path.resolve()
    if path in _seen:
        raise ValueError(f"ruleset extends cycle: {path}")
    rs = parse_ruleset(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}, str(path)
    )
    if rs.extends:
        parent = load_ruleset(_resolve_extends(rs.extends, path.parent), _seen | {path})
        return merge_rulesets(parent, rs)
    return merge_rulesets(Ruleset(), rs)


def _load_user_ruleset(cfg: Path) -> Ruleset:
    user = parse_ruleset(
        yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}, str(cfg)
    )
    if user.extends:
        user = load_ruleset(cfg)
    return user


def load_effective_ruleset(
    ruleset: str | None, cwd: Path, config_path: Path | None = None
) -> Ruleset:
    """--ruleset(既定 base)を読み、cwd から遡って jevapan.yaml を見つけたら
    その上にマージする。ユーザー yaml に extends が無ければ選択中の
    ルールセットを継承する。config_path 指定時は探索をスキップする。"""
    base = load_ruleset(resolve_preset(ruleset or "base"))
    if config_path is not None:
        return merge_rulesets(base, _load_user_ruleset(config_path))
    for d in [cwd, *cwd.parents]:
        for name in ("jevapan.yaml", ".jevapan.yaml"):
            cfg = d / name
            if cfg.exists():
                return merge_rulesets(base, _load_user_ruleset(cfg))
    return base
