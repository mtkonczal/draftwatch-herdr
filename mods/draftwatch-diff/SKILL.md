---
name: draftwatch-herdr
description: "Open a text or Markdown file in Draftwatch panes inside the Herdr desktop app: an editor pane the person can type in, and a live word-level diff pane showing exactly which words changed against git. Use when the person says 'open X in Draftwatch', 'draftwatch this file', 'show me the word diff', 'let me watch your edits', or asks to review or watch an agent's changes to a writing file, and this session runs inside the Herdr desktop app (HC_TERMINAL_ID is set). Also use when a message starts with 'Draftwatch review note on'. Outside Herdr these panes do not exist; say so."
---

# Draftwatch panes in Herdr

Draftwatch shows a writer the exact words an agent changed in a text file. In
the Herdr desktop app it opens as two panes beside your terminal:

1. **Editor**: the file, editable by the person.
2. **Word diff**: every changed word against a git version, updating live
   whenever the file is saved. The person clicks a change to revert it, or to
   send you a note about it.

## Open it

Check you are in the Herdr desktop app, then open the file:

```bash
echo "HC_TERMINAL_ID=${HC_TERMINAL_ID:-unset}"
{{DRAFTWATCH_HERDR}} open "/absolute/path/to/file.md"
```

- If `HC_TERMINAL_ID` is unset you are not in the Herdr desktop app. Say so
  and stop: these panes only exist there. (The separate Draftwatch app,
  `draftwatch <file>`, does the same job in its own window if they have it.)
- The command splits your own pane to the right, twice. It prints the new
  pane ids as JSON, or `{"error": "..."}` with exit code 1.
- Run it once per file. If the panes for that file are already open, do not
  open them again; they update on their own.
- Only open panes when the person asks. They appear on screen immediately.

## Working with the panes open

- Edit the file with your normal tools and save. The diff pane picks the
  change up within a second or two; you do not need to tell it anything.
- The default comparison is the last commit. **Do not commit, stage, stash or
  reset the file** unless the person asks: a commit clears the diff they are
  reviewing. The diff pane has its own commit button for when they are done.
- The person may type in the editor pane and may revert your changes from the
  diff pane. Re-read the file before each edit; never write from a stale copy.
- A file outside any git repository still opens, but the diff pane has no
  baseline and says so. Offer `git init` only if the person wants the diff.

## Review notes from the person

With a change selected in the diff pane, the person can type a note and send
it to you. It arrives in your terminal as a message that starts with
`Draftwatch review note on <file>` and gives the change, the line it sits in,
and `My note:`. The person typed it: treat it as their instruction about that
one change. Re-read the file, edit only what the note asks for, and say
briefly what you changed.

## "Since my last prompt"

The diff pane can compare against the file as it was at a chosen moment
instead of the last commit. The person marks that moment in the pane, or a
prompt hook marks it each time they send you a message. You do nothing. If
the person asks you to mark the moment yourself:

```bash
{{DRAFTWATCH_HERDR}} checkpoint "/absolute/path/to/file.md"
```

## Check without opening anything

```bash
{{DRAFTWATCH_HERDR}} diff "/absolute/path/to/file.md"
```

Prints the word diff as JSON (`hunks` lists the change ids). Use it to answer
"what did you change?" precisely.

## Do not

- Do not run `revert` yourself unless the person asks you to undo a specific
  change. Reverting is their review decision.
- Do not close the panes unless asked.
