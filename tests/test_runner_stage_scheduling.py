import ast
from pathlib import Path


def _reservation_returns() -> dict[int, list[str] | None]:
    tree = ast.parse(Path("runner.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_semantic_stage_reservation"
    )

    found: dict[int, list[str] | None] = {}
    for node in function.body:
        if not isinstance(node, ast.If):
            continue
        comparison = node.test
        if not (
            isinstance(comparison, ast.Compare)
            and isinstance(comparison.left, ast.Name)
            and comparison.left.id == "worker_index"
            and len(comparison.comparators) == 1
            and isinstance(comparison.comparators[0], ast.Constant)
        ):
            continue
        worker_index = int(comparison.comparators[0].value)
        return_node = next(
            child for child in node.body if isinstance(child, ast.Return)
        )
        found[worker_index] = ast.literal_eval(return_node.value)
    return found


def test_context_worker_reserves_resolver_stage():
    assert _reservation_returns()[0] == ["resolve_claim_context"]


def test_materialization_worker_reserves_normalization_capacity():
    assert _reservation_returns()[1] == [
        "normalize_proposition",
        "project_character_knowledge",
    ]


def test_claim_api_supports_stage_filtering():
    tree = ast.parse(Path("pipeline/jobs.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "fetch_next_job"
    )
    keyword_only = [arg.arg for arg in function.args.kwonlyargs]
    assert "job_types" in keyword_only
