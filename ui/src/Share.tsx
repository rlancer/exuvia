import { useCallback, useEffect, useMemo, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { List, ListItem } from "@astryxdesign/core/List";
import { MoreMenu } from "@astryxdesign/core/MoreMenu";
import { Selector } from "@astryxdesign/core/Selector";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Typeahead, TypeaheadItem } from "@astryxdesign/core/Typeahead";
import type { SearchableItem, SearchSource } from "@astryxdesign/core/Typeahead";
import { VStack } from "@astryxdesign/core/VStack";
import { sharingApi, type GeneralAccess, type Member, type Person, type Role, type Sharing } from "./api";
import { appUrl, linkToApp } from "./appMode";
import { AppDialog } from "./ui/AppDialog";
import { useCopy } from "./ui/useCopy";
import { ago } from "./util";

export const GENERAL_LABEL: Record<GeneralAccess, string> = {
  private: "Only people added here",
  view: "Everyone signed in can view",
  edit: "Everyone signed in can edit",
};

const GENERAL_HINT: Record<GeneralAccess, string> = {
  private: "Nobody else on the hub can open it",
  view: "They read its code, data and views",
  edit: "They also change code and run nodes",
};

export const ROLE_LABEL: Record<Role, string> = { viewer: "Viewer", editor: "Editor", owner: "Owner" };
const ROLES = [
  { value: "viewer", label: "Viewer", description: "Reads the code, data and views" },
  { value: "editor", label: "Editor", description: "Also changes code and runs nodes" },
];

const EXPIRY: { label: string; days: number | null }[] = [
  { label: "Never expires", days: null },
  { label: "Expires in 1 day", days: 1 },
  { label: "Expires in 7 days", days: 7 },
  { label: "Expires in 30 days", days: 30 },
];

/** A view, as Share offers it to open as an app. */
type ViewRef = { name: string; title?: string | null };

const looksLikeEmail = (s: string) => /^[^\s@]+@[^\s@]+$/.test(s.trim());

const styles = stylex.create({
  grow: { flexGrow: 1, flexShrink: 1, flexBasis: "200px", minWidth: 0 },
  role: { flexShrink: 0 },
  expired: { opacity: 0.55 },
});

/**
 * Share a hub project, as its owner (or an admin) does: add people as viewers
 * or editors (by name, or by email for someone who hasn't signed in yet), set
 * what everyone else signed in gets, make read-only links, answer access
 * requests and hand the project to a new owner. Everyone else who can open
 * the project sees who has access and can take themselves off it.
 *
 * From the top: the one thing most people came to do (add someone), then who
 * is asking, who has access, what everyone else gets, the address of a view
 * as an app, and links last.
 */
