// Draftwatch word diff as a Herdr pane.
// Shows one file's word-level changes against a git version and reverts a
// change on click. The diff and the revert come from bridge.py and engine.py
// in this folder; engine.py is Draftwatch's own code, copied verbatim, so this
// pane and the Draftwatch app agree on every word. Needs python3 and git.
import { View, div } from "gpui-kit";
import { v_flex, h_flex, Scrollbar, InputState, Input } from "gpui-base";
import { exec, hc, appHome, watchFolder, theme, panes, spaces, run, openOverlay, props as liveProps } from "herdr";

const NAME = "draftwatch-diff";

const MAX_ROWS = 1500;     // rows drawn; a longer document says so instead of slowing the window
const words = (text) => text.split(/\s+/).filter(Boolean);

// A mod pane is not told its own pane id, so find it: the only pane showing
// this mod, else the focused one, else the one in the tab on screen.
function ownPane() {
  const all = panes().filter((p) => p.mod === NAME);
  const given = liveProps()?.pane;
  if (given && all.some((p) => p.pane === given)) return given;
  if (all.length === 1) return all[0].pane;
  const focused = all.find((p) => p.focused);
  if (focused) return focused.pane;
  const shown = new Set(spaces().filter((s) => s.active).flatMap((s) => (s.tabs ?? []).filter((tab) => tab.active).map((tab) => tab.id)));
  return all.find((p) => shown.has(p.tab))?.pane ?? null;
}

// Herdr's own pane menu (Split right, Split down, Zoom, Close pane), the one
// terminals show on right-click.
function paneMenu(_e, cx) {
  cx.stop_propagation();
  const pane = ownPane();
  if (pane) openOverlay({ mod: "menu", kind: "popup", width: 200, props: { tab: "pane", target: { pane, terminal: null } } });
}

function closeButton(t) {
  return div().id("dw-close").w(22).h(22).flex().flex_none().items_center().justify_center().rounded(5).cursor_pointer()
    .text_color(t.dim).hover((s) => s.bg(t.hover).text_color(t.foreground))
    .on_click(() => { const pane = ownPane(); if (pane) run("pane.close", { pane }); }).child("×");
}

// Segments (the main app's schema) -> display lines, each a list of parts.
function toLines(segments) {
  const lines = [];
  let cur = { parts: [], changed: false };
  for (const s of segments) {
    if (s.type === "newline") { lines.push(cur); cur = { parts: [], changed: false }; continue; }
    if (!s.text) continue;
    const kind = s.type === "context" ? "ctx" : s.type;
    if (kind !== "ctx") cur.changed = true;
    cur.parts.push({ kind, text: s.text, hunk: s.hunk ?? null });
  }
  if (cur.parts.length) lines.push(cur);
  return lines;
}

export default class DraftwatchDiff extends View {
  init(initial, cx) {
    const p = initial?.file ? initial : (liveProps() ?? {});
    this.file = p.file ?? null;
    // bridge.py sits beside this file. If you rename this mod folder, change NAME above.
    this.engine = ["python3", `${appHome()}/mods/${NAME}/bridge.py`];
    this.lines = []; this.hunks = []; this.info = null; this.raw = null;
    this.selected = null; this.changesOnly = false; this.error = null; this.note = null;
    this.base = "head"; this.bases = null; this.picking = false; this.message = "";
    this.handle = cx.focus_handle();
    this.input = InputState.new({ placeholder: "Commit message (optional)" });
    this.input.on("change", () => { this.message = this.input.value(); });
    this.input.on("submit", (_e, cx) => this.commit(cx));
    this.input.on("focus", (_e, cx) => { this.typing = true; cx.notify(); });
    this.input.on("blur", (_e, cx) => { this.typing = false; cx.notify(); });
    // Notes to the agent: a note about the selected change, sent to the
    // terminal that opened these panes.
    this.agent = typeof p.agent === "string" ? p.agent : null; this.noteText = "";
    this.noteInput = InputState.new({ placeholder: "Note to the agent about this change" });
    this.noteInput.on("change", () => { this.noteText = this.noteInput.value(); });
    this.noteInput.on("submit", (_e, cx) => this.sendNote(cx));
    this.noteInput.on("focus", (_e, cx) => { this.noting = true; cx.notify(); });
    this.noteInput.on("blur", (_e, cx) => { this.noting = false; cx.notify(); });
    if (!this.file || !this.engine) return;
    // Remember which version this pane compares against (per pane copy).
    try { this.base = localStorage.getItem("base:" + this.file) || "head"; } catch { /* default */ }
    this.dir = this.file.slice(0, this.file.lastIndexOf("/")) || "/";
    this.refresh(cx);
    // The folder watch catches saves; the timer catches what never touches the
    // file (a commit moves the baseline).
    cx.timer.every(15000, (cx) => this.refresh(cx));
    cx.spawn(async (cx) => {
      try {
        for (;;) {
          const changed = await watchFolder(this.dir);
          const name = "/" + this.file.split("/").pop();
          if (!changed.length || changed.some((path) => path === this.dir || path.endsWith(name))) this.refresh(cx);
        }
      } catch { /* the timer still refreshes */ }
    });
  }

