"""Evaluate the extraction pipeline against the labelled cases in `evals/cases`.

Requires a running LLM. Usage:

    PYTHONPATH=src uv run --env-file .env python -m datenkatalog_attribute_extractor_tools.run_extraction_eval
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dcc_backend_common.logger import init_logger

from datenkatalog_attribute_extractor.container import Container
from datenkatalog_attribute_extractor.models.extraction import ExtractionRequest
from datenkatalog_attribute_extractor_tools.acroform import read_acroform_fields
from datenkatalog_attribute_extractor_tools.eval_models import CaseScore, EvalCase, score_case

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


def load_cases(case_id: str | None) -> list[EvalCase]:
    """Load the eval cases to run.

    Args:
        case_id: Run only the case with this stem, or all cases when `None`.

    Returns:
        The cases to evaluate, sorted by name.
    """
    paths = sorted(CASES_DIR.glob("*.json"))
    if case_id is not None:
        paths = [path for path in paths if path.stem == case_id]
    return [EvalCase.from_file(path) for path in paths]


async def evaluate(case: EvalCase) -> CaseScore:
    """Run the pipeline over one case and score the result.

    Args:
        case: The labelled case.

    Returns:
        The score for this case.
    """
    service = Container().extraction_service()
    request = ExtractionRequest(
        content=case.pdf_path.read_bytes(),
        filename=case.pdf_path.name,
        media_type="application/pdf",
    )
    response = await service.extract(request)
    return score_case(case, response.fields)


def report(case: EvalCase, score: CaseScore) -> None:
    """Print a human-readable report for one case."""
    widgets = read_acroform_fields(case.pdf_path)
    text_widgets = [item for item in widgets if item.field_type == "/Tx"]

    print(f"\n=== {score.name} ===")
    print(f"  distinct labels     {score.extracted_count} extracted / {score.expected_count} expected")
    print(f"  precision           {score.precision:.2%}")
    print(f"  recall              {score.recall:.2%}")
    print(f"  f1                  {score.f1:.2%}")

    if score.expected_field_count is not None:
        delta = score.total_fields - score.expected_field_count
        print(f"  total fields        {score.total_fields} (expected ~{score.expected_field_count}, {delta:+d})")
    else:
        print(f"  total fields        {score.total_fields}")

    print(f"  acroform widgets    {len(widgets)} ({len(text_widgets)} text) — count context only, not ground truth")

    if score.duplicate_names:
        print(f"  DUPLICATE NAMES     {', '.join(score.duplicate_names)}")
    if score.missing:
        print(f"  missing ({len(score.missing)}): {', '.join(score.missing)}")
    if score.unexpected:
        print(f"  unexpected ({len(score.unexpected)}): {', '.join(score.unexpected)}")


async def run(case_id: str | None, json_out: Path | None) -> int:
    """Evaluate all selected cases.

    Args:
        case_id: Restrict to a single case, or `None` for all.
        json_out: Optional path to write machine-readable scores to.

    Returns:
        A process exit code: non-zero if any case produced duplicate names.
    """
    init_logger()
    cases = load_cases(case_id)
    if not cases:
        print(f"No eval cases found in {CASES_DIR}", file=sys.stderr)
        return 1

    scores: list[CaseScore] = []
    for case in cases:
        score = await evaluate(case)
        report(case, score)
        scores.append(score)

    print("\n=== summary ===")
    mean_recall = sum(score.recall for score in scores) / len(scores)
    mean_precision = sum(score.precision for score in scores) / len(scores)
    print(f"  cases               {len(scores)}")
    print(f"  mean precision      {mean_precision:.2%}")
    print(f"  mean recall         {mean_recall:.2%}")

    if json_out is not None:
        json_out.write_text(json.dumps([score.as_dict() for score in scores], indent=2), encoding="utf-8")
        print(f"  wrote               {json_out}")

    broken = [score.name for score in scores if score.duplicate_names]
    if broken:
        print(f"  FAILED: duplicate names in {', '.join(broken)}")
        return 1
    return 0


def main() -> None:
    """Entry point for the eval harness."""
    parser = argparse.ArgumentParser(description="Evaluate form field extraction against labelled cases.")
    parser.add_argument("--case-id", default=None, help="Run only the case with this file stem")
    parser.add_argument("--json-out", type=Path, default=None, help="Write scores to this JSON file")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(run(args.case_id, args.json_out)))


if __name__ == "__main__":
    main()
