import { useCallback, useEffect, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { HStack } from "@astryxdesign/core/HStack";
import { List, ListItem } from "@astryxdesign/core/List";
import { MoreMenu } from "@astryxdesign/core/MoreMenu";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Selector } from "@astryxdesign/core/Selector";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VStack } from "@astryxdesign/core/VStack";
import { groupApi, type Group, type GroupRef, type HubGroup, type HubProject, type Member, type Role } from "./api";
import { EnvEditor, type EnvClient } from "./EnvDialog";
import { AddPeople, ROLE_LABEL } from "./Share";
import { AppDialog } from "./ui/AppDialog";

/** A group role, as the people picker offers it (exuvia/groups.py). */
export const GROUP_ROLES = [
  { value: "viewer", label: "Viewer", description: "Opens its projects and sees its variables' names" },
  { value: "editor", label: "Editor", description: "Also changes and runs its projects and sets its variables" },
];

const ROLE_WORD: Record<Role, string> = { viewer: "a viewer", editor: "an editor", owner: "the owner" };

type Tab = "projects" | "variables" | "people";

const styles = stylex.create({
  tabs: { alignSelf: "flex-start" },
});

/**
 * A group of hub projects: the projects in it, the variables and secrets they
 * inherit, and the people it gives access to. Its viewers see all of it; its
 * editors set variables and take projects out; its owner manages its people,
 * renames it, hands it over or deletes it.
 */
