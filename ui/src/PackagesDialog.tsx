import { useEffect, useId, useState } from "react";
import { EditorView } from "@uiw/react-codemirror";
import * as stylex from "@stylexjs/stylex";
import { borderVars, colorVars, radiusVars, spacingVars, typeScaleVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/Code";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { pixel, proportional, Table, type TableColumn } from "@astryxdesign/core/Table";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VStack } from "@astryxdesign/core/VStack";
import { api, type NpmState, type PackagesState } from "./api";
import { IconTrash } from "./icons";
import { AppDialog } from "./ui/AppDialog";
import { CodeEditor } from "./ui/CodeEditor";
import { StateDot } from "./ui/state";
import { useColorMode } from "./theme/colorMode";

const STATUS: Record<PackagesState["status"], string> = {
  none: "no packages listed",
  pending: "waiting to install",
  installing: "installing…",
  ready: "installed",
  failed: "install failed",
  remote: "installed by the executor",
};

const NEW_MANIFEST = '[project]\nname = "project"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = []\n';

/** What a screen reader calls the editor of the manifest. */
const MANIFEST_EDITOR = [EditorView.contentAttributes.of({ "aria-label": "pyproject.toml" })];

/** Who made a request: the built-in agent, an API client, or an MCP client's own name. */
const requester = (origin: string) => (origin === "agent" ? "The agent" : origin === "api" ? "An API client" : origin);

/** Requirement strings typed into the add box: `scipy, acme-models>=2` or one per line. */
const parseSpecs = (text: string) =>
  text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);

/** A row of the table: Astryx's Table reads plain records. */
type Row = { requirement: string; name: string; version: string | null; source: "project" | "exuvia" | null };
/** A row of the npm table (a type, not an interface, so the Table reads it as a record). */
type NpmRow = { name: string; version: string; exact: boolean };

const styles = stylex.create({
  grow: { flexGrow: 1, flexBasis: 0, minWidth: 0 },
  // A requirement is read letter by letter: `>=` stays two characters.
  spec: { fontVariantLigatures: "none", fontFeatureSettings: '"liga" 0, "calt" 0' },
  // The file's editor: a frame of a fixed height, which the editor fills and scrolls in.
  editor: {
    display: "flex",
    flexDirection: "column",
    alignSelf: "stretch",
    height: "260px",
    overflow: "hidden",
    borderWidth: borderVars["--border-width"],
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    fontVariantLigatures: "none",
  },
  // What uv or pip printed: the code ground, the mono, and a height it scrolls in.
  output: {
    margin: 0,
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-3"],
    maxHeight: "240px",
    overflow: "auto",
    backgroundColor: "var(--exuvia-code-background)",
    borderWidth: borderVars["--border-width"],
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    fontFamily: typographyVars["--font-family-code"],
    fontSize: typeScaleVars["--text-code-size"],
    lineHeight: 1.5,
    color: colorVars["--color-text-primary"],
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    fontVariantLigatures: "none",
  },
  wrong: { borderColor: colorVars["--color-border-error"] },
});

type Kind = "python" | "npm";

/** The project's packages, in two tabs: Python packages for nodes (pyproject.toml)
 * and npm packages for views (package.json). */
export function PackagesDialog(props: {
  state: PackagesState | null;
  onChange: (s: PackagesState) => void;
  onClose: () => void;
  initial?: Kind;
}) {
  const { state, onClose } = props;
  const [kind, setKind] = useState<Kind>(props.initial ?? "python");
  const [npm, setNpm] = useState<NpmState | null>(null);
  const [npmErr, setNpmErr] = useState<string | null>(null);
  const panel = useId();
  useEffect(() => {
    api.npm().then(setNpm, (e: Error) => setNpmErr(e.message));
  }, []);
  const python = kind === "python";
  const agent = state?.agent === "install" ? "The agent installs packages itself" : state?.agent === "off" ? "The agent cannot ask for packages" : "Agent requests wait for your approval";
  return (
    <AppDialog
      title="Packages"
      subtitle={
        python
          ? "The packages your nodes import beyond those exuvia comes with, from the project's pyproject.toml."
          : "The npm packages your views import beyond those every view has, from the project's package.json."
      }
      width={640}
      onClose={onClose}
      footerNote={
        (python || npm) && (
          <Text type="supporting" color="secondary">
            {python ? agent : `Every view has ${npm!.builtins.join(", ")}`}
          </Text>
        )
      }
      footer={<Button label="Done" onClick={onClose} />}
    >
      <TabList role="tablist" size="sm" hasDivider value={kind} onChange={(k) => setKind(k as Kind)}>
        <Tab value="python" label="Python, for nodes" panelId={panel} />
        <Tab value="npm" label="npm, for views" panelId={panel} />
      </TabList>
      <VStack gap={4} id={panel} role="tabpanel" aria-label={python ? "Python, for nodes" : "npm, for views"} paddingBlock={3}>
        {python ? <PythonPackages state={state} onChange={props.onChange} /> : <NpmPackages state={npm} onChange={setNpm} loadError={npmErr} />}
      </VStack>
    </AppDialog>
  );
}

