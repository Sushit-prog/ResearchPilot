from __future__ import annotations

import json

import pytest

from app.agent.orchestrator import Orchestrator
from app.config import Config
from app.errors import PlannerError

SECRET = "sk-super-secret-key-123"


async def test_api_key_never_reaches_events_or_console(
    monkeypatch,
    capsys,
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    evidence_handler,
) -> None:
    monkeypatch.setenv("RESEARCHPILOT_LLM_API_KEY", SECRET)
    monkeypatch.setenv("RESEARCHPILOT_MEMORY", "false")
    config = Config.from_env()
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == SECRET

    llm = fake_llm(valid_plan_json)
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=config,
        clock=fixed_clock,
    )
    state = await orchestrator.run()

    event_dump = json.dumps(
        [event.model_dump(mode="json") for event in state.execution_events],
        default=str,
    )
    console_output = capsys.readouterr().out
    assert SECRET not in event_dump
    assert SECRET not in console_output
    assert SECRET not in repr(state.model_dump())


async def test_planner_failure_state_carries_no_secret(
    monkeypatch, fake_llm, registry, fixed_clock, evidence_handler
) -> None:
    monkeypatch.setenv("RESEARCHPILOT_LLM_API_KEY", SECRET)
    monkeypatch.setenv("RESEARCHPILOT_MEMORY", "false")
    config = Config.from_env()
    llm = fake_llm("bad", "bad", "bad")
    orchestrator = Orchestrator(
        "What is X?",
        llm=llm,
        registry=registry,
        handler=evidence_handler,
        config=config,
        clock=fixed_clock,
    )
    with pytest.raises(PlannerError):
        await orchestrator.run()
    dump = orchestrator.state.model_dump_json()
    assert SECRET not in dump
