from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jevapan.scorer import CategoryScore, ScoredBlock


@dataclass
class Block:
    start: int  # 1始まりの開始行
    end: int  # 1始まりの終了行(含む)
    text: str

    @property
    def lines(self) -> tuple[int, int]:
        return (self.start, self.end)


@dataclass
class Violation:
    start: int
    end: int
    scope: str
    category: str
    severity: str
    probability: float
    text: str

    @property
    def lines(self) -> tuple[int, int]:
        return (self.start, self.end)


@dataclass
class LintResult:
    file: str
    blocks: list[Block]
    block_scores: list["ScoredBlock"]
    doc_scores: dict[str, "CategoryScore"]
    violations: list[Violation]
    skipped: list[dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)
