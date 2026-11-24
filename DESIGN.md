# Server OPS — design guidelines

How the web UI (`bin/web-ui`) looks and how a new page or feature keeps that look. All CSS is the `STYLE`
string in `bin/web-ui` (served as `/ui.css`), and all markup comes from the `Page` class and the module helpers
next to it (`badge`, `lamp`, `facts`, `term`, `diff_term`, `level_chip`). When this file and the code disagree,
the code is right and this file gets fixed.

## 1. Principles

- **An ops console.** People scan a page for state first and act on it second. Put a summary before the detail:
  stats, then the attention list, then tiles, then tables. Every alarm links to the place where it can be fixed.
- **Preview before run.** Every action posts to `/preview`, which runs the command with `--check` and shows what
  would change. Only the preview page has the `Run` button. No button changes the server in one click.
- **State is shown by shape and colour together**: a lamp, a stripe on the left of a tile or row, a chip word.
  Never by colour alone. The words are `running`, `failed`, `none`, and so on.
- **A dark industrial control board.** It is dark only, with no light theme. Monospace everywhere, an amber
  accent, lamps, status stripes, hazard tape for prod and danger, and black terminal blocks. Readability is
  fixed inside this look, never by leaving it.
- **Calm by default.** Only what needs a look stands out. Things that are missing or not set up are `muted` and
  are not alarms. Red means broken.

## 2. Hard constraints

- **CSP**: `default-src 'none'; style-src 'self'; form-action 'self'`. So there is **no JavaScript and no inline
  `style=`** attribute or `<style>` element. Interaction uses plain HTML: forms, `<details>`, radio buttons
  styled as `.seg`, `:has()` (see `guide_css()`), anchors, and `<meta refresh>` (`layout(refresh=3)`).
- All CSS goes in `STYLE`. A rule that depends on data is generated in Python and appended, as `guide_css()` is.
- **Every colour is a `--token`** in the first `:root{…}`. The only literals left are `#fff` (text on signs and
  fills) and `#111` (the dark stripes of hazard tape). Do not add more of them. A new one becomes a token.
- No external fonts, CDNs or images. The page is self-contained, and icons are Unicode (`▸ ▾ → ←`) or CSS.
- Python 3.10 (CI runs in `tests/fixtures/ci.Dockerfile`), so no 3.12-only syntax in the f-strings that build markup.
- All UI text is English. Escape every value with `e()`.
- Content is visible at rest. There are no entrance animations and nothing starts at `opacity:0`. The only
  motion is a status signal: the `.lamp.run` pulse, the `.nav i.dot.bad` pulse and the `.bar.run` march. All of
  it stops under `prefers-reduced-motion`.

## 3. Colour tokens

Contrast is WCAG 2.x, measured from the `:root` values. Alpha tokens are measured over `--panel`.

**Surfaces**, from deep to raised:

| Token | Value | Role |
|---|---|---|
| `--board` | `#0c0e0b` | page background (with a faint amber `--wash` glow at the top left) |
| `--panel` | `#13170f` | cards, tiles, table rows, forms, facts, callouts |
| `--panel-2` | `#191e14` | raised: buttons, active tab, row hover, top of plates |
| `--field` | `#0f120c` | inputs, selects, the `.seg` track, `code` |
| `--term-bg` | `#070806` | terminal and log blocks (`.term`, `pre.json`, `pre.cmd`) |
| `--rule` | `#283022` | dividers and card borders, decorative only (1.3–1.4:1) |
| `--edge` | `#7a8566` | **control edges**: inputs, buttons, chips, `.seg`, `.empty` (4.3–5.0:1, needs ≥ 3:1) |

**Text** (minimum 4.5:1; all of them reach it on every surface):

| Token | board | panel | panel-2 | field | Use |
|---|---|---|---|---|---|
| `--ink` `#ece6d3` | 15.5 | 14.5 | 13.6 | 15.1 | body text, values, names |
| `--dim` `#b0b4a1` | 9.1 | 8.6 | 8.0 | 8.9 | labels, column heads, notes, secondary lines |
| `--faint` `#969b88` | 6.8 | 6.3 | 5.9 | 6.6 | the least important text: repo under a name, `since …`, placeholders, `.muted` |
| `--amber` `#ffb22e` | 10.8 | 10.1 | 9.4 | 10.5 | accent: links, h2, focus, active nav/tab, `warn` |
| `--go` `#4fe08c` | 11.4 | 10.7 | 10.0 | 11.1 | `ok`: running, succeeded, up to date |
| `--stop` `#ff5b4f` | 6.3 | 5.9 | 5.6 | 6.2 | `bad`: failed, missing where required, Stop |
| `--steel` `#7b9dd0` | 7.0 | 6.6 | 6.1 | 6.8 | the third state hue: read-only, DB-level, "view" |
| `--steel-ink` `#c3d6f3` | 13.1 | 12.3 | 11.5 | 12.8 | text on steel tints |
| `--fresh` `#e8c56b` | 11.7 | 10.9 | 10.2 | 11.4 | the `fresh` backup level |

