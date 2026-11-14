#!/usr/bin/env python3
"""Draftwatch for Herdr: what the two panes call.

The panes (main.js here and in ../draftwatch-editor) draw. Everything that
must match the Draftwatch app exactly (the git word diff, the change ids, the
revert reconstruction) is in engine.py beside this file, copied verbatim from
Draftwatch. This file adds what is specific to Herdr: opening the panes,
choosing what to compare against, checkpoints, committing, and installing the
optional agent skill and prompt hook.

    python3 bridge.py open draft.md        open the two panes on a file
    python3 bridge.py diff draft.md        the word diff as JSON
    python3 bridge.py baselines draft.md   versions to compare against
    python3 bridge.py revert draft.md ID   put one change back
    python3 bridge.py checkpoint draft.md  compare against the file as it is now
    python3 bridge.py commit draft.md -m "message"
    python3 bridge.py install-skill        teach Claude Code and Codex to open it
    python3 bridge.py install-hook         checkpoint on every Claude Code prompt
    python3 bridge.py uninstall            remove the skill, hook and checkpoints

Needs git and Python 3.9+. No other dependencies, and no Draftwatch install.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
# Herdr reloads a mod when a file in its folder changes, so leave no .pyc here.
sys.dont_write_bytecode = True

from engine import (  # noqa: E402
    commit_list,
    file_mtime,
    git_diff_text,
    git_toplevel,
    is_tracked,
    parse_word_diff,
    push_ref,
    read_text,
    reconstruct,
    run_git,
    verify_commit_ref,
    write_file,
)

MODS = ("draftwatch-editor", "draftwatch-diff")
SKILL_NAME = "draftwatch-herdr"
HOOK_MARK = "checkpoint --hook"


def skill_source():
    """SKILL.md travels inside this mod folder so a copied mod can install it."""
    return os.path.join(HERE, "SKILL.md")


class BridgeError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Checkpoints. A checkpoint is a private copy of the file's text at a moment
# (a prompt being sent to the agent, or "Mark this point" in the pane). It
# lives outside git, so it touches no commit, index or stash, and it lets the
# diff answer "what changed since then?" rather than "since the last commit?".
# --------------------------------------------------------------------------- #

def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "draftwatch", "checkpoints")
    os.makedirs(d, exist_ok=True)
    return d


def checkpoint_path(abspath):
    return os.path.join(cache_dir(), hashlib.sha1(abspath.encode("utf-8")).hexdigest() + ".txt")


def watched_path():
    return os.path.join(cache_dir(), "watched.json")


def watched():
    """Files with Draftwatch panes opened on them: {abspath: last opened}."""
    try:
        with open(watched_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def watch(abspath):
    data = watched()
    data[abspath] = time.time()
    # Forget files not opened for two weeks so the hook stays cheap.
    data = {k: v for k, v in data.items() if time.time() - v < 14 * 86400}
    tmp = watched_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, watched_path())


def checkpoint(path):
    """Remember the file's text as it is now."""
    abspath = os.path.realpath(path)
    if not os.path.isfile(abspath):
        raise BridgeError("No such file: %s" % abspath)
    write_file(checkpoint_path(abspath), read_text(abspath))
    return {"checkpoint": abspath, "at": time.strftime("%H:%M")}


def checkpoint_from_hook():
    """Called by the agent's prompt hook: checkpoint every watched file under
    the folder the prompt was sent from. Silent, and never fails the prompt."""
    try:
        cwd = os.path.realpath(json.load(sys.stdin).get("cwd") or os.getcwd())
    except Exception:
        cwd = os.path.realpath(os.getcwd())
    root = git_toplevel(cwd) or cwd
    for abspath in watched():
        if abspath.startswith(root + os.sep) and os.path.isfile(abspath):
            try:
                checkpoint(abspath)
            except Exception:
                pass


def checkpoint_label(abspath):
    cp = checkpoint_path(abspath)
    if not os.path.isfile(cp):
        return None
    return "my last prompt (%s)" % time.strftime("%H:%M", time.localtime(os.path.getmtime(cp)))


def locate(path):
    """(abspath, repo root or None, path relative to the root or None)."""
    abspath = os.path.realpath(path)
    if not os.path.isfile(abspath):
        raise BridgeError("No such file: %s" % abspath)
    root = git_toplevel(os.path.dirname(abspath))
    return abspath, root, (os.path.relpath(abspath, root) if root else None)


