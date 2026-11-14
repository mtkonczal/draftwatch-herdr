#!/usr/bin/env python3
"""Tests for the bridge the two Herdr panes call (mods/draftwatch-diff/bridge.py).

They run it against real throwaway git repos and a throwaway HOME, so nothing
on the machine running them is touched. The panes themselves (JavaScript drawn
by Herdr) are not exercised here; `herdr-app check` covers loading them.

Run: python3 tests/test_bridge.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mods", "draftwatch-diff"))

TMP = os.path.realpath(tempfile.mkdtemp(prefix="dw-herdr-"))
# Point every per-user location at the sandbox BEFORE importing the bridge.
os.environ["HOME"] = os.path.join(TMP, "home")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "cache")
os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(TMP, "gitconfig")
os.environ.pop("HC_TERMINAL_ID", None)
os.makedirs(os.environ["HOME"])

import bridge as herdr  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL: %s %s" % (name, detail))


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def raises(fn, needle):
    try:
        fn()
    except herdr.BridgeError as e:
        return needle in str(e)
    return False


V1 = "# Title\n\nInflation cooled faster than most forecasters expected.\n\nThe committee faces a hard choice.\n"
V2 = V1.replace("hard", "difficult")
V3 = V2.replace("most", "nearly all").replace("faster", "much faster")


def make_repo():
    repo = os.path.join(TMP, "repo")
    os.makedirs(repo)
    sh(["git", "init", "-q"], repo)
    sh(["git", "config", "user.email", "t@example.invalid"], repo)
    sh(["git", "config", "user.name", "t"], repo)
    f = os.path.join(repo, "draft.md")
    write(f, V1)
    sh(["git", "add", "draft.md"], repo)
    sh(["git", "commit", "-qm", "first"], repo)
    write(f, V2)
    sh(["git", "commit", "-qam", "second"], repo)
    write(f, V3)
    return repo, f


def test_diff_and_baselines(repo, f):
    snap = herdr.snapshot(f)
    check("head diff finds both changes", len(snap["hunks"]) == 2, snap["hunks"])
    check("head baseline labelled", snap["baseline"]["kind"] == "head" and snap["baseline"]["id"] == "head")
    check("accept-all reconstructs the working file", herdr.reconstruct(snap["segments"], {}) == V3)

    bases = herdr.baselines(f)["baselines"]
    check("baselines: last commit first, one earlier commit", [b["id"] for b in bases][0] == "head" and len(bases) == 2, bases)
    first = bases[-1]["id"]
    older = herdr.snapshot(f, first)
    check("diff against the first commit sees three changes", len(older["hunks"]) == 3, older["hunks"])
    check("no relative dates in a diff label (polling must stay quiet)", "ago" not in older["baseline"]["label"])

    check("push baseline without a remote is an error", raises(lambda: herdr.snapshot(f, "push"), "no pushed version"))
    check("an option-shaped ref is refused", raises(lambda: herdr.snapshot(f, "--output=x"), "No such commit"))
    check("missing file is an error", raises(lambda: herdr.snapshot(os.path.join(repo, "nope.md")), "No such file"))

    new = os.path.join(repo, "new.md")
    write(new, "brand new\n")
    snap = herdr.snapshot(new)
    check("uncommitted file diffs against nothing", snap["baseline"]["kind"] == "empty" and len(snap["hunks"]) == 1)
    check("uncommitted file offers no earlier versions", herdr.baselines(new)["baselines"] == [])

    loose = os.path.join(TMP, "loose.md")
    write(loose, "outside any repo\n")
    snap = herdr.snapshot(loose)
    check("file outside a repo: no baseline, no error", snap["repo"] is False and snap["hunks"] == [])
    check("revert outside a repo is refused", raises(lambda: herdr.revert(loose), "Not in a git repository"))


def test_revert(repo, f):
    snap = herdr.snapshot(f)
    target = None
    for s in snap["segments"]:
        if s["type"] == "del" and s["text"] == "most":
            target = s["hunk"]
    herdr.revert(f, target)
    text = read(f)
    check("revert one change restores its wording", "than most forecasters" in text, text)
    check("revert one change leaves the other", "much faster" in text, text)
    check("a stale change id is refused", raises(lambda: herdr.revert(f, target), "no longer in the file"))
    herdr.revert(f)
    check("revert all leaves no word changes", herdr.snapshot(f)["hunks"] == [])
    check("revert with nothing to revert is an error", raises(lambda: herdr.revert(f), "No changes"))

    write(f, V3)
    first = herdr.baselines(f)["baselines"][-1]["id"]
    snap = herdr.snapshot(f, first)
    hard = [s["hunk"] for s in snap["segments"] if s["type"] == "del" and s["text"] == "hard"][0]
    herdr.revert(f, hard, first)
    check("revert against an earlier commit restores that commit's wording", "a hard choice" in read(f), read(f))
    write(f, V3)


def test_checkpoint(repo, f):
    check("no checkpoint yet is an error", raises(lambda: herdr.snapshot(f, "checkpoint"), "No checkpoint"))
    herdr.checkpoint(f)
    check("right after a checkpoint nothing has changed", herdr.snapshot(f, "checkpoint")["hunks"] == [])
    write(f, V3.replace("committee", "FOMC"))
    snap = herdr.snapshot(f, "checkpoint")
    check("checkpoint diff shows only what changed since", len(snap["hunks"]) == 1, snap["hunks"])
    check("head diff still shows everything", len(herdr.snapshot(f)["hunks"]) == 3)
    check("checkpoint is offered first", herdr.baselines(f)["baselines"][0]["id"] == "checkpoint")
    herdr.revert(f, snap["hunks"][0], "checkpoint")
    check("revert against a checkpoint restores its wording", read(f) == V3, read(f))
    check("a checkpoint leaves git alone", subprocess.run(["git", "stash", "list"], cwd=repo, capture_output=True, text=True).stdout == "")

    # The prompt hook: silent, scoped to the repo the prompt came from.
    herdr.watch(f)
    write(f, V3.replace("committee", "FOMC"))

    def hook(cwd):
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps({"cwd": cwd})), io.StringIO()
        try:
            rc = herdr.main(["checkpoint", "--hook"])
            return rc, sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    rc, out = hook(os.path.join(TMP, "home"))
    check("a prompt from another folder leaves the checkpoint alone", len(herdr.snapshot(f, "checkpoint")["hunks"]) == 1)
    rc, out = hook(repo)
    check("the hook prints nothing and succeeds", rc == 0 and out == "", repr(out))
    check("a prompt from the file's repo moves the checkpoint", herdr.snapshot(f, "checkpoint")["hunks"] == [])
    sys.stdin = io.StringIO("not json")
    old_out, sys.stdout = sys.stdout, io.StringIO()
    try:
        rc = herdr.main(["checkpoint", "--hook"])
        noise = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = sys.__stdin__, old_out
    check("the hook survives bad input silently", rc == 0 and noise == "")
    write(f, V3)


def test_commit(repo, f):
    other = os.path.join(repo, "other.md")
    write(other, "do not commit me\n")
    out = herdr.commit(f, "")
    check("commit with no message uses a default", out["message"] == "Update draft.md", out)
    check("commit clears the diff", herdr.snapshot(f)["hunks"] == [])
    status = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    check("commit takes only this file", "other.md" in status and "draft.md" not in status, status)
    check("nothing to commit is an error", raises(lambda: herdr.commit(f, "again"), "Nothing to commit"))


def test_skill_and_hook():
    home = os.environ["HOME"]
    check("no agents present: no skill written", herdr.install_skill() == [])
    os.makedirs(os.path.join(home, ".claude"))
    os.makedirs(os.path.join(home, ".codex"))
    settings = os.path.join(home, ".claude", "settings.json")
    mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
    write(settings, json.dumps({"model": "x", "hooks": {"UserPromptSubmit": [mine]}}))

    herdr.install_skill()
    claude_skill = os.path.join(home, ".claude", "skills", "draftwatch-herdr", "SKILL.md")
    codex_skill = os.path.join(home, ".agents", "skills", "draftwatch-herdr", "SKILL.md")
    check("skill installed for Claude Code and Codex", os.path.isfile(claude_skill) and os.path.isfile(codex_skill))
    text = read(claude_skill)
    check("skill has this machine's command, no placeholder", "{{" not in text and herdr.command() + " open" in text)
    check("the command runs this bridge", herdr.command().endswith("bridge.py") or "bridge.py" in herdr.command())
    check("installing the skill does not touch settings", "checkpoint --hook" not in read(settings))

    check("prompt hook is added on request", herdr.install_hook() == "added")
    check("prompt hook is not added twice", herdr.install_hook() == "already present")
    data = json.loads(read(settings))
    check("the person's own hook and settings survive", data["model"] == "x" and len(data["hooks"]["UserPromptSubmit"]) == 2, data)
    check("settings were backed up first", os.path.isfile(settings + ".bak-before-draftwatch-hook"))

    herdr.uninstall()
    data = json.loads(read(settings))
    check("uninstall removes only the Draftwatch hook", data["hooks"]["UserPromptSubmit"] == [mine], data)
    check("uninstall removes the skills", not os.path.exists(claude_skill) and not os.path.exists(codex_skill))


def test_engine_is_draftwatch():
    """engine.py must be Draftwatch's code, byte for byte. Checked when a
    Draftwatch checkout sits beside this repo; skipped otherwise."""
    draftwatch = os.path.join(os.path.dirname(ROOT), "Draftwatch")
    engine = read(os.path.join(ROOT, "mods", "draftwatch-diff", "engine.py"))
    commit = [w for w in engine.split() if len(w) == 40 and all(c in "0123456789abcdef" for c in w)]
    check("engine.py records the Draftwatch commit it came from", len(commit) == 1, commit)
    if not os.path.isdir(os.path.join(draftwatch, ".git")) or not commit:
        print("note: no Draftwatch checkout beside this repo; engine sync not checked")
        return
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "sync_engine.py"), draftwatch, commit[0], "--check"],
                       capture_output=True, text=True)
    check("engine.py matches Draftwatch at that commit", r.returncode == 0, r.stderr.strip())


def main():
    try:
        repo, f = make_repo()
        test_diff_and_baselines(repo, f)
        test_revert(repo, f)
        test_checkpoint(repo, f)
        test_commit(repo, f)
        test_skill_and_hook()
        test_engine_is_draftwatch()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("bridge: %d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