Use the three greys this way: **ink** for anything a person reads to decide, **dim** for what names it (labels,
headers, notes), and **faint** for context that could be dropped. Do not put whole paragraphs in faint.

**Semantic colour is not the accent.** `go`, `stop` and `amber` (= warn) mean states. Amber is also the brand
accent (links, h2, primary buttons), so a `warn` is always carried by a word or chip as well, never by amber
text alone. `steel` is a neutral "other level", not an error and not a success.

**Lines and tints that go with each hue.** Use `*-line` for chip borders (`--go-line` 4.2, `--stop-line` 3.9,
`--amber-line` 4.1, `--steel-line` 3.9 on panel). Use `*-tint` and `*-on` for chip and seg fills, `*-glow` for
lamp halos. Text on fills: `--on-accent` `#1a1205` on `--amber-fill` reads 10.3:1 (11.2 on `--fresh-fill`);
`--go` on `--go-on` 6.5; `--steel-ink` on `--steel-on` 7.4; `--ink` on `--seg-on` 8.1.

**Sign fills** carry `#fff` text: `--steel-fill` (other environments) 5.7:1, `--stage-fill` 5.8,
`--prod-fill` 6.5. `--hazard` `#f4c20d` with `#111` gives 11.3.

**Terminal** (on `--term-bg`): `--term-ink` 15.6, `--term-faint` (line numbers) 6.3, `--term-go` 11.8,
`--term-stop` 7.2, `--term-steel` 7.4, `--term-amber` 11.1. `--term-hover` is the row hover.

**Chrome**: `--top-1`/`--top-2` (header gradient), `--lamp-off`, `--count-bg`, `--track` (progress bar),
`--seg-on`, `--row-edge`, `--scroll-thumb`.

**Adding a token.** First check that no existing token has the same role. Name it by role, not by colour
(`--term-hover`, not `--green-3`). Put it on the right line of `:root` and measure it before committing:
text ≥ 4.5:1 on `board`, `panel`, `panel-2` and `field`, and control edges and lamps ≥ 3:1. Add its row to
this table.

## 4. Type

- One family: `--mono` (`ui-monospace, "JetBrains Mono", …, monospace`), with `font-variant-numeric:tabular-nums`
  on `body`. Never a sans or a web font.
- **Nothing is smaller than 12 px.** The only exceptions are 11.5 px chips (`.chip`, `.lv`), signs (`.sign`),
  `.seg` segments, nav counts and compact `.controls` buttons.
- `letter-spacing` is at most `.08em`. The single exception is `h2` at `.1em`. Running text uses `0`.

| Role | Selector | Size / weight | Spacing | Case |
|---|---|---|---|---|
| Page title | `h1` | 20 px / 700 | .06em | UPPER |
| Section rule | `h2` | 13 px / 700, amber, rule to the right | .1em | UPPER |
| Card / form title | `h3` | 14 px / 700, ink | .06em | UPPER |
| Command under a title | `h3 small` | 12 px / 400, faint | 0 | as typed |
| Body, inputs | `body`, `input` | 14 px / 1.55 (inputs 16 px under 600 px, so iOS does not zoom) | 0 | as typed |
| Table cells, facts, notes | `.board td`, `.list td`, `.facts`, `.note` | 13 px | 0 | as typed |
| Column heads, stat labels, block labels | `.board th`, `.list th`, `.stat span`, `.block-label` | 12 px / 600, dim | .08em | UPPER |
| Buttons, tabs | `.btn`, `button`, `.tabs a` | 12 px / 700 | .06–.08em | UPPER |
| Chips, levels, signs, segments | `.chip`, `.lv`, `.sign`, `.seg span` | 11.5 px / 700–800 | .06–.08em | UPPER |
| Small print | `small`, `.when`, `.foot` | 12 px | 0 | as typed |
| Big numbers | `.stat b` 22 px, `.plate-version` 34 px, `.keytitle` 26 px | 700–800 | 0 | as typed |

