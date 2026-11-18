"""Coordinate four policy developers and their midpoint peer reviews."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from .workflow import Workflow as GameWorkflow
from .materials import own_tree
from .realtime_team_budget import PhaseUsage
from .realtime_mcp import develop
from gpa.evidence.files import append, digest, dump, load

BRANCHES = ("A", "B", "C", "D")
PEER = dict(zip(BRANCHES, ("B", "C", "D", "A")))
INCOMING = {value: key for key, value in PEER.items()}


def state_file(root, branch):
    return root / "private/team-state" / (branch + ".json")


def state(root, branch):
    path = state_file(root, branch)
    return load(path) if path.exists() else {}


def update(root, branch, **values):
    value = {**state(root, branch), **values, "branch": branch, "observed_ns": time.monotonic_ns()}
    dump(state_file(root, branch), value)
    events = root / "private/team-events" / (branch + ".jsonl")
    events.parent.mkdir(parents=True, exist_ok=True)
    append(events, value)


def freeze_public(workflow, target):
    target.mkdir(parents=True)
    for name in ("evidence", "versions"):
        if (workflow.workspace / name).exists():
            shutil.copytree(workflow.workspace / name, target / name)
    latest = workflow.workspace / "versions" / ("round-%02d" % len(workflow.rounds)) / "policy"
    if latest.exists():
        shutil.copytree(latest, target / "last-submitted-policy")
    files = {}
    for path in sorted(target.rglob("*")):
        if path.is_symlink():
            raise ValueError("peer_snapshot_symlink")
        if path.is_file():
            files[str(path.relative_to(target))] = digest(path)
    manifest = {
        "submitted_rounds": len(workflow.rounds),
        "files": files,
        "official_score_available": False,
        "includes_unsubmitted_edits": False,
    }
    dump(target / "manifest.json", manifest)
    own_tree(target, os.getuid(), writable=False)
    return digest(target / "manifest.json")


class BranchWorkflow(GameWorkflow):
    def __init__(self, root, query, op, team, branch):
        self.team, self.branch = team, branch
        self.development_seconds = 0.0
        self.midpoint = query.get("midpoint_submit", 4)
        self.wait_limit = query.get("phase_wait_timeout_seconds", 20000)
        super().__init__(root, query, op, budget_factory=PhaseUsage)

    def status(self):
        value = super().status()
        value.update(
            branch=self.branch,
            phase=self.budget.phase,
            midpoint_submit=self.midpoint,
            peer_review_target=PEER[self.branch],
        )
        return value

    def submit(self):
        if self.budget.phase == "review":
            return {"error": "review_phase_forbids_submit"}
        if self.budget.phase_reason:
            return {"error": "phase_closed", **self.status()}
        result = super().submit()
        if self.budget.phase == "first" and len(self.rounds) == self.midpoint:
            self.budget.pause("planned_midpoint")
            result["next_phase"] = (
                "Host will resume this same session for one peer review; do not start another run."
            )
        return result

    def phase(self, name, *, resume=False, prompt=None, suffix="", allow_submit=True):
        remaining = self.query.get("development_timeout_seconds", 18000) - self.development_seconds
        if remaining <= 0:
            self.budget.close("workflow_infrastructure_timeout")
            raise TimeoutError("workflow_infrastructure_timeout")
        update(self.team, self.branch, phase=name)
        started = time.monotonic()
        try:
            return develop(
                self,
                phase=name,
                resume=resume,
                prompt_override=prompt,
                prompt_suffix=suffix,
                allow_submit=allow_submit,
                timeout_seconds=remaining,
            )
        finally:
            self.proxy.wait_idle(self.query.get("request_timeout_seconds", 1800) + 35)
            elapsed = time.monotonic() - started
            self.development_seconds += elapsed
            append(
                self.private / "phase-timing.jsonl",
                {
                    "phase": name,
                    "elapsed_seconds": elapsed,
                    "development_seconds": self.development_seconds,
                },
            )

    def wait(self, predicate, label):
        started = time.monotonic()
        while not predicate():
            if (self.team / "private/abort.json").exists() or self.budget.reason:
                raise RuntimeError("team_infrastructure_shutdown")
            if time.monotonic() - started > self.wait_limit:
                raise TimeoutError("peer_wait_timeout:" + label)
            time.sleep(0.2)
        append(
            self.private / "phase-timing.jsonl",
            {"phase": label, "waiting_seconds": time.monotonic() - started},
        )

    def execute(self, initial):
        self.import_initial(initial)
        finishing = (
            " Complete the first submit to reach the scheduled review; the engineering check requires both phases. "
            if self.query.get("engineering_validation")
            else " You may finish early if justified by RGB evidence; an ordinary final answer ends your branch and will not be resumed. "
        )
        result = self.phase(
            "first",
            suffix=(
                " You are independent branch %s of four. After submit %d the host schedules one "
                "peer review and may resume this native session. Before then develop independently. "
                "Peer scores are never available." % (self.branch, self.midpoint)
            )
            + finishing,
        )
        identity = freeze_public(self, self.team / "exchange" / self.branch)
        eligible = result == "planned_midpoint" and not self.budget.reason
        update(
            self.team,
            self.branch,
            first_done=True,
            eligible=eligible,
            snapshot_sha256=identity,
            termination=result,
        )
        if not eligible:
            return
        self.wait(lambda: (self.team / "private/exchange-ready.json").exists(), "midpoint_barrier")
        peers = self.workspace / "peers"
        peers.mkdir(exist_ok=True)
        for branch in BRANCHES:
            source = self.team / "exchange" / branch
            if source.exists():
                shutil.copytree(source, peers / branch)
        own_tree(peers, os.getuid(), writable=False)
        self.budget.enter("review", self.query.get("review_output_tokens", 8192))
        peer = PEER[self.branch]
        if (peers / peer).exists():
            peer_round = load(peers / peer / "manifest.json")["submitted_rounds"]
            self.phase(
                "review",
                resume=True,
                allow_submit=False,
                prompt=(
                    "Scheduled midpoint review in your original developer session. Stop developing your own policy. "
                    "Use Read and mcp__game__write_review. Read peers/%s/last-submitted-policy/policy.py "
                    "and its evidence/round-%02d/feedback.json if present. Immediately call "
                    "mcp__game__write_review with content containing a concise peer_review.md draft, "
                    "including explicit uncertainty or missing submissions. "
                    "You may then read a few of that peer's public feedback/frames and update the report. "
                    "Do not spend this phase analyzing your own workspace or attempting Bash/Edit. "
                    "Paths inside old feedback may name "
                    "their original workspace; resolve frame paths relative to the copied evidence directory. "
                    "Do not run games, submit, modify peer files, or inspect private state. "
                    "Save peer_review.md using mcp__game__write_review, with reusable implementation ideas, the main suspected issue, "
                    "specific code/frame/action file references, and one change worth testing. "
                    "Use at most %d output tokens; all review/auxiliary calls share your original total "
                    "budget. Finish after writing the report. This planned review exit can be resumed "
                    "for your remaining submits."
                    % (peer, peer_round, self.query.get("review_output_tokens", 8192))
                ),
            )
        report = self.workspace / "peer_review.md"
        valid = report.is_file() and not report.is_symlink() and 0 < report.stat().st_size <= 131072
        delivery = {
            "status": "delivered" if valid else "review_missing",
            "from": self.branch,
            "to": peer,
        }
        if valid:
            report.read_text()
            destination = self.team / "private/reviews" / (self.branch + "-to-" + peer + ".md")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report, destination)
            delivery["sha256"] = digest(destination)
        dump(self.team / "private/reviews" / (self.branch + ".json"), delivery)
        update(self.team, self.branch, review_done=True, review=delivery)
        if self.budget.reason:
            return
        source = INCOMING[self.branch]
        self.wait(
            lambda: (
                state(self.team, source).get("review_done")
                or state(self.team, source).get("terminal")
            ),
            "incoming_review",
        )
        record = self.team / "private/reviews" / (source + ".json")
        incoming = load(record) if record.exists() else {"status": "review_missing", "from": source}
        received = self.workspace / "peer_review_received.md"
        if incoming["status"] == "delivered":
            shutil.copyfile(
                self.team / "private/reviews" / (source + "-to-" + self.branch + ".md"), received
            )
        else:
            received.write_text(json.dumps(incoming) + "\n")
        dump(self.private / "review-delivery.json", incoming)
        self.budget.enter("second")
        self.phase(
            "second",
            resume=True,
            suffix=(
                " The one scheduled collaboration is complete. Read peer_review_received.md. "
                "It is advice, not ground truth. You may use peer implementations with file provenance, "
                "but must validate changes through your own remaining submits. Your original cumulative "
                "budget and prior submit count remain in force. Finish whenever justified; no further "
                "resumption is scheduled. Current host status: " + json.dumps(self.status())
            ),
        )


def run_team(root, query, url):
    children, logs = {}, []
    began = time.monotonic()
    try:
        for branch in BRANCHES:
            log = (root / "private" / ("worker-" + branch + ".log")).open("w")
            logs.append(log)
            children[branch] = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gpa.cli",
                    "branch",
                    "--branch",
                    branch,
                    "--root",
                    str(root),
                    "--url",
                    url,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        while any(child.poll() is None for child in children.values()):
            for branch, child in children.items():
                if child.poll() is not None and not state(root, branch).get("terminal"):
                    update(
                        root,
                        branch,
                        first_done=True,
                        terminal=True,
                        status="ERROR",
                        error="worker_disappeared",
                    )
            if not (root / "private/exchange-ready.json").exists() and all(
                state(root, branch).get("first_done") for branch in BRANCHES
            ):
                dump(
                    root / "private/exchange-ready.json",
                    {
                        "observed_ns": time.monotonic_ns(),
                        "snapshots": {b: state(root, b).get("snapshot_sha256") for b in BRANCHES},
                    },
                )
            if time.monotonic() - began > query.get("team_timeout_seconds", 55000):
                raise TimeoutError("team_infrastructure_timeout")
            time.sleep(0.2)
    finally:
        unfinished = any(child.poll() is None for child in children.values())
        if unfinished:
            dump(root / "private/abort.json", {"reason": "team_coordinator_shutdown"})
        for child in children.values():
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + query.get("request_timeout_seconds", 1800) + 120
        incomplete = False
        for child in children.values():
            if child.poll() is None:
                try:
                    child.wait(timeout=max(1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    incomplete = True
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
        for log in logs:
            log.close()
        if incomplete:
            dump(root / "private/cleanup-incomplete.json", {"writers_confirmed_stopped": False})
            raise RuntimeError("team_cleanup_not_confirmed")


def seal_team(root, query, outcome):
    branch_ledgers, requests, pending, errors, rounds = {}, [], {}, {}, []
    for branch in BRANCHES:
        branch_root = root / "branches" / branch
        record_path = branch_root / "result.json"
        record = load(record_path) if record_path.exists() else {"status": "ERROR", "rounds": []}
        outcome.setdefault("branches", {})[branch] = record
        path = branch_root / "private/accounting/budget.json"
        if not path.exists():
            errors[branch] = "missing_branch_ledger"
            continue
        ledger = load(path)
        branch_ledgers[branch] = ledger
        requests.extend(
            {**r, "request_id": branch + ":" + r["request_id"], "branch": branch}
            for r in ledger["requests"]
        )
        pending.update({branch + ":" + k: v for k, v in ledger["pending"].items()})
        if ledger["accounting_error"]:
            errors[branch] = ledger["accounting_error"]
        for index, row in enumerate(record.get("rounds", []), 1):
            source = branch_root / "private" / ("round-%02d" % index)
            target = root / "private" / (branch + "-round-%02d" % index)
            target.mkdir()
            for name in ("result.json", "policy_manifest.json"):
                if (source / name).exists():
                    shutil.copyfile(source / name, target / name)
            samples = source / "play/private_score/samples.jsonl"
            if samples.exists():
                (target / "play/private_score").mkdir(parents=True)
                shutil.copyfile(samples, target / "play/private_score/samples.jsonl")
            rounds.append({**row, "branch": branch, "branch_round": index})
    used = sum(b["used"] for b in branch_ledgers.values())
    budget = {
        "limit": query["developer_output_tokens"],
        "used": used,
        "remaining": query["developer_output_tokens"] - used,
        "active_requests": len(pending),
        "accounting_error": errors or None,
        "stop_reason": "team_finished",
        "requests": requests,
        "pending": pending,
        "branches": branch_ledgers,
    }
    dump(root / "private/accounting/budget.json", budget)
    outcome.update(
        rounds=rounds,
        output_budget={
            k: v for k, v in budget.items() if k not in {"requests", "pending", "branches"}
        },
        termination="branches_finished",
    )
    outcome["failed_branches"] = [
        b for b, value in outcome["branches"].items() if value["status"] != "OK"
    ]
    outcome["team_complete"] = not outcome["failed_branches"]
    if any(value.get("cleanup_error") for value in outcome["branches"].values()):
        raise RuntimeError("branch_cleanup_not_confirmed")
    if pending or errors:
        outcome.update(status="ERROR", error="incomplete_team_accounting")
