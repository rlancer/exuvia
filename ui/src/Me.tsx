import { useCallback, useEffect, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Code } from "@astryxdesign/core/Code";
import { Divider } from "@astryxdesign/core/Divider";
import { DropdownMenu, type DropdownMenuOption } from "@astryxdesign/core/DropdownMenu";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Selector } from "@astryxdesign/core/Selector";
import { Spinner } from "@astryxdesign/core/Spinner";
import { pixel, proportional, Table, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { HUB_PROJECT, usersApi, type UserRow } from "./api";
import { ConnectDialog } from "./Connect";
import { ago } from "./util";
import { AppDialog } from "./ui/AppDialog";
import { IconSliders, IconSpark, IconUser } from "./icons";
import { useMe } from "./useMe";

/**
 * The signed-in person's account menu (EXUVIA_AUTH=headers), at the end of
 * the top bar: a profile icon that opens who they are and what they can do
 * from here. Connect an agent (on a hub: their own agent token, with the MCP
 * setup around it) and, for an admin, Users, which manages the user list.
 * `hub`: this page is the hub's home; a hub project's workbench is one too.
 * `onAdmin`: this page has an admin panel (the hub's home), which takes Users' place.
 */
export function SignedInAs(props: { hub?: boolean; onAdmin?: () => void }) {
  const who = useMe()?.user ?? null;
  const [dialog, setDialog] = useState<"users" | "connect" | null>(null);

  if (!who) return null;
  // The connect setup is for a whole hub; someone in by agent token is an agent.
  const canConnect = (props.hub || !!HUB_PROJECT) && who.method !== "token";
  const email = who.email && who.email !== who.id ? who.email : null;
  const items: DropdownMenuOption[] = [
    ...(canConnect
      ? [{ label: "Connect an agent…", description: "Your agent token and the MCP setup", icon: <IconSpark />, onClick: () => setDialog("connect") }]
      : []),
    ...(who.role === "admin"
      ? [
          props.onAdmin
            ? { label: "Admin…", description: "Users, hub variables and MCP servers", icon: <IconSliders />, onClick: props.onAdmin }
            : { label: "Users…", description: "Roles and disabled accounts", icon: <IconUser />, onClick: () => setDialog("users") },
        ]
      : []),
  ];
  return (
    <>
      <DropdownMenu
        alignment="end"
        menuWidth={260}
        items={items}
        renderTrigger={(trigger) => (
          <IconButton {...trigger} variant="ghost" label="Account" tooltip={email ? `${who.id} · ${email}` : who.id} icon={<IconUser size={16} />} />
        )}
        header={
          <VStack gap={1}>
            <HStack gap={3} vAlign="center" padding={2}>
              <Avatar name={who.id} tooltip={false} />
              <VStack gap={0.5} xstyle={styles.who}>
                <HStack gap={2} vAlign="center">
                  <Text weight="semibold" maxLines={1}>
                    {who.id}
                  </Text>
                  {who.role === "admin" && <Badge variant="info" label="admin" />}
                </HStack>
                {email && (
                  <Text type="supporting" color="secondary" maxLines={1}>
                    {email}
                  </Text>
                )}
              </VStack>
            </HStack>
            {items.length > 0 && <Divider />}
          </VStack>
        }
      />
      {dialog === "users" && <UsersDialog self={who.uid} onClose={() => setDialog(null)} />}
      {dialog === "connect" && <ConnectDialog onClose={() => setDialog(null)} />}
    </>
  );
}

/** A user as a table row: Astryx's Table takes a record, which an interface is not. */
type Row = Pick<UserRow, keyof UserRow>;

/** The user list in a dialog of its own: where there is no admin panel (a project's workbench). */
function UsersDialog(props: { self: string | null; onClose: () => void }) {
  return (
    <AppDialog
      title="Users"
      subtitle="Everyone who has signed in."
      width={760}
      onClose={props.onClose}
      footer={<Button label="Close" onClick={props.onClose} />}
    >
      <UsersPanel self={props.self} />
    </AppDialog>
  );
}

/**
 * The user list, for an admin: who has signed in, what each may do, and a way
 * to turn someone away. A person's role is set in their row; a disabled one
 * stays in the list, greyed and struck through, so they can be let back in.
 */
export function UsersPanel(props: { self: string | null }) {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => usersApi.list().then(setUsers).catch((e: Error) => setErr(e.message)), []);
  useEffect(() => {
    load();
  }, [load]);
  const change = async (u: UserRow, c: { role?: "admin" | "member"; disabled?: boolean }) => {
    setErr(null);
    try {
      await usersApi.update(u.id, c);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const isSelf = (u: UserRow) => u.id === props.self;
  const locked = (u: UserRow) => u.pinned_admin || isSelf(u);
  const dim = (u: UserRow) => (u.disabled ? "disabled" : undefined);

  const columns: TableColumn<Row>[] = [
    {
      key: "name",
      header: "Name",
      width: proportional(1, { minWidth: 120 }),
      renderCell: (u) => (
        <Tooltip content={u.identities.map((i) => `${i.method}: ${i.subject}`).join("\n")} isEnabled={u.identities.length > 0}>
          <Text weight="medium" color={dim(u)} hasStrikethrough={u.disabled} maxLines={1}>
            {u.name}
            {isSelf(u) ? " (you)" : ""}
          </Text>
        </Tooltip>
      ),
    },
    {
      key: "email",
      header: "Email",
      width: proportional(1.15, { minWidth: 120 }),
      renderCell: (u) => (
        <Text color={dim(u) ?? "secondary"} maxLines={1}>
          {u.email ?? ""}
        </Text>
      ),
    },
    {
      key: "role",
      header: "Role",
      width: pixel(120),
      renderCell: (u) => (
        <Selector
          size="sm"
          label={`Role of ${u.name}`}
          isLabelHidden
          value={u.role}
          options={["member", "admin"]}
          isDisabled={locked(u)}
          disabledMessage={u.pinned_admin ? "Named in EXUVIA_ADMINS" : isSelf(u) ? "Another admin can change your role" : undefined}
          onChange={(role) => role && role !== u.role && change(u, { role: role as "admin" | "member" })}
        />
      ),
    },
    {
      key: "last_seen",
      header: "Last seen",
      width: pixel(92),
      renderCell: (u) => (
        <Tooltip content={new Date(u.last_seen * 1000).toLocaleString()}>
          <Text color={dim(u) ?? "secondary"} hasTabularNumbers>
            {ago(u.last_seen)}
          </Text>
        </Tooltip>
      ),
    },
    {
      key: "disabled",
      header: "",
      width: pixel(96),
      align: "end",
      renderCell: (u) => (
        <Button
          size="sm"
          variant={u.disabled ? "secondary" : "ghost"}
          label={u.disabled ? "Enable" : "Disable"}
          isDisabled={locked(u) && !u.disabled}
          onClick={() => change(u, { disabled: !u.disabled })}
        />
      ),
    },
  ];

  return (
    <VStack gap={4}>
      <Text type="supporting" color="secondary">
        A disabled user is turned away even when the sign-in proxy lets them in. People named in <Code>EXUVIA_ADMINS</Code> are always
        admins.
      </Text>
      {err && <Banner role="alert" status="error" title={err} />}
      {!users ? (
        !err && (
          <Center padding={6}>
            <Spinner aria-label="Loading the users" />
          </Center>
        )
      ) : (
        <Table<Row> data={users} columns={columns} idKey="id" density="compact" textOverflow="truncate" xstyle={styles.table} />
      )}
    </VStack>
  );
}

const styles = stylex.create({
  who: { minWidth: 0 },
  table: { minWidth: 0 },
});