export function GroupDialog(props: {
  gid: string;
  self: string | null;
  onClose: () => void;
  onChanged?: () => void;
  initialTab?: Tab;
}) {
  const { gid, onChanged } = props;
  const [g, setG] = useState<Group | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<Tab>(props.initialTab ?? "projects");
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [handOver, setHandOver] = useState<Member | null>(null);
  const [removing, setRemoving] = useState<{ id: string; name: string } | null>(null);

  const load = useCallback(() => groupApi.get(gid).then(setG).catch((e: Error) => setErr(e.message)), [gid]);
  useEffect(() => {
    load();
  }, [load]);

  /** Run a change; the answer is the new group, or something to reload it after. */
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    setBusy(true);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "can_manage" in r) setG(r as Group);
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

  const envClient: EnvClient = {
    load: () => groupApi.env(gid),
    set: (name, b) => groupApi.setEnv(gid, name, b).finally(() => onChanged?.()),
    remove: (name) => groupApi.removeEnv(gid, name).finally(() => onChanged?.()),
  };

  const leave = async (m: Member) => {
    setErr(null);
    try {
      const r = await groupApi.unshare(gid, m.principal);
      onChanged?.();
      if ("left" in r) props.onClose();
      else setG(r);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const manage = !!g?.can_manage;
  return (
    <>
      <AppDialog
        title={g ? g.name : "Group"}
        subtitle={
          g
            ? `You're ${ROLE_WORD[g.you]} of this group${g.owner && g.you !== "owner" ? `, which ${g.owner.name} owns` : ""}.`
            : undefined
        }
        width={640}
        onClose={props.onClose}
        footerNote={
          manage ? (
            <HStack gap={1}>
              <Button variant="ghost" label="Rename" isDisabled={busy} onClick={() => setRenaming(true)} />
              <Button variant="ghost" label="Delete group" isDisabled={busy} onClick={() => setDeleting(true)} />
            </HStack>
          ) : undefined
        }
        footer={<Button label="Done" onClick={props.onClose} />}
      >
        <VStack gap={4}>
          {err && <Banner status="error" title={err} />}
          {!g ? (
            !err && (
              <Center>
                <Spinner aria-label="Loading the group" />
              </Center>
            )
          ) : (
            <>
              <SegmentedControl label="Show" size="sm" value={tab} onChange={(t) => setTab(t as Tab)} xstyle={styles.tabs}>
                <SegmentedControlItem value="projects" label={`Projects (${g.projects.length})`} />
                <SegmentedControlItem value="variables" label={`Variables (${g.variables.length})`} />
                {g.people_enabled && <SegmentedControlItem value="people" label={`People (${g.people.length + 1})`} />}
              </SegmentedControl>

              {tab === "projects" && (
                <VStack gap={3}>
                  <Text type="supporting" color="secondary">
                    {g.people_enabled
                      ? "The group's people get their group role on each of these projects, and every run in them gets the group's variables."
                      : "Every run in these projects gets the group's variables."}{" "}
                    The owner of a project adds it from the project's row on the Projects page.
                  </Text>
                  {g.projects.length ? (
                    <List hasDividers edge="flush">
                      {g.projects.map((p) => (
                        <ListItem
                          key={p.id}
                          href={`/p/${encodeURIComponent(p.id)}/`}
                          label={p.name || p.id}
                          description={p.id}
                          endContent={
                            g.can_edit || p.access === "owner" ? (
                              <Button
                                variant="ghost"
                                size="sm"
                                label="Take out"
                                tooltip={`Take ${p.id} out of ${g.name}: it stops getting the group's variables and people`}
                                isDisabled={busy}
                                onClick={() => setRemoving({ id: p.id, name: p.name || p.id })}
                              />
                            ) : undefined
                          }
                        />
                      ))}
                    </List>
                  ) : (
                    <Text color="secondary">No projects in this group yet.</Text>
                  )}
                </VStack>
              )}

              {tab === "variables" && (
                <VStack gap={4}>
                  <Banner
                    status="warning"
                    title="Everyone who can edit a project in this group can read these secrets"
                    description="A node can read anything in os.environ and send it anywhere. Masking secrets as *** in run logs only stops accidents, so put a project in a group only when you trust everyone who can edit it."
                  />
                  <EnvEditor
                    client={envClient}
                    canEdit={g.can_edit}
                    empty="No variables yet. Every project in the group gets the ones added here, unless the project sets the same name itself."
                  />
                </VStack>
              )}

              {tab === "people" && g.people_enabled && (
                <VStack gap={4}>
                  {manage && (
                    <AddPeople
                      busy={busy}
                      roles={GROUP_ROLES}
                      action="Add"
                      have={new Set([g.owner?.id, ...g.people.map((m) => m.user.id)].filter((x): x is string => !!x))}
                      onShare={(w) => act(() => groupApi.share(gid, w))}
                    />
                  )}
                  <List hasDividers edge="flush">
                    <ListItem
                      startContent={<Avatar size="sm" name={g.owner?.name} tooltip={false} />}
                      label={g.owner ? `${g.owner.name}${g.owner.id === props.self ? " (you)" : ""}` : "No owner"}
                      description={g.owner ? (g.owner.email !== g.owner.name ? (g.owner.email ?? undefined) : undefined) : "Hub admins manage this group"}
                      endContent={<Text color="secondary">Owner</Text>}
                    />
                    {g.people.map((m) => {
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
                                  options={GROUP_ROLES}
                                  value={m.role}
                                  isDisabled={busy}
                                  onChange={(role) =>
                                    role &&
                                    act(() =>
                                      groupApi.share(gid, {
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
                                    { label: "Remove", variant: "destructive" as const, onClick: () => void act(() => groupApi.unshare(gid, m.principal)) },
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
            </>
          )}
        </VStack>
      </AppDialog>

      {renaming && g && (
        <GroupNameDialog
          title="Rename group"
          initial={g.name}
          action="Rename"
          onClose={() => setRenaming(false)}
          onSubmit={async (name) => {
            const ok = await act(() => groupApi.patch(gid, { name }));
            if (ok) setRenaming(false);
            return ok;
          }}
        />
      )}
      <AlertDialog
        isOpen={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(false);
        }}
        title={`Delete ${g?.name ?? "this group"}?`}
        description="Its variables are deleted, and its projects stop getting them and its people. The projects themselves stay."
        actionLabel="Delete group"
        actionVariant="destructive"
        onAction={async () => {
          setDeleting(false);
          if (await act(() => groupApi.remove(gid).then(() => ({})))) props.onClose();
        }}
      />
      <AlertDialog
        isOpen={!!removing}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={`Take ${removing?.name ?? "it"} out of ${g?.name ?? "the group"}?`}
        description="Its runs stop getting the group's variables, and the group's people lose the access the group gave them."
        actionLabel="Take out"
        actionVariant="primary"
        onAction={async () => {
          const p = removing;
          setRemoving(null);
          if (p) await act(() => groupApi.setProjectGroup(p.id, null));
        }}
      />
      <AlertDialog
        isOpen={!!handOver}
        onOpenChange={(open) => {
          if (!open) setHandOver(null);
        }}
        title={`Make ${handOver?.user.name ?? "them"} the owner?`}
        description="They'll manage the group's people and can delete it. The current owner stays on as an editor."
        actionLabel="Make owner"
        actionVariant="primary"
        onAction={async () => {
          const m = handOver;
          setHandOver(null);
          if (m) await act(() => groupApi.patch(gid, { owner: m.user.id! }));
        }}
      />
    </>
  );
}

/** A group's name: for a new group, or to rename one. */
export function GroupNameDialog(props: {
  title: string;
  subtitle?: string;
  initial?: string;
  action: string;
  onClose: () => void;
  /** False keeps the dialog open (the error is shown by whoever opened it, or here). */
  onSubmit: (name: string) => Promise<boolean>;
}) {
  const [name, setName] = useState(props.initial ?? "");
  const [busy, setBusy] = useState(false);
  const ok = !!name.trim() && name.trim().length <= 80;
  return (
    <AppDialog
      title={props.title}
      subtitle={props.subtitle}
      onClose={props.onClose}
      onSubmit={async () => {
        if (!ok || busy) return;
        setBusy(true);
        const done = await props.onSubmit(name.trim());
        if (!done) setBusy(false);
      }}
      footer={(formId) => (
        <>
          <Button variant="ghost" label="Cancel" onClick={props.onClose} />
          <Button type="submit" form={formId} variant="primary" label={props.action} isDisabled={!ok || busy} />
        </>
      )}
    >
      <TextInput label="Name" hasAutoFocus value={name} placeholder="Research team" onChange={setName} status={name.trim().length > 80 ? { type: "error", message: "At most 80 characters." } : undefined} />
    </AppDialog>
  );
}

/**
 * Put a project in a group, move it to another or take it out. Adding needs
 * the project's owner and an editor of the group, and says what it gives
 * away: the group's people get access to the project, and everyone who can
 * edit the project can read the group's secrets.
 */
export function ProjectGroupDialog(props: {
  project: HubProject;
  groups: HubGroup[];
  /** People sign in on this hub (groups have people). */
  people: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { project: p } = props;
  const current: GroupRef | null = p.group ?? null;
  // The project's owner (everyone, on a hub without sharing) adds it to a group they edit.
  const owner = !p.access || p.access === "owner";
  const targets = owner ? props.groups.filter((g) => g.access !== "viewer") : [];
  const [choice, setChoice] = useState<string>(current?.id ?? NONE);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const target = choice === NONE ? null : (props.groups.find((g) => g.id === choice) ?? (current?.id === choice ? current : null));
  const changed = (target?.id ?? null) !== (current?.id ?? null);

  const options = [
    { value: NONE, label: "No group", description: "Only the project's own variables and sharing" },
    ...(current && !targets.some((g) => g.id === current.id) ? [{ value: current.id, label: current.name, description: "Its group now" }] : []),
    ...targets.map((g) => ({
      value: g.id,
      label: g.name,
      description: `${g.projects} project${g.projects === 1 ? "" : "s"}, ${g.variables} variable${g.variables === 1 ? "" : "s"}`,
    })),
  ];

  const submit = async () => {
    if (!changed || busy) return;
    setErr(null);
    setBusy(true);
    try {
      await groupApi.setProjectGroup(p.id, target?.id ?? null);
      props.onDone();
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  const whoEdits =
    p.general === "edit"
      ? `Everyone signed in can edit ${p.id}, so everyone on this hub`
      : props.people
        ? `${p.id}'s owner and editors`
        : `Everyone who can open ${p.id}`;

  return (
    <AppDialog
      title="Group"
      subtitle={p.name || p.id}
      width={520}
      onClose={props.onClose}
      onSubmit={submit}
      footer={(formId) => (
        <>
          <Button variant="ghost" label="Cancel" onClick={props.onClose} />
          <Button
            type="submit"
            form={formId}
            variant="primary"
            label={!changed ? "Save" : target ? (current ? "Move" : "Add to group") : "Take out"}
            isDisabled={!changed || busy}
          />
        </>
      )}
    >
      <VStack gap={4}>
        <Selector label="Group" width="100%" value={choice} onChange={(v) => v && setChoice(v)} options={options} />
        {owner && !targets.length && (
          <Text type="supporting" color="secondary">
            You aren't an editor of any group yet. Create one under Groups on the Projects page.
          </Text>
        )}
        {!owner && (
          <Text type="supporting" color="secondary">
            Only the project's owner can put it in a group. You can take it out of its group if you edit that group.
          </Text>
        )}
        {changed && target && (
          <Banner
            status="warning"
            title={`${whoEdits} will be able to read ${target.name}'s secrets`}
            description={
              (props.people ? `${target.name}'s people also get their group role on ${p.id}. ` : "") +
              "Anyone who can edit a project can run code that reads every variable its runs get: masking secrets in logs only stops accidents."
            }
          />
        )}
        {changed && !target && current && (
          <Text type="supporting" color="secondary">
            Its runs stop getting {current.name}'s variables{props.people ? `, and ${current.name}'s people lose the access the group gave them` : ""}.
          </Text>
        )}
        {err && <Banner role="alert" status="error" title={err} />}
      </VStack>
    </AppDialog>
  );
}

const NONE = "(none)";
