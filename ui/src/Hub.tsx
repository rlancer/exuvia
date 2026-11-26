import { useCallback, useEffect, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, sizeVars, spacingVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Center } from "@astryxdesign/core/Center";
import { Code } from "@astryxdesign/core/Code";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { List, ListItem } from "@astryxdesign/core/List";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Selector } from "@astryxdesign/core/Selector";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { ApiError, groupApi, hubApi, PROJECT_ID, type GeneralAccess, type HubGroup, type HubListing, type HubProject } from "./api";
import { GroupDialog, GroupNameDialog, ProjectGroupDialog } from "./GroupDialog";
import { notifyError } from "./toast";
import { GENERAL_LABEL, ShareDialog } from "./Share";
import { ConnectDialog } from "./Connect";
import { DataKeysDialog } from "./DataKeys";
import { HubVariablesPanel } from "./AdminPage";
import { SignedInAs } from "./Me";
import { HelpMenu } from "./Help";
import { useMe } from "./useMe";
import { useColorMode } from "./theme/colorMode";
import { Brand, Mark } from "./Brand";
import { AppDialog } from "./ui/AppDialog";
import { IconFolder, IconKey, IconMoon, IconPlus, IconShare, IconSpark, IconSun, IconTrash } from "./icons";
import { HubNav, workbenchUrl as workbench } from "./HubNav";
import { Toasts } from "./toast";
import { isProjectSort, recentWork, SORT_LABEL, sortProjects, type ProjectSort } from "./projectOrder";
import { ago } from "./util";

/** A project id from what is typed in the id field: spaces and other characters become `-`. */
export const toProjectId = (s: string) =>
  s
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^[-_]+/, "")
    .slice(0, 64);

/** The id a project name suggests: "Market research" → "market-research". */
export const idFromName = (name: string) => toProjectId(name.trim().toLowerCase()).replace(/[-_]+$/, "");

type Listing = HubListing;

/** Which projects the list shows, on a hub where people sign in. */
type Filter = "all" | "mine" | "shared";

const ROLE_WORD = { viewer: "can view", editor: "can edit", owner: "owner" } as const;

/** The project list's order, kept in this browser. */
const SORT_KEY = "exuvia-hub-sort";
const keptSort = (): ProjectSort => {
  try {
    const s = localStorage.getItem(SORT_KEY);
    return isProjectSort(s) ? s : "used";
  } catch {
    return "used";
  }
};
const keepSort = (s: ProjectSort) => {
  try {
    localStorage.setItem(SORT_KEY, s);
  } catch {
    // Private windows and blocked storage: the order lasts as long as the page.
  }
};

/**
 * The hub's home page, at `/` of `exuvia hub`: every project, with a button
 * to create one and a way to delete one; each opens its workbench at
 * `/p/<id>/`. Without the API token it asks for it.
 *
 * The top bar is the workbench's app bar: where you are, then the hub's own
 * menus. Under it, the hub's left nav (src/HubNav.tsx) and the page, a
 * document that scrolls, one column wide: its title with the one thing to do
 * here (New project), then the projects as rows.
 */
