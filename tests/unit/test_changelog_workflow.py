"""Tests for the ``Changelog`` GitHub Actions workflow.

Dependabot never writes towncrier fragments, so the news-fragment check is
skipped for its pull requests and must keep running for everyone else. The
``if:`` conditions are evaluated here with a deliberately tiny evaluator that
understands only the expression shapes this workflow uses, and the report step's
script is executed for real under ``bash``, the way GitHub Actions runs it.
"""

from pathlib import Path
from typing import Any

import pytest
import re
import subprocess
import yaml


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "changelog.yml"

#: Matches ``${{ path }}`` and ``${{ path != 'literal' }}`` / ``==``.
EXPRESSION = re.compile(
    r"^\$\{\{\s*(?P<path>[\w.\-]+)\s*(?:(?P<op>==|!=)\s*'(?P<literal>[^']*)'\s*)?\}\}$"
)


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    """The parsed ``jobs`` mapping of the workflow.

    :returns: job id to job definition.
    """
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _context(author: str, **results: str) -> dict[str, Any]:
    """Build the slice of the GitHub expression context the workflow reads.

    :param author: login of the pull request's author.
    :param results: job id to ``needs.<job>.result``.
    :returns: a nested mapping shaped like the Actions contexts.
    """
    return {
        "github": {"event": {"pull_request": {"user": {"login": author}}}},
        "needs": {job: {"result": result} for job, result in results.items()},
    }


def evaluate(expression: str, context: dict[str, Any]) -> object:
    """Evaluate one of the expression shapes used in this workflow.

    String comparison is case-insensitive, as it is in GitHub Actions.

    :param expression: a ``${{ ... }}`` expression.
    :param context: the contexts the expression may read.
    :returns: the looked-up value, or the boolean result of a comparison.
    :raises ValueError: for any expression shape this evaluator does not know.
    """
    match = EXPRESSION.match(expression.strip())
    if match is None:
        raise ValueError(f"unsupported expression: {expression!r}")
    value: Any = context
    for part in match["path"].split("."):
        value = value[part]
    if match["op"] is None:
        return value
    equal = str(value).casefold() == match["literal"].casefold()
    return equal if match["op"] == "==" else not equal


class TestEvaluator:
    """The evaluator must not be the reason a test passes."""

    def test_rejects_expressions_it_does_not_understand(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            evaluate("${{ always() }}", _context("someone"))

    def test_comparison_is_case_insensitive(self) -> None:
        context = _context("Dependabot[BOT]")
        expression = "${{ github.event.pull_request.user.login == 'dependabot[bot]' }}"
        assert evaluate(expression, context) is True


class TestChangelogCheck:
    @pytest.mark.parametrize(
        ("author", "runs"),
        [
            ("dependabot[bot]", False),
            ("ericof", True),
            ("dependabot", True),
            ("renovate[bot]", True),
        ],
    )
    def test_runs_for_everyone_but_dependabot(
        self, jobs: dict[str, Any], author: str, runs: bool
    ) -> None:
        condition = jobs["changelog"].get("if")

        # A job without ``if:`` always runs.
        actual = True if condition is None else evaluate(condition, _context(author))
        assert actual is runs

    def test_still_runs_towncrier_check(self, jobs: dict[str, Any]) -> None:
        """Skipping Dependabot must not have weakened the check itself."""
        scripts = [step.get("run", "") for step in jobs["changelog"]["steps"]]

        assert any("towncrier check" in script for script in scripts)


class TestReport:
    @pytest.fixture
    def step(self, jobs: dict[str, Any]) -> dict[str, Any]:
        """The report job's single summary-writing step."""
        (step,) = (s for s in jobs["report"]["steps"] if "run" in s)
        return step

    def run_report(
        self, step: dict[str, Any], tmp_path: Path, context: dict[str, Any]
    ) -> list[str]:
        """Execute the report script and return the summary it wrote.

        :param step: the workflow step to run.
        :param tmp_path: where to put the step summary file.
        :param context: the contexts the step's ``env:`` expressions read.
        :returns: the lines written to ``GITHUB_STEP_SUMMARY``.
        """
        summary = tmp_path / "summary.md"
        env = {
            name: str(evaluate(value, context))
            for name, value in step.get("env", {}).items()
        }
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        env["PATH"] = "/usr/bin:/bin"
        # The default shell GitHub Actions uses for ``run:`` on Linux runners.
        subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            env=env,
            check=True,
        )
        return summary.read_text().splitlines()

    @pytest.mark.parametrize(
        ("author", "result", "expected"),
        [
            ("dependabot[bot]", "skipped", "| changelog | skipped (dependabot) |"),
            ("ericof", "success", "| changelog | success |"),
            ("ericof", "failure", "| changelog | failure |"),
            # Skipped for another reason, e.g. the config job failed.
            ("ericof", "skipped", "| changelog | skipped |"),
            ("dependabot[bot]", "failure", "| changelog | failure |"),
        ],
    )
    def test_changelog_row(
        self,
        step: dict[str, Any],
        tmp_path: Path,
        author: str,
        result: str,
        expected: str,
    ) -> None:
        context = _context(author, config="success", changelog=result)

        lines = self.run_report(step, tmp_path, context)

        assert lines == [
            "# Workflow Report",
            "| Job ID | Conclusion |",
            "| --- | --- |",
            "| config | success |",
            expected,
        ]

    def test_script_does_not_interpolate_expressions(
        self, step: dict[str, Any]
    ) -> None:
        """Values reach the script through ``env:``, never spliced into its text."""
        assert "${{" not in step["run"]