  async call(args) {
    const r = await exec(this.engine[0], [...this.engine.slice(1), ...args], { cwd: this.dir });
    let out = null;
    try { out = JSON.parse(r.stdout); } catch { /* not JSON: report below */ }
    if (out?.error) throw new Error(out.error);
    if (r.code !== 0 || !out) throw new Error((r.stderr || r.stdout).trim() || `engine exited ${r.code}`);
    return { out, raw: r.stdout };
  }

  refresh(cx) {
    if (this.loading) { this.again = true; return; }
    this.loading = true;
    cx.spawn(async (cx) => {
      let changed = false;
      try {
        const base = this.base;
        let got;
        try { got = await this.call(["diff", this.file, "--base", base]); }
        catch (e) {
          // A remembered version can vanish (a rebase, a deleted remote): fall back once.
          if (base === "head") throw e;
          this.setBase("head"); this.note = `${String(e).replace(/^Error: /, "")} Showing the last commit instead.`;
          got = await this.call(["diff", this.file, "--base", "head"]);
        }
        const { out, raw } = got;
        changed = raw !== this.raw || this.error !== null;
        if (raw !== this.raw) {
          this.raw = raw; this.info = out; this.hunks = out.hunks; this.lines = toLines(out.segments);
          if (this.selected && !this.hunks.includes(this.selected)) this.selected = null;
        }
        this.error = null;
      } catch (e) { changed = this.error !== String(e); this.error = String(e); }
      finally {
        this.loading = false;
        if (changed) cx.notify();           // an unchanged poll must not redraw the window
        if (this.again) { this.again = false; this.refresh(cx); }
      }
    });
  }

  revert(cx) {
    const hunk = this.selected;
    if (!hunk || this.busy) return;
    this.busy = true; this.note = null; this.noteOk = false;
    cx.spawn(async (cx) => {
      try { await this.call(["revert", this.file, hunk, "--base", this.base]); this.selected = null; }
      catch (e) { this.note = String(e).replace(/^Error: /, ""); }
      finally { this.busy = false; this.refresh(cx); cx.notify(); }
    });
    cx.notify();
  }

  setBase(id) {
    this.base = id;
    try { localStorage.setItem("base:" + this.file, id); } catch { /* not remembered */ }
  }

  // Open or close the list of versions to compare against.
  pick(cx) {
    this.picking = !this.picking;
    if (this.picking) cx.spawn(async (cx) => {
      try { this.bases = (await this.call(["baselines", this.file])).out.baselines; }
      catch (e) { this.bases = []; this.note = String(e).replace(/^Error: /, ""); }
      cx.notify();
    });
    cx.notify();
  }

  // Checkpoints: remember the file as it is now and
  // compare against that. The agent's prompt hook does the same on each prompt.
  mark(cx) {
    cx.spawn(async (cx) => {
      try { await this.call(["checkpoint", this.file]); this.choose("checkpoint", cx); }
      catch (e) { this.note = String(e).replace(/^Error: /, ""); this.noteOk = false; cx.notify(); }
    });
  }