export default function Hub() {
  const [listing, setListing] = useState<Listing | null>(null);
  const [needToken, setNeedToken] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // The New project dialog, and the group it starts in ("" for none).
  // `/?new`, the left nav's New project from another page, opens it at once.
  const [newIn, setNewIn] = useState<string | null>(() => (new URLSearchParams(location.search).has("new") ? "" : null));
  const [deleting, setDeleting] = useState<HubProject | null>(null);
  const [sharing, setSharing] = useState<HubProject | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<ProjectSort>(keptSort);
  const [connecting, setConnecting] = useState(false);
  const [dataKeys, setDataKeys] = useState(false);
  // The group open on its page: from a row, or `/?group=<id>` (the Environment dialog's link).
  const [openGroup, setOpenGroup] = useState<string | null>(() => new URLSearchParams(location.search).get("group"));
  const [newGroup, setNewGroup] = useState(false);
  // `/?env=hub`, the Environment dialog's link to the hub's variables: an admin goes on to the admin
  // page's section for them, anyone else sees them read-only here.
  const [hubEnvOpen, setHubEnvOpen] = useState(() => new URLSearchParams(location.search).get("env") === "hub");
  const [grouping, setGrouping] = useState<HubProject | null>(null);
  const [dark, toggleTheme] = useColorMode();
  // Someone signed in reaches Connect agent from their account menu.
  const who = useMe();
  const signedIn = !!who?.user;
  // The bar and the rows give up their words before their controls.
  const compact = useMediaQuery("(max-width: 600px)");

  const load = useCallback(
    () =>
      hubApi
        .projects()
        .then((l) => {
          setListing(l);
          setNeedToken(false);
          setErr(null);
        })
        .catch((e: Error) => {
          if (e instanceof ApiError && e.status === 401) setNeedToken(true);
          else setErr(e.message);
        }),
    [],
  );

  useEffect(() => {
    load();
    // Agents add projects and nodes over the API: show them when the tab comes back.
    const onFocus = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onFocus);
    return () => document.removeEventListener("visibilitychange", onFocus);
  }, [load]);

  const all = listing?.projects ?? [];
  const groups: HubGroup[] = listing?.groups ?? [];
  /** Without sharing everyone owns every project; with it, the owner does (admins are owners everywhere). */
  const owns = (p: HubProject) => !listing?.sharing || p.access === "owner";
  /** Whether the Group button shows on a project's row: its owner can add it to a group they edit, a group's editor can take it out. */
  const canGroup = (p: HubProject) => {
    if (owns(p)) return !!p.group || groups.some((g) => g.access !== "viewer");
    return !!p.group && groups.some((g) => g.id === p.group!.id && g.access !== "viewer");
  };
  const showGroup = (gid: string | null) => {
    setOpenGroup(gid);
    const url = new URL(location.href);
    if (gid) url.searchParams.set("group", gid);
    else url.searchParams.delete("group");
    history.replaceState(null, "", url);
  };
  const closeHubEnv = () => {
    setHubEnvOpen(false);
    const url = new URL(location.href);
    url.searchParams.delete("env");
    history.replaceState(null, "", url);
  };
  const hubEnv = listing?.hub_env;
  // An admin, or the operator (the API token, or a hub nobody signs in to): who the admin page is for.
  const isAdmin = !!hubEnv?.can_edit || who?.user?.role === "admin";
  useEffect(() => {
    // The admin panel's old links (`/?admin=<tab>`), and an admin following the hub's variables link: on to the admin page.
    const q = new URLSearchParams(location.search);
    const tab = q.get("admin");
    if (tab) location.replace(`/admin?section=${encodeURIComponent(tab)}`);
    else if (hubEnvOpen && listing && isAdmin) location.replace("/admin?section=variables");
  }, [hubEnvOpen, listing, isAdmin]);
  const uid = who?.user?.uid ?? null;
  // People and roles exist where people sign in; a token hub shares by link only.
  const people = !!listing?.sharing?.people && !!uid;
  const mine = (p: HubProject) => !!uid && p.owner?.id === uid;
  const projects = sortProjects(!people || filter === "all" ? all : all.filter((p) => (filter === "mine" ? mine(p) : !mine(p))), sort);
  // What you created or changed lately, above the list: only when the list has more than that.
  const recent = recentWork(all);
  const showRecent = recent.length > 0 && all.length > recent.length;
  const canDelete = owns;
  const someDeletable = projects.some(canDelete);
  useEffect(() => {
    document.title = "Projects · exuvia";
    // `?new` has done its work once the dialog is open: a reload doesn't open it again.
    const url = new URL(location.href);
    if (url.searchParams.has("new")) {
      url.searchParams.delete("new");
      history.replaceState(null, "", url);
    }
  }, []);

  /** A row's second line: the id, the size, when (what the list is ordered by), then whose it is and who else can open it. */
  const meta = (p: HubProject, when: string) => {
    const parts: ReactNode[] = [
      <span key="id" {...stylex.props(styles.mono)}>
        {p.id}
      </span>,
      `${p.nodes} node${p.nodes === 1 ? "" : "s"}`,
    ];
    if (when) parts.push(when);
    if (people && p.access) {
      parts.push(mine(p) ? "yours" : p.owner ? `${p.owner.name}'s` : "no owner");
      if (!mine(p)) parts.push(`you ${ROLE_WORD[p.access]}`);
      if (p.general)
        parts.push(
          <Tooltip key="general" content={GENERAL_LABEL[p.general]}>
            <span>{p.general === "private" ? (p.shared ? "shared" : "private") : `everyone can ${p.general}`}</span>
          </Tooltip>,
        );
    }
    if (!people && listing?.sharing && p.shared) parts.push("shared by link");
    if (p.group) parts.push(`in ${p.group.name}`);
    return parts.flatMap((part, i) => (i ? [" · ", part] : [part]));
  };

  /** When, on a row of the list: what its order goes by. */
  const listWhen = (p: HubProject) =>
    sort === "used" ? (p.opened_at ? `you opened it ${ago(p.opened_at)}` : "") : p.changed_at ? `changed ${ago(p.changed_at)}` : "";

  const row = (p: HubProject, when: string) => (
    <ListItem
      key={p.id}
      href={workbench(p.id)}
      label={
        <HStack gap={2} vAlign="center">
          <Text weight="semibold" maxLines={1}>
            {p.name || p.id}
          </Text>
          {p.requests ? (
            <Tooltip content="People asking for access: open Share to answer">
              <Badge variant="info" label={`${p.requests} request${p.requests === 1 ? "" : "s"}`} />
            </Tooltip>
          ) : null}
        </HStack>
      }
      description={
        <Text type="supporting" color="secondary" maxLines={1} hasTruncateTooltip={false}>
          {meta(p, when)}
        </Text>
      }
      endContent={
        listing?.sharing || canDelete(p) ? (
          <HStack gap={0.5} vAlign="center">
            {canGroup(p) && (
              <IconButton
                variant="ghost"
                size="sm"
                label={p.group ? `${p.id}'s group: ${p.group.name}` : `Put ${p.id} in a group`}
                tooltip={p.group ? `In ${p.group.name}: move or take out` : "Put in a group: share its variables and access"}
                icon={<IconFolder />}
                onClick={() => setGrouping(p)}
              />
            )}
            {listing?.sharing && (
              <Button
                variant="ghost"
                size="sm"
                label={p.access === "owner" ? "Share" : "Access"}
                icon={<IconShare />}
                isIconOnly={compact}
                tooltip={p.access === "owner" ? `Share ${p.id}` : `Who has access to ${p.id}`}
                onClick={() => setSharing(p)}
              />
            )}
            {canDelete(p) ? (
              <IconButton
                variant="ghost"
                size="sm"
                label={`Delete ${p.id}`}
                tooltip={`Delete ${p.id}`}
                icon={<IconTrash />}
                onClick={() => setDeleting(p)}
              />
            ) : (
              // Keeps Share and Access in one column down the list.
              someDeletable && <span {...stylex.props(styles.slot)} />
            )}
          </HStack>
        ) : undefined
      }
    />
  );

  return (
    <div {...stylex.props(styles.page)}>
      <header {...stylex.props(styles.bar)}>
        <Brand>projects</Brand>
        <span {...stylex.props(styles.grow)} />
        {listing && who !== undefined && !signedIn && (
          <>
            <Button
              label="Connect agent"
              icon={<IconSpark />}
              isIconOnly={compact}
              tooltip="Connect a coding agent to every project on this hub over MCP"
              onClick={() => setConnecting(true)}
            />
            <Button
              label="Data API"
              icon={<IconKey />}
              isIconOnly={compact}
              tooltip="Read-only keys for apps of your own that read this hub's data"
              onClick={() => setDataKeys(true)}
            />
            <Divider orientation="vertical" xstyle={styles.rule} />
          </>
        )}
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
        <HubNav place={{ page: "home" }} listing={listing} onNewProject={() => setNewIn("")} />
        <div {...stylex.props(styles.scroll)}>
          <main {...stylex.props(styles.main)}>
            {needToken ? (
              <TokenForm />
            ) : err ? (
              <Banner
                role="alert"
                status="error"
                title="Couldn't load the projects"
                description={err}
                endContent={<Button size="sm" label="Try again" onClick={load} />}
              />
            ) : !listing ? (
              <Center padding={8}>
                <Spinner label="Loading…" />
              </Center>
            ) : all.length === 0 && groups.length === 0 ? (
              <EmptyState
                icon={<Mark size={40} />}
                title="No projects yet"
                headingLevel={1}
                description="A project is one graph of Python and SQL nodes, with its own runs, outputs and views."
                actions={<Button variant="primary" label="New project" icon={<IconPlus />} onClick={() => setNewIn("")} />}
              />
            ) : all.length === 0 ? (
              <VStack gap={8}>
                <EmptyState
                  icon={<Mark size={40} />}
                  title="No projects yet"
                  headingLevel={1}
                  description="A project is one graph of Python and SQL nodes, with its own runs, outputs and views."
                  actions={<Button variant="primary" label="New project" icon={<IconPlus />} onClick={() => setNewIn("")} />}
                />
                <Groups groups={groups} onOpen={showGroup} onNew={() => setNewGroup(true)} people={people} />
              </VStack>
            ) : (
              <VStack gap={4}>
                <HStack gap={3} vAlign="center">
                  <div {...stylex.props(styles.title)}>
                    <Heading level={1}>Projects</Heading>
                    <Text color="secondary" hasTabularNumbers>
                      {projects.length}
                    </Text>
                  </div>
                  <span {...stylex.props(styles.grow)} />
                  <Button variant="primary" label="New project" icon={<IconPlus />} onClick={() => setNewIn("")} />
                </HStack>
                {showRecent && (
                  <VStack gap={2}>
                    <Heading level={2}>Recently worked on</Heading>
                    <div {...stylex.props(styles.list)}>
                      <List hasDividers density="spacious">
                        {recent.map((p) => row(p, `you changed it ${ago(p.edited_at)}`))}
                      </List>
                    </div>
                  </VStack>
                )}
                <VStack gap={2}>
                  {showRecent && <Heading level={2}>All projects</Heading>}
                  <HStack gap={2} vAlign="center" wrap="wrap">
                    {people && (
                      <SegmentedControl label="Which projects" size="sm" value={filter} onChange={(f) => setFilter(f as Filter)}>
                        <SegmentedControlItem value="all" label="All" />
                        <SegmentedControlItem value="mine" label="Owned by me" />
                        <SegmentedControlItem value="shared" label="Shared with me" />
                      </SegmentedControl>
                    )}
                    <span {...stylex.props(styles.grow)} />
                    <Selector
                      label="Sort by"
                      isLabelHidden
                      size="sm"
                      variant="ghost"
                      value={sort}
                      onChange={(v) => {
                        if (!isProjectSort(v)) return;
                        setSort(v);
                        keepSort(v);
                      }}
                      renderValue={(o) => `Sort: ${o.label}`}
                      options={(Object.keys(SORT_LABEL) as ProjectSort[]).map((value) => ({ value, label: SORT_LABEL[value] }))}
                    />
                  </HStack>
                </VStack>
                {projects.length === 0 ? (
                  <Text color="secondary">No projects here.</Text>
                ) : (
                  <div {...stylex.props(styles.list)}>
                    <List hasDividers density="spacious">
                      {projects.map((p) => row(p, listWhen(p)))}
                    </List>
                  </div>
                )}
                <Groups groups={groups} onOpen={showGroup} onNew={() => setNewGroup(true)} people={people} />
              </VStack>
            )}
          </main>
        </div>
      </div>

      {newIn !== null && listing && (
        <NewProjectDialog
          templates={listing.templates}
          taken={all.map((p) => p.id)}
          access={people ? listing.sharing!.default_access : null}
          groups={groups.filter((g) => g.access !== "viewer")}
          group={newIn}
          people={people}
          onClose={() => setNewIn(null)}
          onCreated={(id) => location.assign(workbench(id))}
        />
      )}
      {connecting && listing && <ConnectDialog auth={listing.auth} onClose={() => setConnecting(false)} />}
      {dataKeys && <DataKeysDialog onClose={() => setDataKeys(false)} />}
      {sharing && (
        <ShareDialog project={sharing.id} name={sharing.name} self={uid} onClose={() => setSharing(null)} onChanged={load} />
      )}
      {hubEnvOpen && listing && !isAdmin && hubEnv && <HubEnvDialog onClose={closeHubEnv} />}
      {openGroup && (
        <GroupDialog
          gid={openGroup}
          self={uid}
          projects={all}
          ownsProject={owns}
          onNewProject={() => setNewIn(openGroup)}
          onClose={() => showGroup(null)}
          onChanged={load}
        />
      )}
      {newGroup && (
        <GroupNameDialog
          title="New group"
          subtitle={people ? "You own it. Its projects share its variables and secrets, and the people you add." : "Its projects share its variables and secrets."}
          action="Create"
          onClose={() => setNewGroup(false)}
          onSubmit={async (name) => {
            try {
              const g = await groupApi.create(name);
              setNewGroup(false);
              await load();
              showGroup(g.id);
              return true;
            } catch (e) {
              notifyError("Couldn't create the group", e);
              return false;
            }
          }}
        />
      )}
      {grouping && listing && (
        <ProjectGroupDialog
          project={grouping}
          groups={groups}
          people={people}
          onClose={() => setGrouping(null)}
          onDone={() => {
            setGrouping(null);
            load();
          }}
        />
      )}
      {deleting && (
        <DeleteDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null);
            load();
          }}
        />
      )}
      <Toasts />
    </div>
  );
}

