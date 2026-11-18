"""Track resource reservations across processes."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from gpa.config import DEFAULT_BROWSER_MEMORY_MIB

GIB = 1024**3
MIB = 1024**2
SESSION_ENV = "RGBGAME_RESOURCE_SESSION"


class ResourceStopped(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def host_memory():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, rest = line.split(":", 1)
        values[key] = int(rest.split()[0]) * 1024
    return values


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ResourceSession:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.config = json.loads((self.path / "session.json").read_text())
        self.id = self.config["id"]
        self.watchdog = None
        self.previous_env = None

    @classmethod
    def current(cls, *, required=True):
        value = os.environ.get(SESSION_ENV)
        if not value:
            if required:
                raise ResourceStopped("resource_session_required")
            return None
        return cls(value)

    @classmethod
    def create(cls, path, *, output_limit=512 * 1024, model_concurrency=4, browser_limit=4):
        path = Path(path).resolve()
        path.mkdir(parents=True, exist_ok=False)
        available = host_memory()["MemAvailable"]
        budget = min(16 * GIB, available - 10 * GIB)
        if budget < 4 * GIB:
            raise ResourceStopped("not_enough_host_memory_for_safe_evaluation")
        config = {
            "id": uuid.uuid4().hex,
            "owner_pid": os.getpid(),
            "cgroup_parent": "rgbgame-eval.slice",
            "memory_budget": budget,
            "minimum_available": 8 * GIB,
            "resume_available": 10 * GIB,
            "critical_available": 4 * GIB,
            "output_limit": output_limit,
            "model_concurrency": model_concurrency,
            "browser_limit": browser_limit,
            "started_ns": time.monotonic_ns(),
        }
        atomic_json(path / "session.json", config)
        session = cls(path)
        with session.db() as db:
            db.executescript("""
                CREATE TABLE groups(id TEXT PRIMARY KEY, memory INTEGER, owner INTEGER);
                CREATE TABLE allocations(id TEXT PRIMARY KEY, group_id TEXT, kind TEXT,
                    memory INTEGER, owner INTEGER, name TEXT, cgroup TEXT);
                CREATE TABLE requests(id TEXT PRIMARY KEY, owner INTEGER, reserved INTEGER,
                    output INTEGER, state TEXT, started REAL);
            """)
        return session

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path / "admission.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def check(self):
        if (self.path / "abort.json").exists():
            raise ResourceStopped(
                "resource_session_aborted: " + (self.path / "abort.json").read_text()
            )
        if (self.path / "stop.json").exists():
            raise ResourceStopped("resource_session_closed")
        try:
            heartbeat = json.loads((self.path / "heartbeat.json").read_text())
            if time.monotonic() - heartbeat["monotonic"] > 6:
                raise ResourceStopped("resource_watchdog_stale")
            return heartbeat
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ResourceStopped("resource_watchdog_not_ready") from error

    @contextmanager
    def activity(self, kind):
        """Keep compatibility probes separate from live evaluations, across processes."""
        if kind not in {"probe", "evaluation"}:
            raise ValueError("unknown_resource_activity")
        self.check()
        with (self.path / "activity.lock").open("a") as lock:
            operation = fcntl.LOCK_EX if kind == "probe" else fcntl.LOCK_SH
            try:
                fcntl.flock(lock, operation | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ResourceStopped(
                    "compatibility_probes_and_evaluations_must_run_separately; finish the active run first"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def used(db):
        groups = {r["id"]: r["memory"] for r in db.execute("SELECT * FROM groups")}
        actual = {}
        for row in db.execute("SELECT group_id, memory FROM allocations"):
            actual[row["group_id"]] = actual.get(row["group_id"], 0) + row["memory"]
        return sum(
            max(groups.get(key, 0), actual.get(key, 0)) for key in groups.keys() | actual.keys()
        )

    def wait_admission(self, operation, timeout):
        deadline = time.monotonic() + timeout
        while True:
            heartbeat = self.check()
            if (
                not heartbeat.get("admission_paused")
                and host_memory()["MemAvailable"] >= self.config["minimum_available"]
            ):
                with self.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    value = operation(db)
                    if value is not None:
                        return value
            if time.monotonic() >= deadline:
                raise ResourceStopped("resource_admission_timeout")
            time.sleep(0.1)

    @contextmanager
    def group(self, memory, *, timeout=1800):
        if memory > self.config["memory_budget"]:
            raise ResourceStopped("task_minimum_memory_exceeds_session_budget")
        identity = uuid.uuid4().hex

        def admit(db):
            fixed = db.execute(
                "SELECT coalesce(sum(memory),0) FROM allocations WHERE kind IN ('game','vision')"
            ).fetchone()[0]
            if fixed + memory > self.config["memory_budget"]:
                raise ResourceStopped("task_minimum_memory_plus_shared_services_exceeds_session_budget")
            if self.used(db) + memory > self.config["memory_budget"]:
                return None
            db.execute("INSERT INTO groups VALUES(?,?,?)", (identity, memory, os.getpid()))
            return identity

        self.wait_admission(admit, timeout)
        try:
            yield identity
        finally:
            with self.db() as db:
                db.execute("DELETE FROM groups WHERE id=?", (identity,))

    def allocate(self, group, kind, memory, name, *, timeout=1800):
        identity = uuid.uuid4().hex

        def admit(db):
            if kind in {"browser", "play"}:
                count = db.execute(
                    "SELECT count(*) FROM allocations WHERE kind IN ('browser','play')"
                ).fetchone()[0]
                if count >= self.config["browser_limit"]:
                    return None
            old = db.execute(
                "SELECT coalesce(sum(memory),0) FROM allocations WHERE group_id=?", (group,)
            ).fetchone()[0]
            row = db.execute("SELECT memory FROM groups WHERE id=?", (group,)).fetchone()
            guaranteed = row[0] if row else 0
            after = self.used(db) - max(old, guaranteed) + max(old + memory, guaranteed)
            if after > self.config["memory_budget"]:
                return None
            db.execute(
                "INSERT INTO allocations VALUES(?,?,?,?,?,?,?)",
                (identity, group, kind, memory, os.getpid(), name, None),
            )
            return identity

        return self.wait_admission(admit, timeout)

    @contextmanager
    def play(self, group, *, browser=True, browser_memory=DEFAULT_BROWSER_MEMORY_MIB * MIB):
        """Reserve the entire submit before creating either half; avoid partial-allocation deadlocks."""
        identity = self.allocate(
            group,
            "play" if browser else "play_fake",
            browser_memory + 768 * MIB if browser else 768 * MIB,
            "play-" + uuid.uuid4().hex,
        )
        try:
            yield identity
        finally:
            self.release(identity)

    def validate_play(self, identity, group, *, kind=None, memory=None):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM allocations WHERE id=? AND group_id=?", (identity, group)
            ).fetchone()
            if row is None or row["kind"] not in {"play", "play_fake"}:
                raise ResourceStopped("invalid_execution_reservation")
            required = memory + 768 * MIB if kind == "browser" else memory
            if required is not None and row["memory"] < required:
                raise ResourceStopped("execution_reservation_smaller_than_container_limits")

    def attach(self, identity, pid):
        relative = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
        with self.db() as db:
            db.execute("UPDATE allocations SET cgroup=? WHERE id=?", (relative, identity))

    def release(self, identity):
        with self.db() as db:
            db.execute("DELETE FROM allocations WHERE id=?", (identity,))

    @contextmanager
    def model_request(
        self, reserved, *, timeout=1800, deadline_ns=None, cancelled=None, priority="bulk"
    ):
        identity = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        reserved = max(1, int(reserved))
        while True:
            self.check()
            if (deadline_ns is not None and time.monotonic_ns() >= deadline_ns) or (
                cancelled and cancelled()
            ):
                raise ResourceStopped("model_request_cancelled_before_admission")
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                settled, pending, active = db.execute(
                    """SELECT
                        coalesce(sum(CASE WHEN state != 'active' THEN coalesce(output, reserved) END), 0),
                        coalesce(sum(CASE WHEN state = 'active' THEN reserved END), 0),
                        count(CASE WHEN state = 'active' THEN 1 END)
                       FROM requests"""
                ).fetchone()
                output_limit = self.config["output_limit"]
                if settled + reserved > output_limit:
                    raise ResourceStopped("global_model_output_budget_exhausted")
                limit = self.config["model_concurrency"] - (priority != "realtime")
                if settled + pending + reserved <= output_limit and active < max(1, limit):
                    db.execute(
                        "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                        (identity, os.getpid(), reserved, None, "active", time.monotonic()),
                    )
                    break
            if time.monotonic() >= deadline:
                raise ResourceStopped("model_request_admission_timeout")
            time.sleep(0.1)
        ticket = {"output_tokens": None}
        try:
            yield ticket
        finally:
            with self.db() as db:
                db.execute(
                    "UPDATE requests SET output=?, state=? WHERE id=?",
                    (
                        ticket["output_tokens"],
                        "complete" if ticket["output_tokens"] is not None else "unknown_usage",
                        identity,
                    ),
                )

    def start(self):
        self.previous_env = os.environ.get(SESSION_ENV)
        os.environ[SESSION_ENV] = str(self.path)
        self.watchdog = subprocess.Popen(
            [
                "systemd-run", "--user", "--scope", "--quiet", "--collect",
                "--unit=rgbgame-watchdog-" + self.id[:12] + "-" + uuid.uuid4().hex[:8],
                "--property=MemoryMax=128M", "--property=MemorySwapMax=0",
                sys.executable, "-m", "gpa.execution.watchdog", str(self.path),
            ],
            stdout=(self.path / "watchdog.stdout").open("w"),
            stderr=(self.path / "watchdog.stderr").open("w"),
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.watchdog.poll() is not None:
                raise ResourceStopped("resource_watchdog_start_failed")
            if (self.path / "heartbeat.json").exists():
                self.check()
                return self
            time.sleep(0.05)
        raise ResourceStopped("resource_watchdog_start_timeout")

    @classmethod
    def recover(cls, path):
        """Restart supervision while retaining every confirmed/unknown usage charge."""
        from .docker import docker

        session = cls(path)
        if process_alive(session.config["owner_pid"]):
            raise ResourceStopped("cannot_recover_a_live_resource_owner")
        remaining = docker(
            ["ps", "-aq", "--filter", "label=rgbgame.session=" + session.id]
        ).stdout.split()
        if remaining:
            raise ResourceStopped("clean_session_containers_before_recovery")
        history = session.path / "recovery" / str(time.time_ns())
        history.mkdir(parents=True)
        for name in (
            "abort.json",
            "stop.json",
            "heartbeat.json",
            "cleanup.json",
            "game-service.json",
            "vision-service.json",
            "session.json",
            "watchdog.stdout",
            "watchdog.stderr",
        ):
            original = session.path / name
            if original.exists():
                if name == "session.json":
                    __import__("shutil").copyfile(original, history / name)
                else:
                    original.replace(history / name)
        with session.db() as db:
            db.execute("DELETE FROM allocations")
            db.execute("DELETE FROM groups")
            db.execute("UPDATE requests SET state='unknown_usage' WHERE state='active'")
        session.config["owner_pid"] = os.getpid()
        session.config["memory_budget"] = min(
            session.config["memory_budget"], host_memory()["MemAvailable"] - 10 * GIB
        )
        if session.config["memory_budget"] < 4 * GIB:
            raise ResourceStopped("not_enough_memory_to_recover_session")
        session.config["recovered_ns"] = time.monotonic_ns()
        atomic_json(session.path / "session.json", session.config)
        atomic_json(
            history / "receipt.json",
            {"usage_ledger_preserved": True, "requests": session.snapshot()["requests"]},
        )
        return cls(path)

    def close(self):
        atomic_json(self.path / "stop.json", {"reason": "session_closed"})
        if self.watchdog:
            try:
                self.watchdog.wait(timeout=50)
            except subprocess.TimeoutExpired as error:
                raise ResourceStopped("resource_watchdog_cleanup_still_running:" + str(self.path)) from error
        if self.previous_env is None:
            os.environ.pop(SESSION_ENV, None)
        else:
            os.environ[SESSION_ENV] = self.previous_env
        cleanup = self.path / "cleanup.json"
        if not cleanup.exists() or not json.loads(cleanup.read_text()).get("complete"):
            raise ResourceStopped("resource_session_cleanup_unconfirmed:" + str(self.path))

    def snapshot(self):
        with self.db() as db:
            return {
                table: [dict(row) for row in db.execute("SELECT * FROM " + table)]
                for table in ("groups", "allocations", "requests")
            }
