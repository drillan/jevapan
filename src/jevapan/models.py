from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jevapan.engine import CallRecord
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
    """違反レコード。結果種別の契約:
    - locate 由来: probability に Noul の yes 確率、score/confidence は None
    - locate 未指定 flag(Score 由来): probability は None、
      score/confidence に Score の値を保持する。
      Score confidence は水準分布の集中度であり違反確率ではないので
      probability に流用しない。"""

    start: int
    end: int
    scope: str
    category: str
    severity: str
    probability: float | None
    text: str
    col: int = 0  # 行内の文字オフセット(0始まり)。同一行の別文を区別する
    score: float | None = None
    confidence: float | None = None

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
    # 1 API リクエスト=1 CallRecord。usage 集計はここから行う
    # (ScoreResult.usage はリクエスト単位で複製されているため使わない)
    calls: list["CallRecord"] = field(default_factory=list)
