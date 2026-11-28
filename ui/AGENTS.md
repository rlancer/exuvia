# AGENTS.md

How the exuvia UI (`ui/`) is built. The rules for the repo as a whole are in
`../AGENTS.md`.

## The UI is Astryx components and StyleX, and nothing else

Every screen is made of [Astryx](https://github.com/meta/astryx) components
(`@astryxdesign/core`), and every style of the app's own is StyleX
(`stylex.create`) reading Astryx's design tokens. There is no stylesheet
written by hand: no `.css` file of our own, no `className="…"` string, no
`style={{…}}`, no hex colour and no font named outside `src/theme/`. (The one
`.css` in `src/` is `theme/exuvia.css`, which `bun run theme` generates.)

- **Components first.** Before a raw element, look for the component:
  `bun run astryx search "<what you need>"`, then
  `bun run astryx component <Name>` for its props (read them: names differ
  from other libraries, e.g. `label`, `isDisabled`, `isOpen`).
- **Layout** is `HStack` / `VStack` / `Stack` / `Layout` props (`gap`,
  `padding`, `vAlign`). Where the frame is the app's own (the workbench's
  grid, a pane that scrolls, an editor's column) write StyleX on a raw
  element: `<div {...stylex.props(styles.pane)}>`.
- **Tokens only.** `import { colorVars, spacingVars, radiusVars, … } from
  "@astryxdesign/core/theme/tokens.stylex"`. Raw `px` only for the size of a
  region (a 340px panel, a 40px bar), never for spacing, type or radius.
  exuvia's own values are theme-local tokens (`src/theme/exuviaTheme.ts`,
  `localTokens`), read as `"var(--exuvia-code-background)"`.
- **Colour is the theme's.** `src/theme/brand.ts` is the palette,
  `src/theme/exuviaTheme.ts` maps it onto Astryx's tokens. A new colour, a
  denser control or a restyled component is a change there, never on a page.
  The app loads the theme as built: run `bun run theme` after a change to
  either file and commit what it writes (`exuvia.css`, `exuvia.js` and their
  declarations); `bun run build` fails while they are behind.
- **States** of a node, a run or a view go through `src/ui/state.tsx`:
  `<StateDot state={…} />`, `statusOf(state)`, `statusText[…]`. Green, amber
  and red are states and never decoration.

### StyleX, as this app writes it

```tsx
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";

const NARROW = "@media (max-width: 1100px)";

const styles = stylex.create({
  row: {
    display: "flex",
    gap: spacingVars["--spacing-2"],
    // A condition goes inside the property, never around the rule.
    paddingInline: { default: spacingVars["--spacing-4"], [NARROW]: spacingVars["--spacing-2"] },
    backgroundColor: { default: "transparent", ":hover": colorVars["--color-background-muted"] },
  },
  on: { backgroundColor: colorVars["--color-accent-muted"] },
});

<div {...stylex.props(styles.row, isOn && styles.on)} />   // a raw element
<Button xstyle={styles.end} label="Save" />                // an Astryx component
```

- No descendant or class selectors (`.a .b`, `> svg`): style the element
  itself; for "when my ancestor is hovered" use `stylex.when.ancestor(":hover")`
  with `stylex.defaultMarker()` on the ancestor (see `src/Brand.tsx`).
- A state is a conditional style (`cond && styles.on`), not a class name.
- Third-party widgets are themed through their own API: CodeMirror through
  `src/ui/CodeEditor.tsx`, React Flow through its `--xy-*` custom properties
  set in a StyleX rule (`src/Canvas.tsx`).

### What is already there

