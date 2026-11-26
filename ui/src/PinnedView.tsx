import { useCallback, useEffect, useMemo, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, fontWeightVars, radiusVars, shadowVars, spacingVars, textSizeVars } from "@astryxdesign/core/theme/tokens.stylex";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Text } from "@astryxdesign/core/Text";
import { api, type Project, type ProjectView } from "./api";
import { paramsState, Refreshing, useLiveRefresh, ViewFrame } from "./Views";
import type { Params, ParamsState } from "./views/protocol";
import type { FrameStatus } from "./views/host";
import { IconExpand, IconWindow } from "./icons";
import { StateDot } from "./ui/state";

/**
 * A view pinned on the canvas (`"canvas": true` in its view.json): the view
 * itself, rendered live in a card the size its manifest names, so the graph
 * carries its own overview. It reads, refreshes its live nodes and changes
 * parameters exactly as it does in Views mode; the header opens it there.
 * Its own module, loaded with the first pinned view: it brings the views'
 * runtime host with it.
 */
export default function PinnedView(props: {
  view: ProjectView;
  /** The snapshot the canvas shows, at the project's default parameters. */
  project: Project;
  width: number;
  height: number;
  dark: boolean;
  onOpen: () => void;
  /** As wide as where it is put (the notebook's column), at the height its manifest names. */
  fill?: boolean;
}) {
  const { project, dark } = props;
  // Parameters the view itself set (useParams in its code): the card then shows that set.
  const [overrides, setOverrides] = useState<Params>({});
  const [at, setAt] = useState<Project | null>(null);
  const custom = Object.keys(overrides).length > 0;
  useEffect(() => {
    if (!custom) return setAt(null);
    let live = true;
    api.project(overrides).then((p) => live && setAt(p), () => live && setOverrides({}));
    return () => {
      live = false;
    };
  }, [project, overrides, custom]);
  const shown = at ?? project;
  const view = shown.views.find((v) => v.name === props.view.name) ?? props.view;
  const params = useMemo(() => paramsState(shown.params), [shown]);
  const refreshing = useLiveRefresh(view, overrides);
  const [status, setStatus] = useState<FrameStatus>({ phase: "loading" });
  useEffect(() => setStatus({ phase: "loading" }), [view.hash]);

  const changeParams = useCallback(
    async (patch: Params): Promise<ParamsState> => {
      const next = { ...overrides, ...patch };
      for (const k of Object.keys(next)) if (next[k] === null || next[k] === "") delete next[k];
      const p = await api.project(next);
      setOverrides(next);
      setAt(p);
      return paramsState(p.params);
    },
    [overrides],
  );

  const title = view.title || view.name;
  const problem = view.parse_error ?? ("error" in status ? status.error : null);
  return (
    <div {...stylex.props(styles.card)}>
      {/* The header is the card's handle: drag it to move the card, double-click it to open the view. */}
      <div {...stylex.props(styles.head, props.fill && styles.still)} data-pin-head onDoubleClick={props.onOpen}>
        <IconWindow />
        <span {...stylex.props(styles.title)} title={title}>
          {title}
        </span>
        <Refreshing view={view} nodes={refreshing} />
        <StateDot state={view.state} label={`${view.state}${view.reason ? `: ${view.reason}` : ""}`} />
        <IconButton size="sm" variant="ghost" label={`Open ${title}`} tooltip="Open in Views" icon={<IconExpand />} onClick={props.onOpen} />
      </div>
      {problem ? (
        <div {...stylex.props(styles.problem)}>
          <Text type="supporting" color="secondary" maxLines={3}>
            {problem}
          </Text>
        </div>
      ) : null}
      {/* Wheel and drag inside the view are the view's own: they do not pan the canvas. */}
      <div {...stylex.props(styles.body, props.fill ? styles.fill(props.height) : styles.size(props.width, props.height))}>
        {view.parse_error ? null : (
          <ViewFrame key={view.hash} view={view} params={params} dark={dark} hidden={false} refreshing={refreshing} onParams={changeParams} onStatus={setStatus} />
        )}
      </div>
    </div>
  );
}

const styles = stylex.create({
  card: {
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    color: colorVars["--color-text-primary"],
    backgroundColor: colorVars["--color-background-card"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border-emphasized"],
    borderRadius: radiusVars["--radius-container"],
    boxShadow: shadowVars["--shadow-low"],
  },
  head: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlock: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-1"],
    cursor: "grab",
    color: colorVars["--color-text-secondary"],
    backgroundColor: colorVars["--color-background-surface"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  title: {
    flexGrow: 1,
    minWidth: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    color: colorVars["--color-text-primary"],
    fontSize: textSizeVars["--font-size-base"],
    fontWeight: fontWeightVars["--font-weight-semibold"],
  },
  // Nothing to drag in the notebook.
  still: { cursor: "default" },
  problem: { paddingInline: spacingVars["--spacing-3"], paddingBlock: spacingVars["--spacing-1"] },
  body: { position: "relative", display: "flex", cursor: "default", backgroundColor: colorVars["--color-background-body"] },
  // The size the view's manifest names (`canvas.width`, `canvas.height`).
  size: (width: number, height: number) => ({ width, height }),
  fill: (height: number) => ({ width: "100%", height }),
});
