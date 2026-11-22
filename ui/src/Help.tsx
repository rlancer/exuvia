import { useCallback, useEffect, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Button } from "@astryxdesign/core/Button";
import { Divider } from "@astryxdesign/core/Divider";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Link } from "@astryxdesign/core/Link";
import { Popover } from "@astryxdesign/core/Popover";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";
import { VStack } from "@astryxdesign/core/VStack";
import { about as fetchAbout, type About } from "./api";
import { Code } from "./Code";
import { IconHelp } from "./icons";
import { StateDot } from "./ui/state";
import { ago } from "./util";

const styles = stylex.create({
  button: { position: "relative", display: "inline-flex" },
  // The news of a release: a dot on the button's corner, ringed in the bar's ground.
  dot: {
    position: "absolute",
    insetBlockStart: spacingVars["--spacing-0-5"],
    insetInlineEnd: spacingVars["--spacing-0-5"],
    pointerEvents: "none",
    boxShadow: `0 0 0 2px ${colorVars["--color-background-surface"]}`,
  },
  baseline: { alignItems: "baseline" },
  grow: { flexGrow: 1, minWidth: 0 },
});

const SEEN_KEY = "exuvia-update-seen";

function seenUpdate(): string | null {
  try {
    return localStorage.getItem(SEEN_KEY);
  } catch {
    return null; // private mode
  }
}

function markSeen(version: string) {
  try {
    localStorage.setItem(SEEN_KEY, version);
  } catch {
    /* private mode */
  }
}

const INSTALL: Record<About["install"], string> = {
  deployed: "Deployed server",
  source: "Development install from a clone",
  uvx: "Run with uvx",
  "uv-tool": "Installed with uv tool",
  pipx: "Installed with pipx",
  pip: "Installed with pip",
};

/**
 * The top bar's help button: the running version and whether a newer one is
 * out, plus links to the docs and the issue tracker. A new release is never
 * announced in a toast or a banner: the button gets a small dot until the
 * menu has been opened once for that release, and the menu says how to
 * update. What it says depends on the install: a command for someone's own
 * install, or, on a deployed server, that whoever runs it updates it.
 */
export function HelpMenu() {
  const [info, setInfo] = useState<About | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [seen, setSeen] = useState(seenUpdate);

  const load = useCallback((refresh = false) => {
    setChecking(true);
    return fetchAbout(refresh)
      .then((a) => {
        setInfo(a);
        setFailed(false);
      })
      .catch(() => setFailed(true)) // an older server without /api/about
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const fresh = info?.update === "available" && info.latest ? info.latest : null;
  // Opening the menu is seeing the news: the dot stays away until the next release.
  useEffect(() => {
    if (open && fresh && seen !== fresh) {
      markSeen(fresh);
      setSeen(fresh);
    }
  }, [open, fresh, seen]);

  const dot = !!fresh && seen !== fresh;
  const label = dot ? `Help · exuvia ${fresh} is available` : "Help and version";

  return (
    <Popover
      label="Help"
      isOpen={open}
      onOpenChange={setOpen}
      placement="below"
      alignment="end"
      width={340}
      padding={4}
      content={
        <VStack gap={3}>
          <VStack gap={1}>
            <HStack gap={2} xstyle={styles.baseline}>
              <Heading level={3}>exuvia</Heading>
              <Text type="code" color="secondary">
                {info?.version ?? (failed ? "unknown" : "…")}
              </Text>
            </HStack>
            {info && (
              <Text type="supporting" color="secondary">
                {INSTALL[info.install]}
              </Text>
            )}
          </VStack>
          {info && <Update info={info} checking={checking} onCheck={() => load(true)} />}
          {failed && !info && (
            <Text type="supporting" color="secondary">
              This server doesn't say which version it runs.
            </Text>
          )}
          <Divider />
          <VStack as="nav" gap={1.5}>
            <Link isExternalLink href={info?.docs_url ?? "https://exuvia.lobster.mp/"}>
              Documentation
            </Link>
            <Link isExternalLink href={info?.issues_url ?? "https://github.com/rlancer/exuvia/issues"}>
              Report an issue
            </Link>
            <Link isExternalLink href={info?.repo_url ?? "https://github.com/rlancer/exuvia"}>
              Source on GitHub
            </Link>
          </VStack>
        </VStack>
      }
    >
      <span {...stylex.props(styles.button)}>
        <IconButton variant="ghost" label={label} tooltip={label} icon={<IconHelp />} />
        {dot && <StatusDot variant="accent" label="A new version is available" xstyle={styles.dot} />}
      </span>
    </Popover>
  );
}

function Update(props: { info: About; checking: boolean; onCheck: () => void }) {
  const { info, checking, onCheck } = props;
  const again = <Button size="sm" label={checking ? "Checking…" : "Check again"} isDisabled={checking} onClick={onCheck} />;
  const muted = (text: string) => (
    <Text type="supporting" color="secondary">
      {text}
    </Text>
  );
  if (info.update === "off") return muted("Update checks are off on this server.");
  if (info.update === "dev") return muted("A development build: there is no release to compare it with.");
  if (info.update === "error") {
    return (
      <VStack gap={2} hAlign="start">
        {muted(`Couldn't check for a newer version${info.error ? `: ${info.error}` : ""}.`)}
        {again}
      </VStack>
    );
  }
  if (info.update === "current") {
    return (
      <HStack gap={2} vAlign="center">
        <StateDot state="fresh" label="Up to date" />
        <Text xstyle={styles.grow}>
          Up to date
          {info.checked_at ? (
            <Text type="supporting" color="secondary">
              {" "}
              · checked {ago(info.checked_at)}
            </Text>
          ) : null}
        </Text>
        {again}
      </HStack>
    );
  }
  // A newer release is out.
  return (
    <VStack gap={2}>
      <Text>
        <Text weight="semibold">exuvia {info.latest}</Text> is out.{" "}
        {info.changes_url && (
          <Link isExternalLink href={info.changes_url}>
            What changed
          </Link>
        )}
      </Text>
      {info.upgrade ? (
        <>
          {muted("To update, run this, then start exuvia again:")}
          <Code text={info.upgrade} />
        </>
      ) : (
        <Text type="supporting" color="secondary">
          Whoever runs this server updates it by deploying the new image,{" "}
          <Text type="code" size="xsm" color="inherit">
            {info.image}:{info.latest}
          </Text>
          . Nothing to install on your side.
        </Text>
      )}
    </VStack>
  );
}
