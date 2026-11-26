import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, shadowVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Popover } from "@astryxdesign/core/Popover";
import { Selector } from "@astryxdesign/core/Selector";
import { Spinner } from "@astryxdesign/core/Spinner";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";
import { VStack } from "@astryxdesign/core/VStack";
import { api, apiUrl, HUB_PROJECT, type Project } from "./api";
import { useAccess } from "./Access";
import { appSearch, appUrl, paramsOf, projectRoot } from "./appMode";
import { Mark } from "./Brand";
import { codeGroup, DownloadMenu, viewOutputGroups } from "./Export";
import { IconCopy, IconExpand, IconMoon, IconPlay, IconShare, IconSliders, IconSun } from "./icons";
import { useColorMode } from "./theme/colorMode";
import { failed, Toasts } from "./toast";
import { StateDot, statusOf, statusText } from "./ui/state";
import { useCopy } from "./ui/useCopy";
import type { FrameError, FrameStatus } from "./views/host";
import type { Params } from "./views/protocol";
import { busy, Refreshing, useLiveRefresh, ViewFrame } from "./views/frame";
import { ParamBar, paramsState, useParamChange } from "./views/params";

const ShareDialog = lazy(() => import("./Share").then((m) => ({ default: m.ShareDialog })));

/**
 * App mode (`?app=<view>`): one view, full bleed, with none of the
 * workbench around it. What the workbench would have offered around the view
 * (its parameters, the other views, Run, Download, the theme, a link to
 * this page, the way back to the workbench) is in one floating Tools button.
 * The page follows the project the way the workbench does: a run that
 * replaces an output the view reads reloads it, a saved file rebuilds it.
 */
