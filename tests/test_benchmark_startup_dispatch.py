"""Temporary cross-platform benchmark for pull request feedback."""

import json
import os
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
import warnings
from pathlib import Path

import pytest


BASE_SENTINELS = """\
        with self.processes_management_lock:
            worker_sentinels = [
                p.sentinel for p in list(self.processes.values())
            ]
"""
ORIGINAL_SENTINELS = """\
        worker_sentinels = [p.sentinel for p in list(self.processes.values())]
"""

BASE_MANAGER_COMMENT = """\
            # At least one process must be started so that its sentinel is
            # available to the executor manager thread.
"""
ORIGINAL_MANAGER_COMMENT = """\
            # Start the processes so that their sentinels are known.
"""

EARLY_MANAGER_START = """\
            if (
                self._executor_manager_thread is None
                and self._context.get_start_method() != "fork"
            ):
                # Dispatch pending work as soon as the first worker can consume
                # it, while the remaining workers are still being started. A
                # fork context cannot safely start more processes after this
                # creates the executor manager thread.
                self._start_executor_manager_thread()
"""


def _restore_base_process_executor(source):
    replacements = (
        (BASE_SENTINELS, ORIGINAL_SENTINELS),
        (BASE_MANAGER_COMMENT, ORIGINAL_MANAGER_COMMENT),
        (EARLY_MANAGER_START, ""),
    )
    for new, old in replacements:
        assert source.count(new) == 1
        source = source.replace(new, old)
    return source


def test_startup_dispatch_benchmark(tmp_path):
    if os.environ.get("PYTHON_VERSION") != "3.13":
        pytest.skip("benchmark only runs on the Python 3.13 CI jobs")
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        pytest.skip("benchmark only runs on Linux and Windows")

    repository = Path(__file__).resolve().parents[1]
    revisions = {}
    for name in ("base", "head"):
        destination = tmp_path / name / "loky"
        shutil.copytree(repository / "loky", destination)
        revisions[name] = destination.parent

    base_executor = revisions["base"] / "loky" / "process_executor.py"
    base_executor.write_text(
        _restore_base_process_executor(
            base_executor.read_text(encoding="utf-8")
        ),
        encoding="utf-8",
    )

    helper = tmp_path / "benchmark_once.py"
    helper.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time

            from loky import ProcessPoolExecutor


            def main():
                workers = int(sys.argv[1])
                start = time.perf_counter()
                executor = ProcessPoolExecutor(max_workers=workers)
                try:
                    futures = [
                        executor.submit(time.sleep, 0.01) for _ in range(2)
                    ]
                    for future in futures:
                        future.result()
                    elapsed = time.perf_counter() - start
                    process_count = len(executor._processes)
                finally:
                    executor.shutdown(wait=True)

                print(json.dumps({
                    "elapsed": elapsed,
                    "process_count": process_count,
                }))


            if __name__ == "__main__":
                main()
            """
        ),
        encoding="utf-8",
    )

    cpu_count = os.cpu_count() or 1
    worker_counts = sorted({cpu_count, min(64, max(32, 8 * cpu_count))})
    # Keep the whole benchmark below loky's 60 s faulthandler timeout on the
    # slower Windows runner while retaining an odd number of paired samples.
    repetitions = 7
    warmups = 1

    def measure(revision, workers):
        env = os.environ.copy()
        env.pop("COVERAGE_PROCESS_START", None)
        source = str(revisions[revision])
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        print(
            f"benchmark measure revision={revision} workers={workers}",
            flush=True,
        )
        started_at = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, str(helper), str(workers)],
                cwd=tmp_path,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                "benchmark helper timed out for "
                f"revision={revision}, workers={workers}"
            ) from error
        print(
            "benchmark completed "
            f"revision={revision} workers={workers} "
            f"wall_seconds={time.perf_counter() - started_at:.3f}",
            flush=True,
        )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        assert result["process_count"] == workers
        return result["elapsed"]

    report = {
        "platform": sys.platform,
        "cpu_count": cpu_count,
        "task_count": 2,
        "task_delay_seconds": 0.01,
        "repetitions": repetitions,
        "results": [],
    }
    for workers in worker_counts:
        for _ in range(warmups):
            measure("base", workers)
            measure("head", workers)

        samples = {"base": [], "head": []}
        for repetition in range(repetitions):
            order = (
                ("base", "head") if repetition % 2 == 0 else ("head", "base")
            )
            for revision in order:
                samples[revision].append(measure(revision, workers))

        base_median = statistics.median(samples["base"])
        head_median = statistics.median(samples["head"])
        report["results"].append(
            {
                "workers": workers,
                "base_median_ms": round(base_median * 1000, 3),
                "head_median_ms": round(head_median * 1000, 3),
                "median_change_percent": round(
                    100 * (head_median / base_median - 1), 2
                ),
                "base_samples_ms": [
                    round(sample * 1000, 3) for sample in samples["base"]
                ],
                "head_samples_ms": [
                    round(sample * 1000, 3) for sample in samples["head"]
                ],
            }
        )

    warnings.warn(
        "STARTUP_DISPATCH_BENCHMARK " + json.dumps(report, sort_keys=True),
        RuntimeWarning,
    )
