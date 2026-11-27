import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Code } from "@astryxdesign/core/Code";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { FileInput } from "@astryxdesign/core/FileInput";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { List, ListItem } from "@astryxdesign/core/List";
import { SideNavItem } from "@astryxdesign/core/SideNav";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { pixel, proportional, Table, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import {
  about as loadAbout,
  adminApi,
  ApiError,
  hubApi,
  hubEnvApi,
  usersApi,
  type About,
  type AdminSetting,
  type HubListing,
  type HubProject,
} from "./api";
import { McpPresetsPanel } from "./AgentMcp";
import { Brand, Mark } from "./Brand";
import { EnvEditor, type EnvClient } from "./EnvDialog";
import { HelpMenu } from "./Help";
import { HubNav } from "./HubNav";
import { DeleteDialog } from "./Hub";
import { SignedInAs, UsersPanel } from "./Me";
import { GENERAL_LABEL, ShareDialog } from "./Share";
import { useColorMode } from "./theme/colorMode";
import { useMe } from "./useMe";
import { ago } from "./util";
import {
  IconChevronRight,
  IconDownload,
  IconFolder,
  IconKey,
  IconMoon,
  IconPlug,
  IconShare,
  IconSliders,
  IconSpark,
  IconSun,
  IconTrash,
  IconUser,
} from "./icons";

/** The admin page's sections, in the order the side nav lists them. */
export type AdminSection = "overview" | "users" | "projects" | "variables" | "mcp" | "backup" | "server";

const SECTIONS: Record<AdminSection, { title: string; about: string; icon: ReactNode }> = {
  overview: { title: "Overview", about: "This hub at a glance: its version, and what is on it.", icon: <IconSpark /> },
  users: { title: "Users", about: "Everyone who has signed in, what each may do, and who is turned away.", icon: <IconUser /> },
  projects: { title: "Projects", about: "Every project on the hub, whoever owns it.", icon: <IconFolder /> },
  variables: {
    title: "Hub variables",
    about: "Every project on the hub gets these in os.environ, unless the project or its group sets the same name.",
    icon: <IconKey />,
  },
  mcp: { title: "MCP servers", about: "Servers anyone can connect to the built-in agent; nobody has one until they do.", icon: <IconPlug /> },
  backup: { title: "Export and import", about: "Every project with its history and outputs, as one archive.", icon: <IconDownload /> },
  server: {
    title: "Server settings",
    about: "How this server is set up. Environment variables set these when it starts: change one there and restart.",
    icon: <IconSliders />,
  },
};

const isSection = (s: string | null): s is AdminSection => !!s && s in SECTIONS;

/**
 * The hub's admin page, at `/admin`: everything an admin manages for the whole hub, one section at a time
 * (`/admin?section=<id>`), listed in the page's own left nav, and as tabs on a narrow screen. An admin, or the operator
 * (the API token, or a hub nobody signs in to), uses it; anyone else is told it is for admins.
 */
export default function AdminPage() {
  const [listing, setListing] = useState<HubListing | null>(null);
  const [err, setErr] = useState<{ status?: number; message: string } | null>(null);
  const [section, setSection] = useState<AdminSection>(() => {
    const s = new URLSearchParams(location.search).get("section");
    return isSection(s) ? s : "overview";
  });
  const [dark, toggleTheme] = useColorMode();
  const who = useMe();
  const narrow = useMediaQuery("(max-width: 800px)");
  // The page scrolls under the bar, beside the left nav.
  const scroller = useRef<HTMLDivElement>(null);

  const load = useCallback(
    () =>
      hubApi
        .projects()
        .then((l) => {
          setListing(l);
          setErr(null);
        })
        .catch((e: Error) => setErr({ status: e instanceof ApiError ? e.status : undefined, message: e.message })),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    document.title = `${SECTIONS[section].title} · Admin · exuvia`;
  }, [section]);

  const people = !!listing?.sharing?.people && !!who?.user;
  const isAdmin = !!listing?.hub_env?.can_edit || who?.user?.role === "admin";
  const sections: AdminSection[] = [
    "overview",
    ...(who?.user?.role === "admin" ? (["users"] as const) : []),
    "projects",
    ...(listing?.hub_env ? (["variables"] as const) : []),
    ...(listing?.agent_mcp?.can_edit ? (["mcp"] as const) : []),
    "backup",
    "server",
  ];
  const shown = sections.includes(section) ? section : "overview";
  const go = (s: AdminSection) => {
    setSection(s);
    const url = new URL(location.href);
    if (s === "overview") url.searchParams.delete("section");
    else url.searchParams.set("section", s);
    history.replaceState(null, "", url);
    scroller.current?.scrollTo({ top: 0 });
  };

  let body: ReactNode;
  if (err)
    body =
      err.status === 401 ? (
        <EmptyState
          icon={<Mark size={40} />}
          title="Enter the hub's token first"
          headingLevel={1}
          description="This hub asks for its API token. Enter it on the projects page, then come back."
          actions={<Button variant="primary" label="Go to the projects" href="/" />}
        />
      ) : (
        <Banner role="alert" status="error" title="Couldn't load the hub" description={err.message} endContent={<Button size="sm" label="Try again" onClick={load} />} />
      );
  else if (!listing || who === undefined)
    body = (
      <Center padding={10}>
        <Spinner label="Loading…" />
      </Center>
    );
  else if (!isAdmin)
    body = (
      <EmptyState
        icon={<Mark size={40} />}
        title="For hub admins"
        headingLevel={1}
        description="This page manages the whole hub: its users, variables, MCP servers and settings. Ask an admin if you need something changed here."
        actions={<Button variant="primary" label="Go to the projects" href="/" />}
      />
    );
  else
    body = (
      <div {...stylex.props(styles.frame)}>
        {narrow && (
          <TabList role="tablist" value={shown} onChange={(s) => go(s as AdminSection)} hasDivider overflow="scroll">
            {sections.map((s) => (
              <Tab key={s} value={s} label={SECTIONS[s].title} />
            ))}
          </TabList>
        )}
        <main {...stylex.props(styles.main)}>
          <VStack gap={6}>
            <VStack gap={1}>
              <Heading level={1}>{SECTIONS[shown].title}</Heading>
              <Text color="secondary">{SECTIONS[shown].about}</Text>
            </VStack>
            {shown === "overview" && <Overview listing={listing} people={people} onGo={go} />}
            {shown === "users" && <UsersPanel self={who?.user?.uid ?? null} />}
            {shown === "projects" && <Projects listing={listing} people={people} self={who?.user?.uid ?? null} onChanged={load} />}
            {shown === "variables" && <HubVariablesPanel canEdit />}
            {shown === "mcp" && <McpPresetsPanel />}
            {shown === "backup" && <Backup onRestored={load} />}
            {shown === "server" && <ServerSettings />}
          </VStack>
        </main>
      </div>
    );

  return (
    <div {...stylex.props(styles.page)}>
      <header {...stylex.props(styles.bar)}>
        <Brand home="/">admin</Brand>
        <span {...stylex.props(styles.grow)} />
        <HStack gap={0.5} vAlign="center">
          <HelpMenu />
          <IconButton
            variant="ghost"
            label={dark ? "Light theme" : "Dark theme"}
            tooltip={dark ? "Light theme" : "Dark theme"}
            icon={dark ? <IconSun /> : <IconMoon />}
            onClick={toggleTheme}
          />
          <SignedInAs hub />
        </HStack>
      </header>
      <div {...stylex.props(styles.body)}>
        <HubNav
          place={{
            page: "admin",
            // The sections, the left nav's own items here; on a narrow screen they are tabs over the section instead.
            sections:
              listing && isAdmin && !narrow
                ? sections.map((s) => (
                    <SideNavItem key={s} label={SECTIONS[s].title} icon={SECTIONS[s].icon} isSelected={s === shown} onClick={() => go(s)} />
                  ))
                : undefined,
          }}
          listing={listing}
        />
        <div ref={scroller} {...stylex.props(styles.scroll)}>
          {body}
        </div>
      </div>
    </div>
  );
}