def committed(root, relpath):
    """True when HEAD exists and git tracks the file (so HEAD is a baseline)."""
    rc, _, _ = run_git(["rev-parse", "--verify", "--quiet", "HEAD"], root)
    return rc == 0 and is_tracked(root, relpath)


def short_sha(root, ref):
    rc, out, _ = run_git(["rev-parse", "--short", ref], root)
    return out.strip() if rc == 0 else ref[:7]


def resolve_base(root, relpath, base, abspath=None):
    """The baseline to diff against, in the main app's shape plus a label.

    `base` is "head" (default), "push" (what the remote has), or a commit sha.
    Labels carry no relative dates on purpose: the panes poll, and a label that
    changes every minute would redraw the window for nothing."""
    if base == "checkpoint":
        label = checkpoint_label(abspath) if abspath else None
        if label is None:
            raise BridgeError("No checkpoint for this file yet.")
        return {"kind": "checkpoint", "ref": checkpoint_path(abspath), "id": "checkpoint", "label": label}
    if not committed(root, relpath):
        # Same rule as the main app: a file git has never committed diffs
        # against the empty tree, so the whole file reads as new.
        return {"kind": "empty", "ref": "empty", "id": "head", "label": "nothing (not committed yet)"}
    if base in (None, "", "head"):
        return {"kind": "head", "ref": "HEAD", "id": "head", "label": "last commit (%s)" % short_sha(root, "HEAD")}
    if base == "push":
        name, short, _ = push_ref(root)
        if name is None:
            raise BridgeError("This branch has no pushed version to compare against.")
        return {"kind": "push", "ref": name, "id": "push", "label": "last push (%s %s)" % (name, short)}
    if not verify_commit_ref(root, base):
        raise BridgeError("No such commit: %s" % base)
    rc, out, _ = run_git(["log", "-1", "--format=%h %s", base], root)
    label = out.strip() if rc == 0 else base[:7]
    return {"kind": "commit", "ref": base, "id": base, "label": label[:60]}


def snapshot(path, base=None):
    """The file's word diff against a baseline, in the main app's schema."""
    abspath, root, relpath = locate(path)
    out = {
        "file": abspath,
        "name": os.path.basename(abspath),
        "mtime": file_mtime(abspath),
        "repo": False,
        "baseline": None,
        "segments": [],
        "hunks": [],
    }
    if root is None:
        return out
    baseline = resolve_base(root, relpath, base, abspath)
    if baseline["kind"] == "checkpoint":
        # The engine's own command for a not-yet-committed file
        # (`--no-index` against /dev/null), with the checkpoint as the old side.
        rc, diff_text, err = run_git(["diff", "-U1000000", "--no-index", "--no-color",
                                      "--word-diff=porcelain", baseline["ref"], abspath], root)
        if rc not in (0, 1):
            raise BridgeError("git diff failed: " + err.strip())
    else:
        diff_text = git_diff_text(root, baseline, relpath, abspath)
    segments, hunks = parse_word_diff(diff_text)
    out.update(repo=True, root=root, relpath=relpath,
               baseline={"kind": baseline["kind"], "id": baseline["id"], "label": baseline["label"]},
               segments=segments, hunks=hunks)
    return out


def baselines(path):
    """Every version the file can be compared against, newest first."""
    abspath, root, relpath = locate(path)
    cp = checkpoint_label(abspath)
    first = [{"id": "checkpoint", "label": "Since " + cp}] if cp and root else []
    if root is None or not committed(root, relpath):
        return {"baselines": first}
    out = first + [{"id": "head", "label": "Last commit (%s)" % short_sha(root, "HEAD")}]
    name, short, when = push_ref(root)
    if name is not None:
        out.append({"id": "push", "label": "Last push (%s %s, %s)" % (name, short, when)})
    # Commits that touched this file. The newest is HEAD's version of the file,
    # which "Last commit" already covers.
    out.extend({"id": c["ref"], "label": c["label"]} for c in commit_list(root, relpath)[1:])
    return {"baselines": out}