**Uppercase** is only for short fixed words: headings, column heads, buttons, tabs, and chip and level words.
**Never uppercase case-sensitive data**: commands, paths, unit names, refs, hashes, users, units such as `KB`,
`h` or `min`. Put such values in plain text or `<code>`, not in a `.chip`. (A few chips still show a time or
size, such as `2 h ago` and `29.0 GB free`. That is debt, not a pattern to copy.) Card names (`.unit .name`),
facts headers and `details > summary` are deliberately not uppercase.

## 5. Layout and spacing

- `main` has `22px 28px 60px` padding and a `max-width:1760px`. The header `.top` holds the mark, `.nav`, the
  `.busy` lamp, and `.who` (clock, account, Sign out).
- **`.deck`** is the grid for forms and cards: `repeat(auto-fit, minmax(min(100%,560px),1fr))`, `gap:12px`.
  Forms should be wide; do not cap inputs. `.deck.wide` gives one column.
- **`.split`** is the `200px` sticky `.index` plus `.sections`, for long pages with anchors (Actions). `h2[id]`
  has a `scroll-margin`.
- **`.units`**: tiles in `repeat(auto-fill, minmax(290px,1fr))` with `gap:10px`. **`.plates`**: big version
  plates, minimum 420 px.
- **Vertical rhythm.** A block (`.stats`, `.list`, `.facts`, `.deck`, `.bulkbox`, a standalone `form.action`,
  `.scroll`) ends with **14 px**. A `.board` ends with 8 px, because its rows already have 6 px spacing. `h2`
  sits 34 px above its content and 12 px below. Callouts and tape take 16 px. Inside cards, use flex or grid
  `gap`, never stacked margins.
- **Breakpoints.**
  - `≤ 900px`: the header becomes a grid and `.nav` scrolls on one line. `.deck` goes to one column, `.stats` to
    two columns, `label` stacks (label above field) and `.form-foot` stacks with the button on the right. Button
    hit areas grow to 40 px. A `.board` becomes cards: no `thead`, one block per row, the stripe on the card, and
    each cell named by its `data-label`. `.facts` stacks, `.split` collapses (the index turns into chip links),
    and `.list` scrolls inside itself. `.tabs` scroll.
  - `≤ 600px`: `.ramp` and `.keyhead` go to one column, big numbers shrink, and inputs move to 16 px.
- At **390 px there is no horizontal page scroll**. Wide content scrolls inside `.scroll`, `.list`, `.term` or `.nav`.

## 6. Components

**Page and nav**: `return self.layout(title, body, section='key')`. `section` lights the nav button and adds
the "View only" callout for viewers. A new section needs entries in `NAVIGATION` and `SECTIONS`. Nav marks are
`i.count` and `i.dot.warn|bad` (see `Page.marks`). Use `refresh=3` only while something runs.

**Section rule**: `<h2 id="anchor">Backups</h2>` is amber, uppercase and extends a line to the right. It is the
main way to break up a page. Do not use it inside cards; use `h3` there.

**Stats strip**: `<div class="stats"><div class="stat"><b>13</b><span>projects</span></div>…</div>`. Use it for
3–6 headline numbers at the top of a page. On a phone it becomes two columns, and an odd last stat spans both.

**Attention list**: `Page.attention(data)` gives `.attention` with a red stripe, a `.attention-title` with its
count, and lines as `<li><a href>{lamp}<span>text</span></a></li>` in two columns. More than `ATTENTION_FOLD`
(3) alerts of one kind fold into one `<details class="attention-group">` (`22 environments have no backup ▸ SHOW`).
With nothing to report it shows `.attention.calm`, with a green lamp and one sentence. A new alert kind adds an
`ATTENTION_GROUPS` text, and every alert links to where it is fixed.

**Unit tiles**: `self.tile(name, kind, state_html, detail_html, controls_html)` inside `<div class="units">`.
`kind` is `ok | bad | warn | run | muted`, which sets the lamp and the left stripe. `state` is a chip, usually
`unit_state(units, name)`. `detail` is 12 px dim. Controls come from `self.unit_controls(unit, ops)` (Stop gets
`button.stop`), `self.button(...)`, or `<a class="btn">`, and they sit at the bottom of the tile.

**Board table**: the main status table, with each row a card.
```html
<div class="scroll"><table class="board"><thead><tr><th scope="col">Project · repo</th>…</tr></thead><tbody>
<tr class="is-ok"><td class="project" data-label="Project · repo"><a href="…">name</a><small>repo</small></td>
<td data-label="Newest backup"><span class="line"><b>nightly</b> {chip}</span><span class="line"><b>full</b> …</span></td></tr>
```
- The stripe comes from the row class: `is-ok`, `is-bad`, `is-idle`, or none (amber, meaning in progress or
  needs a look).
