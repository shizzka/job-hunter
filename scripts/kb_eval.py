#!/usr/bin/env python3
"""Offline, content-private comparison of legacy KB context and retrieval.

Usage: python3 scripts/kb_eval.py --profile qa [--questions /local/questions.txt] [--budget 12000]
Each non-comment line is: question → {"all": ["marker"], "any": ["alternative"]}
Every ``all`` marker and at least one ``any`` marker must occur for a hit.
Either group may be omitted. Markers are literal case-insensitive substrings;
Unicode NFKC and whitespace normalization apply to markers and context alike.
Questions and labels remain local. Output contains aggregate counts, optionally
ordinal question IDs (--details), never questions, markers, or retrieved text.
This measures context coverage, not answer quality or factual correctness.
Legacy is measured at its original 6000-character limit and at --budget;
retrieval uses --budget (12000 by default to leave room beyond boundaries).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass(frozen=True)
class Question:
    query: str
    all_markers: tuple[str, ...] = ()
    any_markers: tuple[str, ...] = ()


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def read_questions(path: Path) -> list[Question]:
    questions = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            query, labels = raw.split("→", 1)
            labels = json.loads(labels)
            if not query.strip() or not isinstance(labels, dict) or set(labels) - {"all", "any"}:
                raise ValueError
            for group in labels.values():
                if not isinstance(group, list) or not group or any(
                    not isinstance(item, str) or not normalize(item) for item in group
                ):
                    raise ValueError
            if not labels:
                raise ValueError
            questions.append(Question(query.strip(), tuple(labels.get("all", [])), tuple(labels.get("any", []))))
        except (ValueError, TypeError):
            raise ValueError(f"Invalid question format at line {line_number}") from None
    if not questions:
        raise ValueError("Questions file contains no questions")
    return questions


def score_context(question: Question, context: str) -> dict:
    text = normalize(context)
    all_hits = [normalize(marker) in text for marker in question.all_markers]
    any_hits = [normalize(marker) in text for marker in question.any_markers]
    return {
        "hit": all(all_hits) and (any(any_hits) if any_hits else True),
        "markers_found": sum(all_hits) + sum(any_hits),
        "markers_total": len(all_hits) + len(any_hits),
    }


def evaluate(questions: list[Question], profile_dir: Path, legacy_builder, retriever, *, budget: int = 12000, details: bool = False) -> dict:
    legacy_context = legacy_builder(limit_chars=6000, profile_dir=str(profile_dir))
    same_budget_context = legacy_builder(limit_chars=budget, profile_dir=str(profile_dir)) if budget != 6000 else legacy_context
    totals = {name: {"hits": 0, "markers_found": 0, "markers_total": 0} for name in ("legacy", "legacy_same_budget", "retrieval")}
    rows = []
    for index, question in enumerate(questions, 1):
        contexts = {
            "legacy": legacy_context,
            "legacy_same_budget": same_budget_context,
            "retrieval": retriever(question.query, profile_dir=str(profile_dir), limit_chars=budget),
        }
        row = {"id": f"q{index:03d}"}
        for name, context in contexts.items():
            score = score_context(question, context)
            totals[name]["hits"] += int(score["hit"])
            totals[name]["markers_found"] += score["markers_found"]
            totals[name]["markers_total"] += score["markers_total"]
            row[name] = score["hit"]
        rows.append(row)
    for metrics in totals.values():
        metrics["hit_rate"] = round(metrics["hits"] / len(questions), 4)
        metrics["marker_coverage"] = round(metrics["markers_found"] / metrics["markers_total"], 4)
    result = {"questions": len(questions), "budgets": {"legacy": 6000, "legacy_same_budget": budget, "retrieval": budget}, **totals}
    if details:
        result["details"] = rows
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="Profile name (default uses JOB_HUNTER_HOME directly)")
    parser.add_argument("--questions", type=Path, help="Local questions file; defaults to profile knowledge/kb_eval_questions.txt")
    parser.add_argument("--details", action="store_true", help="Include ordinal question IDs and hit booleans")
    parser.add_argument("--budget", type=int, default=12000, help="Retrieval and same-budget legacy character limit (default: 12000); original legacy always uses 6000")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.profile):
        parser.error("Invalid profile name")
    if args.budget <= 0:
        parser.error("Budget must be positive")
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        import config
        from prompt_blocks import build_knowledge_base_block
        from kb_retrieval import get_knowledge_index, retrieve_knowledge

        root = Path(os.path.expanduser(config.JOB_HUNTER_HOME))
        profile_dir = root if args.profile == "default" else root / "profiles" / args.profile
        knowledge = profile_dir / "knowledge"
        if knowledge.is_symlink():
            raise ValueError("Symlinked knowledge directory is not allowed")
        if not knowledge.is_dir():
            raise ValueError("Profile knowledge directory is missing")
        question_path = (args.questions or knowledge / "kb_eval_questions.txt").expanduser().resolve()
        questions = read_questions(question_path)
        # Both implementations see the same snapshot, with evaluation labels
        # excluded even when --questions names a custom file inside knowledge/.
        # TemporaryDirectory uses private permissions and removes it on exit.
        with tempfile.TemporaryDirectory(prefix="jh-kb-eval-") as temporary:
            snapshot = Path(temporary)
            (snapshot / "knowledge").mkdir()
            for filename in ("facts.json", "candidate_interview.json"):
                source = profile_dir / filename
                if source.is_symlink():
                    raise ValueError("Symlinked profile sources are not allowed")
                if source.is_file():
                    shutil.copyfile(source, snapshot / filename)
            for source in knowledge.iterdir():
                if source.name == "kb_eval_questions.txt" or source.resolve() == question_path:
                    continue
                if source.suffix not in {".md", ".txt"}:
                    continue
                if source.is_symlink():
                    raise ValueError("Symlinked knowledge sources are not allowed")
                if source.is_file():
                    shutil.copyfile(source, snapshot / "knowledge" / source.name)
            result = evaluate(questions, snapshot, build_knowledge_base_block, retrieve_knowledge, budget=args.budget, details=args.details)
            mandatory = [chunk for chunk in get_knowledge_index(snapshot).chunks if chunk.mandatory]
            required = len("\n\n".join(chunk.render() for chunk in mandatory))
            result["mandatory"] = {"chunks": len(mandatory), "chars": required,
                                   "budget_fraction": round(required / args.budget, 4)}
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        # Do not echo exception messages, file paths, source text or queries.
        print(f"Evaluation failed ({type(exc).__name__}); check local inputs and module availability.", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_logging_disable)


if __name__ == "__main__":
    raise SystemExit(main())
