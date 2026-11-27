import { useEffect, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/Code";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { HStack } from "@astryxdesign/core/HStack";
import { List, ListItem } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { VStack } from "@astryxdesign/core/VStack";
import { api, type EnvState, type GraphNode, type PackagesState, type StorageState } from "./api";
import { asker } from "./EnvDialog";
import { STATUS } from "./PackagesDialog";
import { places } from "./Storage";
import { IconClock, IconDisk, IconKey, IconPackage } from "./icons";
import { StateDot } from "./ui/state";
import { failed } from "./toast";
import { fmtBytes, until } from "./util";

/** How many rows of a section the panel lists before "Manage…" takes over. */
const SHOWN = 6;

/**
 * The side panel's Settings tab: how the project runs and what it keeps. Its
 * packages, environment, schedule and storage, one section each. What the
 * agent asks for (a package, a secret) is answered here, beside the chat that
 * asked; the whole of a section is its dialog (Packages, Environment) or its
 * page in the main area (Schedule, Storage). A viewer, who changes nothing,
 * sees Schedule and Storage.
 */
export function ProjectSettings(props: {
  canEdit: boolean;
  pkgs: PackagesState | null;
  onPkgs: (s: PackagesState) => void;
  env: EnvState | null;
  onEnv: (s: EnvState) => void;
  /** Open the Packages dialog. */
  onPackages: () => void;
  /** Open the Environment dialog, on a variable to set when one is named. */
  onEnvironment: (name?: string) => void;
  /** The graph's nodes, for the ones on a schedule. */
  nodes: GraphNode[];
  /** Show the Schedule page in the main area. */
  onSchedule: () => void;
  /** Show the Storage page in the main area. */
  onStorage: () => void;
}) {
  const { canEdit } = props;
  return (
    <div {...stylex.props(styles.scroll)}>
      {canEdit && <PackagesSection pkgs={props.pkgs} onPkgs={props.onPkgs} onManage={props.onPackages} />}
      {canEdit && <EnvSection env={props.env} onEnv={props.onEnv} onManage={props.onEnvironment} />}
      <ScheduleSection nodes={props.nodes} onOpen={props.onSchedule} />
      <StorageSection onOpen={props.onStorage} />
    </div>
  );
}

/** A section's trigger: its icon, its name, and what it has to say before it is opened. */
function Trigger({ icon, label, end }: { icon: ReactNode; label: string; end?: ReactNode }) {
  return (
    <HStack gap={2} vAlign="center" xstyle={styles.trigger}>
      {icon}
      <Text weight="medium">{label}</Text>
      <span {...stylex.props(styles.grow)} />
      {end}
    </HStack>
  );
}

function PackagesSection({ pkgs, onPkgs, onManage }: { pkgs: PackagesState | null; onPkgs: (s: PackagesState) => void; onManage: () => void }) {
  const [busy, setBusy] = useState<number | null>(null);
  const act = (id: number, call: () => Promise<PackagesState>) => {
    setBusy(id);
    call()
      .then(onPkgs, failed("Couldn't answer the request"))
      .finally(() => setBusy(null));
  };
  const status = pkgs?.status ?? "none";
  const own = (pkgs?.packages ?? []).filter((p) => p.source !== "exuvia");
  const waiting = pkgs?.requests.length ?? 0;
  return (
    <Collapsible
      xstyle={styles.section}
      trigger={
        <Trigger
          icon={<IconPackage />}
          label="Packages"
          end={
            waiting ? (
              <Badge variant="info" label={waiting} />
            ) : status === "failed" ? (
              <Badge variant="error" label="Failed" />
            ) : status === "installing" || status === "pending" ? (
              <Badge variant="info" label="Installing" />
            ) : undefined
          }
        />
      }
    >
      <VStack gap={3} xstyle={styles.body}>
        <HStack gap={2} vAlign="center" role="status">
          <StateDot state={status === "installing" ? "running" : status} label={STATUS[status]} />
          <Text type="supporting" color="secondary">
            {STATUS[status][0].toUpperCase() + STATUS[status].slice(1)}
            {status === "ready" && pkgs?.installed ? `: ${pkgs.installed} on top of exuvia's own` : ""}
          </Text>
        </HStack>
        {pkgs?.requests.map((r) => (
          <Banner
            key={r.id}
            status="info"
            title={
              <>
                {asker(r.origin)} asks for <Code>{r.packages.join(", ")}</Code>
              </>
            }
            description={r.reason || undefined}
            endContent={
              <HStack gap={1}>
                <Button size="sm" label="Install" isDisabled={busy !== null} isLoading={busy === r.id} onClick={() => act(r.id, () => api.packagesApprove(r.id))} />
                <Button size="sm" variant="ghost" label="Dismiss" isDisabled={busy !== null} onClick={() => act(r.id, () => api.packagesDismiss(r.id))} />
              </HStack>
            }
          />
        ))}
        {own.length > 0 && (
          <List density="compact" hasDividers>
            {own.slice(0, SHOWN).map((p) => (
              <ListItem
                key={p.requirement}
                label={<Text type="code">{p.name}</Text>}
                endContent={
                  <Text type="supporting" color="secondary" hasTabularNumbers>
                    {p.version ?? "not installed"}
                  </Text>
                }
              />
            ))}
          </List>
        )}
        <HStack gap={2} vAlign="center">
          <Button size="sm" label="Manage packages" onClick={onManage} />
          {own.length > SHOWN && (
            <Text type="supporting" color="secondary">
              {own.length - SHOWN} more
            </Text>
          )}
        </HStack>
      </VStack>
    </Collapsible>
  );
}

function EnvSection({ env, onEnv, onManage }: { env: EnvState | null; onEnv: (s: EnvState) => void; onManage: (name?: string) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const dismiss = (name: string) => {
    setBusy(name);
    api
      .dismissEnvRequest(name)
      .then(onEnv, failed("Couldn't dismiss the request"))
      .finally(() => setBusy(null));
  };
  const vars = env?.variables ?? [];
  const inherited = (env?.inherited ?? []).filter((v) => !v.overridden).length;
  const waiting = env?.requests.length ?? 0;
  return (
    <Collapsible
      xstyle={styles.section}
      trigger={<Trigger icon={<IconKey />} label="Environment" end={waiting ? <Badge variant="info" label={waiting} /> : undefined} />}
    >
      <VStack gap={3} xstyle={styles.body}>
        {env?.requests.map((r) => (
          <Banner
            key={r.name}
            status="info"
            title={`${asker(r.origin)} asks you to set ${r.name}`}
            description={r.reason || undefined}
            endContent={
              <HStack gap={1}>
                <Button size="sm" label="Set it" isDisabled={busy !== null} onClick={() => onManage(r.name)} />
                <Button size="sm" variant="ghost" label="Dismiss" isDisabled={busy !== null} isLoading={busy === r.name} onClick={() => dismiss(r.name)} />
              </HStack>
            }
          />
        ))}
        {vars.length > 0 ? (
          <List density="compact" hasDividers>
            {vars.slice(0, SHOWN).map((v) => (
              <ListItem
                key={v.name}
                label={<Text type="code">{v.name}</Text>}
                endContent={
                  <Text type="supporting" color="secondary">
                    {v.secret ? "Secret" : "Value"}
                  </Text>
                }
              />
            ))}
          </List>
        ) : (
          <Text type="supporting" color="secondary">
            No variables of the project's own yet: API keys, tokens and settings your nodes read from <Code>os.environ</Code>.
          </Text>
        )}
        {inherited > 0 && (
          <Text type="supporting" color="secondary">
            {inherited} more from {env?.group ? `the group ${env.group.name} and ` : ""}the hub
          </Text>
        )}
        <HStack gap={2} vAlign="center">
          <Button size="sm" label="Manage variables" onClick={() => onManage()} />
          {vars.length > SHOWN && (
            <Text type="supporting" color="secondary">
              {vars.length - SHOWN} more
            </Text>
          )}
        </HStack>
      </VStack>
    </Collapsible>
  );
}

function ScheduleSection({ nodes, onOpen }: { nodes: GraphNode[]; onOpen: () => void }) {
  // Soonest first; paused ones after them.
  const scheduled = nodes
    .filter((n) => n.schedule)
    .sort((a, b) => Number(a.schedule!.paused) - Number(b.schedule!.paused) || (a.schedule!.next ?? Infinity) - (b.schedule!.next ?? Infinity));
  return (
    <Collapsible
      xstyle={styles.section}
      trigger={
        <Trigger
          icon={<IconClock />}
          label="Schedule"
          end={
            <Text type="supporting" color="secondary" hasTabularNumbers>
              {scheduled.length ? `${scheduled.length} ${scheduled.length === 1 ? "node" : "nodes"}` : "none"}
            </Text>
          }
        />
      }
    >
      <VStack gap={3} xstyle={styles.body}>
        {scheduled.length > 0 ? (
          <List density="compact" hasDividers>
            {scheduled.slice(0, SHOWN).map((n) => (
              <ListItem
                key={n.name}
                label={<Text type="code">{n.name}</Text>}
                description={
                  <Text type="code" color="secondary">
                    {n.schedule!.cron}
                  </Text>
                }
                endContent={
                  <Text type="supporting" color="secondary">
                    {n.schedule!.paused ? "paused" : n.schedule!.next ? until(n.schedule!.next) : ""}
                  </Text>
                }
              />
            ))}
          </List>
        ) : (
          <Text type="supporting" color="secondary">
            Nothing runs by itself yet. Give a node a cron: <Code>schedule="0 18 * * 1-5"</Code> in <Code>@exuvia.node</Code>.
          </Text>
        )}
        <HStack gap={2} vAlign="center">
          <Button size="sm" label="Open schedule" tooltip="Every schedule, the upcoming runs and the live nodes, in the main area" onClick={onOpen} />
          {scheduled.length > SHOWN && (
            <Text type="supporting" color="secondary">
              {scheduled.length - SHOWN} more
            </Text>
          )}
        </HStack>
      </VStack>
    </Collapsible>
  );
}

/** How many of the places on disk the panel lists, largest first. */
const PLACES = 4;

function StorageSection({ onOpen }: { onOpen: () => void }) {
  const [data, setData] = useState<StorageState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Measured when the tab is shown; the Storage page measures again, and keeps up with runs.
  useEffect(() => {
    let live = true;
    api.storage().then(
      (s) => live && setData(s),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
  }, []);
  return (
    <Collapsible
      xstyle={styles.section}
      trigger={
        <Trigger
          icon={<IconDisk />}
          label="Storage"
          end={
            data ? (
              <Text type="supporting" color="secondary" hasTabularNumbers>
                {fmtBytes(data.total_bytes)}
              </Text>
            ) : undefined
          }
        />
      }
    >
      <VStack gap={3} xstyle={styles.body}>
        {err && (
          <Text type="supporting" color="secondary">
            Couldn't measure the project: {err}
          </Text>
        )}
        {data && (
          <List density="compact" hasDividers>
            {places(data)
              .filter((p) => p.bytes > 0)
              .slice(0, PLACES)
              .map((p) => (
                <ListItem
                  key={p.key}
                  label={p.label}
                  endContent={
                    <Text type="supporting" color="secondary" hasTabularNumbers>
                      {fmtBytes(p.bytes)}
                    </Text>
                  }
                />
              ))}
          </List>
        )}
        <HStack>
          <Button size="sm" label="Open storage" tooltip="Every node's outputs, the Parquet files and the records, in the main area" onClick={onOpen} />
        </HStack>
      </VStack>
    </Collapsible>
  );
}

const styles = stylex.create({
  scroll: { flexGrow: 1, minHeight: 0, overflowY: "auto" },
  section: {
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  trigger: { flexGrow: 1, minWidth: 0, paddingInline: spacingVars["--spacing-2"] },
  body: { paddingInline: spacingVars["--spacing-3"], paddingBottom: spacingVars["--spacing-3"] },
  grow: { flexGrow: 1 },
});
