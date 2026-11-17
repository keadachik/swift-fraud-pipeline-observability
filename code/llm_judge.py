"""
LLM-as-a-Judge for SWIFT Prompt Chaining final review.

Discrete labels only (Arize guidance): no 1–100 scores.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from openai import OpenAI

from config import Config


@dataclass
class JudgeResult:
    judge_name: str
    label: str
    rationale: str
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FinalReviewJudge:
    """
    Judges whether a final fraud decision's justification is grounded
    in the SWIFT message (and optional prior analysis), and whether it is clear.
    """

    GROUNDING_LABELS = {"grounded", "not_grounded"}
    CLARITY_LABELS = {"clear", "unclear"}

    def __init__(self, model: Optional[str] = None):
        self.config = Config()
        kwargs = {"api_key": self.config.OPENAI_API_KEY}
        if getattr(self.config, "OPENAI_BASE_URL", ""):
            kwargs["base_url"] = self.config.OPENAI_BASE_URL
        self.client = OpenAI(**kwargs)
        # Prefer a capable judge model when available; fall back to project default.
        self.model = model or "gpt-4o"

    def _chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return json.loads(response.choices[0].message.content or "{}")

    def judge_grounding(
        self,
        message: Dict[str, Any],
        decision: str,
        justification: str,
        prior_analysis: Optional[Dict[str, Any]] = None,
    ) -> JudgeResult:
        """
        Label: grounded | not_grounded

        not_grounded = invents facts (BIC, amount, countries, etc.) not supported
        by the message / prior analysis.
        """
        system_prompt = """You are an evaluation judge for a SWIFT fraud review system.
Decide if the analyst justification is grounded in the provided evidence only.

Definitions:
- grounded: All material claims are supported by the SWIFT message and/or prior analysis.
- not_grounded: Invents or contradicts facts (wrong BIC, amount, country, flags, etc.).

Respond with JSON only:
{"label": "grounded" | "not_grounded", "rationale": "one or two short sentences"}
Do not use any other label. Do not score 1-100."""

        user_prompt = f"""SWIFT message:
{json.dumps(message, indent=2, default=str)}

Prior analysis (may be partial):
{json.dumps(prior_analysis or {}, indent=2, default=str)}

Final decision: {decision}

Justification to judge:
{justification}
"""
        raw = self._chat_json(system_prompt, user_prompt)
        label = str(raw.get("label", "")).strip().lower()
        if label not in self.GROUNDING_LABELS:
            label = "not_grounded"
            raw["normalized"] = True
        return JudgeResult(
            judge_name="final_review_grounding",
            label=label,
            rationale=str(raw.get("rationale", "")),
            raw=raw,
        )

    def judge_clarity(
        self,
        decision: str,
        justification: str,
    ) -> JudgeResult:
        """Label: clear | unclear"""
        system_prompt = """You are an evaluation judge.
Decide if the fraud decision justification is clear to a human reviewer.

Definitions:
- clear: States the decision rationale in understandable language with concrete factors.
- unclear: Vague, contradictory, or missing why the decision was made.

Respond with JSON only:
{"label": "clear" | "unclear", "rationale": "one or two short sentences"}
Do not use any other label. Do not score 1-100."""

        user_prompt = f"""Decision: {decision}

Justification:
{justification}
"""
        raw = self._chat_json(system_prompt, user_prompt)
        label = str(raw.get("label", "")).strip().lower()
        if label not in self.CLARITY_LABELS:
            label = "unclear"
            raw["normalized"] = True
        return JudgeResult(
            judge_name="final_review_clarity",
            label=label,
            rationale=str(raw.get("rationale", "")),
            raw=raw,
        )

    def judge_final_review(
        self,
        message: Dict[str, Any],
        decision: str,
        justification: str,
        prior_analysis: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, JudgeResult]:
        return {
            "grounding": self.judge_grounding(
                message, decision, justification, prior_analysis
            ),
            "clarity": self.judge_clarity(decision, justification),
        }
