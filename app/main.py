from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from app.agent.orchestrator import Orchestrator
from app.agent.researcher import Researcher
from app.agent.state import AgentState, AgentStatus
from app.config import Config
from app.errors import ConfigurationError, ResearchPilotError
from app.llm import build_llm
from app.llm.base import LLMProvider
from app.observability import ConsoleEventSink, EventEmitter, InMemoryEventSink
from app.tools.armed import FAILURE_MODES, ArmedTool, FailureArm
from app.tools.base import Tool, ToolRegistry
from app.tools.calculator import CalculatorTool
from app.tools.failure_simulator import FailureSimulatorTool
from app.tools.web_search import WebSearchTool
from app.tools.webpage_fetch import WebpageFetchTool

_HTTP_TIMEOUT_S = 10.0


def build_tool_registry(
    config: Config,
    *,
    client: httpx.AsyncClient | None = None,
    simulate_failure: str | None = None,
) -> ToolRegistry:
    """Register the four tools; optionally arm --simulate-failure (§12).

    The arm is shared between the two network tools so the induced failure
    lands on the run's first network call, whichever tool that turns out to be.
    """
    if config.search.provider != "tavily":
        raise ConfigurationError(
            f"unsupported search provider: {config.search.provider!r} (supported: 'tavily')"
        )
    api_key = config.search.api_key.get_secret_value() if config.search.api_key else None
    shared = client or httpx.AsyncClient(
        timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        follow_redirects=True,
        max_redirects=20,
    )
    search: Tool[Any, Any] = WebSearchTool(api_key=api_key, client=shared)
    fetch: Tool[Any, Any] = WebpageFetchTool(client=shared)
    if simulate_failure is not None:
        arm = FailureArm()
        search = ArmedTool(search, simulate_failure, arm)
        fetch = ArmedTool(fetch, simulate_failure, arm)
    registry = ToolRegistry()
    registry.register(search)
    registry.register(fetch)
    registry.register(CalculatorTool())
    registry.register(FailureSimulatorTool())
    return registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-agent",
        description=(
            "ResearchPilot — autonomous research agent: plans, gathers cited "
            "evidence, recovers from tool failures, writes a structured report."
        ),
    )
    parser.add_argument(
        "--query",
        required=True,
        metavar="GOAL",
        help="high-level research goal",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        default=None,
        help="directory for the generated report (default: reports/ or RESEARCHPILOT_REPORT_DIR)",
    )
    parser.add_argument(
        "--simulate-failure",
        choices=FAILURE_MODES,
        default=None,
        dest="simulate_failure",
        help="arm a one-shot induced failure on the first network tool call (demo)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every execution event",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        dest="no_memory",
        help="skip the SQLite memory lookup and insert",
    )
    return parser


def resolve_config(args: argparse.Namespace, base: Config | None = None) -> Config:
    """Flags override environment: --output → report_dir, --verbose → verbose,
    --no-memory → memory_enabled=False."""
    config = Config.from_env() if base is None else base
    updates: dict[str, object] = {}
    if args.output is not None:
        updates["report_dir"] = args.output
    if args.verbose:
        updates["verbose"] = True
    if args.no_memory:
        updates["memory_enabled"] = False
    return config.model_copy(update=updates) if updates else config


async def run_query(
    args: argparse.Namespace,
    *,
    llm: LLMProvider | None = None,
    client: httpx.AsyncClient | None = None,
    config: Config | None = None,
) -> AgentState:
    """One end-to-end run: config → provider → registry → orchestrator.

    llm/client/config are injectable seams for offline tests; production paths
    go through build_llm and a real httpx client.
    """
    cfg = config if config is not None else resolve_config(args)
    provider = llm if llm is not None else build_llm(cfg)
    registry = build_tool_registry(
        cfg, client=client, simulate_failure=args.simulate_failure
    )
    emitter = EventEmitter(
        lambda: datetime.now(timezone.utc),
        sinks=[ConsoleEventSink(verbose=cfg.verbose)],
    )
    researcher = Researcher(registry=registry, config=cfg, emitter=emitter)
    orchestrator = Orchestrator(
        args.query,
        llm=provider,
        registry=registry,
        handler=researcher,
        config=cfg,
        emitter=emitter,
    )
    emitter.add(InMemoryEventSink(orchestrator.state.execution_events))
    return await orchestrator.run()


def main(
    argv: Sequence[str] | None = None,
    *,
    llm: LLMProvider | None = None,
    client: httpx.AsyncClient | None = None,
    config: Config | None = None,
) -> int:
    """CLI entry point. Exit codes: 0 report written, 1 no report, 2 bad usage."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        state = asyncio.run(
            run_query(args, llm=llm, client=client, config=config)
        )
    except ConfigurationError as exc:
        print(f"[CONFIG] {exc}", file=sys.stderr)
        return 2
    except ResearchPilotError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    if state.status is AgentStatus.COMPLETED and state.report_path:
        return 0
    reason = state.warnings[-1] if state.warnings else f"status {state.status.value}"
    print(f"[FAIL] run ended without a report ({reason})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