def commit(path, message):
    """Commit this one file, exactly as the main app's commit button does."""
    abspath, root, relpath = locate(path)
    if root is None:
        raise BridgeError("Not in a git repository: nothing to commit to.")
    msg = (message or "").strip() or "Update %s" % relpath
    rc, _, err = run_git(["add", "--", relpath], root)
    if rc != 0:
        raise BridgeError("git add failed: " + err.strip())
    rc, out, err = run_git(["commit", "-m", msg, "--", relpath], root)
    if rc != 0:
        combined = err.strip() or out.strip()
        if "nothing to commit" in combined or "nothing added to commit" in combined \
                or "no changes added to commit" in combined:
            raise BridgeError("Nothing to commit: no saved changes since the last commit.")
        raise BridgeError("git commit failed: " + combined)
    return {"committed": short_sha(root, "HEAD"), "message": msg}


def revert(path, hunk_id=None, base=None):
    """Write the file with one change (or, with no id, every change) put back
    to the baseline's wording."""
    snap = snapshot(path, base)
    if not snap["repo"]:
        raise BridgeError("Not in a git repository: nothing to revert to.")
    if not snap["hunks"]:
        raise BridgeError("No changes to revert.")
    if hunk_id is not None and hunk_id not in snap["hunks"]:
        raise BridgeError("That change is no longer in the file. Refresh and try again.")
    current = read_text(snap["file"])
    # The diff must describe exactly the bytes about to be replaced. If it
    # does not (the file changed between the diff and now, or it has a shape
    # the parser cannot round-trip), refuse instead of writing a guess.
    if reconstruct(snap["segments"], {}) != current:
        raise BridgeError("The file does not match its diff (it may have just changed). "
                          "Nothing was written.")
    if hunk_id is None:
        new_text = reconstruct(snap["segments"], {}, pending_is_accept=False)
    else:
        new_text = reconstruct(snap["segments"], {hunk_id: "reject"})
    if file_mtime(snap["file"]) != snap["mtime"]:
        raise BridgeError("The file changed while reverting. Nothing was written.")
    write_file(snap["file"], new_text)
    return {"reverted": len(snap["hunks"]) if hunk_id is None else 1}


def command():
    """How to run this file from a shell, as a quoted string."""
    return "python3 %s" % shlex.quote(os.path.join(HERE, "bridge.py"))


def skill_homes():
    """Where coding agents look for skills, for the agents present here:
    Claude Code reads ~/.claude/skills, Codex reads ~/.agents/skills."""
    home = os.path.expanduser("~")
    out = []
    if os.path.isdir(os.path.join(home, ".claude")):
        out.append(os.path.join(home, ".claude", "skills"))
    if os.path.isdir(os.path.join(home, ".agents")) or os.path.isdir(os.path.join(home, ".codex")):
        out.append(os.path.join(home, ".agents", "skills"))
    return out


def install_skill():
    """Write the agent skill, with this machine's command filled in."""
    with open(skill_source(), encoding="utf-8") as f:
        text = f.read().replace("{{DRAFTWATCH_HERDR}}", command())
    written = []
    for root in skill_homes():
        dest = os.path.join(root, SKILL_NAME)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(text)
        written.append(dest)
    return written