export default function AppView(props: { view: string }) {
  const { canEdit, access: acc } = useAccess();
  const canShare = !!HUB_PROJECT && !!acc?.sharing && !(acc.link && !acc.user);
  const [dark, toggleTheme] = useColorMode();
  const [name, setName] = useState(props.view);
  // Null until the address's parameters are read: they need the project's declarations to be typed.
  const [overrides, setOverrides] = useState<Params | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [badAddress, setBadAddress] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [conn, setConn] = useState(true);
  const [status, setStatus] = useState<FrameStatus>({ phase: "loading" });
  const [errors, setErrors] = useState<FrameError[]>([]);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [copied, copy] = useCopy();
  const firstLoad = useRef(true);

  // The project at our parameter set, read again on every graph or run event.
  useEffect(() => {
    let live = true;
    api.project(overrides ?? {}).then(
      (p) => {
        if (!live) return;
        if (overrides === null) {
          const fromAddress = paramsOf(location.search, p.params);
          setOverrides(fromAddress);
          if (Object.keys(fromAddress).length) return; // read again at those values
        }
        firstLoad.current = false;
        setProject(p);
        setLoadErr(null);
      },
      (e: Error) => {
        if (!live) return;
        // The address's parameters were refused: show the view at the defaults and say why.
        if (firstLoad.current && overrides && Object.keys(overrides).length) {
          setBadAddress(e.message);
          setOverrides({});
        } else setLoadErr(e.message);
      },
    );
    return () => {
      live = false;
    };
  }, [overrides, tick]);

  useEffect(() => {
    const es = new EventSource(apiUrl("/api/events"));
    let pending: number | null = null;
    es.onopen = () => setConn(true);
    es.onerror = () => setConn(false);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data);
      if ((e.type === "graph" || e.type === "run" || e.type === "hello") && pending === null)
        pending = window.setTimeout(() => {
          pending = null;
          setTick((t) => t + 1);
        }, 120);
    };
    return () => {
      es.close();
      if (pending) clearTimeout(pending);
    };
  }, []);

  // The address says what is shown, so copying it (or reloading) gives the same page.
  useEffect(() => {
    if (overrides) history.replaceState(null, "", location.pathname + appSearch(name, overrides) + location.hash);
  }, [name, overrides]);

  const views = project?.views ?? [];
  const view = views.find((v) => v.name === name) ?? null;
  const state = useMemo(() => paramsState(project?.params ?? []), [project]);
  const refreshing = useLiveRefresh(view, overrides);
  useEffect(() => {
    if (project) document.title = `${view ? view.title || view.name : name} · ${project.name}`;
  }, [project, view, name]);

  const changeParams = useParamChange(overrides, (next, p) => {
    setOverrides(next);
    setProject(p);
    setBadAddress(null);
  });

  const onStatus = useCallback((s: FrameStatus) => {
    setStatus(s);
    if ("error" in s) setErrors((es) => [...es.slice(-19), s]);
  }, []);
  useEffect(() => {
    setErrors([]);
    setStatus({ phase: "loading" });
  }, [view?.hash, name]);

  const toRun = view ? view.outputs.filter((o) => (o.state !== "fresh" || !o.artifact_id) && !busy(o.state)).map((o) => o.node) : [];
  const workbench = projectRoot(HUB_PROJECT) + `#views/${name}`;
  const title = view ? view.title || view.name : name;
  const problem = !!badAddress || errors.length > 0 || !conn;

  if (!project) {
    return (
      <div {...stylex.props(styles.center)}>
        {loadErr ? <Banner status="error" title={loadErr} /> : <Spinner aria-label="Loading the view" />}
      </div>
    );
  }

  if (!view || view.state === "error") {
    return (
      <div {...stylex.props(styles.center)}>
        <EmptyState
          icon={<Mark size={40} />}
          title={view ? `${title} can't be shown` : `No view named ${name}`}
          headingLevel={1}
          description={view ? (view.reason ?? "Its view.json needs fixing.") : "It was renamed or deleted, or the address is wrong."}
          actions={<Button variant="primary" label="Open the workbench" href={view ? workbench : projectRoot(HUB_PROJECT) + "#views"} />}
        />
      </div>
    );
  }

  const tools = (
    <VStack gap={3}>
      <VStack gap={1}>
        <HStack gap={2} vAlign="center">
          <Heading level={4} accessibilityLevel={2} maxLines={1} xstyle={styles.grow}>
            {title}
          </Heading>
          <HStack gap={1.5} vAlign="center" xstyle={styles.fixed}>
            <StateDot state={view.state} />
            <Text type="supporting" weight="medium" xstyle={statusText[statusOf(view.state)]}>
              {view.state}
            </Text>
          </HStack>
        </HStack>
        {view.state !== "ready" && view.reason && (
          <Text type="supporting" xstyle={statusText[statusOf(view.state)]}>
            {view.reason}
          </Text>
        )}
        {!conn && <Text type="supporting" color="secondary">Reconnecting to the project…</Text>}
      </VStack>

      {views.length > 1 && (
        <Selector
          label="View"
          size="sm"
          width="100%"
          options={views.map((v) => ({ value: v.name, label: v.title || v.name, icon: <StateDot state={v.state} /> }))}
          value={name}
          onChange={(v) => v && setName(v)}
        />
      )}

      {state.specs.length > 0 && (
        <VStack gap={1.5}>
          <Text type="supporting" color="secondary">
            Parameters
          </Text>
          <ParamBar specs={project.params} overrides={overrides ?? {}} onChange={changeParams} />
        </VStack>
      )}

      {badAddress && <Banner status="warning" title="The address's parameters were left out" description={badAddress} />}
      {errors.length > 0 && (
        <Banner
          status="error"
          title={`${errors.length} error${errors.length === 1 ? "" : "s"} from the view`}
          description={`${errors[errors.length - 1].phase}: ${errors[errors.length - 1].error}`}
        />
      )}

      {canEdit && toRun.length > 0 && (
        <Button
          variant="primary"
          icon={<IconPlay />}
          label={`Run ${toRun.length === 1 ? toRun[0] : `${toRun.length} nodes`}`}
          tooltip="Run the read nodes that are missing or stale, for these parameters"
          clickAction={() => api.runWith(toRun, overrides ?? {}).then(() => undefined, failed("Couldn't start the run"))}
        />
      )}

      <Divider />
      <HStack gap={1} vAlign="center" wrap="wrap">
        <Button
          size="sm"
          icon={<IconCopy />}
          label={copied ? "Copied" : "Copy link"}
          tooltip="This view as an app, with these parameters"
          onClick={() => copy(appUrl(location.origin, HUB_PROJECT, name, overrides ?? {}))}
        />
        {canShare && (
          <Button
            size="sm"
            icon={<IconShare />}
            label="Share"
            tooltip="Who can open this project, and links for people without an account"
            onClick={() => {
              setToolsOpen(false);
              setSharing(true);
            }}
          />
        )}
        <DownloadMenu
          small
          label="Download"
          align="left"
          up
          title={`Download what ${title} reads, or its code`}
          groups={[...view.outputs.flatMap((o) => viewOutputGroups(view.name, o, state.params)), codeGroup({ view: view.name, title: "View code" })]}
        />
      </HStack>
      <HStack gap={1} vAlign="center">
        <Button size="sm" variant="ghost" icon={<IconExpand />} label="Open in the workbench" tooltip="The graph, the code and every view" href={workbench} />
        <span {...stylex.props(styles.grow)} />
        <IconButton
          size="sm"
          variant="ghost"
          label={dark ? "Light theme" : "Dark theme"}
          tooltip={dark ? "Light theme" : "Dark theme"}
          icon={dark ? <IconSun /> : <IconMoon />}
          onClick={toggleTheme}
        />
      </HStack>
    </VStack>
  );

  return (
    <div {...stylex.props(styles.app)}>
      <ViewFrame
        key={view.name + view.hash}
        view={view}
        params={state}
        dark={dark}
        hidden={false}
        refreshing={refreshing}
        onParams={changeParams}
        onStatus={onStatus}
      />
      {status.phase !== "loading" && refreshing.length > 0 && (
        <div {...stylex.props(styles.refreshing)}>
          <Refreshing view={view} nodes={refreshing} />
        </div>
      )}
      {status.phase === "loading" && (
        <div {...stylex.props(styles.loading)}>
          <Spinner aria-label="Loading the view" />
        </div>
      )}
      <div {...stylex.props(styles.tools, toolsOpen && styles.toolsOpen)}>
        <Popover label={`Tools for ${title}`} placement="above" alignment="end" width={360} isOpen={toolsOpen} onOpenChange={setToolsOpen} content={tools}>
          <IconButton
            label="Tools"
            tooltip="Parameters, views, download, share and the workbench"
            icon={<IconSliders size={16} />}
            xstyle={styles.toolsButton}
          />
        </Popover>
        {problem && (
          <span {...stylex.props(styles.dot)}>
            {errors.length > 0 ? <Badge variant="error" label={errors.length} /> : <StatusDot variant={badAddress ? "warning" : "neutral"} label="Something to look at" />}
          </span>
        )}
      </div>
      <Suspense fallback={null}>
        {sharing && HUB_PROJECT && (
          <ShareDialog project={HUB_PROJECT} name={project.name} self={acc?.user?.uid ?? null} views={views} view={name} onClose={() => setSharing(false)} />
        )}
      </Suspense>
      <Toasts />
    </div>
  );
}

