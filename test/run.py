"""Runs the whole test suite in parallel: every unittest class (slow classes: every test) in its own process,
next to the node tests (markdown unit test, browser end-to-end tests). Prints failing jobs' output, then a
summary with the slowest jobs. Exit status 1 on any failure.

Usage: python3 test/run.py [-j N] [-v] [PATTERN ...]
  PATTERN   only jobs whose name contains one of these (e.g. test_server.TestChat, browser)
  -j N      parallel jobs (default: number of CPUs)
  -v        print every job's output, not only the failing ones'
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

import fastenv  # noqa: E402

# classes whose tests each run in their own process: they are slow one after another (long mock turns,
# PTYs), and a test class's server starts in ~0.2s
SPLIT = {"test_subagents.TestSubagents", "test_peers.TestPeers", "test_branches.TestBranches",
         "test_tui.EndToEndTest", "test_tui.FirstRunTest",
         "test_server.TestUsageAndSubjects", "test_server.TestOnboarding", "test_server.TestChat",
         "test_build.BuildTest", "test_llm.RetryTest"}
JOB_TIMEOUT = 120


def python_jobs():
    suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    classes = {}  # "module.Class" -> [test ids]

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            elif isinstance(t, unittest.loader._FailedTest):
                classes.setdefault(t.id(), [t.id()])  # import error: running it reports the error
            else:
                cls = type(t).__module__ + "." + type(t).__qualname__
                classes.setdefault(cls, []).append(t.id())
    walk(suite)
    py = [sys.executable, "-m", "unittest", "-q"]
    jobs = []
    for cls, ids in classes.items():
        if cls in SPLIT:
            jobs += [(i, py + [i]) for i in ids]
        else:
            jobs.append((cls, py + [cls]))
    return jobs


def node_jobs():
    node = shutil.which("node")
    if not node:
        return [], ["node tests (needs node)"]
    jobs = [("markdown.test.mjs", [node, "--test", os.path.join(HERE, "markdown.test.mjs")])]
    chrome = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        return jobs, ["browser tests (needs chromium)"]
    jobs += [("browser.mjs PART=%s" % part, ["env", "PART=" + part, node, os.path.join(HERE, "browser.mjs")])
             for part in ("1", "2")]  # its two halves at the same time
    jobs.append(("browser_onboarding.mjs", [node, os.path.join(HERE, "browser_onboarding.mjs")]))
    return jobs, []


def run(job, env):
    name, cmd = job
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=JOB_TIMEOUT)
        ok, out = p.returncode == 0, p.stdout.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        ok, out = False, (e.stdout or b"").decode(errors="replace") + "\n[timed out after %ds]" % JOB_TIMEOUT
    return name, ok, time.time() - t, out


def main(argv):
    args, patterns, jobs_n, verbose = list(argv), [], os.cpu_count() or 4, False
    while args:
        a = args.pop(0)
        if a == "-j":
            jobs_n = int(args.pop(0))
        elif a == "-v":
            verbose = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            patterns.append(a)
    t0 = time.time()
    nj, skipped = node_jobs()
    jobs = nj + python_jobs()  # the browser tests are the longest: start them first
    if patterns:
        jobs = [j for j in jobs if any(p in j[0] for p in patterns)]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([HERE, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep))
    env.update({k: os.environ.get(k, v) for k, v in fastenv.FAST_ENV.items()})
    # one empty build shared by the browser tests (each copies it into its own temp dir); in the repo, not
    # /tmp: snap Chromium has a private /tmp
    tmp = tempfile.mkdtemp(prefix=".test-tmp-", dir=ROOT)
    try:
        if any(j[0].startswith("browser") for j in jobs):
            built = os.path.join(tmp, "harness.html")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py"), "--fresh", "-o", built],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print("build failed:\n" + r.stdout + r.stderr)
                return 1
            env["SFH_TEST_BUILD"] = built
        results = []
        with ThreadPoolExecutor(jobs_n) as ex:
            for f in as_completed([ex.submit(run, j, env) for j in jobs]):
                name, ok, dt, out = f.result()
                results.append((name, ok, dt))
                if not ok or verbose:
                    print("%s %s (%.1fs)\n%s" % ("=" * 20, ("FAIL " if not ok else "") + name, dt, out.rstrip()))
                else:
                    print(".", end="", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print("\n\nslowest jobs: " + ", ".join("%s %.1fs" % (n, dt) for n, _, dt in sorted(results, key=lambda r: -r[2])[:6]))
    for s in skipped:
        print("skipped " + s)
    print("%d jobs in %.1fs: %s" % (len(results), time.time() - t0,
                                    "FAILED: " + ", ".join(n for n, _, _ in failed) if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