  choose(id, cx) { this.setBase(id); this.picking = false; this.selected = null; this.note = null; this.refresh(cx); cx.notify(); }

  // Commit this one file as it is on disk; the comparison returns to the new commit.
  commit(cx) {
    if (this.busy || !this.info?.repo) return;
    const message = this.message.trim();
    this.busy = true; this.note = null;
    cx.spawn(async (cx) => {
      try {
        const { out } = await this.call(["commit", this.file, "-m", message]);
        this.message = ""; this.input.set_value(""); this.setBase("head"); this.selected = null;
        this.note = `Committed ${out.committed}: ${out.message}`; this.noteOk = true;
      } catch (e) { this.note = String(e).replace(/^Error: /, ""); this.noteOk = false; }
      finally { this.busy = false; this.refresh(cx); cx.notify(); }
    });
    cx.notify();
  }

  select(hunk, cx) { this.selected = this.selected === hunk ? null : hunk; this.note = null; cx.notify(); }

  // One changed or deleted word (or a whole changed line) as a clickable chip.
  chip(id, text, part, t) {
    const add = part.kind === "add", on = this.selected === part.hunk, color = add ? t.ansi2 : t.ansi1;
    return div().id(id).px(3).rounded(3).cursor_pointer()
      .bg(color + (on ? "55" : "26")).text_color(add ? t.foreground : color)
      .when(!add, (e) => e.line_through())
      .when(on, (e) => e.border_1().border_color(t.accent))
      .hover((s) => s.bg(color + "55"))
      .on_click((_e, cx) => this.select(part.hunk, cx))
      .child(text);
  }

  row(line, i, t) {
    if (!line.parts.length) return div().h(6).flex_none();
    const heading = line.parts[0].text.startsWith("#");
    if (!line.changed)
      return div().w_full().text_color(t.muted).when(heading, (e) => e.font_semibold().text_color(t.foreground)).child(line.parts.map((p) => p.text).join(""));
    // A line that is one whole added or deleted run wraps as a single block.
    if (line.parts.length === 1) return div().w_full().child(this.chip(`dw-${i}`, line.parts[0].text, line.parts[0], t));
    // Mixed line: one element per word so changed words sit inline and the line wraps.
    const cells = [];
    line.parts.forEach((part, j) => words(part.text).forEach((w, k) =>
      cells.push(part.kind === "ctx" ? div().child(w) : this.chip(`dw-${i}-${j}-${k}`, w, part, t))));
    return h_flex().w_full().flex_wrap().items_baseline().gap_x(5).gap_y(3).when(heading, (e) => e.font_semibold()).children(cells);
  }

  rows(t) {
    const out = [];
    let skipped = 0, drawn = 0;
    this.lines.forEach((line, i) => {
      if (drawn >= MAX_ROWS) return;
      if (this.changesOnly && !line.changed) { if (line.parts.length) skipped++; return; }
      if (skipped) { out.push(div().text_color(t.dim).text_size(11).child(`⋯ ${skipped} unchanged`)); skipped = 0; }
      out.push(this.row(line, i, t)); drawn++;
    });
    if (skipped) out.push(div().text_color(t.dim).text_size(11).child(`⋯ ${skipped} unchanged`));
    if (drawn >= MAX_ROWS) out.push(div().text_color(t.dim).text_size(11).child(`Showing the first ${MAX_ROWS} lines`));
    return out;
  }

