"""Steps 7b-9: AI exception agent, output validation, and bounded retry.

The agent is confined to one job: given a validated project record and retrieved
contract sections, return a structured recommendation that cites the contract.
All routing, retries and stop conditions stay in code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from . import config
from .contracts import ContractLibrary, ContractSection
from .state import ProjectRecord


class Citation(BaseModel):
    section_id: str
    quote: str


class AgentRecommendation(BaseModel):
    exception_classification: str
    explanation: str
    recommendation: str
    recommended_billable_hours: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    citations: list[Citation] = Field(min_length=1)


# Sent to the API as the structured-output schema. Numeric bounds and the
# non-empty citation list are enforced afterwards by AgentRecommendation.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "exception_classification": {"type": "string"},
        "explanation": {"type": "string"},
        "recommendation": {"type": "string"},
        "recommended_billable_hours": {"type": "number"},
        "confidence": {"type": "number"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"section_id": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["section_id", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "exception_classification",
        "explanation",
        "recommendation",
        "recommended_billable_hours",
        "confidence",
        "citations",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the billing exception analyst for an engineering services firm.
You receive one project's reconciled hours and units for a billing period, the client's
billing rules, and excerpts from the client's contract. Decide how many hours should be
billed and explain why in terms a billing supervisor can verify.

Base the recommendation only on the contract excerpts provided. Cite every section you
rely on by its section_id with a short verbatim quote. If the excerpts do not settle the
question, say so in the explanation and give a low confidence. A supervisor reviews every
recommendation before anything is invoiced."""


class TransientModelError(Exception):
    """Timeout, rate limit, connection or server error from the model provider."""


class ModelOutputError(Exception):
    """The model answered, but not with something usable (refusal, empty output)."""


@dataclass
class LLMResponse:
    text: str
    usage: dict = field(default_factory=dict)


