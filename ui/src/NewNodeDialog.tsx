import { useId, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { borderVars, colorVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Field } from "@astryxdesign/core/Field";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { ToggleButton, ToggleButtonGroup } from "@astryxdesign/core/ToggleButton";
import { VStack } from "@astryxdesign/core/VStack";
import { api, type GraphNode } from "./api";
import { AppDialog } from "./ui/AppDialog";

const styles = stylex.create({
  // An input is a chip with an edge, so a name that is not picked still reads as one to press.
  chip: { borderWidth: borderVars["--border-width"], borderStyle: "solid", borderColor: colorVars["--color-border-emphasized"] },
  chipOn: { borderColor: colorVars["--color-accent"], backgroundColor: colorVars["--color-accent-muted"] },
});

/**
 * A new node: its name (which is its file's), whether it is Python or SQL,
 * the nodes it reads and a line on what it makes. The inputs are chips to
 * press, every node of the graph in sight at once.
 */
export function NewNodeDialog(props: {
  nodes: GraphNode[];
  initialDeps: string[];
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"python" | "sql">("python");
  const [deps, setDeps] = useState<string[]>(props.initialDeps);
  const [description, setDescription] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const taken = props.nodes.some((n) => n.name === name);
  const kindId = useId();
  const depsId = useId();

  const submit = async () => {
    if (!name || taken || busy) return;
    setErr(null);
    setBusy(true);
    try {
      await api.create({ name, kind, deps, description });
      props.onCreated(name);
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  return (
    <AppDialog
      title="New node"
      onClose={props.onClose}
      onSubmit={submit}
      footer={(formId) => (
        <>
          <Button variant="ghost" label="Cancel" onClick={props.onClose} />
          <Button variant="primary" type="submit" form={formId} label="Create" isDisabled={!name || taken} isLoading={busy} />
        </>
      )}
    >
      <VStack gap={4}>
        {err && <Banner status="error" title="Couldn't create the node" description={err} />}
        <TextInput
          label="Name"
          hasAutoFocus
          hasSpellCheck={false}
          autoCapitalize="none"
          autoComplete="off"
          value={name}
          placeholder="rolling_corr"
          description={`The file is nodes/${name || "name"}.${kind === "sql" ? "sql" : "py"}; other nodes read it by this name.`}
          status={taken ? { type: "error", message: `There is already a node ${name}.` } : undefined}
          onChange={(v) => setName(v.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
        />
        <Field label="Kind" inputID={kindId} isGroupLabel>
          <SegmentedControl label="Kind" value={kind} onChange={(k) => setKind(k as "python" | "sql")}>
            <SegmentedControlItem value="python" label="Python" />
            <SegmentedControlItem value="sql" label="SQL" />
          </SegmentedControl>
        </Field>
        {props.nodes.length > 0 && (
          <Field label="Inputs (upstream nodes)" inputID={depsId} isGroupLabel>
            <ToggleButtonGroup type="multiple" size="sm" label="Inputs (upstream nodes)" value={deps} onChange={setDeps}>
              {props.nodes.map((n) => (
                <ToggleButton key={n.name} value={n.name} label={n.name} xstyle={[styles.chip, deps.includes(n.name) && styles.chipOn]}>
                  <Text type="code" color="inherit">
                    {n.name}
                  </Text>
                </ToggleButton>
              ))}
            </ToggleButtonGroup>
          </Field>
        )}
        <TextInput label="Description" value={description} placeholder="What this node produces" onChange={setDescription} />
      </VStack>
    </AppDialog>
  );
}
