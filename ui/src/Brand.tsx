import type { ReactNode } from "react";
import { MARK } from "./mark";

/** The exuvia mark. With `flat` it is one shape in the colour of the text around it. */
export function Mark({ size = 20, flat = false }: { size?: number; flat?: boolean }) {
  return (
    <svg className="mark" width={size} height={size} viewBox={`0 0 ${MARK.box} ${MARK.box}`} aria-hidden focusable="false">
      {flat ? (
        <path d={MARK.outline} fill="currentColor" />
      ) : (
        <g strokeWidth="0.6" strokeLinejoin="round">
          {MARK.facets.map(([d, tone]) => (
            <path key={d} d={d} fill={tone} stroke={tone} />
          ))}
        </g>
      )}
    </svg>
  );
}

/**
 * The top bar's lockup: the mark, the name, and where you are. `home` makes
 * the name a link back to the hub's list of projects.
 */
export function Brand({ home, children }: { home?: string; children?: ReactNode }) {
  const name = (
    <>
      <Mark />
      <span className="wordmark">exuvia</span>
    </>
  );
  return (
    <div className="brand">
      {home ? (
        <a className="brand-name" href={home} title="All projects">{name}</a>
      ) : (
        <span className="brand-name">{name}</span>
      )}
      {children != null && (
        <span className="proj">
          <span className="sep" aria-hidden>/</span>
          <span className="proj-name">{children}</span>
        </span>
      )}
    </div>
  );
}