/** The hub at a glance: its version (and a newer one, when there is), then a row per thing on it, each opening its section. */
function Overview(props: { listing: HubListing; people: boolean; onGo: (s: AdminSection) => void }) {
  const { listing } = props;
  const [info, setInfo] = useState<About | null>(null);
  const [users, setUsers] = useState<number | null>(null);
  const [busy, setBusy] = useState<boolean | null>(null);
  useEffect(() => {
    loadAbout()
      .then(setInfo)
      .catch(() => setInfo(null));
    adminApi
      .state()
      .then((s) => setBusy(s.busy))
      .catch(() => setBusy(null));
    if (props.people)
      usersApi
        .list()
        .then((u) => setUsers(u.filter((x) => !x.disabled).length))
        .catch(() => setUsers(null));
  }, [props.people]);

  const owners = new Set(listing.projects.map((p) => p.owner?.id).filter(Boolean)).size;
  const rows: { label: string; value: ReactNode; about: string; section?: AdminSection }[] = [
    {
      label: "Projects",
      value: listing.projects.length,
      about: props.people ? `Owned by ${owners} ${owners === 1 ? "person" : "people"}` : "Everyone who gets in can open every one",
      section: "projects",
    },
    ...(props.people
      ? [{ label: "Users", value: users ?? "…", about: "Signed in at least once and not disabled", section: "users" as const }]
      : []),
    { label: "Groups", value: listing.groups?.length ?? 0, about: "Folders of projects that share variables (managed on the projects page)" },
    ...(listing.hub_env
      ? [{ label: "Hub variables", value: listing.hub_env.variables, about: "Every project gets them", section: "variables" as const }]
      : []),
    ...(listing.agent_mcp
      ? [{ label: "MCP servers on offer", value: listing.agent_mcp.presets, about: "For anyone to connect to the agent", section: "mcp" as const }]
      : []),
    {
      label: "Right now",
      value: busy === null ? "…" : busy ? "busy" : "idle",
      about: busy ? "Runs, backfills or agent turns are in flight" : "Nothing is running",
    },
  ];

  return (
    <VStack gap={6}>
      {info?.update === "available" && (
        <Banner
          status="info"
          title={`exuvia ${info.latest} is out (this hub runs ${info.version})`}
          description={info.upgrade ? `Update with: ${info.upgrade}` : "Deploy the new image to update."}
          endContent={info.changes_url ? <Button size="sm" label="What's new" href={info.changes_url} target="_blank" /> : undefined}
        />
      )}
      <div {...stylex.props(styles.panel)}>
        <List hasDividers edge="flush">
          <ListItem
            label={<Text weight="semibold">Version</Text>}
            description={info ? installLine(info) : "…"}
            endContent={
              <HStack gap={2} vAlign="center">
                <Text hasTabularNumbers weight="semibold">
                  {info?.version ?? "…"}
                </Text>
                <span {...stylex.props(styles.chevron, styles.hidden)}>
                  <IconChevronRight />
                </span>
              </HStack>
            }
          />
          {rows.map((r) => (
            <ListItem
              key={r.label}
              label={<Text weight="semibold">{r.label}</Text>}
              description={r.about}
              onClick={r.section ? () => props.onGo(r.section!) : undefined}
              endContent={
                <HStack gap={2} vAlign="center">
                  <Text hasTabularNumbers weight="semibold">
                    {r.value}
                  </Text>
                  {/* A row that opens its section says so; the others keep the space, so the numbers line up. */}
                  <span {...stylex.props(styles.chevron, !r.section && styles.hidden)}>
                    <IconChevronRight />
                  </span>
                </HStack>
              }
            />
          ))}
        </List>
      </div>
    </VStack>
  );
}