const styles = stylex.create({
  // The view has the whole window: no bars, no borders.
  app: { position: "fixed", inset: 0, display: "flex", flexDirection: "column", backgroundColor: colorVars["--color-background-body"] },
  center: { minHeight: "100dvh", display: "grid", placeItems: "center", padding: spacingVars["--spacing-6"] },
  loading: { position: "absolute", inset: 0, display: "grid", placeItems: "center", pointerEvents: "none" },
  // Over the view's top corner while newer data is on its way: the view stays readable under it.
  refreshing: {
    position: "fixed",
    insetBlockStart: spacingVars["--spacing-3"],
    insetInlineEnd: spacingVars["--spacing-3"],
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: spacingVars["--spacing-3"],
    borderRadius: radiusVars["--radius-full"],
    backgroundColor: colorVars["--color-background-surface"],
    boxShadow: shadowVars["--shadow-med"],
  },
  // The one control on the page: in a corner, quiet until it is wanted.
  tools: {
    position: "fixed",
    insetBlockEnd: spacingVars["--spacing-4"],
    insetInlineEnd: spacingVars["--spacing-4"],
    opacity: { default: 0.72, ":hover": 1, ":focus-within": 1 },
    transitionProperty: "opacity",
    transitionDuration: "150ms",
  },
  toolsOpen: { opacity: 1 },
  toolsButton: { borderRadius: radiusVars["--radius-full"], boxShadow: shadowVars["--shadow-med"] },
  dot: { position: "absolute", insetBlockStart: `calc(${spacingVars["--spacing-1"]} * -1)`, insetInlineEnd: `calc(${spacingVars["--spacing-1"]} * -1)`, pointerEvents: "none" },
  grow: { flexGrow: 1, minWidth: 0 },
  fixed: { flexShrink: 0, whiteSpace: "nowrap" },
});
