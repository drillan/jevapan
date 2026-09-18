import asyncio
from dataclasses import dataclass
from typing import Any

from typesafe_sdk import JSONContent, Noul, Score

MAX_QUESTIONS_PER_REQUEST = 50

Questions = dict[str, Noul | Score]


@dataclass
class ScoreResult:
    score: float
    confidence: float


@dataclass
class Engine:
    client: Any  # AsyncTypeSafeClient(テストではモックを差す)
    sem: asyncio.Semaphore | None

    async def _call(self, state: JSONContent, questions: Questions) -> Any:
        if self.sem:
            async with self.sem:
                return await self.client.system_one(state=state, questions=questions)
        return await self.client.system_one(state=state, questions=questions)

    async def noul(self, state: JSONContent, instructions: str) -> float:
        resp = await self._call(state, {"q": Noul(instructions=instructions)})
        return float(resp.answers["q"].noul)

    async def score(
        self, state: JSONContent, instructions: str, levels: list[str]
    ) -> ScoreResult:
        resp = await self._call(
            state, {"q": Score(instructions=instructions, criteria=levels)}
        )
        a = resp.answers["q"]
        return ScoreResult(score=a.score, confidence=a.confidence)

    async def noul_batch(
        self, state: JSONContent, questions: dict[str, str]
    ) -> dict[str, float]:
        out: dict[str, float] = {}
        keys = list(questions)
        for i in range(0, len(keys), MAX_QUESTIONS_PER_REQUEST):
            chunk = keys[i : i + MAX_QUESTIONS_PER_REQUEST]
            resp = await self._call(
                state, {k: Noul(instructions=questions[k]) for k in chunk}
            )
            for k in chunk:
                out[k] = float(resp.answers[k].noul)
        return out

    async def score_batch(
        self, state: JSONContent, questions: dict[str, tuple[str, list[str]]]
    ) -> dict[str, ScoreResult]:
        out: dict[str, ScoreResult] = {}
        keys = list(questions)
        for i in range(0, len(keys), MAX_QUESTIONS_PER_REQUEST):
            chunk = keys[i : i + MAX_QUESTIONS_PER_REQUEST]
            resp = await self._call(
                state,
                {
                    k: Score(
                        instructions=questions[k][0],
                        criteria=questions[k][1],
                    )
                    for k in chunk
                },
            )
            for k in chunk:
                a = resp.answers[k]
                out[k] = ScoreResult(score=a.score, confidence=a.confidence)
        return out
