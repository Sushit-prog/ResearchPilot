from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from pydantic import ValidationError

from app.agent.verifier import validate_plan
from app.config import Config
from app.errors import PlannerError, PlanValidationError, ResearchPilotError
from app.llm.base import LLMProvider
from app.models.events import EventKind
from app.models.plan import Plan
from app.observability import EventEmitter
from app.tools.base import ToolRegistry

PLAN_CONTRACT = (
    'Return ONLY a JSON object: {"goal": string, "steps": [{"id": string, '
    '"objective": string, "tool": string, "arguments": object, '
    '"expected_output": string}]}'
)

PLAN_TIME_INVARIANT = (
    "Plan-time invariant: every step's arguments must be computable from the goal "
    "alone. Never reference URLs, search results, or numbers only discovered at "
    "runtime — discovered data belongs to deterministic runtime units, not to "
    "plan arguments."
)

NO_PLACEHOLDER_RULE = (
    "Arguments must be literal values computable from the goal — never placeholders "
    "or result references such as 'URL_FROM_STEP_1' or '{step_1.results[0].url}'. "
    "The web_search tool fetches result pages itself, so never plan webpage_fetch "
    "steps to load search results; webpage_fetch accepts only a literal URL that "
    "appears in the goal."
)


def normalize_goal(goal: str) -> str:
    text = unicodedata.normalize("NFC", goal)
    return " ".join(text.split())


def build_plan_prompt(goal: str, schemas: dict[str, dict[str, Any]]) -> tuple[str, str]:
    system = (
        "You decompose a high-level research goal into a short sequence of tool steps.\n"
        f"{PLAN_CONTRACT}\n\n{PLAN_TIME_INVARIANT}\n\n"
        "Registered tools (name -> description and input JSON schema):\n"
        f"{json.dumps(schemas, indent=2)}\n\n"
        "Rules: step ids must be unique; each tool must be one of the registered "
        "names; arguments must validate against that tool's input schema; "
        f"expected_output states what the step must produce. {NO_PLACEHOLDER_RULE}"
    )
    user = f"Goal:\n{goal}"
    return system, user


def _load_plan_json(raw: str) -> dict[str, Any]:
    candidates = [raw.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    candidates.extend(match.strip() for match in fenced)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        raise PlanValidationError("planner output must be a JSON object")
    raise PlanValidationError("planner output is not valid JSON")


def parse_plan(raw: str, registry: ToolRegistry, config: Config) -> Plan:
    data = _load_plan_json(raw)
    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanValidationError(f"plan failed schema validation: {exc}") from exc
    validate_plan(plan, registry, config.max_steps)
    return plan


async def generate_plan(
    llm: LLMProvider,
    registry: ToolRegistry,
    config: Config,
    emitter: EventEmitter,
    normalized_goal: str,
) -> Plan:
    if not normalized_goal:
        raise PlanValidationError("goal must not be empty")
    system, user = build_plan_prompt(normalized_goal, registry.schemas())
    feedback = ""
    for attempt in range(1, config.max_plan_attempts + 1):
        try:
            raw = await llm.complete(system=system, user=user + feedback)
        except ResearchPilotError as exc:
            raise PlannerError(f"planner LLM call failed: {exc}") from exc
        try:
            plan = parse_plan(raw, registry, config)
        except PlanValidationError as exc:
            emitter.emit(EventKind.PLAN_INVALID, attempt=attempt, message=str(exc))
            if attempt >= config.max_plan_attempts:
                raise PlannerError(
                    f"plan still invalid after {config.max_plan_attempts} attempts: {exc}"
                ) from exc
            feedback = (
                "\n\nYour previous output was rejected.\n"
                f"Previous output:\n{raw}\n"
                f"Error:\n{exc}\n"
                f"{NO_PLACEHOLDER_RULE}\n"
                "Return a corrected plan as JSON only."
            )
            continue
        emitter.emit(
            EventKind.PLAN_CREATED,
            message=f"{len(plan.steps)} steps",
            data={
                "goal": plan.goal,
                "steps": [
                    {"id": step.id, "objective": step.objective, "tool": step.tool}
                    for step in plan.steps
                ],
            },
        )
        return plan
    raise PlannerError("plan generation exhausted")
