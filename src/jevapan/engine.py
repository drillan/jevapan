import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import JSONContent, Noul, Score

MAX_QUESTIONS_PER_REQUEST = 50

Questions = dict[str, Noul | Score]

# 1 API リクエストごとの usage/model を記録するログ。lint_text がファイル
# ごとに set するため、同一 Engine を共有する並列実行でも帰属が壊れない。
# score_batch はリクエスト全体の usage を各回答に複製するため、集計は
# 必ずこのログ(リクエスト単位)から行う
USAGE_LOG: ContextVar[list["CallRecord"] | None] = ContextVar(
    "jevapan_usage_log", default=None
)


def payload_chars(state: Any, questions: dict[str, Any] | None = None) -> int:
    """実ペイロードの文字数概算(state + questions の JSON 化長)。

    予算設計の根拠: 日本語は概ね 1 文字 ≈ 1 token で、state + 質問 +
    回答を合わせたリクエスト全体がモデルの入力制約に収まる必要がある。
    各段の上限(32000字、境界ウィンドウは共有前提で16000字)は一般的な
    コンテキスト上限に対する保守的な頭金として設計している。"""
    total = len(json.dumps(state, ensure_ascii=False))
    if questions:
        total += len(json.dumps(questions, ensure_ascii=False))
    return total


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ScoreResult:
    score: float
    confidence: float
    model: str = ""
    usage: Usage = field(default_factory=Usage)


@dataclass
class NoulResult:
    """noul/noul_batch の結果。確率に加えて model と usage を保持する
    (閾値評価の再現性のため)。"""

    probs: dict[str, float]
    model: str = ""
    usage: Usage = field(default_factory=Usage)


def _usage_of(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
    )


def _model_of(resp: Any) -> str:
    return str(getattr(resp, "model", "") or "")


@dataclass
class CallRecord:
    """1 API リクエスト分の記録。stage は Engine メソッド名
    (noul/score/noul_batch/score_batch)。"""

    stage: str
    model: str
    usage: Usage


def _record_call(stage: str, resp: Any) -> None:
    log = USAGE_LOG.get()
    if log is not None:
        log.append(CallRecord(stage, _model_of(resp), _usage_of(resp)))


@dataclass
class Engine:
    client: Any  # AsyncTypeSafeClient(テストではモックを差す)
    sem: asyncio.Semaphore | None

    async def _call(self, state: JSONContent, questions: Questions) -> Any:
        if self.sem:
            async with self.sem:
                return await self.client.system_one(state=state, questions=questions)
        return await self.client.system_one(state=state, questions=questions)

    async def noul(self, state: JSONContent, instructions: str) -> NoulResult:
        resp = await self._call(state, {"q": Noul(instructions=instructions)})
        _record_call("noul", resp)
        return NoulResult(
            probs={"q": float(resp.answers["q"].noul)},
            model=_model_of(resp),
            usage=_usage_of(resp),
        )

    async def score(
        self, state: JSONContent, instructions: str, levels: list[str]
    ) -> ScoreResult:
        resp = await self._call(
            state, {"q": Score(instructions=instructions, criteria=levels)}
        )
        _record_call("score", resp)
        a = resp.answers["q"]
        return ScoreResult(
            score=a.score,
            confidence=a.confidence,
            model=_model_of(resp),
            usage=_usage_of(resp),
        )

    async def noul_batch(
        self, state: JSONContent, questions: dict[str, str]
    ) -> NoulResult:
        probs: dict[str, float] = {}
        usage = Usage()
        model = ""
        keys = list(questions)
        for i in range(0, len(keys), MAX_QUESTIONS_PER_REQUEST):
            chunk = keys[i : i + MAX_QUESTIONS_PER_REQUEST]
            resp = await self._call(
                state, {k: Noul(instructions=questions[k]) for k in chunk}
            )
            for k in chunk:
                probs[k] = float(resp.answers[k].noul)
            _record_call("noul_batch", resp)
            u = _usage_of(resp)
            usage.input_tokens += u.input_tokens
            usage.output_tokens += u.output_tokens
            model = _model_of(resp) or model
        return NoulResult(probs=probs, model=model, usage=usage)

    async def score_batch(
        self, state: JSONContent, questions: dict[str, tuple[str, list[str]]]
    ) -> dict[str, ScoreResult]:
        """注意: 各 ScoreResult.usage はリクエスト全体の usage の複製。
        合算すると重複計上になるため、集計には使わず USAGE_LOG /
        LintResult.calls(リクエスト単位)から行うこと。"""
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
            _record_call("score_batch", resp)
            for k in chunk:
                a = resp.answers[k]
                out[k] = ScoreResult(
                    score=a.score,
                    confidence=a.confidence,
                    model=_model_of(resp),
                    usage=_usage_of(resp),
                )
        return out
