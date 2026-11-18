"""Monitor memory use and clean up session containers."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .resources import GIB, ResourceSession, atomic_json, host_memory, process_alive


def number(path):
    try:
        return int(Path(path).read_text())
    except (OSError, ValueError):
        return None


def host_pressure():
    """Read PSI and cumulative swap-out pages without changing host settings."""
    sample = {"some_avg10": None, "full_avg10": None, "swap_out_pages": None}
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            name, *values = line.split()
            sample[name + "_avg10"] = float(dict(value.split("=") for value in values)["avg10"])
        for line in Path("/proc/vmstat").read_text().splitlines():
            if line.startswith("pswpout "):
                sample["swap_out_pages"] = int(line.split()[1])
                break
    except (OSError, ValueError, KeyError):
        pass
    return sample


class PressureGuard:
    def __init__(self):
        self.previous = None
        self.severe_since = None

    def update(self, sample, now):
        rate = 0.0
        pages = sample["swap_out_pages"]
        if self.previous is not None and pages is not None:
            previous_time, previous_pages = self.previous
            if now > previous_time:
                rate = max(0, pages - previous_pages) * os.sysconf("SC_PAGE_SIZE") / (now - previous_time)
        if pages is not None:
            self.previous = now, pages
        some, full = sample["some_avg10"] or 0, sample["full_avg10"] or 0
        elevated = full >= 2 or some >= 10 or rate >= 4 * 1024**2
        severe = full >= 10 or (some >= 20 and rate >= 16 * 1024**2)
        if severe:
            if self.severe_since is None:
                self.severe_since = now
        else:
            self.severe_since = None
        stop = self.severe_since is not None and now - self.severe_since >= 5
        return {**sample, "swap_out_bytes_per_second": rate, "elevated": elevated, "stop": stop}


def sample_cgroup(relative):
    root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    result = {
        key: number(root / key)
        for key in ("memory.current", "memory.peak", "pids.current", "pids.peak")
    }
    try:
        result["events"] = dict(
            (key, int(value))
            for key, value in (
                line.split() for line in (root / "memory.events").read_text().splitlines()
            )
        )
    except OSError:
        result["events"] = {}
    return result


def owned_containers(session):
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=rgbgame.session=" + session.id],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        raise RuntimeError("cannot_query_owned_containers")
    return result.stdout.split()


def stop_owned(session, reason):
    if not (session.path / "abort.json").exists():
        atomic_json(session.path / "abort.json", {"reason": reason, "at": time.monotonic()})
    names = owned_containers(session)
    if names:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=15)


def main():
    session = ResourceSession(sys.argv[1])
    lower_since = None
    last_gpu = 0
    gpu = None
    gpu_processes = None
    pressure_guard = PressureGuard()
    paused = False
    recovering_since = None
    parent = "/rgbgame.slice/rgbgame-eval.slice"
    initial_oom = sample_cgroup(parent)["events"].get("oom_kill", 0)
    journal = (session.path / "resources.jsonl").open("a", buffering=1)
    try:
        while not (session.path / "stop.json").exists():
            started = time.monotonic()
            if not process_alive(session.config["owner_pid"]):
                stop_owned(session, "controller_disappeared")
                return
            memory = host_memory()
            pressure = pressure_guard.update(host_pressure(), started)
            parent_sample = sample_cgroup(parent)
            state = session.snapshot()
            owners = [
                *state["allocations"],
                *state["groups"],
                *(r for r in state["requests"] if r["state"] == "active"),
            ]
            if any(not process_alive(r["owner"]) for r in owners):
                stop_owned(session, "workload_owner_disappeared")
                return
            containers = {
                r["name"]: sample_cgroup(r["cgroup"]) for r in state["allocations"] if r["cgroup"]
            }
            if started - last_gpu >= 5:
                try:
                    result = subprocess.run(
                        [
                            "nvidia-smi",
                            "--query-gpu=memory.total,memory.free",
                            "--format=csv,noheader,nounits",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=1,
                    )
                    gpu = result.stdout.strip() if result.returncode == 0 else None
                    processes = subprocess.run(
                        [
                            "nvidia-smi",
                            "--query-compute-apps=pid,used_gpu_memory",
                            "--format=csv,noheader,nounits",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=1,
                    )
                    gpu_processes = processes.stdout.strip() if processes.returncode == 0 else None
                except (OSError, subprocess.TimeoutExpired):
                    gpu = None
                    gpu_processes = None
                last_gpu = started
            sample = {
                "monotonic": started,
                "mem_available": memory["MemAvailable"],
                "swap_free": memory["SwapFree"],
                "host_pressure": pressure,
                "parent": parent_sample,
                "containers": containers,
                "gpu_memory_mib": gpu,
                "gpu_compute_processes_pid_memory_mib": gpu_processes,
            }
            journal.write(json.dumps(sample) + "\n")
            if memory["MemAvailable"] < 6 * GIB:
                lower_since = lower_since or started
            else:
                lower_since = None
            if memory["MemAvailable"] < session.config["minimum_available"] or pressure["elevated"]:
                paused, recovering_since = True, None
            elif paused and memory["MemAvailable"] >= session.config["resume_available"]:
                recovering_since = recovering_since or started
                if started - recovering_since >= 10:
                    paused = False
            else:
                recovering_since = None
            reason = None
            if memory["MemAvailable"] < session.config["critical_available"]:
                reason = "critical_host_memory"
            elif lower_since is not None and started - lower_since >= 5:
                reason = "sustained_low_host_memory"
            elif pressure["stop"]:
                reason = "sustained_host_memory_pressure"
            elif parent_sample["events"].get("oom_kill", 0) > initial_oom:
                reason = "task_cgroup_oom"
            elif any(r["events"].get("oom_kill", 0) for r in containers.values()):
                reason = "container_oom"
            if any(r["kind"] == "vision" for r in state["allocations"]):
                try:
                    gpu_free = int(gpu.splitlines()[0].split(",")[1])
                except (AttributeError, ValueError, IndexError):
                    reason = reason or "gpu_monitor_unavailable"
                else:
                    if gpu_free < 3 * 1024:
                        reason = reason or "gpu_memory_reserve_exhausted"
            if reason:
                stop_owned(session, reason)
                return
            atomic_json(
                session.path / "heartbeat.json",
                {
                    "monotonic": time.monotonic(),
                    "mem_available": memory["MemAvailable"],
                    "gpu_memory_mib": gpu,
                    "admission_paused": paused,
                },
            )
            time.sleep(max(0.05, 1 - (time.monotonic() - started)))
    except BaseException as error:
        stop_owned(session, "watchdog_failure:" + type(error).__name__)
        raise
    finally:
        journal.close()
        names = owned_containers(session)
        if names:
            subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=15)
        networks = subprocess.run(
            ["docker", "network", "ls", "-q", "--filter", "label=rgbgame.session=" + session.id],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if networks.returncode == 0 and networks.stdout.split():
            subprocess.run(
                ["docker", "network", "rm", *networks.stdout.split()],
                capture_output=True,
                timeout=15,
            )
        remaining = owned_containers(session)
        atomic_json(
            session.path / "cleanup.json",
            {"containers_remaining": remaining, "complete": not remaining},
        )
        if remaining:
            atomic_json(
                session.path / "abort.json",
                {"reason": "watchdog_cleanup_incomplete", "containers": remaining},
            )
        else:
            with session.db() as db:
                db.execute("DELETE FROM allocations")


if __name__ == "__main__":
    main()
