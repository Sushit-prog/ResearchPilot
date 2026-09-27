from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agent.state import AgentStatus
from app.config import Config
from app.main import build_parser, main, resolve_config, run_query
from app.models.events import EventKind

PLAN_DATA = {
    "goal": "research orbital mechanics",
    "steps": [
        {
            "id": "s1",
            "objective": "search for orbital mechanics sources",
            "tool": "web_search",
            "arguments": {"query": "orbital mechanics"},
            "expected_output": "search results with citable evidence",
        }
    ],
}

CITATIONS = [
    ("s1:2", "https://example.com/a"),
    ("s1:3", "https://example.org/b"),
    ("s1:4", "https://example.net/c"),
]


def plan_json() -> str:
    return json.dumps(PLAN_DATA)


def page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return (
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body>{body}</body></html>"
    )


def html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def healthy_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.tavily.com":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Orbital mechanics primer",
                        "url": "https://example.com/a",
                        "content": "orbital mechanics primer covering Kepler orbits",
                    },
                    {
                        "title": "Orbital mechanics guide",
                        "url": "https://example.org/b",
                        "content": "orbital mechanics guide for satellite missions",
                    },
                    {
                        "title": "Orbital mechanics reference",
                        "url": "https://example.net/c",
                        "content": "orbital mechanics reference on transfer trajectories",
                    },
                ]
            },
        )
    if request.url.path == "/b":
        return html(
            page(
                "Orbital Mechanics Guide",
                "Orbital mechanics guides satellite mission planners through burns.",
                "Hohmann transfers minimize the fuel cost of orbit changes.",
            )
        )
    if request.url.path == "/c":
        return html(
            page(
                "Orbital Mechanics Reference",
                "Orbital mechanics references catalogue trajectory options.",
                "Inclination changes consume the most delta-v of any maneuver.",
            )
        )
    return html(
        page(
            "Orbital Mechanics Primer",
            "Orbital mechanics describes satellite motion under gravity alone.",
            "Kepler's laws shape every closed orbit around a primary body.",
        )
    )


def dead_pages_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.tavily.com":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Orbital mechanics primer",
                        "url": "https://example.com/a",
                        "content": "orbital mechanics primer covering Kepler orbits",
                    },
                    {
                        "title": "Orbital mechanics guide",
                        "url": "https://example.org/b",
                        "content": "orbital mechanics guide for satellite missions",
                    },
                ]
            },
        )
    return httpx.Response(404)


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def offline_config(tmp_path: Path) -> Config:
    return Config.from_env(
        {
            "TAVILY_API_KEY": "tvly-test-key",
            "RESEARCHPILOT_REPORT_DIR": str(tmp_path / "reports"),
            "RESEARCHPILOT_MEMORY": "false",
        }
    )


def test_parser_requires_query() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([])
    assert exc_info.value.code == 2


def test_parser_rejects_unknown_failure_mode() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--query", "x", "--simulate-failure", "explode"])
    assert exc_info.value.code == 2


def test_parser_flags_round_trip() -> None:
    args = build_parser().parse_args(
        [
            "--query",
            "what is X",
            "--output",
            "out",
            "--verbose",
            "--no-memory",
            "--simulate-failure",
            "timeout",
        ]
    )
    assert args.query == "what is X"
    assert args.output == "out"
    assert args.verbose is True
    assert args.no_memory is True
    assert args.simulate_failure == "timeout"


def test_resolve_config_applies_flags() -> None:
    args = build_parser().parse_args(
        ["--query", "x", "--output", "out", "--verbose", "--no-memory"]
    )
    config = resolve_config(args, base=Config())
    assert config.report_dir == "out"
    assert config.verbose is True
    assert config.memory_enabled is False


def test_resolve_config_defaults_untouched() -> None:
    args = build_parser().parse_args(["--query", "x"])
    base = Config()
    config = resolve_config(args, base=base)
    assert config.report_dir == base.report_dir
    assert config.verbose is base.verbose
    assert config.memory_enabled is base.memory_enabled


async def test_run_query_completes_full_pipeline(
    fake_llm, synthesis_json, tmp_path
) -> None:
    args = build_parser().parse_args(["--query", "research orbital mechanics"])
    llm = fake_llm(plan_json(), synthesis_json(CITATIONS))
    state = await run_query(
        args,
        llm=llm,
        client=client_for(healthy_handler),
        config=offline_config(tmp_path),
    )
    assert state.status is AgentStatus.COMPLETED
    assert state.report_path is not None
    report = Path(state.report_path)
    assert report.exists()
    assert "# ResearchPilot Report" in report.read_text(encoding="utf-8")
    assert len(state.evidence) == 3
    assert len(state.sources) == 3
    assert all(result.attempts == 1 for result in state.tool_results)


async def test_run_query_simulated_failure_recovers(
    fake_llm, synthesis_json, tmp_path
) -> None:
    args = build_parser().parse_args(
        ["--query", "research orbital mechanics", "--simulate-failure", "temporary_error"]
    )
    llm = fake_llm(plan_json(), synthesis_json(CITATIONS))
    state = await run_query(
        args,
        llm=llm,
        client=client_for(healthy_handler),
        config=offline_config(tmp_path),
    )
    assert state.status is AgentStatus.COMPLETED
    assert state.report_path is not None
    recovered = [result for result in state.tool_results if result.attempts > 1]
    assert len(recovered) == 1
    assert recovered[0].tool == "web_search"
    kinds = [event.event for event in state.execution_events]
    assert EventKind.RETRY_STARTED in kinds
    assert EventKind.TOOL_SUCCEEDED in kinds
    assert len(state.evidence) == 3


def test_main_returns_0_and_prints_planning_trace(
    fake_llm, synthesis_json, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("RESEARCHPILOT_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCHPILOT_MEMORY", "false")
    for name in (
        "RESEARCHPILOT_LLM_PROVIDER",
        "RESEARCHPILOT_LLM_BASE_URL",
        "RESEARCHPILOT_LLM_MODEL",
        "RESEARCHPILOT_LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    llm = fake_llm(plan_json(), synthesis_json(CITATIONS))
    exit_code = main(
        ["--query", "research orbital mechanics"],
        llm=llm,
        client=client_for(healthy_handler),
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[PLAN] 1 steps:" in captured.out
    assert "[REPORT]" in captured.out
    assert "[DONE] completed" in captured.out
    assert Path(tmp_path, "research-orbital-mechanics.md").exists()


def test_main_returns_1_when_all_pages_fail(
    fake_llm, synthesis_json, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("RESEARCHPILOT_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCHPILOT_MEMORY", "false")
    llm = fake_llm(plan_json())
    exit_code = main(
        ["--query", "research orbital mechanics"],
        llm=llm,
        client=client_for(dead_pages_handler),
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "[FAIL] run ended without a report" in captured.err
    assert "no evidence collected" in captured.err


def test_main_returns_2_without_llm_configuration(monkeypatch, capsys) -> None:
    for name in (
        "RESEARCHPILOT_LLM_PROVIDER",
        "RESEARCHPILOT_LLM_BASE_URL",
        "RESEARCHPILOT_LLM_MODEL",
        "RESEARCHPILOT_LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    exit_code = main(["--query", "x"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "[CONFIG]" in captured.err
    assert "RESEARCHPILOT_LLM_PROVIDER" in captured.err