- **Every `<th>` has `scope="col"`, and every `<td>` has a `data-label`** equal to its header, or
  `data-label=""` for a cell that needs no name. That is what labels the phone cards.
  `assert_board_cells_labelled` in `tests/test_ui.py` checks it.
- `.line` with `<b>` gives a sub-label and value pair inside one cell. Modifiers are `td.project`, `td.target`,
  `td.when`, `.board.keys` and `.board.activity`.

**List table**: `<table class="list">` is plain and dense, for many rows of the same kind (runners, users,
sessions). Use 12 px uppercase dim heads with `white-space:nowrap`. On a phone it scrolls sideways inside
itself. Wrap it in `.scroll` when it can be wide on a desktop as well. `.grants` and `.matrix` are its relatives
for permission grids.

**Facts**: `facts([('Command', '<code>…</code>'), ('Code', badge(...))])` is a key/value panel. The `th` is
13 px dim and not uppercase. It stacks on a phone. Use it at the top of detail and preview pages.

**Chips**: `badge(text, kind)` gives `<span class="chip ok|bad|warn|muted|run">`. Use one or two words of state.
`ok` = green, `bad` = red, `warn` = amber outline, `muted` = faint (not installed, none, n/a, not an alarm),
`run` = solid amber (running now). Do not put chips on chips. A chip is not a button.

**Lamps**: `lamp(kind)` is an 11 px dot with a glow. `run` pulses. Put one before a name or alert line, never
on its own without a word.

**Environment signs**: `<span class="sign {env}">{env}</span>` is a solid sign. `prod` is red with a hazard
strip at the bottom, `stage` is brown-amber, and anything else is steel. Use `env_class()` and
`environment_order()` (prod, stage, others) whenever environments sit side by side.

**Access levels**: `level_chip(level)` gives `.lv .lv-<level>` and climbs in danger: `phpfpm-readonly` steel →
`phpfpm` green → `docker` solid amber → `full` red with a hazard border. The backup levels are `lv-db` (steel)
and `lv-fresh` (solid yellow), and the tools level is `lv-tools`. `<ol class="ramp">` explains a ladder, one
`<li class="lv-x"><b>…</b><span>…</span></li>` per step. A new level needs its `.lv`, `.ramp` and `.seg` rules
together.

**Segmented control**: `Page.seg(name, current, levels, none='—')` builds radio buttons as one strip, with no
script. The checked segment takes its level's fill and an underline. The empty choice is `lv-none lv-off`: when
checked it stays quiet (transparent with an edge underline), because "none" is not an achievement. If the empty
choice stands for an inherited level (`none='all: db'`), it drops `lv-off`. The permission grid uses
`pm-view` (steel), `pm-manage` (amber) and `pm-none`.

**Forms**:
```python
self.form('install-metrics', 'Worker metrics',
          '<label><span>Port (optional)</span><input type="text" name="port" placeholder="9101" autocomplete="off"></label>'
          '<label class="check"><input type="checkbox" name="full" value="yes"> full – every table (--full)</label>',
          note='What it does, defaults, side effects.', command='bin/install-metrics', danger=False)
```
- `form.action` is a panel with a 2 px amber top. It returns `''` when `self.may(action)` is false, so the
  page must still read well with no forms at all.
- A field is always `<label><span>Label</span><input|select|textarea></label>`. That gives a two-column grid
  (`minmax(9em,14em)` for the label, then the field) that stacks under 900 px. Checkboxes use `label.check`.
  Placeholders show an example or the default. Put the command in `command=`, which renders as `h3 small`.
- `.form-foot` puts the note on the left and the button on the right. The default label is `Preview`, drawn as
  an amber outline. The preview page then shows `.changes`, `facts`, `term(output)` and a form with
  `<a class="btn">Cancel</a><button class="run">Run</button>`.
- A **danger form** (`danger=True`) gets a hazard-tape top border. When something can be lost or locked out,
  set `plan.danger` so the preview shows `.tape` above Run.
- One-button actions on tiles or rows use `self.button(action, fields, label, kind)`, which goes through the
  same preview.
- `.toolbar` / `form.search` is a GET filter row. `form.bulk` in `.bulkbox` is a row of bulk controls.

**Buttons**: `.btn` (a link) and `button` share one look: 12 px uppercase on `--panel-2` with an `--edge`
border, at least 32 px tall (40 px on a phone). Hover turns them amber. Kinds:
- `button.run` and `.btn.go` / `button.go` are solid amber, for the one primary step on a page (Run, Sign in,
  + Add a runner).
- `button.stop` is a red outline (Stop, Remove).
- `.controls .btn` is compact at 28 px and 11.5 px, for tiles and rows only.