  // Notes to the agent. The wording of the change, the line it sits
  // in, and the person's note, pasted into the agent's terminal and submitted.
  sendNote(cx) {
    const note = this.noteText.trim(), hunk = this.selected;
    if (!note || !hunk || !this.agent || this.busy) return;
    const del = [], add = [];
    let context = "";
    for (const line of this.lines) {
      if (!line.parts.some((p) => p.hunk === hunk)) continue;
      for (const p of line.parts) if (p.hunk === hunk) (p.kind === "add" ? add : del).push(p.text);
      if (!context) context = line.parts.filter((p) => p.kind !== "del").map((p) => p.text).join("");
    }
    const clip = (text, n) => (text.length > n ? text.slice(0, n) + "…" : text);
    const was = del.join(" "), now = add.join(" ");
    const change = was && now ? `"${clip(was, 200)}" became "${clip(now, 200)}"` : now ? `added "${clip(now, 200)}"` : `removed "${clip(was, 200)}"`;
    const text = [`Draftwatch review note on ${this.file}`, `Change: ${change}`, `In the line: ${clip(context.trim(), 300)}`, `My note: ${note}`].join("\n");
    this.busy = true; this.note = null;
    cx.spawn(async (cx) => {
      try {
        const r = await hc(["send", this.agent, text]);
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim() || "hc send failed");
        this.noteText = ""; this.noteInput.set_value(""); this.selected = null;
        this.note = `Note sent to ${this.agent}`; this.noteOk = true;
      } catch (e) { this.note = `Note not sent: ${String(e).replace(/^Error: /, "")}`; this.noteOk = false; }
      finally { this.busy = false; cx.notify(); }
    });
    cx.notify();
  }

  selectedText() {
    const del = [], add = [];
    for (const line of this.lines) for (const p of line.parts) if (p.hunk === this.selected) (p.kind === "add" ? add : del).push(p.text);
    const clip = (s) => (s.length > 60 ? s.slice(0, 60) + "…" : s);
    return { del: clip(del.join(" ")), add: clip(add.join(" ")) };
  }

  button(id, label, t, primary, action) {
    return div().id(id).h(26).px(10).flex().items_center().flex_none().rounded(6).cursor_pointer().text_size(12)
      .bg(primary ? t.solid : t.raised).text_color(primary ? t.solid_foreground : t.foreground)
      .hover((s) => s.opacity(0.85)).on_click((_e, cx) => action(cx)).child(label);
  }

  render() {
    const t = theme();
    const root = v_flex().id("draftwatch-diff").size_full().on_mouse_down("right", paneMenu).min_h(0).track_focus(this.handle)
      .on_key_down((e, cx) => { if (e.key === "escape" && (this.typing || this.picking || this.noting)) { this.typing = false; this.picking = false; this.noting = false; this.handle.focus(); cx.stop_propagation(); cx.notify(); } }).bg(t.background).text_color(t.foreground).text_size(14);
    if (!this.file || !this.engine)
      return root.p(14).text_color(t.dim).child("No file. Ask your agent to open a file in Draftwatch, or run: python3 ~/.config/herdr-app/mods/draftwatch-diff/bridge.py open <file>");
    const info = this.info;
    let add = 0, del = 0;
    for (const line of this.lines) for (const p of line.parts) { if (p.kind === "add") add += words(p.text).length; else if (p.kind === "del") del += words(p.text).length; }
    const empty = this.error ? this.error.replace(/^Error: /, "")
      : !info ? "Loading…"
      : !info.repo ? "This file is not in a git repository, so there is no baseline to compare against."
      : !this.hunks.length ? "No changes against the baseline." : null;
    const sel = this.selected ? this.selectedText() : null;
    return root
      // Two rows, so a narrow pane still shows the file name and the version it compares against.
      .child(h_flex().h(34).flex_none().pl(14).pr(8).gap(10).items_center()
        .child(div().flex_1().min_w(0).font_semibold().truncate().child(this.file.split("/").pop()))
        .child(div().flex_none().text_size(11).text_color(t.dim).child(`${this.hunks.length} change${this.hunks.length === 1 ? "" : "s"}`))
        .child(div().flex_none().text_size(11).text_color(t.ansi2).child(`+${add}`))
        .child(div().flex_none().text_size(11).text_color(t.ansi1).child(`−${del}`))
        .child(closeButton(t)))
      .child(h_flex().h(36).flex_none().px(14).pb(8).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().id("dw-base").flex_1().min_w(0).h(26).px(8).flex().items_center().rounded(6).text_size(12).text_color(t.muted)
          .when(Boolean(info?.repo), (e) => e.cursor_pointer().border_1().border_color(this.picking ? t.accent : t.border).hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.pick(cx)))
          .child(div().truncate().child(info?.baseline ? `Compared with ${info.baseline.label} ▾` : "Word diff")))
        .child(this.button("dw-mode", this.changesOnly ? "Show full text" : "Changes only", t, false, (cx) => { this.changesOnly = !this.changesOnly; cx.notify(); })))
      .when(this.picking, (e) => e.child(v_flex().id("dw-bases").flex_none().max_h(220).overflow_y_scroll().p(6).border_b(1).border_color(t.border).bg(t.surface)
        .child(div().px(8).py(3).text_size(11).text_color(t.dim).child("Compare the file against"))
        // Checkpoints: mark now as the point to compare against.
        .child(h_flex().id("dw-mark").h(26).flex_none().px(8).gap(8).items_center().rounded(6).cursor_pointer().text_size(12).text_color(t.accent)
          .hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.mark(cx)).child("Mark this point (show only what changes from now)"))
        .children(this.bases === null ? [div().px(8).py(4).text_size(12).text_color(t.dim).child("Loading versions…")]
          : !this.bases.length ? [div().px(8).py(4).text_size(12).text_color(t.dim).child("No committed versions of this file yet.")]
          : this.bases.map((b, i) => h_flex().id(`dw-base-${i}`).h(26).flex_none().px(8).gap(8).items_center().rounded(6).cursor_pointer().text_size(12)
              .when(b.id === this.base, (x) => x.bg(t.selected)).hover((s) => s.bg(t.hover))
              .on_click((_e, cx) => this.choose(b.id, cx))
              .child(div().truncate().child(b.label))))))
      .child(div().flex_1().min_h(0).relative()
        .child(v_flex().id("draftwatch-diff-scroll").absolute().inset_0().overflow_y_scroll().p(16).gap(4)
          .children(empty ? [div().text_color(t.dim).child(empty)] : this.rows(t)))
        .child(Scrollbar.vertical("draftwatch-diff-scroll").absolute().inset_0()))
      .when(Boolean(this.note), (e) => e.child(div().flex_none().px(14).py(6).text_size(12).text_color(this.noteOk ? t.muted : t.ansi1).child(this.note)))
      .when(Boolean(sel), (e) => e.child(h_flex().flex_none().px(14).py(8).gap(8).items_center().border_t(1).border_color(t.border)
        .child(h_flex().flex_1().min_w(0).gap(6).text_size(12)
          .when(Boolean(sel?.del), (x) => x.child(div().truncate().line_through().text_color(t.ansi1).child(sel.del)))
          .when(Boolean(sel?.add), (x) => x.child(div().truncate().text_color(t.ansi2).child(sel.add))))
        .child(this.button("dw-cancel", "Keep", t, false, (cx) => { this.selected = null; cx.notify(); }))
        .child(this.button("dw-revert", this.busy ? "Reverting…" : "Revert this change", t, true, (cx) => this.revert(cx)))))
      // Notes to the agent: only when an agent opened these panes.
      .when(Boolean(sel) && Boolean(this.agent), (e) => e.child(h_flex().flex_none().px(14).pb(8).gap(8).items_center()
        .child(div().id("dw-note").flex_1().min_w(0).h(30).px(10).flex().items_center().rounded(7).border_1().border_color(this.noting ? t.accent : t.border).cursor_text()
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.noteInput).h_full().text_size(12).text_color(t.foreground)))
        .child(this.button("dw-note-send", `Send note to ${this.agent}`, t, false, (cx) => this.sendNote(cx)))))
      .when(Boolean(info?.repo), (e) => e.child(h_flex().flex_none().p(10).gap(8).items_center().border_t(1).border_color(t.border)
        .child(div().id("dw-commit-message").flex_1().min_w(0).h(30).px(10).flex().items_center().rounded(7).border_1().border_color(this.typing ? t.accent : t.border).cursor_text()
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.input).h_full().text_size(12).text_color(t.foreground)))
        .child(this.button("dw-commit", this.busy ? "Working…" : "Commit file", t, true, (cx) => this.commit(cx)))));
  }
}
