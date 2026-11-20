import ast
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError
from pydantic_monty import CollectString, Monty, MontyError


class Repair(BaseModel):
    result: str = Field(min_length=1)


class Submission(Repair):
    kind: Literal["python", "text"]
    tests: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = Field(min_length=1, max_length=32)


def json_body(raw):
    lines = raw.strip().splitlines()
    if len(lines) >= 3 and lines[0].strip().lower() in ("```json", "```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return raw


def read_submission(raw, original):
    try:
        if original is None:
            submission = Submission.model_validate_json(json_body(raw))
        else:
            repair = Repair.model_validate_json(json_body(raw))
            submission = original.model_copy(update={"result": repair.result})
    except ValidationError as error:
        raise ValueError("Engineer 返回的 JSON 缺少有效结果或测试。") from error
    if not submission.result.strip():
        raise ValueError("Engineer 返回了空结果。")
    return submission


def run_checks(submission, folder):
    folder.mkdir(parents=True, exist_ok=False)
    filename = "result.py" if submission.kind == "python" else "result.txt"
    with (folder / filename).open("x", encoding="utf-8") as file:
        file.write(submission.result)
    with (folder / "tests.json").open("x", encoding="utf-8") as file:
        json.dump(submission.tests, file, ensure_ascii=False, indent=2)

    checks = []
    output = CollectString(max_bytes=4096)
    run_error = None
    limits = {"max_feed_duration_secs": 1, "max_memory": 16_000_000, "max_total_sleep_secs": 0, "max_recursion_depth": 100}
    with Monty(max_processes=1, request_timeout=8) as pool, pool.checkout(limits=limits) as session:
        try:
            if submission.kind == "python":
                session.feed_run(submission.result + "\nNone", print_callback=output)
            for expression in submission.tests:
                try:
                    ast.parse(expression, mode="eval")
                    session.feed_run(f"assert ({expression}) is True", inputs={"result_text": submission.result}, print_callback=output)
                except (SyntaxError, MontyError) as error:
                    checks.append({"test": expression, "passed": False, "evidence": str(error)[:1000]})
                else:
                    checks.append({"test": expression, "passed": True, "evidence": "Returned True"})
        except MontyError as error:
            run_error = str(error)[:1000]
    return {"passed": run_error is None and all(c["passed"] for c in checks),
            "checks": checks, "output": output.output, "error": run_error}
