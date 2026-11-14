#!/usr/bin/env python3
"""Copy the two mods into Herdr's mods folder, or update them in place.

    python3 tools/install.py            install or update the panes
    python3 tools/install.py --skill    also write the agent skill
    python3 tools/install.py --hook     also add the Claude Code prompt hook

An agent installing from the GitHub link does the same thing by hand, following
Herdr's own guide: copy `mods/draftwatch-editor` and `mods/draftwatch-diff`
into `~/.config/herdr-app/mods/`, then `herdr-app check` each one.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")
MODS = ("draftwatch-editor", "draftwatch-diff")
FILES = ("main.js", "mod.json", "bridge.py", "engine.py", "SKILL.md")


def main():
    dest_root = os.path.join(HOME, "mods")
    if not os.path.isdir(dest_root):
        sys.exit("Herdr's mods folder was not found at %s. Install the Herdr desktop app and open it once." % dest_root)
    for mod in MODS:
        os.makedirs(os.path.join(dest_root, mod), exist_ok=True)
        for name in FILES:
            src = os.path.join(ROOT, "mods", mod, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(dest_root, mod, name))
        print("installed", os.path.join(dest_root, mod))
    bridge = os.path.join(dest_root, "draftwatch-diff", "bridge.py")
    for flag, cmd in (("--skill", "install-skill"), ("--hook", "install-hook")):
        if flag in sys.argv:
            print(subprocess.run([sys.executable, bridge, cmd], capture_output=True, text=True).stdout.strip())
    if shutil.which("herdr-app"):
        for mod in MODS:
            subprocess.run(["herdr-app", "check", os.path.join(dest_root, mod)])


if __name__ == "__main__":
    main()