| For | Use |
| --- | --- |
| A dialog | `AppDialog` (`src/ui/AppDialog.tsx`): title, one-line subtitle, body, footer |
| "Are you sure?" | Astryx `AlertDialog` |
| A code editor | `CodeEditor` (`src/ui/CodeEditor.tsx`) |
| A table of rows | `DataTable` for a few hundred rows that are already here; `RowsTable` for any number, drawn as they scroll into view and read a page at a time (`useRowPages` for an output as stored, `useAskedRows` for one filtered, sorted or queried: the SQL is built in `src/tableQuery.ts`), all in `src/NodeOutput.tsx`. A node's output wherever it is shown (the Data tab, a notebook cell, a chart full screen) is `OutputTable.tsx`: `useOutputTable` with `OutputRows` under the chart and `OutputChartPick`, so it filters, sorts and queries the same everywhere |
| A state's dot or colour | `src/ui/state.tsx` |
| An error the user should see | `notifyError` / `failed` (`src/toast.tsx`) |
| Icons | `src/icons.tsx` (the app's own 16px set), passed to a component's `icon` |

## Hierarchy

- **Two bars, from the outside in.** The app bar is the project as a whole
  on the left (its name, then Share and download beside it) and the app
  itself on the right (help, theme, account). The project's bar under it is
  Graph and Views, and what the graph is doing and can be told to do (state
  counts, New node, Run stale). A control goes in the bar of the thing it
  acts on.
- **The side panel** is Agent, Files, History and Settings. Settings is how
  the project runs and what it keeps (packages, environment, schedule,
  storage): a section each, with what waits for a person (the agent's
  requests) answered there, and the whole of it in a dialog or a page in
  the main area.
- **One primary action per region** (`variant="primary"`): Run stale in the
  project's bar, Run in a node's inspector, the submit of a dialog. Everything
  else is `secondary` or `ghost`; what destroys is `destructive` and asks
  first.
- **Rank with weight and the secondary colour, not with size.** A title is a
  `Heading`; body text is `Text`; a hint, a timestamp or a count is
  `<Text type="supporting" color="secondary">`. No uppercase micro-labels.
- **Rows, not cards**, for lists of people, links, runs, files (`List` /
  `ListItem`, `Table`). `Badge` is for a count or a status, never for
  metadata.
- Every icon-only control has a `label` and a `tooltip`.

## Checking a change

```bash
bun run typecheck
bun run test
bun run build                      # also what `exuvia serve` shows
bun run doctor                     # Astryx's own checks of the setup and the code
```

Look at it in both colour modes and at a narrow width (the workbench stacks
under 1100px, and its bars lose their words under 900px).

Capture in dark mode. A screenshot of a change, whether to check it or to
show it in a PR, is taken in the dark theme, as the website's are: a
Playwright context with `colorScheme: "dark"` and
`localStorage["exuvia-theme"] = "dark"` set before the page loads
(`site/tools/capture.mjs` does both). Take a light one as well when the
change touches colour.

A change to what the workbench shows on every screen (its bars, the side
panel's tabs, where a control lives) makes the website's screenshots wrong,
and nothing checks them: recapture them in the same PR with
`site/tools/capture.mjs` (its header says how: modes `app`, `hub` and
`tutorial`, each against a scratch copy) and look at each one.

## Astryx's own guide

Written by `bun run astryx init`; it rewrites this block on an upgrade.

<!-- ASTRYX:START -->
Astryx v0.7.17 · 174 components
CLI: run every command as `bunx astryx <cmd>` (shown below as `astryx ...`).

SETUP (once, in app entry e.g. main.tsx) — without these, components render unstyled:
  import "@astryxdesign/core/reset.css";
  import "@astryxdesign/core/astryx.css";

WORKFLOW — start every page from a template, never from scratch:
1. `astryx build "<idea>"` — names the template to scaffold from and the parts it lacks.
2. `astryx template <name> <path>` — scaffold it; keep its frame, gap and padding; replace the content.
3. `astryx component <Name>` to read props before using one; `astryx template <Block>` prints a block example to read for a part it lacks. Astryx names differ from other libraries: buttons, badges, tokens and tabs take their text in `label` (Text, Heading and Link take children), booleans start with `is` or `has` (`isOpen`, `isDisabled`), and every input takes `value` and `onChange`.
Changing an existing page? Keep it: skip step 2 and add components inside its sections; read a block example when it helps.
Before you deliver a build, run `astryx doctor --built <dir>`; don't deliver it if it fails.

RULES:
- No <div> — components handle all layout and spacing.
- Read `astryx docs layout` before changing a template's frame.
- Dense data = rows (Table, List/Item), not Card-wrapped lists. Badge = counts only.
- Style with component props first, then the xstyle prop / StyleX tokens. No raw hex/px.
- THEMES: palettes and custom colors go through the theme, never :root overrides; so does a denser or roomier app (`editor set <name> spacing tight`), never gap and padding changes page by page. A change to one kind of part (the sidebar, its selected item, the gaps in a form) is a theme change too, not props on one page: `editor how "<ask>"` names the `set … --on <part>` line. No app yet: `astryx experimental theme preview app <dir>` makes a starter app that shows the theme. To make or change a theme (colors, fonts, corners, spacing, a look from a picture), prefer the experimental theme editor over hand-written theme files: `astryx experimental theme editor new <name> --like neutral`, then `editor set <name> <thing> <value>` or `editor how "<what you want>"`; wire it with `astryx theme add <name>`; after each change look at it with `astryx experimental theme editor show <name> --png` (takes pictures of real components) and open the PNGs it writes; a theme change is not done until you have looked at the pictures. In code, extend an imported theme to customize it; `theme eject <slug>` only forks source. See `astryx docs theme`.
- SELF-CHECK: re-read the file; replace any className=, style={{…}}, raw <div>/<span>, imported .css/@apply, or hardcoded #hex/px with the component or the xstyle prop + a token.

`astryx help` lists every command. Key ones beyond the workflow:
  search "<query>"   find any component / hook / doc / template
  discover <words>   integrations you could add
  docs <topic>       getting-started, principles, tokens, theme … (`astryx docs` lists all)
  docs cli           commands and API reference
  docs authoring     build an integration package, and every field it holds
  upgrade --from <old version> --apply   run after a dependency bump
To remove this block, run `astryx init --remove-agents`, which removes it from the agent files at the project root and in the folder you run it in; from any other file it leaves, delete everything from ASTRYX:START to ASTRYX:END.
<!-- ASTRYX:END -->