const GROUP_ROLE_WORD = { viewer: "you can view", editor: "you can edit", owner: "yours" } as const;

/**
 * The hub's groups the person has a role on, under the projects: each opens
 * its page (projects, variables, people). Anyone signed in can create one.
 */
function Groups(props: { groups: HubGroup[]; people: boolean; onOpen: (gid: string) => void; onNew: () => void }) {
  const { groups } = props;
  return (
    <VStack gap={3}>
      <HStack gap={3} vAlign="center">
        <div {...stylex.props(styles.title)}>
          <Heading level={2}>Groups</Heading>
          <Text color="secondary" hasTabularNumbers>
            {groups.length}
          </Text>
        </div>
        <span {...stylex.props(styles.grow)} />
        <Button label="New group" icon={<IconPlus />} onClick={props.onNew} />
      </HStack>
      <Text type="supporting" color="secondary">
        {props.people
          ? "A group is a folder of projects: they share its environment variables and secrets, and its people get their group role on each of them."
          : "A group is a folder of projects that share its environment variables and secrets."}
      </Text>
      {groups.length > 0 && (
        <div {...stylex.props(styles.list)}>
          <List hasDividers density="spacious">
            {groups.map((g) => (
              <ListItem
                key={g.id}
                onClick={() => props.onOpen(g.id)}
                startContent={<IconFolder />}
                label={
                  <Text weight="semibold" maxLines={1}>
                    {g.name}
                  </Text>
                }
                description={
                  <Text type="supporting" color="secondary" maxLines={1} hasTruncateTooltip={false}>
                    {[
                      `${g.projects} project${g.projects === 1 ? "" : "s"}`,
                      `${g.variables} variable${g.variables === 1 ? "" : "s"}`,
                      ...(props.people ? [g.access === "owner" ? GROUP_ROLE_WORD.owner : GROUP_ROLE_WORD[g.access]] : []),
                    ].join(" · ")}
                  </Text>
                }
              />
            ))}
          </List>
        </div>
      )}
    </VStack>
  );
}

