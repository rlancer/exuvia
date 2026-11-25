import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { VStack } from "@astryxdesign/core/VStack";
import { hubEnvApi } from "./api";
import { McpPresetsPanel } from "./AgentMcp";
import { EnvEditor, type EnvClient } from "./EnvDialog";
import { UsersPanel } from "./Me";
import { AppDialog } from "./ui/AppDialog";

/** The admin panel's tabs. */
export type AdminTab = "users" | "variables" | "mcp";

const TITLES: Record<AdminTab, string> = { users: "Users", variables: "Hub variables", mcp: "MCP servers" };
const ABOUT: Record<AdminTab, string> = {
  users: "Everyone who has signed in, and what each may do.",
  variables: "Every project on the hub gets these in os.environ, unless the project or its group sets the same name.",
  mcp: "Servers anyone can connect to the built-in agent; nobody has one until they do.",
};

/**
 * What a hub admin manages for the whole hub, in one place: the users (where people sign in), the hub's
 * variables, and the MCP servers on offer for the agent. `tabs`: the ones this hub has, in order.
 * `self`: the admin's own user id, whose role they can't change.
 */
export function AdminDialog(props: {
  tabs: AdminTab[];
  tab: AdminTab;
  onTab: (tab: AdminTab) => void;
  self: string | null;
  onClose: () => void;
}) {
  const tab = props.tabs.includes(props.tab) ? props.tab : props.tabs[0];
  return (
    <AppDialog
      title="Admin"
      subtitle={ABOUT[tab]}
      width={800}
      onClose={props.onClose}
      footer={<Button label="Done" onClick={props.onClose} />}
    >
      <VStack gap={4}>
        <TabList role="tablist" value={tab} onChange={(t) => props.onTab(t as AdminTab)} hasDivider>
          {props.tabs.map((t) => (
            <Tab key={t} value={t} label={TITLES[t]} />
          ))}
        </TabList>
        {tab === "users" && <UsersPanel self={props.self} />}
        {tab === "variables" && <HubVariablesPanel canEdit />}
        {tab === "mcp" && <McpPresetsPanel />}
      </VStack>
    </AppDialog>
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
