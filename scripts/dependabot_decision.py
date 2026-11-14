"""Decision logic for auto-merging green Dependabot PRs (update type x check states -> merge or skip).

Not wired into a workflow: this task could not add or edit anything under
.github/workflows/ (a CI-config path chargehand refuses to push, since a
pushed workflow can run with the repository's secrets). .github/workflows/
already has a dependabot-automerge.yml (#27, #28) that does something close
to this with inline bash; this module is the pure, testable version of the
"should this PR merge" question that a future edit to that workflow could
call instead of re-deriving it in a `run:` block.

update_type is one of dependabot/fetch-metadata's semver buckets:
"version-update:semver-patch", "...-minor", "...-major" (anything else -
e.g. a version-update with no semver info - is treated like major: left
for a human). check_conclusions is one entry per check run on the PR's
head commit, using the GitHub Checks API vocabulary: None/"" for a run
that has not completed yet, otherwise its conclusion
("success", "neutral", "skipped", "failure", "cancelled", "timed_out",
"action_required", "startup_failure", ...).
"""
from dataclasses import dataclass

GREEN = {"success", "neutral", "skipped"}
MERGEABLE_TYPES = {"version-update:semver-patch", "version-update:semver-minor"}


@dataclass(frozen=True)
class Decision:
    action: str  # "merge" or "skip"
    reason: str


def decide(update_type: str, check_conclusions: list) -> Decision:
    if update_type not in MERGEABLE_TYPES:
        return Decision("skip", f"{update_type} is not a patch/minor update; leaving it for review")

    if not check_conclusions:
        return Decision("skip", "no checks ran on this commit; never merge without CI")

    pending = [c for c in check_conclusions if not c]
    if pending:
        return Decision("skip", "checks are still running")

    bad = [c for c in check_conclusions if c not in GREEN]
    if bad:
        return Decision("skip", f"{len(bad)} check(s) did not finish green: {sorted(set(bad))}")

    return Decision("merge", "patch/minor update, every check finished success/neutral/skipped")
