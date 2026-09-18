from dataclasses import dataclass


@dataclass
class Block:
    start: int  # 1始まりの開始行
    end: int  # 1始まりの終了行(含む)
    text: str

    @property
    def lines(self) -> tuple[int, int]:
        return (self.start, self.end)