Use only one solid amber button per view.

**Callouts**: `<div class="callout"><p>…</p></div>` is a panel with a 4 px left stripe: green for done or info,
`.bad` for an error, `.warn` for caution. Use it for results and for "could not read" messages. `.changes` is the
amber "What changes" list on a preview.

**Tape**: `<div class="tape"><span>Deploy to PROD</span></div>` is black and yellow hazard tape with a solid
label. Use it only for irreversible or prod actions, at most once per view. Inside steps it is `.step.is-warn`.

**Details**: `<details><summary>access.log</summary><div class="inner">…</div></details>` is for long or
secondary content (logs, rarely used options). `details.more` is transparent and sits inside a form or under a
card, for "Optional: change the defaults". Summaries are 13 px ink, not uppercase, with an amber `▸`/`▾`.

**Terminal and code**: `term(text)` gives a numbered `.term` with a 70vh cap that scrolls inside itself.
`diff_term(lines)` colours `+`, `-` and `@@` lines. `.term.typed` puts a `$` before each line. `pre.json` is for
request bodies and `pre.cmd` is for copyable commands (under a `.block-label`). Inline values use `<code>`.
Never wrap terminal text in uppercase or chips.

**Steps and guide**: `<ol class="steps">` with `guide_step()` items gives a big amber `01`, `02`… counter, an h3
with a `muted` chip saying where it runs, the text, `pre.cmd` blocks and a `.source`. `.steps-list` is a light
numbered list inside a card. `.guide` is a grid of option labels, with fields hidden through `guide_css()`.

**Empty state**: `<div class="empty">No actions yet. <a href="…">Add one</a></div>` is a dashed box with dim
text. Always say what is missing and how to add it.

**Others in use**: `.plate` (version plate on Update), `.keyhead` and `.keytitle` (key header), `.tabs` with
`a.on` (tabs joined to the box below), `.pointer` (a dashed card that points to another page), `.bar` with
`.run` / `.bad` (job progress), `.runs` (recent jobs list), `.busy` (header link to a running job).

## 7. Checklist for a new page or feature

- [ ] Built from existing helpers and classes: `layout`, `form`, `button`, `tile`, `facts`, `badge`, `lamp`,
      `seg`, `term`, `.deck`, `.board`, `.list`. A new class only for a new kind of thing, and its CSS goes in
      `STYLE`.
- [ ] No JS, no inline `style=` or `<style>`, no external resources. Every value goes through `e()`.
- [ ] Colours only through `var(--token)`. A new token is named by role, measured, and added to §3.
- [ ] Text ≥ 4.5:1 and control edges ≥ 3:1 on the surface they sit on, in `--ink`, `--dim` or `--faint`.
- [ ] Sizes ≥ 12 px (11.5 px only for chips, signs and segments). `letter-spacing` ≤ .08em. No uppercase on
      commands, paths, units or names.
- [ ] Summary before detail: `h1`, then stats or attention, then `h2` sections. Alarms link to their fix.
- [ ] State shown with a stripe, lamp or chip **and** a word. Missing or not installed is `muted`, not `bad`.
- [ ] `.board` cells have `data-label` and headers have `scope="col"`. Covered by `assert_board_cells_labelled`.
- [ ] Every change goes through `/preview` (a `--check` run) before `Run`. Dangerous ones set `danger=True` or
      `plan.danger`.
- [ ] Permissions: the action is in `ACTION_SECTIONS` (or `unit_sections`), and forms and buttons appear only
      when `page.may(...)`. The page still reads well view-only. A new section goes in `SECTIONS` and
      `NAVIGATION`. A new root command goes in `bin/install-ui`'s `ROOT_COMMANDS`, which then needs
      `sudo bin/install-ui` on the server.
- [ ] At 390 px there is no horizontal page scroll. Wide tables scroll inside `.scroll` or `.list`. Forms
      stack. Buttons are ≥ 40 px tall.
- [ ] Nothing animates in. Any new motion is a status signal and stops under `prefers-reduced-motion`.
- [ ] Tests in `tests/test_ui.py` cover the markup, permissions and the preview path:
      `python3 -m unittest discover -s tests`.
- [ ] Look at it: run the `web-ui` launch config (`python3 tests/preview_ui.py`, at 127.0.0.1:9178), add the
      path to `tests/shoot_ui.sh`, run `tests/shoot_ui.sh round-N` (shots land in the git-ignored `.design-review/round-N/`), and view the desktop (1600 px)
      and phone (390 px) shots.
- [ ] README.md's section for the feature is updated (the documentation of record).