/** Python packages: what pyproject.toml lists, adding and removing,
 * the install status and log, and the agent's requests to approve. */
function PythonPackages(props: { state: PackagesState | null; onChange: (s: PackagesState) => void }) {
  const { state, onChange } = props;
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // What went wrong, and which action it was (its message shows next to that action).
  const [err, setErr] = useState<{ at: string; text: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [dark] = useColorMode();

  const act = async (label: string, fn: () => Promise<PackagesState>) => {
    setBusy(label);
    setErr(null);
    try {
      onChange(await fn());
      return true;
    } catch (e) {
      setErr({ at: label, text: (e as Error).message });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const add = async () => {
    const specs = parseSpecs(adding);
    if (specs.length && !busy && (await act("add", () => api.packagesEdit({ add: specs })))) setAdding("");
  };

  const status = state?.status ?? "none";
  // What an action just answered is announced; what a past install left is read with the status.
  const failure = (text: string, announce = true) => (
    <pre role={announce ? "alert" : undefined} {...stylex.props(styles.output, styles.wrong)}>
      {text}
    </pre>
  );

  const columns: TableColumn<Row>[] = [
    {
      key: "requirement",
      header: "Package",
      width: proportional(2, { minWidth: 140 }),
      renderCell: (p) => (
        <Text type="code" xstyle={styles.spec}>
          {p.requirement}
        </Text>
      ),
    },
    {
      key: "version",
      header: "Version",
      width: proportional(1, { minWidth: 72 }),
      renderCell: (p) => (
        <Text type="code" color="secondary">
          {p.version ?? "—"}
        </Text>
      ),
    },
    {
      key: "source",
      header: "Source",
      width: proportional(1.5, { minWidth: 110 }),
      renderCell: (p) => (
        <Text type="supporting" color="secondary">
          {p.source === "exuvia" ? "comes with exuvia" : p.source === "project" ? "installed" : ""}
        </Text>
      ),
    },
    {
      key: "remove",
      header: "",
      width: pixel(56),
      align: "end",
      renderCell: (p) => (
        <IconButton
          variant="ghost"
          size="sm"
          label={`Remove ${p.name}`}
          tooltip={`Remove ${p.name}`}
          icon={<IconTrash />}
          isDisabled={!!busy && busy !== `rm${p.name}`}
          isLoading={busy === `rm${p.name}`}
          onClick={() => act(`rm${p.name}`, () => api.packagesEdit({ remove: [p.name] }))}
        />
      ),
    },
  ];

  return (
    <>
        {/* What the environment is doing, and what went wrong if it failed. */}
        <VStack gap={2}>
          <HStack gap={2} vAlign="center">
            <HStack gap={2} vAlign="center" xstyle={styles.grow} role="status">
              <StateDot state={status === "installing" ? "running" : status} label={STATUS[status]} />
              <Text weight="medium">{STATUS[status]}</Text>
              {status === "ready" && !!state?.installed && (
                <Text type="supporting" color="secondary">
                  {state.installed} on top of exuvia's own
                </Text>
              )}
            </HStack>
            {status === "failed" && state?.error && (
              <Button size="sm" label="Retry" isDisabled={!!busy && busy !== "retry"} isLoading={busy === "retry"} onClick={() => act("retry", api.packagesRetry)} />
            )}
          </HStack>
          {status === "failed" && state?.error && failure(state.error, false)}
          {err?.at === "retry" && failure(err.text)}
        </VStack>

        {!!state?.requests.length && (
          <VStack gap={2}>
            {state.requests.map((r) => (
              <VStack key={r.id} gap={2}>
                <Banner
                  status="info"
                  title={
                    <>
                      {requester(r.origin)} asks for <Code xstyle={styles.spec}>{r.packages.join(", ")}</Code>
                    </>
                  }
                  description={r.reason || undefined}
                  endContent={
                    <HStack gap={1}>
                      <Button
                        size="sm"
                        label={busy === `req${r.id}` ? "Checking…" : "Install"}
                        isDisabled={!!busy}
                        onClick={() => act(`req${r.id}`, () => api.packagesApprove(r.id))}
                      />
                      <Button variant="ghost" size="sm" label="Dismiss" isDisabled={!!busy} onClick={() => act(`req${r.id}`, () => api.packagesDismiss(r.id))} />
                    </HStack>
                  }
                />
                {err?.at === `req${r.id}` && failure(err.text)}
              </VStack>
            ))}
          </VStack>
        )}

        <VStack gap={2}>
          <HStack gap={2} vAlign="end">
            <TextInput
              label="Packages to add"
              description="A package added without a version is pinned to the one installed."
              hasAutoFocus
              hasSpellCheck={false}
              autoCapitalize="none"
              autoComplete="off"
              value={adding}
              placeholder="scikit-learn, acme-models>=2"
              onChange={setAdding}
              onEnter={add}
              containerXstyle={styles.grow}
              controlXstyle={styles.spec}
            />
            <Button variant="primary" label={busy === "add" ? "Checking…" : "Add"} isDisabled={!parseSpecs(adding).length || !!busy} onClick={add} />
          </HStack>
          {err && (err.at === "add" || err.at.startsWith("rm")) && failure(err.text)}
        </VStack>

        {state && state.packages.length > 0 && <Table<Row> data={state.packages.map((p) => ({ ...p }))} columns={columns} idKey="requirement" density="compact" />}

        {/* What few people need: the file itself, and what the installer printed. */}
        <VStack gap={2}>
          <Collapsible
            trigger={<Text weight="medium">Edit pyproject.toml</Text>}
            chevronPosition="start"
            isOpen={editing !== null}
            onOpenChange={(open) => setEditing(open ? (state?.manifest ?? NEW_MANIFEST) : null)}
          >
            {editing !== null && (
              <VStack gap={2} hAlign="end" paddingBlock={2}>
                <div {...stylex.props(styles.editor)}>
                  <CodeEditor value={editing} onChange={setEditing} dark={dark} extensions={MANIFEST_EDITOR} />
                </div>
                {err?.at === "manifest" && failure(err.text)}
                <Button
                  size="sm"
                  label="Save and install"
                  isDisabled={!!busy && busy !== "manifest"}
                  isLoading={busy === "manifest"}
                  onClick={async () => {
                    if (await act("manifest", () => api.packagesManifest(editing))) setEditing(null);
                  }}
                />
              </VStack>
            )}
          </Collapsible>
          {state?.log && (
            <Collapsible trigger={<Text weight="medium">Install log</Text>} chevronPosition="start" isOpen={showLog} onOpenChange={setShowLog}>
              <VStack paddingBlock={2}>
                <pre {...stylex.props(styles.output)}>{state.log}</pre>
              </VStack>
            </Collapsible>
          )}
        </VStack>
    </>
  );
}

/** npm packages for views: what package.json lists, adding and removing. Nothing
 * installs: the server fetches a package's modules when a view imports it. */
function NpmPackages(props: { state: NpmState | null; onChange: (s: NpmState) => void; loadError: string | null }) {
  const { state, onChange: setState } = props;
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(props.loadError);

  const act = async (label: string, fn: () => Promise<NpmState>) => {
    setBusy(label);
    setErr(null);
    try {
      setState(await fn());
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  };

  const add = async () => {
    const specs = parseSpecs(adding);
    if (specs.length && !busy && (await act("add", () => api.npmEdit({ add: specs })))) setAdding("");
  };

  const columns: TableColumn<NpmRow>[] = [
    {
      key: "name",
      header: "Package",
      width: proportional(2, { minWidth: 140 }),
      renderCell: (p) => (
        <Text type="code" xstyle={styles.spec}>
          {p.name}
        </Text>
      ),
    },
    {
      key: "version",
      header: "Version",
      width: proportional(1, { minWidth: 72 }),
      renderCell: (p) => (
        <Text type="code" color="secondary" xstyle={styles.spec}>
          {p.version}
        </Text>
      ),
    },
    {
      key: "remove",
      header: "",
      width: pixel(56),
      align: "end",
      renderCell: (p) => (
        <IconButton
          variant="ghost"
          size="sm"
          label={`Remove ${p.name}`}
          tooltip={`Remove ${p.name}`}
          icon={<IconTrash />}
          isDisabled={!!busy && busy !== `rm${p.name}`}
          isLoading={busy === `rm${p.name}`}
          onClick={() => act(`rm${p.name}`, () => api.npmEdit({ remove: [p.name] }))}
        />
      ),
    },
  ];

  return (
    <>
        {state?.error && (
          <pre role="alert" {...stylex.props(styles.output, styles.wrong)}>
            {state.error}
          </pre>
        )}
        <VStack gap={2}>
          <HStack gap={2} vAlign="end">
            <TextInput
              label="Packages to add"
              description="A package added without a version is pinned to its newest release."
              hasAutoFocus
              hasSpellCheck={false}
              autoCapitalize="none"
              autoComplete="off"
              value={adding}
              placeholder="recharts, date-fns@^4"
              onChange={setAdding}
              onEnter={add}
              containerXstyle={styles.grow}
              controlXstyle={styles.spec}
            />
            <Button variant="primary" label={busy === "add" ? "Checking…" : "Add"} isDisabled={!parseSpecs(adding).length || !!busy} onClick={add} />
          </HStack>
          {err && (
            <pre role="alert" {...stylex.props(styles.output, styles.wrong)}>
              {err}
            </pre>
          )}
        </VStack>
        {state && state.packages.length > 0 && <Table<NpmRow> data={state.packages.map((p) => ({ ...p }))} columns={columns} idKey="name" density="compact" />}
        <Text type="supporting" color="secondary">
          A view imports these by name. exuvia fetches them from {state?.cdn ?? "the CDN"} when a view needs them and hands them
          to the view, which still has no network: a package that loads map tiles, fonts or data from the internet will not work
          there.
        </Text>
    </>
  );
}