class AnthropicLLM:
    """Live model call through the Anthropic API with JSON-schema-constrained output."""

    def __init__(self, model: str | None = None):
        import anthropic

        self._anthropic = anthropic
        # Retries are counted by the workflow (max 3), so the SDK's own retries are off.
        self.client = anthropic.Anthropic(max_retries=0, timeout=120.0)
        self.model = model or config.model_name()

    def complete(self, system: str, user: str, context: dict) -> LLMResponse:
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                # Re-route to another model if this one declines on policy grounds.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (a.APITimeoutError, a.APIConnectionError, a.RateLimitError, a.InternalServerError) as exc:
            raise TransientModelError(str(exc)) from exc
        except a.APIStatusError as exc:
            # Auth, permission or bad-request errors: counted as a failed attempt, then escalated.
            raise ModelOutputError(f"API error {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise ModelOutputError("model declined the request")
        if response.stop_reason == "max_tokens":
            raise ModelOutputError("model output was cut off at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise ModelOutputError("model returned no text")
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        return LLMResponse(text=text, usage=usage)


class MockLLM:
    """Deterministic stand-in for the model so the pipeline runs without an API key.

    `failures` scripts the first calls to fail, for exercising the retry path:
    "api_error", "invalid_json", "missing_field" or "no_citation".
    """

    def __init__(self, failures: list[str] | None = None):
        self.failures = list(failures or [])
        self.calls = 0

    def complete(self, system: str, user: str, context: dict) -> LLMResponse:
        self.calls += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure == "api_error":
                raise TransientModelError("simulated timeout")
            if failure == "invalid_json":
                return LLMResponse(text="Recommend capping hours.")
            body = self._recommendation(context)
            if failure == "missing_field":
                body.pop("recommended_billable_hours")
            if failure == "no_citation":
                body["citations"] = []
            return LLMResponse(text=json.dumps(body))
        return LLMResponse(text=json.dumps(self._recommendation(context)))

    @staticmethod
    def _recommendation(context: dict) -> dict:
        project, rules = context["project"], context["billing_rules"]
        sections = {s["section_id"]: s for s in context["contract_sections"]}
        section = sections.get("4.2") or next(iter(sections.values()))
        cap = round(project["units"] * rules["complex_cap_hours_per_unit"], 2)
        billable = min(project["hours"], cap)
        return {
            "exception_classification": "overrun_beyond_capped_band",
            "explanation": (
                f"Recorded {project['hours']} h for {project['units']} units "
                f"({project['hours_per_unit']} h/unit) far exceeds the "
                f"{rules['standard_hours_per_unit']} h/unit standard. Section {section['section_id']} "
                f"allows billing up to {rules['complex_cap_hours_per_unit']} h/unit for complex "
                "make-ready work with supervisor approval; hours above that are absorbed."
            ),
            "recommendation": f"Cap billable hours at {billable} and attach make-ready justification.",
            "recommended_billable_hours": billable,
            "confidence": 0.82,
            "citations": [{"section_id": section["section_id"], "quote": section["text"][:160]}],
        }


@dataclass
class AgentAttempt:
    attempt: int
    ok: bool
    error: str | None = None
    usage: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    recommendation: AgentRecommendation | None
    attempts: list[AgentAttempt]
    sections: list[ContractSection]

    @property
    def escalated(self) -> bool:
        return self.recommendation is None


def build_context(record: ProjectRecord, rules: dict, sections: list[ContractSection]) -> dict:
    return {
        "project": {
            "project_id": record.project_id,
            "client_id": record.client_id,
            "unit_type": record.unit_type,
            "hours": record.hours,
            "units": record.units,
            "hours_per_unit": record.hours_per_unit,
            "variance_vs_standard": record.variance,
            "exception_details": record.exception_details,
        },
        "billing_rules": rules,
        "contract_sections": [
            {"section_id": s.section_id, "title": s.title, "text": s.text} for s in sections
        ],
    }


def validate_output(text: str, allowed_sections: set[str]) -> AgentRecommendation:
    """Step 8: schema check, required fields, and a citation to a retrieved section."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"output is not valid JSON ({exc.msg})") from exc
    try:
        result = AgentRecommendation.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'output'}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"output failed schema validation ({problems})") from exc
    unknown = [c.section_id for c in result.citations if c.section_id not in allowed_sections]
    if unknown:
        raise ValueError(f"citations reference sections that were not retrieved: {unknown}")
    return result


class ExceptionAgent:
    def __init__(self, llm, contracts: ContractLibrary, max_attempts: int = config.MAX_AI_ATTEMPTS):
        self.llm = llm
        self.contracts = contracts
        self.max_attempts = max_attempts

    def run(self, record: ProjectRecord, rules: dict) -> AgentResult:
        query = f"overrun variance cap hours per unit complex make-ready {record.exception_details or ''}"
        sections = self.contracts.retrieve(record.client_id, query, k=config.CONTRACT_SECTIONS_TO_RETRIEVE)
        context = build_context(record, rules, sections)
        allowed = {s.section_id for s in sections}
        base_prompt = (
            "Reconcile this project and recommend billable hours.\n\n"
            f"{json.dumps(context, indent=2)}"
        )

        attempts: list[AgentAttempt] = []
        feedback = ""
        for number in range(1, self.max_attempts + 1):
            prompt = base_prompt + feedback
            try:
                response = self.llm.complete(SYSTEM_PROMPT, prompt, context)
                recommendation = validate_output(response.text, allowed)
            except TransientModelError as exc:
                attempts.append(AgentAttempt(number, False, f"transient model error: {exc}"))
                continue
            except (ModelOutputError, ValueError) as exc:
                attempts.append(AgentAttempt(number, False, str(exc)))
                # Retry with an improved prompt that names what was wrong.
                feedback = (
                    f"\n\nYour previous answer was rejected: {exc}. Return JSON matching the schema, "
                    "with at least one citation to a section_id from contract_sections."
                )
                continue
            attempts.append(AgentAttempt(number, True, usage=response.usage))
            return AgentResult(recommendation, attempts, sections)
        return AgentResult(None, attempts, sections)
