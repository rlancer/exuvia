import { useEffect, useRef, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Badge } from "@astryxdesign/core/Badge";
import { IconButton } from "@astryxdesign/core/IconButton";
import { SideNav, SideNavItem, SideNavSection } from "@astryxdesign/core/SideNav";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { hubApi, type HubListing } from "./api";
import { IconHome, IconPlus, IconSliders } from "./icons";
import { sortProjects } from "./projectOrder";
import { useMe } from "./useMe";

/** Where on the hub the page is: its home, its admin page (with that page's own sections), or a project's workbench. */
export type HubPlace = { page: "home" } | { page: "admin"; sections?: ReactNode } | { page: "project"; id: string };

export const workbenchUrl = (id: string) => `/p/${encodeURIComponent(id)}/`;

/** Under this width (where the bars give up their words) the nav is always its rail of icons. */
const RAIL_ONLY = "(max-width: 900px)";

/**
 * Whether the nav is folded to its rail, kept in this browser: one choice for
 * the hub's own pages and one for the workbench, which needs its width for the
 * graph and starts folded unless the window is wide.
 */
function useFolded(place: HubPlace): [boolean, (folded: boolean) => void] {
  const key = `exuvia-nav-folded-${place.page === "project" ? "project" : "hub"}`;
  const [folded, setFolded] = useState(() => {
    try {
      const kept = localStorage.getItem(key);
      if (kept !== null) return kept === "1";
    } catch {
      // Blocked storage: the default.
    }
    return place.page === "project" && !window.matchMedia("(min-width: 1500px)").matches;
  });
  const keep = (f: boolean) => {
    setFolded(f);
    try {
      localStorage.setItem(key, f ? "1" : "0");
    } catch {
      // Private windows: the choice lasts as long as the page.
    }
  };
  return [folded, keep];
}

/** The hub's listing for the nav: the one the page has already loaded, or its own `GET /api/projects` (null when this page may not list them). */
function useListing(given: HubListing | null | undefined): HubListing | null {
  const [own, setOwn] = useState<HubListing | null>(null);
  const asked = useRef(false);
  useEffect(() => {
    if (given !== undefined || asked.current) return;
    asked.current = true;
    hubApi
      .projects()
      .then(setOwn)
      .catch(() => setOwn(null));
  }, [given]);
  return given !== undefined ? given : own;
}

/**
 * The hub's left nav, on every page of the hub: its home and (for an admin)
 * its admin page, then every project you can open, the one you used last
 * first. It folds to a rail of icons, where each project is its initials.
 *
 * `listing` is the page's own when it has loaded one (the home and admin
 * pages); left out, the nav asks for it (the workbench), and shows nothing to
 * someone who may not list the hub's projects (a share link, a token not yet
 * entered).
 */
export function HubNav(props: { place: HubPlace; listing?: HubListing | null; onNewProject?: () => void }) {
  const { place } = props;
  const listing = useListing(props.listing);
  const who = useMe();
  const [folded, setFolded] = useFolded(place);
  const railOnly = useMediaQuery(RAIL_ONLY);
  if (!listing) return null;

  // An admin, or the operator (the API token, or a hub nobody signs in to): as the home page decides.
  const isAdmin = !!listing.hub_env?.can_edit || who?.user?.role === "admin";
  const projects = sortProjects(listing.projects, "used");
  const current = place.page === "project" ? place.id : null;

  return (
    <SideNav
      xstyle={styles.nav}
      collapsible={{ isCollapsed: railOnly || folded, onCollapsedChange: setFolded, hasButton: !railOnly }}
    >
      <SideNavSection title="Hub" isHeaderHidden>
        <SideNavItem label="Home" icon={<IconHome />} href="/" isSelected={place.page === "home"} />
        {isAdmin && (
          <SideNavItem label="Admin" icon={<IconSliders />} href="/admin" isSelected={place.page === "admin" && !place.sections}>
            {place.page === "admin" ? place.sections : undefined}
          </SideNavItem>
        )}
      </SideNavSection>
      <SideNavSection
        title="Projects"
        endContent={
          <IconButton
            variant="ghost"
            size="sm"
            label="New project"
            tooltip="New project"
            icon={<IconPlus />}
            {...(props.onNewProject ? { onClick: props.onNewProject } : { href: "/?new" })}
          />
        }
      >
        {projects.map((p) => (
          <SideNavItem
            key={p.id}
            label={p.name || p.id}
            icon={<Avatar name={p.name || p.id} size={20} shape="rounded" tooltip={false} />}
            href={workbenchUrl(p.id)}
            isSelected={p.id === current}
            endContent={p.requests ? <Badge variant="info" label={p.requests} /> : undefined}
          />
        ))}
      </SideNavSection>
    </SideNav>
  );
}

const styles = stylex.create({
  // The nav stands on the bars' surface and scrolls on its own, as tall as the page under the app bar.
  nav: {
    flexShrink: 0,
    height: "100%",
    minHeight: 0,
    backgroundColor: colorVars["--color-background-surface"],
    borderInlineEndWidth: "1px",
    borderInlineEndStyle: "solid",
    borderInlineEndColor: colorVars["--color-border"],
  },
});
