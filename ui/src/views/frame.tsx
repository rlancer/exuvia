import { useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars } from "@astryxdesign/core/theme/tokens.stylex";
import { HStack } from "@astryxdesign/core/HStack";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { api, type ProjectView } from "../api";
import { ViewBridge, frameDocument, type FrameStatus } from "./host";
import type { Params, ParamsState } from "./protocol";

/*
 * Where a view runs, wherever it is shown: Views mode, a view as an app
 * (AppView.tsx) and a view pinned on the canvas or in the notebook
 * (PinnedView.tsx). The sandboxed frame, the live nodes it refreshes and the
 * indicator that says so.
 */

/**
 * Live nodes (`live=True`): opening a view, or showing it at other
 * parameters, re-runs the live nodes it depends on. The view renders what is
 * stored at once and updates when the new outputs land. Returns the read
 * nodes being re-run (by that or by any other run), for the indicator and
 * the view's `useRefreshing()`; empty when nothing is on its way.
 * `params` null: not known yet, ask nothing.
 */
export function useLiveRefresh(view: ProjectView | null, params: Params | null): string[] {
  const name = view?.name ?? null;
  const live = (view?.live?.length ?? 0) > 0;
  const key = params === null ? null : JSON.stringify(params);
  // "asking": the request is out. A view: the answer came while that
  // snapshot was shown, which predates the runs; the next one shows them.
  const [waiting, setWaiting] = useState<"asking" | ProjectView | null>(null);
  const shown = useRef(view);
  shown.current = view;
  useEffect(() => {
    if (!name || !live || key === null) return;
    let on = true;
    setWaiting("asking");
    api.refreshView(name, JSON.parse(key) as Params).then(
      (r) => on && setWaiting(r.started.length ? shown.current : null),
      (e: Error) => {
        if (!on) return;
        setWaiting(null);
        console.warn(`Could not refresh the live nodes of ${name}: ${e.message}`);
      },
    );
    return () => {
      on = false;
      setWaiting(null);
    };
  }, [name, live, key]);
  useEffect(() => {
    if (waiting !== null && waiting !== "asking" && waiting !== view) setWaiting(null);
  }, [view, waiting]);
  return useMemo(() => {
    if (!view) return [];
    if (waiting === "asking" || waiting === view) return view.reads;
    return view.outputs.filter((o) => busy(o.state)).map((o) => o.node);
  }, [view, waiting]);
}

/** A read node being re-run: its output is on its way. */
export const busy = (state: string) => state === "running" || state === "queued";

/** "Refreshing", while what a view reads is being re-run and it shows the last output. */
export function Refreshing(props: { view: ProjectView; nodes: string[]; xstyle?: stylex.StyleXStyles }) {
  if (!props.nodes.length) return null;
  const live = props.view.live ?? [];
  const tip = live.length
    ? `Showing the last output while the live data is refreshed (${live.join(", ")}); the view updates when it lands`
    : `Showing the last output while ${props.nodes.join(", ")} re-run${props.nodes.length === 1 ? "s" : ""}; the view updates when it lands`;
  return (
    <Tooltip content={tip}>
      <HStack gap={1.5} vAlign="center" xstyle={props.xstyle}>
        <Spinner size="sm" aria-label="Refreshing" />
        <Text type="supporting" color="secondary">
          Refreshing
        </Text>
      </HStack>
    </Tooltip>
  );
}

/** The sandboxed iframe and its bridge. Remounted when the view's files change. */
export function ViewFrame(props: {
  view: ProjectView;
  params: ParamsState;
  dark: boolean;
  /** Kept running but not shown (while the Code pane is open). */
  hidden: boolean;
  /** Read nodes being re-run, for the view's `useRefreshing()`. */
  refreshing?: string[];
  onParams: (p: Params) => Promise<ParamsState>;
  onStatus: (s: FrameStatus) => void;
}) {
  const ref = useRef<HTMLIFrameElement>(null);
  const bridge = useRef<ViewBridge | null>(null);
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    const p = latest.current;
    const b = new ViewBridge(ref.current!, p.view, p.params, p.dark, {
      setParams: (x) => latest.current.onParams(x),
      onStatus: (s) => latest.current.onStatus(s),
    });
    bridge.current = b;
    b.setRefreshing(p.refreshing ?? []);
    const stop = b.start();
    // Only now load the document: its bootstrap's first message must find
    // the bridge listening.
    ref.current!.srcdoc = frameDocument();
    return () => {
      stop();
      bridge.current = null;
    };
  }, []);

  useEffect(() => bridge.current?.update(props.view, props.params), [props.view, props.params]);
  useEffect(() => bridge.current?.setTheme(props.dark), [props.dark]);
  const refreshing = (props.refreshing ?? []).join();
  useEffect(() => bridge.current?.setRefreshing(refreshing ? refreshing.split(",") : []), [refreshing]);

  return (
    <iframe
      ref={ref}
      {...stylex.props(styles.frame, props.hidden && styles.hidden)}
      title={props.view.title || props.view.name}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      hidden={props.hidden}
    />
  );
}

const styles = stylex.create({
  frame: {
    display: "block",
    flexGrow: 1,
    minHeight: 0,
    width: "100%",
    borderWidth: 0,
    backgroundColor: colorVars["--color-background-body"],
  },
  hidden: { display: "none" },
});
