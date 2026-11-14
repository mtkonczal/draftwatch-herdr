// Draftwatch editor as a Herdr pane.
// Type in one text file beside the agent. Saving writes to disk only if the
// file is still what this pane last read, so an agent's save is never
// overwritten by accident; when the file changes underneath unsaved typing,
// the pane asks which version to keep.
import { View, div } from "gpui-kit";
import { v_flex, h_flex, Editor, EditorState } from "gpui-base";
import { readTextFile, writeTextFile, watchFolder, theme, settings, panes, spaces, run, openOverlay, props as liveProps } from "herdr";

const NAME = "draftwatch-editor";

const countWords = (text) => (text.match(/\S+/g) ?? []).length;

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

export default class DraftwatchEditor extends View {
  init(initial, cx) {
    const p = initial?.file ? initial : (liveProps() ?? {});
    this.file = p.file ?? null;
    this.disk = null;          // the file's text as last read or written by this pane
    this.theirs = null;        // newer text on disk while this pane has unsaved typing
    this.dirty = false; this.error = null; this.note = null; this.words = 0;
    if (!this.file) return;
    this.dir = this.file.slice(0, this.file.lastIndexOf("/")) || "/";
    cx.spawn(async (cx) => {
      try {
        const text = await readTextFile(this.file);
        if (text === null) throw new Error("This file is no longer available");
        this.disk = text; this.words = countWords(text);
        const t = theme(), ext = this.file.includes(".") ? this.file.split(".").pop() : "txt";
        this.state = EditorState.new({ language: ext, value: text, soft_wrap: true, line_numbers: false, folding: false,
          font_family: settings().font_family, font_size: 14,
          syntax: { keyword: t.ansi5, string: t.ansi2, comment: t.dim, function: t.ansi4, type: t.ansi3, number: t.ansi1, constant: t.ansi6 } });
        this.state.on("change", (_e, cx) => {
          const dirty = this.state.value() !== this.disk;
          if (dirty !== this.dirty) { this.dirty = dirty; this.note = null; cx.notify(); }   // redraw on the flip, not per key
        });
      } catch (e) { this.error = String(e).replace(/^Error: /, ""); }
      finally { cx.notify(); }
      this.watch(cx);
    });
  }

  watch(cx) {
    cx.spawn(async (cx) => {
      try {
        for (;;) {
          const changed = await watchFolder(this.dir);
          const name = "/" + this.file.split("/").pop();
          if (changed.length && !changed.some((path) => path === this.dir || path.endsWith(name))) continue;
          const text = await readTextFile(this.file);
          if (text === null || text === this.disk || !this.state) continue;      // gone, or this pane's own save
          if (this.dirty) { this.theirs = text; }                                 // ask; never drop typing
          else { this.disk = text; this.words = countWords(text); this.state.set_value(text); this.note = "Reloaded from disk"; }
          cx.notify();
        }
      } catch { /* no watcher: Reload still works */ }
    });
  }

  save(cx) {
    if (!this.state || !this.dirty || this.saving) return;
    if (this.theirs !== null) { this.note = "The file changed on disk. Choose a version first."; cx.notify(); return; }
    const text = this.state.value();
    this.saving = true;
    cx.spawn(async (cx) => {
      try {
        await writeTextFile(this.file, text, this.disk);
        this.disk = text; this.words = countWords(text);
        this.dirty = this.state.value() !== this.disk; this.note = "Saved";
      } catch {
        // Stale: the file is not what this pane last read. Fetch it and ask.
        const now = await readTextFile(this.file).catch(() => null);
        if (now !== null && now !== this.disk) this.theirs = now;
        this.note = "Not saved: the file changed on disk.";
      } finally { this.saving = false; cx.notify(); }
    });
  }

  // Take the version on disk and drop this pane's unsaved typing.
  loadTheirs(cx) {
    this.disk = this.theirs; this.theirs = null; this.words = countWords(this.disk);
    this.state.set_value(this.disk); this.dirty = false; this.note = "Loaded the version on disk"; cx.notify();
  }

  // Keep typing here; the next save replaces what is on disk now.
  keepMine(cx) { this.disk = this.theirs; this.theirs = null; this.dirty = this.state.value() !== this.disk; this.note = null; cx.notify(); }

  button(id, label, t, primary, enabled, action) {
    return div().id(id).h(26).px(10).flex().items_center().flex_none().rounded(6).text_size(12)
      .bg(primary && enabled ? t.solid : t.raised).text_color(!enabled ? t.dim : primary ? t.solid_foreground : t.foreground)
      .when(enabled, (e) => e.cursor_pointer().hover((s) => s.opacity(0.85)))
      .on_click((_e, cx) => { if (enabled) action(cx); }).child(label);
  }

  render() {
    const t = theme();
    const root = v_flex().id("draftwatch-editor").size_full().on_mouse_down("right", paneMenu).min_h(0).bg(t.background).text_color(t.foreground).text_size(13)
      .on_key_down((e, cx) => { if (e.keystroke === "cmd-s") { cx.stop_propagation(); this.save(cx); } });
    if (!this.file) return root.p(14).text_color(t.dim).child("No file. Ask your agent to open a file in Draftwatch, or run: python3 ~/.config/herdr-app/mods/draftwatch-diff/bridge.py open <file>");
    const status = this.saving ? "Saving…" : this.dirty ? "Unsaved changes" : this.note ?? "";
    return root
      .child(h_flex().h(38).flex_none().px(14).gap(10).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().truncate().child(this.file.split("/").pop()))
        .child(div().text_size(11).text_color(t.dim).child(`${this.words.toLocaleString()} words`))
        .child(div().flex_1())
        .child(div().text_size(11).text_color(this.dirty ? t.blocked : t.dim).child(status))
        .child(this.button("dw-save", "Save", t, true, this.dirty && !this.saving && this.theirs === null, (cx) => this.save(cx))).child(closeButton(t)))
      .when(this.theirs !== null, (e) => e.child(h_flex().flex_none().px(14).py(8).gap(8).items_center().bg(t.raised).border_b(1).border_color(t.border)
        .child(div().flex_1().min_w(0).text_size(12).child("This file changed on disk while you had unsaved edits."))
        .child(this.button("dw-theirs", "Load disk version", t, false, true, (cx) => this.loadTheirs(cx)))
        .child(this.button("dw-mine", "Keep my edits", t, true, true, (cx) => this.keepMine(cx)))))
      .child(this.state ? Editor.new(this.state).flex_1().min_h(0)
        : div().p(14).text_color(t.dim).child(this.error ?? "Loading…"));
  }
}