/** The hub's own variables, read-only: what a non-admin sees from a project's Environment dialog. */
function HubEnvDialog(props: { onClose: () => void }) {
  return (
    <AppDialog
      title="Hub variables"
      subtitle="Every project on the hub gets these in os.environ, unless the project or its group sets the same name."
      width={680}
      onClose={props.onClose}
      footer={<Button label="Done" onClick={props.onClose} />}
    >
      <HubVariablesPanel canEdit={false} />
    </AppDialog>
  );
}

/** Asks for the hub's API token, checks it, then has the server keep it in a cookie. */
function TokenForm() {
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await hubApi.projects(token.trim());
      // The server answers /?token= with an HttpOnly cookie and a redirect back to /.
      location.assign(`/?token=${encodeURIComponent(token.trim())}`);
    } catch (e) {
      setBusy(false);
      setErr(e instanceof ApiError && e.status === 401 ? "That isn't this hub's token." : (e as Error).message);
    }
  };

  return (
    <Center axis="horizontal">
      <Card width="100%" maxWidth={460} padding={6} elevation="low">
        <form onSubmit={submit}>
          <VStack gap={4}>
            <VStack gap={2} hAlign="center">
              <Mark size={40} />
              <Heading level={2} accessibilityLevel={1} justify="center">
                Sign in to this hub
              </Heading>
              <Text color="secondary" justify="center">
                Enter the hub's API token. This browser keeps it in a cookie, so you only enter it once.
              </Text>
            </VStack>
            <TextInput
              type="password"
              label="API token"
              hasAutoFocus
              autoComplete="current-password"
              value={token}
              onChange={setToken}
              status={err ? { type: "error", message: err } : undefined}
            />
            <Text type="supporting" color="secondary">
              Hub on a private network? Setting <Code>EXUVIA_BROWSER_AUTH=open</Code> on it lets browsers in without the token.
            </Text>
            <HStack hAlign="end">
              <Button type="submit" variant="primary" label="Continue" isDisabled={!token.trim() || busy} />
            </HStack>
          </VStack>
        </form>
      </Card>
    </Center>
  );
}