const installLine = (a: About) => {
  const how = a.deployed ? "Deployed image" : `Installed with ${a.install}`;
  const state =
    a.update === "current" ? "up to date" : a.update === "available" ? `${a.latest} is out` : a.update === "error" ? "couldn't check for updates" : null;
  return state ? `${how} · ${state}` : how;
};

/** A project as a table row: Astryx's Table takes a record, which an interface is not. */
type ProjectRow = Pick<HubProject, keyof HubProject>;

/** Every project on the hub in a table: whose it is, how big, where it is and who else gets in, with Open, Share and Delete. */
function Projects(props: { listing: HubListing; people: boolean; self: string | null; onChanged: () => void }) {
  const [sharing, setSharing] = useState<HubProject | null>(null);
  const [deleting, setDeleting] = useState<HubProject | null>(null);
  const projects = [...props.listing.projects].sort((a, b) => (b.changed_at ?? 0) - (a.changed_at ?? 0));
  const shares = !!props.listing.sharing;

  const columns: TableColumn<ProjectRow>[] = [
    {
      key: "name",
      header: "Project",
      width: proportional(1.4, { minWidth: 160 }),
      renderCell: (p) => (
        <VStack gap={0}>
          <Text weight="medium" maxLines={1}>
            {p.name}
          </Text>
          <Text type="supporting" color="secondary" maxLines={1}>
            {p.id} · {p.nodes} node{p.nodes === 1 ? "" : "s"}
            {p.group ? ` · in ${p.group.name}` : ""}
          </Text>
        </VStack>
      ),
    },
    ...(props.people
      ? [
          {
            key: "owner",
            header: "Owner",
            width: proportional(1, { minWidth: 110 }),
            renderCell: (p: ProjectRow) => (
              <Text color={p.owner ? undefined : "secondary"} maxLines={1}>
                {p.owner ? (p.owner.id === props.self ? `${p.owner.name} (you)` : p.owner.name) : "nobody"}
              </Text>
            ),
          },
          {
            key: "general",
            header: "Everyone else",
            width: pixel(130),
            renderCell: (p: ProjectRow) => (
              <Tooltip content={p.general ? GENERAL_LABEL[p.general] : ""} isEnabled={!!p.general}>
                <Text color="secondary" maxLines={1}>
                  {p.general === "private" ? (p.shared ? "shared" : "private") : p.general ? `can ${p.general}` : ""}
                </Text>
              </Tooltip>
            ),
          },
        ]
      : []),
    {
      key: "changed_at",
      header: "Changed",
      width: pixel(96),
      renderCell: (p) => (
        <Text color="secondary" hasTabularNumbers>
          {ago(p.changed_at) || "—"}
        </Text>
      ),
    },
    {
      key: "id",
      header: "",
      width: pixel(shares ? 156 : 120),
      align: "end",
      renderCell: (p) => (
        <HStack gap={1} hAlign="end">
          <Button size="sm" variant="ghost" label="Open" href={`/p/${encodeURIComponent(p.id)}/`} />
          {shares && (
            <IconButton size="sm" variant="ghost" label={`Share ${p.name}`} tooltip="Share" icon={<IconShare />} onClick={() => setSharing(p)} />
          )}
          <IconButton size="sm" variant="ghost" label={`Delete ${p.name}`} tooltip="Delete" icon={<IconTrash />} onClick={() => setDeleting(p)} />
        </HStack>
      ),
    },
  ];

  return (
    <VStack gap={4}>
      {projects.length === 0 ? (
        <Text color="secondary">No projects yet. They are created on the projects page.</Text>
      ) : (
        <Table<ProjectRow> data={projects} columns={columns} idKey="id" density="compact" textOverflow="truncate" xstyle={styles.table} />
      )}
      {sharing && (
        <ShareDialog
          project={sharing.id}
          name={sharing.name}
          self={props.self}
          onClose={() => setSharing(null)}
          onChanged={props.onChanged}
        />
      )}
      {deleting && <DeleteDialog project={deleting} onClose={() => setDeleting(null)} onDeleted={props.onChanged} />}
    </VStack>
  );
}

