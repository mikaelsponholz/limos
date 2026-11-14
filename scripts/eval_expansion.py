"""Live comparison: python -m scripts.eval_expansion --compare-model MODEL_ID.

By default compare GEMINI_MODEL with --compare-model. Alternatively supply all
model IDs explicitly with --models MODEL_ID MODEL_ID. No model IDs are guessed.
"""

import argparse
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from agent.expansion import build_event_items, expand_event
from agent.intent import helsinki_today, resolve_request
from agent.llm import GeminiLLM, LLMOutputError
from gateway.policy.mandates import load_mandates
from gateway.schemas import IntentItem, RequestCreate
from gateway.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Evaluation:
    preset: str
    model: str
    items: list[IntentItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    latency_seconds: float = 0
    validation: str = "first attempt"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def format_report(results: list[Evaluation], skipped: list[str]) -> str:
    lines = [
        "# Event expansion comparison",
        "",
        "Live ingredient proposals only. No catalogue, offers or orders were requested.",
        "",
    ]
    lines.extend(f"Skipped {_cell(name)}: item-list preset, no event." for name in skipped)
    lines.extend(
        [
            "",
            "| Preset | Model | Items | Latency (s) | Validation |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for result in results:
        lines.append(
            f"| {_cell(result.preset)} | {_cell(result.model)} | {len(result.items)} | "
            f"{result.latency_seconds:.3f} | {_cell(result.validation)} |"
        )
    for result in results:
        lines.extend(["", f"## {_cell(result.preset)} | {_cell(result.model)}", ""])
        lines.extend(
            f"- {_cell(item.description)} ({_cell(item.description_fi)}): "
            f"{item.quantity:g} {item.unit}"
            for item in result.items
        )
        if not result.items:
            lines.append("No merged items.")
        lines.extend(["", "Sanity and assumption notes:", ""])
        lines.extend(f"- {_cell(note)}" for note in result.notes)
        if not result.notes:
            lines.append("- None.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", help="Explicit model IDs to compare")
    parser.add_argument("--compare-model", help="Second model ID alongside GEMINI_MODEL")
    args = parser.parse_args(argv)
    settings = Settings()
    if not settings.gemini_api_key.strip() or not settings.gemini_model.strip():
        print("Set GEMINI_API_KEY and GEMINI_MODEL in the environment before evaluating expansion.")
        return 1
    if args.models:
        models = list(dict.fromkeys(args.models))
    elif args.compare_model:
        models = list(dict.fromkeys([settings.gemini_model, args.compare_model]))
    else:
        parser.error("Provide --compare-model MODEL_ID or --models MODEL_ID MODEL_ID.")
    if any(not model.strip() for model in models):
        parser.error("Model IDs must not be blank.")

    mandates = load_mandates()
    today = helsinki_today(datetime.now(UTC))
    results = []
    skipped = []
    failed = False
    for path in sorted((ROOT / "presets").glob("*.json")):
        preset = json.loads(path.read_text(encoding="utf-8"))
        body = RequestCreate.model_validate_json(json.dumps(preset["request"]))
        if body.event is None:
            skipped.append(preset["preset_id"])
            continue
        intent = resolve_request(body, None, None, today).intent
        assert intent.event is not None
        mandate = mandates.get(body.buyer_id or "")
        for model in models:
            llm = GeminiLLM(replace(settings, gemini_expansion_model=model))
            started = perf_counter()
            try:
                proposal = expand_event(
                    llm, intent.event, intent.dishes, intent.preferences, intent.exclude_allergens
                )
                expanded = build_event_items(
                    proposal.value,
                    intent.event,
                    intent.dishes,
                    intent.preferences,
                    intent.exclude_allergens,
                    waste_percent=mandate.waste_percent if mandate else None,
                    explicit_items=intent.items,
                )
            except LLMOutputError:
                failed = True
                result = Evaluation(
                    preset["preset_id"],
                    model,
                    notes=["Expansion failed validation after the repair retry."],
                    validation="failed after repair retry",
                )
            except Exception:
                # SDK exceptions may contain credentials or remote request details.
                failed = True
                result = Evaluation(
                    preset["preset_id"],
                    model,
                    notes=["Expansion call failed. Check Gemini configuration and connectivity."],
                    validation="call failed",
                )
            else:
                result = Evaluation(
                    preset["preset_id"],
                    proposal.model_id,
                    expanded.items,
                    expanded.assumptions,
                    validation="repair retry" if proposal.repaired else "first attempt",
                )
            results.append(replace(result, latency_seconds=perf_counter() - started))
    report = ROOT / ".runtime" / "eval_expansion.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(format_report(results, skipped), encoding="utf-8")
    print(report)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