def claude_settings():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def install_hook():
    """Add a Claude Code prompt hook that checkpoints watched files each time
    a prompt is sent. Opt-in: it edits ~/.claude/settings.json (a backup is
    kept beside it) and runs on every prompt in every session."""
    path = claude_settings()
    if not os.path.isfile(path):
        raise BridgeError("No Claude Code settings at %s" % path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if any(HOOK_MARK in json.dumps(e) for e in entries):
        return "already present"
    shutil.copyfile(path, path + ".bak-before-draftwatch-hook")
    entries.append({"hooks": [{"type": "command", "command": command() + " " + HOOK_MARK, "timeout": 5}]})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return "added"


def uninstall():
    """Remove what this file added outside the mods folder: the skill, the
    prompt hook and the checkpoint copies. Delete the two mod folders yourself
    (or ask your agent to) to remove the panes."""
    removed = []
    for root in skill_homes():
        dest = os.path.join(root, SKILL_NAME)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
            removed.append(dest)
    path = claude_settings()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("hooks", {}).get("UserPromptSubmit", [])
        kept = [e for e in entries if HOOK_MARK not in json.dumps(e)]
        if len(kept) != len(entries):
            if kept:
                data["hooks"]["UserPromptSubmit"] = kept
            else:
                del data["hooks"]["UserPromptSubmit"]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            removed.append("prompt hook in " + path)
    shutil.rmtree(cache_dir(), ignore_errors=True)
    return {"removed": removed}


def herdr_app(*args):
    exe = shutil.which("herdr-app")
    if exe is None:
        raise BridgeError("herdr-app is not on PATH.")
    p = subprocess.run([exe] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise BridgeError((p.stdout or p.stderr).strip() or "herdr-app failed")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def own_pane():
    """The pane of the terminal this runs in, so the new panes open beside the
    agent that asked rather than beside whatever happens to have focus."""
    me = os.environ.get("HC_TERMINAL_ID")
    if not me:
        return None
    for space in herdr_app("ls").get("spaces", []):
        for tab in space.get("tabs", []):
            for term in tab.get("terminals", []):
                if term.get("terminal") == me:
                    return term.get("pane")
    return None


def open_panes(path, pane=None):
    abspath = os.path.realpath(path)
    if not os.path.isfile(abspath):
        raise BridgeError("No such file: %s" % abspath)
    editor_mod = os.path.join(os.path.dirname(HERE), "draftwatch-editor", "main.js")
    if not os.path.isfile(editor_mod):
        raise BridgeError("The draftwatch-editor mod is missing. Copy both mod folders into Herdr's mods folder.")
    watch(abspath)      # the prompt hook checkpoints watched files
    props = {"file": abspath}
    # The terminal that opened the panes is the agent the diff pane sends
    # review notes to.
    if os.environ.get("HC_TERMINAL_ID"):
        props["agent"] = os.environ["HC_TERMINAL_ID"]
    split = {"direction": "right", "mod": "draftwatch-editor", "props": props}
    target = pane or own_pane()
    if target:
        split["pane"] = target
    editor = herdr_app("do", "pane.split", json.dumps(split))
    split = {"direction": "right", "mod": "draftwatch-diff", "props": props}
    if editor.get("pane"):
        split["pane"] = editor["pane"]
    diff = herdr_app("do", "pane.split", json.dumps(split))
    return {"file": abspath, "editor": editor, "diff": diff}


def main(argv=None):
    # git may refresh its index while diffing; the mods watch the file's folder
    # and should not be woken by their own reads.
    os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")
    parser = argparse.ArgumentParser(prog="bridge.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open", help="open the editor and word-diff panes in Herdr")
    p.add_argument("file")
    p.add_argument("--pane", help="split this pane (default: the pane this runs in, else the focused one)")
    sub.add_parser("install-skill", help="write the draftwatch-herdr skill for Claude Code and Codex")
    sub.add_parser("install-hook", help="add a Claude Code hook that checkpoints open files on every prompt")
    sub.add_parser("uninstall", help="remove the skill, the hook and the checkpoint copies")
    base_help = "compare against: head (default), push, or a commit sha"
    p = sub.add_parser("diff", help="print the word diff as JSON")
    p.add_argument("file")
    p.add_argument("--base", help=base_help)
    p = sub.add_parser("baselines", help="list the versions the file can be compared against")
    p.add_argument("file")
    p = sub.add_parser("revert", help="undo one change on disk (or all with --all)")
    p.add_argument("file")
    p.add_argument("hunk", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--base", help=base_help)
    p = sub.add_parser("checkpoint", help="remember the file as it is now, to compare against")
    p.add_argument("file", nargs="?")
    p.add_argument("--hook", action="store_true", help="from an agent's prompt hook: every watched file under its folder")
    p = sub.add_parser("commit", help="commit this one file")
    p.add_argument("file")
    p.add_argument("-m", "--message", default="")
    args = parser.parse_args(argv)
    if args.cmd == "checkpoint" and args.hook:
        checkpoint_from_hook()      # prints nothing: a prompt hook's output is fed to the agent
        return 0
    try:
        if args.cmd == "open":
            out = open_panes(args.file, args.pane)
        elif args.cmd == "install-skill":
            out = {"skill": install_skill(), "command": command()}
        elif args.cmd == "install-hook":
            out = {"prompt_hook": install_hook()}
        elif args.cmd == "uninstall":
            out = uninstall()
        elif args.cmd == "diff":
            out = snapshot(args.file, args.base)
        elif args.cmd == "baselines":
            out = baselines(args.file)
        elif args.cmd == "commit":
            out = commit(args.file, args.message)
        elif args.cmd == "checkpoint":
            if not args.file:
                raise BridgeError("Give a file, or --hook.")
            out = checkpoint(args.file)
        else:
            if bool(args.hunk) == bool(args.all):
                raise BridgeError("Give one change id, or --all.")
            out = revert(args.file, None if args.all else args.hunk, args.base)
    except BridgeError as e:
        json.dump({"error": str(e)}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
