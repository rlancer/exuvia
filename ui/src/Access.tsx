import { createContext, useContext, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, sizeVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { VStack } from "@astryxdesign/core/VStack";
import { HUB_PROJECT, sharingApi, type Access, type Role } from "./api";
import { Mark } from "./Brand";
import { notifyError } from "./toast";

/**
 * What this page may do in its project (exuvia/sharing.py). A viewer reads
 * everything but changes nothing: the workbench hides what would change the
 * project, and the server turns such requests away regardless.
 */
export interface AccessState {
  role: Role;
  canEdit: boolean;
  canManage: boolean;
  access: Access | null;
}

export const FULL_ACCESS: AccessState = { role: "owner", canEdit: true, canManage: true, access: null };

export const AccessContext = createContext<AccessState>(FULL_ACCESS);

export const useAccess = () => useContext(AccessContext);

export function accessState(a: Access | null): AccessState {
  const role = a?.role ?? "owner";
  return { role, canEdit: role !== "viewer", canManage: role === "owner", access: a };
}

const ownerName = (a: Access | null) => a?.owner?.name ?? "its owner";

/**
 * A strip under the top bar: this page can only look, and how to get more.
 * It is a row of the workbench's grid, between the app bar and the workspace.
 */
export function ViewOnlyBanner() {
  const { access } = useAccess();
  const [sent, setSent] = useState(!!access?.requested);
  if (!access) return null;
  const ask = async () => {
    try {
      await sharingApi.request(HUB_PROJECT!, { role: "editor" });
      setSent(true);
    } catch (e) {
      notifyError("Couldn't request edit access", e);
    }
  };
  return (
    <div {...stylex.props(styles.strip)} role="status">
      <Text xstyle={styles.words}>
        <Text weight="semibold">View only.</Text>{" "}
        {access.link
          ? "You opened this project with a share link: you can look at its graph, data and views, but not change them."
          : "You can look at this project but not change it."}
      </Text>
      {access.can_request &&
        (sent ? (
          <Text color="secondary">Edit access requested from {ownerName(access)}.</Text>
        ) : (
          <Button size="sm" label="Request edit access" clickAction={ask} />
        ))}
    </div>
  );
}

/** What can be asked for, and what each lets you do. */
const ASK: { value: Role; label: string; description: string }[] = [
  { value: "viewer", label: "View access", description: "Read its code, data and views" },
  { value: "editor", label: "Edit access", description: "Also change code and run nodes" },
];

/** The page for a project this person can't open: who to ask, and a button to ask. */
export function NoAccess(props: { access: Access | null; error?: string }) {
  const a = props.access;
  const [role, setRole] = useState<Role>("viewer");
  const [message, setMessage] = useState("");
  const [sent, setSent] = useState(!!a?.requested);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canAsk = !!a?.can_request && !sent;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canAsk || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await sharingApi.request(HUB_PROJECT!, { role, message: message.trim() || undefined });
      setSent(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main {...stylex.props(styles.page)}>
      <Card width="100%" maxWidth={460} padding={6} elevation="low">
        <form onSubmit={submit}>
          <VStack gap={4}>
            <VStack gap={2} hAlign="center">
              <Mark size={40} />
              <Heading level={2} accessibilityLevel={1} justify="center">
                You need access
              </Heading>
              <Text color="secondary" justify="center">
                {props.error ??
                  (a?.user
                    ? `You're signed in as ${a.user.id}, and project ${HUB_PROJECT} isn't shared with you.`
                    : `Project ${HUB_PROJECT} isn't shared with you.`)}
              </Text>
            </VStack>
            {canAsk && (
              <>
                <Selector label="Ask for" value={role} options={ASK} onChange={(r) => r && setRole(r as Role)} />
                <TextArea label="Message" isOptional rows={3} maxLength={500} value={message} onChange={setMessage} placeholder="What you need it for" />
              </>
            )}
            {sent && <Banner role="status" status="success" title="Request sent" description="The owner sees it in the project's Share dialog." />}
            {err && <Banner role="alert" status="error" title={err} />}
            <HStack gap={2} hAlign="end">
              <Button variant={canAsk ? "ghost" : "secondary"} href="/" label="All projects" />
              {canAsk && <Button type="submit" variant="primary" label="Request access" isDisabled={busy} />}
            </HStack>
          </VStack>
        </form>
      </Card>
    </main>
  );
}

const COMPACT = "@media (max-width: 900px)";

const styles = stylex.create({
  // On the accent's soft ground, with the app bar's own side padding so the two line up.
  strip: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: spacingVars["--spacing-3"],
    rowGap: spacingVars["--spacing-1"],
    minWidth: 0,
    // As tall with the button as without it.
    minHeight: sizeVars["--size-element-lg"],
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: { default: spacingVars["--spacing-4"], [COMPACT]: spacingVars["--spacing-2"] },
    backgroundColor: colorVars["--color-accent-muted"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  words: { flexGrow: 1, flexShrink: 1, flexBasis: "240px", minWidth: 0 },
  // The whole page: one card in the middle of it.
  page: {
    boxSizing: "border-box",
    display: "grid",
    placeItems: "center",
    minHeight: "100dvh",
    padding: spacingVars["--spacing-4"],
  },
});
