import { useCallback, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import type { ISODateString } from "@astryxdesign/core/Calendar";
import { DateInput } from "@astryxdesign/core/DateInput";
import { IconButton } from "@astryxdesign/core/IconButton";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { TextInput, type TextInputProps } from "@astryxdesign/core/TextInput";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { api, type Project, type ProjectParam } from "../api";
import { IconClose } from "../icons";
import type { Params, ParamsState } from "./protocol";

/*
 * The run parameters a view is shown with: Views mode, a view as an app and a
 * pinned view each keep their own set, and change it the same way.
 */

/** Overrides → the parameter set a view sees (resolved values + declarations). */
export function paramsState(params: ProjectParam[]): ParamsState {
  return { params: Object.fromEntries(params.map((p) => [p.name, p.value])), specs: params };
}

/** The overrides after a change: `patch` over them, and a parameter set to null or "" goes back to its default (dropped). */
export function mergeParams(overrides: Params | null, patch: Params): Params {
  const next: Params = { ...overrides, ...patch };
  for (const k of Object.keys(next)) if (next[k] === null || next[k] === "") delete next[k];
  return next;
}

/**
 * Change the parameter set a view is shown with, from a bar of controls or
 * from the view itself (`useParams`). The server checks the new set (an
 * unknown or bad value is a 400, and the promise rejects); once it takes it,
 * `onChanged` gets the new overrides and the project at them.
 */
export function useParamChange(
  overrides: Params | null,
  onChanged: (next: Params, project: Project) => void,
): (patch: Params) => Promise<ParamsState> {
  const changed = useRef(onChanged);
  changed.current = onChanged;
  return useCallback(
    async (patch: Params): Promise<ParamsState> => {
      const next = mergeParams(overrides, patch);
      const p = await api.project(next);
      changed.current(next, p);
      return paramsState(p.params);
    },
    [overrides],
  );
}

/** The run parameters, one control each. A value other than the default is marked, and has a way back to it. */
export function ParamBar(props: { specs: ProjectParam[]; overrides: Params; onChange: (p: Params) => Promise<ParamsState> }) {
  return (
    <div {...stylex.props(styles.params)}>
      {props.specs.map((p) => (
        <Param key={p.name} spec={p} custom={Object.prototype.hasOwnProperty.call(props.overrides, p.name)} onChange={props.onChange} />
      ))}
    </div>
  );
}

function Param(props: { spec: ProjectParam; custom: boolean; onChange: (p: Params) => Promise<ParamsState> }) {
  const { spec: p, custom } = props;
  const v = p.value;
  const set = (value: unknown) => props.onChange({ [p.name]: value }).catch(() => {});
  const field = { label: p.name, isLabelHidden: true, size: "sm", controlXstyle: custom && styles.custom } as const;
  return (
    <span {...stylex.props(styles.param)}>
      <Tooltip content={p.description} isEnabled={!!p.description}>
        <Text type="code" color={custom ? "accent" : "secondary"}>
          {p.name}
        </Text>
      </Tooltip>
      {p.type === "boolean" ? (
        <Switch size="sm" label={p.name} isLabelHidden value={!!v} onChange={(on) => set(on)} />
      ) : p.type === "date" ? (
        <DateInput
          {...field}
          format="system_date"
          width={184}
          value={typeof v === "string" && v ? (v as ISODateString) : undefined}
          onChange={(d) => d !== v && set(d ?? null)}
        />
      ) : p.type === "number" ? (
        <NumberInput {...field} width={96} isWheelEnabled={false} value={v == null ? null : Number(v)} onChange={(n) => n !== v && set(n)} />
      ) : (
        <TextParam key={String(v)} field={field} value={v == null ? "" : String(v)} onCommit={set} />
      )}
      {custom && (
        <IconButton
          size="sm"
          variant="ghost"
          icon={<IconClose />}
          label={`Reset ${p.name} to its default`}
          tooltip={`Back to the default (${String(p.default)})`}
          onClick={() => set(null)}
        />
      )}
    </span>
  );
}

/** A text parameter: typed freely, and set when the field is left or Enter is pressed. */
function TextParam(props: { field: Pick<TextInputProps, "label" | "isLabelHidden" | "size" | "controlXstyle">; value: string; onCommit: (text: string) => void }) {
  const [text, setText] = useState(props.value);
  return (
    <span {...stylex.props(styles.contents)} onBlur={() => text !== props.value && props.onCommit(text)}>
      <TextInput {...props.field} width={150} value={text} onChange={setText} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} />
    </span>
  );
}

const styles = stylex.create({
  params: { display: "flex", flexWrap: "wrap", alignItems: "center", columnGap: spacingVars["--spacing-4"], rowGap: spacingVars["--spacing-2"] },
  // A parameter's name, its control and the way back to its default stay together when the row wraps.
  param: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-1-5"], flexShrink: 0, whiteSpace: "nowrap" },
  // A parameter set to something other than its default.
  custom: { backgroundColor: colorVars["--color-accent-muted"], borderColor: colorVars["--color-accent"] },
  contents: { display: "contents" },
});