/** Download an export of every project, or restore one: what is in the archive replaces same-named projects. */
function Backup(props: { onRestored: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const restore = async () => {
    if (!file) return;
    setBusy(true);
    setErr(null);
    setDone(null);
    try {
      const r = await adminApi.importHub(file);
      setDone(r.projects);
      setFile(null);
      props.onRestored();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <VStack gap={8}>
      <VStack gap={3}>
        <Heading level={2}>Export</Heading>
        <Text color="secondary">
          One .tar.gz with every project's code, run history and outputs, the user list, sharing and groups. Outputs already in a remote
          artifact store stay there. With <Code>EXUVIA_SECRETS_KEY</Code> set, secrets stay encrypted and the key is not in it; without it,
          each project's own key goes along, so keep the archive as safe as the hub.
        </Text>
        <HStack>
          <Button variant="primary" label="Download an export" icon={<IconDownload />} href={adminApi.exportUrl} />
        </HStack>
      </VStack>
      <VStack gap={3}>
        <Heading level={2}>Import</Heading>
        <Text color="secondary">
          Restore projects from an export. Each project in the archive replaces the one of the same name here, history and all; projects
          not in it are left alone.
        </Text>
        {err && <Banner role="alert" status="error" title={err} />}
        {done && (
          <Banner
            status="success"
            title={done.length ? `Restored ${done.length} project${done.length === 1 ? "" : "s"}` : "The archive had no projects"}
            description={done.join(", ") || undefined}
          />
        )}
        <FileInput
          label="Export archive"
          placeholder="Choose a .tar.gz"
          accept=".gz,.tgz,application/gzip"
          value={file}
          onChange={(f) => setFile(Array.isArray(f) ? (f[0] ?? null) : f)}
        />
        <HStack>
          <Button label={busy ? "Importing…" : "Import"} isDisabled={!file || busy} onClick={() => setConfirming(true)} />
        </HStack>
      </VStack>
      <AlertDialog
        isOpen={confirming}
        onOpenChange={(open) => {
          if (!open) setConfirming(false);
        }}
        title="Replace projects from this archive?"
        description="Every project in it replaces the one of the same name on this hub, with its history and outputs. This can't be undone; download an export first if you may want them back."
        actionLabel="Import"
        actionVariant="destructive"
        onAction={() => {
          setConfirming(false);
          restore();
        }}
      />
    </VStack>
  );
}

/** A setting as a table row: Astryx's Table takes a record, which an interface is not. */
type SettingRow = Pick<AdminSetting, keyof AdminSetting> & { id: string };

/** The server's setup (exuvia/admin.py), group by group: read-only, with the variable that sets each. */
function ServerSettings() {
  const [groups, setGroups] = useState<{ title: string; items: AdminSetting[] }[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // On a phone the variable goes under the setting's name, so the table fits without scrolling sideways.
  const narrow = useMediaQuery("(max-width: 800px)");
  useEffect(() => {
    adminApi
      .settings()
      .then((r) => setGroups(r.groups))
      .catch((e: Error) => setErr(e.message));
  }, []);

  const columns: TableColumn<SettingRow>[] = [
    {
      key: "label",
      header: "Setting",
      width: proportional(1, { minWidth: narrow ? 120 : 150 }),
      renderCell: (r) => (
        <VStack gap={0.5}>
          <Tooltip content={r.note ?? ""} isEnabled={!!r.note}>
            <Text weight="medium" maxLines={2}>
              {r.label}
            </Text>
          </Tooltip>
          {narrow && r.env && (
            <Text type="supporting" color="secondary" xstyle={styles.breakAnywhere}>
              {r.env}
            </Text>
          )}
        </VStack>
      ),
    },
    {
      key: "value",
      header: "Value",
      width: proportional(1.3, { minWidth: narrow ? 120 : 150 }),
      renderCell: (r) => (
        <Text color={r.set ? undefined : "secondary"} maxLines={3}>
          {r.value}
          {r.set ? "" : " (default)"}
        </Text>
      ),
    },
    ...(narrow
      ? []
      : [
          {
            key: "env",
            header: "Variable",
            width: proportional(1.1, { minWidth: 240 }),
            renderCell: (r: SettingRow) => (r.env ? <Code>{r.env}</Code> : null),
          },
        ]),
  ];

  if (err) return <Banner role="alert" status="error" title={err} />;
  if (!groups)
    return (
      <Center padding={6}>
        <Spinner label="Loading…" />
      </Center>
    );
  return (
    <VStack gap={8}>
      <Text type="supporting" color="secondary">
        Secrets (tokens, keys, webhook URLs) show only as set or not set, and addresses without their passwords. Hover a setting for more.
      </Text>
      {groups.map((g) => (
        <VStack key={g.title} gap={3}>
          <Heading level={2}>{g.title}</Heading>
          <Table<SettingRow>
            data={g.items.map((i) => ({ ...i, id: i.env ?? i.label }))}
            columns={columns}
            idKey="id"
            density="compact"
            textOverflow="wrap"
            xstyle={styles.table}
          />
        </VStack>
      ))}
    </VStack>
  );
}

/** The hub's own variables: an admin sets them, everyone else sees their names and plain values. */
export function HubVariablesPanel(props: { canEdit: boolean }) {
  const client: EnvClient = { load: hubEnvApi.env, set: hubEnvApi.setEnv, remove: hubEnvApi.removeEnv };
  return (
    <VStack gap={4}>
      <Banner
        status="warning"
        title="Everyone who can edit any project on this hub can read these secrets"
        description="A node can read anything in os.environ and send it anywhere. Masking secrets as *** in run logs only stops accidents. Put a key here only when everyone on the hub may use it; a key for some people belongs in a group. Runs started from now on use them."
      />
      <EnvEditor
        client={client}
        canEdit={props.canEdit}
        empty={
          props.canEdit
            ? "No variables yet. Every project on the hub gets the ones added here, unless the project or its group sets the same name."
            : "The hub has no variables."
        }
      />
    </VStack>
  );
}

const NARROW = "@media (max-width: 800px)";

const styles = stylex.create({
  // The bar on top, then the hub's left nav beside the page, which scrolls on its own.
  page: { display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", height: "100dvh" },
  body: { display: "flex", minHeight: 0 },
  scroll: { flexGrow: 1, minWidth: 0, overflowY: "auto" },
  bar: {
    display: "flex",
    alignItems: "center",
    flexShrink: 0,
    gap: { default: spacingVars["--spacing-3"], [NARROW]: spacingVars["--spacing-2"] },
    minWidth: 0,
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: { default: spacingVars["--spacing-4"], [NARROW]: spacingVars["--spacing-2"] },
    backgroundColor: colorVars["--color-background-surface"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  grow: { flexGrow: 1 },
  // The section; on a narrow screen, under its tabs.
  frame: { display: "flex", flexDirection: "column" },
  main: {
    boxSizing: "border-box",
    flexGrow: 1,
    minWidth: 0,
    maxWidth: "960px",
    paddingInline: { default: spacingVars["--spacing-8"], [NARROW]: spacingVars["--spacing-4"] },
    paddingBlockStart: { default: spacingVars["--spacing-8"], [NARROW]: spacingVars["--spacing-6"] },
    paddingBlockEnd: spacingVars["--spacing-10"],
  },
  // The overview's rows stand on one surface, as the projects page's rows do.
  panel: {
    backgroundColor: colorVars["--color-background-surface"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    paddingInline: spacingVars["--spacing-4"],
  },
  table: { minWidth: 0 },
  chevron: { display: "inline-flex", color: colorVars["--color-text-secondary"] },
  hidden: { visibility: "hidden" },
  // A variable name has no spaces to wrap at.
  breakAnywhere: { overflowWrap: "anywhere" },
});