function NewProjectDialog(props: {
  templates: string[];
  taken: string[];
  /** On a hub where people sign in: the default for who else can open it. */
  access: GeneralAccess | null;
  /** The groups the person can add projects to. */
  groups: HubGroup[];
  /** The group it starts in ("": none), when it is made from a group's page. */
  group: string;
  /** People sign in on this hub (groups have people). */
  people: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [access, setAccess] = useState<GeneralAccess>(props.access ?? "edit");
  const [id, setId] = useState("");
  const [idEdited, setIdEdited] = useState(false);
  const [template, setTemplate] = useState("");
  const [group, setGroup] = useState(props.group);
  const inGroup = props.groups.find((g) => g.id === group) ?? null;
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const finalId = idEdited ? id : idFromName(name);
  const problem = !finalId
    ? null
    : !PROJECT_ID.test(finalId)
      ? "Ids are up to 64 letters, digits, - or _, starting with a letter or digit."
      : props.taken.includes(finalId)
        ? `There is already a project ${finalId}.`
        : null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!finalId || problem || busy) return;
    setErr(null);
    setBusy(true);
    try {
      await hubApi.create({
        id: finalId,
        name: name.trim() || undefined,
        template: template || undefined,
        access: props.access ? access : undefined,
        group: inGroup?.id,
      });
      props.onCreated(finalId);
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  return (
    <AppDialog
      title="New project"
      subtitle={props.group && inGroup ? `In ${inGroup.name}` : undefined}
      onClose={props.onClose}
      onSubmit={submit}
      footer={(formId) => (
        <>
          <Button variant="ghost" label="Cancel" onClick={props.onClose} />
          <Button type="submit" form={formId} variant="primary" label="Create and open" isDisabled={!finalId || !!problem || busy} />
        </>
      )}
    >
      <VStack gap={4}>
        <TextInput label="Name" hasAutoFocus value={name} placeholder="Market research" onChange={setName} />
        <TextInput
          label="Id"
          description="In its URL: /p/<id>/"
          value={finalId}
          placeholder="market-research"
          hasSpellCheck={false}
          autoCapitalize="off"
          status={problem ? { type: "error", message: problem } : undefined}
          onChange={(v) => {
            setIdEdited(true);
            setId(toProjectId(v));
          }}
        />
        {props.templates.length > 0 && (
          <Selector
            label="Start from"
            value={template || BLANK}
            onChange={(t) => setTemplate(!t || t === BLANK ? "" : t)}
            options={[{ value: BLANK, label: "An empty graph" }, ...props.templates.map((t) => ({ value: t, label: `Template: ${t}` }))]}
          />
        )}
        {props.access && (
          <Selector
            label="Who else can open it"
            value={access}
            onChange={(g) => g && setAccess(g as GeneralAccess)}
            options={(Object.keys(GENERAL_LABEL) as GeneralAccess[]).map((g) => ({
              value: g,
              label: g === "private" ? "Only me, and people I share it with" : GENERAL_LABEL[g],
            }))}
          />
        )}
        {props.groups.length > 0 && (
          <Selector
            label="Group"
            value={group || BLANK}
            onChange={(g) => setGroup(!g || g === BLANK ? "" : g)}
            description={
              inGroup
                ? `Its runs get ${inGroup.name}'s variables and secrets${props.people ? `, and ${inGroup.name}'s people get their group role on it` : ""}.`
                : undefined
            }
            options={[
              { value: BLANK, label: "No group" },
              ...props.groups.map((g) => ({
                value: g.id,
                label: g.name,
                description: `${g.projects} project${g.projects === 1 ? "" : "s"}, ${g.variables} variable${g.variables === 1 ? "" : "s"}`,
              })),
            ]}
          />
        )}
        {err && <Banner role="alert" status="error" title={err} />}
      </VStack>
    </AppDialog>
  );
}

/** The Start from and Group choice that is none. */
const BLANK = "(empty)";

/**
 * Deleting a project takes its code, runs and outputs with it, so a click is
 * not enough: the button works once the project's id is typed.
 */
export function DeleteDialog(props: { project: HubProject; onClose: () => void; onDeleted: () => void }) {
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { id } = props.project;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (typed !== id || busy) return;
    setErr(null);
    setBusy(true);
    try {
      await hubApi.remove(id);
      props.onDeleted();
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  return (
    <AppDialog
      title={`Delete ${props.project.name || id}?`}
      subtitle="Its code, run history and outputs are deleted. This can't be undone."
      onClose={props.onClose}
      onSubmit={submit}
      footer={(formId) => (
        <>
          <Button variant="ghost" label="Cancel" onClick={props.onClose} />
          <Button type="submit" form={formId} variant="destructive" label="Delete project" isDisabled={typed !== id || busy} />
        </>
      )}
    >
      <VStack gap={4}>
        <TextInput
          label={`Type ${id} to confirm`}
          hasAutoFocus
          hasSpellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
          value={typed}
          onChange={setTyped}
        />
        {err && <Banner role="alert" status="error" title={err} />}
      </VStack>
    </AppDialog>
  );
}

/**
 * The page's frame: the bar is the workbench's app bar (src/App.tsx), and the
 * column under it is as wide as a line of text reads well.
 */
const COMPACT = "@media (max-width: 900px)";

const styles = stylex.create({
  // The bar on top, then the left nav beside the page, which scrolls on its own.
  page: { display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", height: "100dvh" },
  body: { display: "flex", minHeight: 0 },
  scroll: { flexGrow: 1, minWidth: 0, overflowY: "auto" },
  bar: {
    display: "flex",
    alignItems: "center",
    flexShrink: 0,
    gap: { default: spacingVars["--spacing-3"], [COMPACT]: spacingVars["--spacing-2"] },
    minWidth: 0,
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: { default: spacingVars["--spacing-4"], [COMPACT]: spacingVars["--spacing-2"] },
    backgroundColor: colorVars["--color-background-surface"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  grow: { flexGrow: 1 },
  // The page is a flex column, where the rule's own 100% height comes to nothing: it stretches instead.
  rule: { alignSelf: "stretch", height: "auto", marginBlock: spacingVars["--spacing-1"] },
  main: {
    boxSizing: "border-box",
    width: "100%",
    maxWidth: "760px",
    marginInline: "auto",
    paddingInline: spacingVars["--spacing-4"],
    paddingBlockStart: spacingVars["--spacing-8"],
    paddingBlockEnd: spacingVars["--spacing-10"],
  },
  title: { display: "flex", alignItems: "baseline", gap: spacingVars["--spacing-2"], minWidth: 0 },
  // The rows stand on one surface, as a panel of the workbench does.
  list: {
    overflow: "hidden",
    backgroundColor: colorVars["--color-background-surface"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
  },
  slot: { width: sizeVars["--size-element-sm"], flexShrink: 0 },
  mono: { fontFamily: typographyVars["--font-family-code"] },
});
