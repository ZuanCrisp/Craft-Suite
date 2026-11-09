"""Helpers to build throw-away upstream repositories for tests (no network)."""
import os
import subprocess

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
           GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")


def g(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()


def write(path, rel, text):
    full = os.path.join(path, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(text)


def make_upstream(path, branch="trunk", seed=""):
    """Default branch is deliberately NOT main/master. Returns commit shas."""
    os.makedirs(path)
    g(path, "init", "-q", "-b", branch)
    write(path, "README.md", "hello" + seed + "\n")
    write(path, "LICENSE", "MIT\n")
    write(path, ".github/workflows/ci.yml", "on: push\njobs: {}\n")
    write(path, ".github/dependabot.yml", "version: 2\n")
    g(path, "add", "-A")
    g(path, "commit", "-qm", "c1 with workflows")
    c1 = g(path, "rev-parse", "HEAD")
    g(path, "tag", "v1.0.0")                       # lightweight, tree has workflows
    g(path, "rm", "-rq", ".github")
    write(path, "src/main.rs", "fn main(){}\n")
    g(path, "add", "-A")
    g(path, "commit", "-qm", "c2 no workflows")
    c2 = g(path, "rev-parse", "HEAD")
    g(path, "tag", "-a", "v2.0.0", "-m", "annotated release")   # annotated, clean tree
    # a second branch + side tag that must NOT be mirrored as a branch
    g(path, "checkout", "-q", "-b", "side")
    write(path, "side.txt", "x\n")
    g(path, "add", "-A")
    g(path, "commit", "-qm", "side")
    g(path, "checkout", "-q", branch)
    return c1, c2


def bare(path):
    os.makedirs(path)
    g(path, "init", "-q", "--bare")
    return path