export function ShareDialog(props: {
  project: string;
  name?: string;
  self: string | null;
  /** The project's views, each of which opens as an app; the one shown now comes first. */
  views?: ViewRef[];
  view?: string;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const { project, onChanged } = props;
  const [s, setS] = useState<Sharing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<{ url: string; name: string } | null>(null);
  const [handOver, setHandOver] = useState<Member | null>(null);

  const load = useCallback(() => sharingApi.get(project).then(setS).catch((e: Error) => setErr(e.message)), [project]);
  useEffect(() => {
    load();
  }, [load]);

  /** Run a change; the answer is the new sharing (or a reason it failed). */
  const act = async (fn: () => Promise<Sharing | unknown>) => {
    setErr(null);
    setBusy(true);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "project" in r) setS(r as Sharing);
      else await load();
      onChanged?.();
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const manage = !!s?.can_manage;
  const leave = async (m: Member) => {
    setErr(null);
    try {
      const r = await sharingApi.unshare(project, m.principal);
      onChanged?.();
      if ("left" in r) location.assign("/");
      else setS(r);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <>
      <AppDialog
        title="Share"
        subtitle={props.name || project}
        width={560}
        onClose={props.onClose}
        footer={<Button label="Done" onClick={props.onClose} />}
        footerNote={
          s && !manage ? (
            <Text type="supporting" color="secondary">
              You're {s.you === "editor" ? "an editor" : "a viewer"} here. Only the owner{s.owner ? ` (${s.owner.name})` : ""} or a hub admin changes
              who has access.
            </Text>
          ) : undefined
        }
      >
        <VStack gap={5}>
          {err && <Banner status="error" title={err} />}
          {!s ? (
            !err && (
              <Center>
                <Spinner aria-label="Loading who has access" />
              </Center>
            )
          ) : (
            <>
              {manage && s.people_enabled && (
                <AddPeople
                  busy={busy}
                  have={new Set([s.owner?.id, ...s.people.map((m) => m.user.id)].filter((x): x is string => !!x))}
                  onShare={(w) => act(() => sharingApi.share(project, w))}
                />
              )}

              {manage && !!s.requests?.length && (
                <VStack gap={2}>
                  <Heading level={3}>Asking for access</Heading>
                  <List hasDividers edge="flush">
                    {s.requests.map((r) => (
                      <RequestRow
                        key={r.user_id}
                        person={r.user}
                        asked={r.role}
                        message={r.message}
                        at={r.created_at}
                        busy={busy}
                        onApprove={(role) => act(() => sharingApi.approve(project, r.user_id, role))}
                        onDismiss={() => act(() => sharingApi.dismiss(project, r.user_id))}
                      />
                    ))}
                  </List>
                </VStack>
              )}

              {s.people_enabled && (
                <VStack gap={2}>
                  <Heading level={3}>People with access</Heading>
                  <List hasDividers edge="flush">
                    <ListItem
                      startContent={<Avatar size="sm" name={s.owner?.name} tooltip={false} />}
                      label={s.owner ? `${s.owner.name}${s.owner.id === props.self ? " (you)" : ""}` : "No owner"}
                      description={s.owner ? (s.owner.email !== s.owner.name ? (s.owner.email ?? undefined) : undefined) : "Hub admins manage this project"}
                      endContent={<Text color="secondary">Owner</Text>}
                    />
                    {s.people.map((m) => {
                      const mine = m.kind === "user" && m.user.id === props.self;
                      return (
                        <ListItem
                          key={m.principal}
                          startContent={<Avatar size="sm" name={m.user.name} tooltip={false} />}
                          label={
                            <HStack gap={2} vAlign="center">
                              <Text maxLines={1}>
                                {m.user.name}
                                {mine ? " (you)" : ""}
                              </Text>
                              {m.pending && <Badge label="Invited" />}
                            </HStack>
                          }
                          description={
                            m.pending
                              ? "Takes effect when they first sign in with this email"
                              : m.user.email && m.user.email !== m.user.name
                                ? m.user.email
                                : undefined
                          }
                          endContent={
                            manage ? (
                              <HStack gap={1} vAlign="center">
                                <Selector
                                  label={`Role of ${m.user.name}`}
                                  isLabelHidden
                                  size="sm"
                                  variant="ghost"
                                  options={ROLES}
                                  value={m.role}
                                  isDisabled={busy}
                                  onChange={(role) =>
                                    role &&
                                    act(() =>
                                      sharingApi.share(project, {
                                        ...(m.kind === "user" ? { user: m.user.id! } : { email: m.user.email! }),
                                        role: role as Role,
                                      }),
                                    )
                                  }
                                />
                                <MoreMenu
                                  size="sm"
                                  label={`More for ${m.user.name}`}
                                  isDisabled={busy}
                                  alignment="end"
                                  items={[
                                    ...(m.kind === "user"
                                      ? [{ label: "Make owner", description: "You stay on as an editor", onClick: () => setHandOver(m) }]
                                      : []),
                                    { label: "Remove", variant: "destructive" as const, onClick: () => void act(() => sharingApi.unshare(project, m.principal)) },
                                  ]}
                                />
                              </HStack>
                            ) : (
                              <HStack gap={2} vAlign="center">
                                <Text color="secondary">{ROLE_LABEL[m.role]}</Text>
                                {mine && <Button size="sm" label="Leave" onClick={() => leave(m)} />}
                              </HStack>
                            )
                          }
                        />
                      );
                    })}
                  </List>
                </VStack>
              )}

              {s.people_enabled && (
                <VStack gap={2}>
                  <Heading level={3}>General access</Heading>
                  {manage ? (
                    <Selector
                      label="General access"
                      isLabelHidden
                      width="100%"
                      options={(Object.keys(GENERAL_LABEL) as GeneralAccess[]).map((g) => ({ value: g, label: GENERAL_LABEL[g], description: GENERAL_HINT[g] }))}
                      value={s.general}
                      isDisabled={busy}
                      onChange={(g) => g && act(() => sharingApi.patch(project, { general: g as GeneralAccess }))}
                    />
                  ) : (
                    <Text>{GENERAL_LABEL[s.general]}</Text>
                  )}
                  {s.default && (
                    <Text type="supporting" color="secondary">
                      The hub's default, since the project has no setting of its own yet.
                    </Text>
                  )}
                </VStack>
              )}

              {!!props.views?.length && <AppLink project={project} views={props.views} initial={props.view} />}

              {manage && (
                <Links
                  links={s.links ?? []}
                  views={props.views ?? []}
                  busy={busy}
                  made={made}
                  onCreate={async (name, days) => {
                    setErr(null);
                    setBusy(true);
                    try {
                      const l = await sharingApi.createLink(project, { name, expires_in_days: days });
                      setMade({ url: location.origin + l.url, name: l.name });
                      await load();
                      onChanged?.();
                    } catch (e) {
                      setErr((e as Error).message);
                    } finally {
                      setBusy(false);
                    }
                  }}
                  onRevoke={(id) => act(() => sharingApi.revokeLink(project, id))}
                />
              )}
            </>
          )}
        </VStack>
      </AppDialog>

      <AlertDialog
        isOpen={!!handOver}
        onOpenChange={(open) => {
          if (!open) setHandOver(null);
        }}
        title={`Make ${handOver?.user.name ?? "them"} the owner?`}
        description="They'll manage who has access and can delete the project. The current owner stays on as an editor."
        actionLabel="Make owner"
        actionVariant="primary"
        onAction={async () => {
          const m = handOver;
          setHandOver(null);
          if (m) await act(() => sharingApi.patch(project, { owner: m.user.id! }));
        }}
      />
    </>
  );
}

/** Someone to share with: a person from the user list, or an email nobody has signed in with yet. */
type Pick = SearchableItem<{ person?: Person; email?: string }>;

/** A name or email box that suggests people from the user list, a role, and Share. */
export function AddPeople(props: {
  busy: boolean;
  /** The owner and everyone it is shared with: not suggested again. */
  have: Set<string>;
  onShare: (w: { user?: string; email?: string; role: Role }) => Promise<boolean>;
  /** The roles offered (default: a project's), and the button's label (default: Share). */
  roles?: { value: string; label: string; description: string }[];
  action?: string;
}) {
  const [role, setRole] = useState<Role>("viewer");
  const [picked, setPicked] = useState<Pick | null>(null);
  const { have } = props;

  const source = useMemo<SearchSource<Pick>>(
    () => ({
      bootstrap: () => [],
      async search(q) {
        const people = await sharingApi.people(q).catch(() => [] as Person[]);
        const items: Pick[] = people
          .filter((p) => p.id && !have.has(p.id))
          .map((p) => ({ id: p.id!, label: p.name, auxiliaryData: { person: p } }));
        const email = q.trim();
        if (looksLikeEmail(email) && !people.some((p) => p.email === email)) {
          items.push({ id: `email:${email}`, label: email, auxiliaryData: { email } });
        }
        return items;
      },
    }),
    [have],
  );

  const target = picked?.auxiliaryData?.person ? { user: picked.auxiliaryData.person.id! } : picked?.auxiliaryData?.email ? { email: picked.auxiliaryData.email } : null;
  const share = async () => {
    if (target && (await props.onShare({ ...target, role }))) setPicked(null);
  };

  return (
    <VStack gap={2}>
      <Heading level={3}>Add people</Heading>
      <HStack gap={2} vAlign="end" wrap="wrap">
      <Typeahead<Pick>
        label="Add people by name or email"
        isLabelHidden
        placeholder="Name or email"
        searchSource={source}
        value={picked}
        onChange={setPicked}
        containerXstyle={styles.grow}
        emptySearchText="Nobody matches. Type a full email to invite someone who hasn't signed in yet."
        renderItem={(item) => (
          <TypeaheadItem
            item={item}
            icon={<Avatar size="xsm" name={item.label} tooltip={false} />}
            description={item.auxiliaryData?.email ? "Invite: takes effect when they first sign in" : (item.auxiliaryData?.person?.email ?? undefined)}
          />
        )}
      />
      <Selector label="Role" isLabelHidden options={props.roles ?? ROLES} value={role} onChange={(r) => r && setRole(r as Role)} containerXstyle={styles.role} />
      <Button variant="primary" label={props.action ?? "Share"} isDisabled={!target || props.busy} onClick={share} />
      </HStack>
    </VStack>
  );
}

function RequestRow(props: {
  person: Person;
  asked: Role;
  message: string;
  at: number;
  busy: boolean;
  onApprove: (role: Role) => void;
  onDismiss: () => void;
}) {
  const [role, setRole] = useState<Role>(props.asked);
  const name = props.person.name;
  return (
    <ListItem
      align="start"
      startContent={<Avatar size="sm" name={name} tooltip={false} />}
      label={name}
      description={
        <Text type="supporting" color="secondary">
          Asked for {props.asked === "editor" ? "edit" : "view"} access {ago(props.at)}
          {props.message ? `: “${props.message}”` : ""}
        </Text>
      }
      endContent={
        <HStack gap={1} vAlign="center">
          <Selector label={`Role to give ${name}`} isLabelHidden size="sm" variant="ghost" options={ROLES} value={role} onChange={(r) => r && setRole(r as Role)} />
          <Button size="sm" variant="primary" label="Approve" isDisabled={props.busy} onClick={() => props.onApprove(role)} />
          <Button size="sm" variant="ghost" label="Dismiss" isDisabled={props.busy} onClick={props.onDismiss} />
        </HStack>
      }
    />
  );
}

/** Options for what a link or address opens: the workbench, or one view as an app. */
const viewOptions = (views: ViewRef[]) => views.map((v) => ({ value: v.name, label: v.title || v.name, description: "As an app, full screen" }));

/**
 * The address of one view as an app: the view full screen, without the
 * workbench. It opens for whoever can already open the project; someone
 * without access gets it through a share link, below.
 */
function AppLink(props: { project: string; views: ViewRef[]; initial?: string }) {
  const [view, setView] = useState(props.views.some((v) => v.name === props.initial) ? props.initial! : props.views[0].name);
  const [copied, copy] = useCopy();
  const url = appUrl(location.origin, props.project, view);
  return (
    <VStack gap={2}>
      <VStack gap={0.5}>
        <Heading level={3}>Open a view as an app</Heading>
        <Text type="supporting" color="secondary">
          The view full screen, without the workbench, for anyone who can open the project. Parameters and the other views are under its Tools button.
        </Text>
      </VStack>
      <HStack gap={2} vAlign="end" wrap="wrap">
        <Selector label="View" isLabelHidden options={viewOptions(props.views)} value={view} onChange={(v) => v && setView(v)} containerXstyle={styles.role} />
        <TextInput label="The view's address as an app" isLabelHidden isReadOnly value={url} containerXstyle={styles.grow} />
        <Button label={copied ? "Copied" : "Copy"} onClick={() => copy(url)} />
      </HStack>
    </VStack>
  );
}

function Links(props: {
  links: NonNullable<Sharing["links"]>;
  views: ViewRef[];
  busy: boolean;
  made: { url: string; name: string } | null;
  onCreate: (name: string, days: number | null) => void;
  onRevoke: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("0");
  const [copied, copy] = useCopy();
  // What the new link opens: the workbench (""), or a view as an app.
  const [opens, setOpens] = useState("");
  const made = props.made ? linkToApp(props.made.url, opens || null) : "";
  const create = () => {
    props.onCreate(name.trim(), EXPIRY[Number(expiry)].days);
    setName("");
  };
  return (
    <VStack gap={2}>
      <VStack gap={0.5}>
        <Heading level={3}>Share links</Heading>
        <Text type="supporting" color="secondary">
          Anyone with a link can open this project read-only, without signing in. Revoke a link to close it.
        </Text>
      </VStack>
      {props.made && (
        <Banner status="success" title="Link created. It is shown once: copy it now." collapsible={false}>
          <HStack gap={2} vAlign="end" wrap="wrap">
            {props.views.length > 0 && (
              <Selector
                label="What the link opens"
                isLabelHidden
                options={[{ value: "", label: "The workbench", description: "The graph, the code and every view" }, ...viewOptions(props.views)]}
                value={opens}
                onChange={(v) => setOpens(v ?? "")}
                containerXstyle={styles.role}
              />
            )}
            <TextInput label="The new link" isLabelHidden isReadOnly value={made} containerXstyle={styles.grow} />
            <Button label={copied ? "Copied" : "Copy"} onClick={() => copy(made)} />
          </HStack>
        </Banner>
      )}
      {props.links.length > 0 && (
        <List hasDividers edge="flush">
          {props.links.map((l) => (
            <ListItem
              key={l.id}
              label={
                <HStack gap={2} vAlign="center" xstyle={l.expired ? styles.expired : undefined}>
                  <Text maxLines={1}>{l.name}</Text>
                  <Text type="code" color="secondary">
                    …{l.hint}
                  </Text>
                  {l.expired && <Badge variant="warning" label="Expired" />}
                </HStack>
              }
              description={`${l.expired ? "Expired" : l.expires_at ? `Expires ${new Date(l.expires_at * 1000).toLocaleDateString()}` : "No expiry"} · ${l.last_used ? `opened ${ago(l.last_used)}` : "never opened"}`}
              endContent={<Button size="sm" variant="ghost" label="Revoke" isDisabled={props.busy} onClick={() => props.onRevoke(l.id)} />}
            />
          ))}
        </List>
      )}
      <HStack gap={2} vAlign="end" wrap="wrap">
        <TextInput
          label="Link name"
          isLabelHidden
          placeholder="What it's for (e.g. the board)"
          value={name}
          onChange={(v) => setName(v.slice(0, 80))}
          onEnter={create}
          containerXstyle={styles.grow}
        />
        <Selector
          label="Link expiry"
          isLabelHidden
          options={EXPIRY.map((x, i) => ({ value: String(i), label: x.label }))}
          value={expiry}
          onChange={(v) => v && setExpiry(v)}
          containerXstyle={styles.role}
        />
        <Button label="Create link" isDisabled={props.busy} onClick={create} />
      </HStack>
    </VStack>
  );
}
