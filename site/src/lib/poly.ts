// The site's low-poly animals: the lobster, and four of the animals that wore
// a shell like it long before there were lobsters. They are loose sketches of
// real fossils, oldest first, chosen for how the body changes:
//
//   Waptia          Cambrian   shrimp-like, a two-flapped tail, no claws.
//                              (An early relative of the crustaceans: where
//                              exactly it sits is still argued over.)
//   Ceratiocaris    Silurian   a phyllocarid: a big hinged shield over the
//                              front, a three-spined tail.
//   Palaeopalaemon  Devonian   among the first decapods: ten legs, a tail
//                              fan, long feelers, small pincers.
//   Eryma           Jurassic   an early clawed lobster, the claws still slim.
//   Homarus         today      the lobster, one claw heavier than the other.
//
// They are not drawn to one scale (Ceratiocaris was the giant of the five).
//
// There is no model file. `build` makes one mesh from a body plan and a set
// of proportions, so every form has the same triangles in the same order and
// one can be morphed into the next by moving its corners.
//
// Nothing here touches the page until `painter` is given a canvas, so a
// component can also measure an animal while the site is being built.

export type P3 = [number, number, number];
export type Mesh = { v: Float32Array; tris: number[]; edges: number[] };
export type Box = { left: number; right: number; top: number; bottom: number };

/** One animal's proportions, as multiples of the lobster's. */
export interface Stage {
  name: string;
  age: string;
  scale: number;
  carapace: [length: number, width: number, height: number];
  abdomen: [length: number, width: number];
  claw: number;
  /** How stout the claw's palm is. */
  palm: number;
  arm: number;
  /** How much heavier the crusher claw is than the cutter. */
  crusher: number;
  leg: number;
  antenna: number;
  eye: number;
  rostrum: number;
  /** The tail: how far it spreads, and how long its edge, side blades, notches and middle are. */
  tail: [spread: number, edge: number, blade: number, notch: number, middle: number];
}

// Oldest first. The last is the lobster.
export const STAGES: Stage[] = [
  {
    // A short saddle of a shell, a long thin body, a tail of two flaps. No claws.
    name: "Waptia",
    age: "Cambrian",
    scale: 0.74,
    carapace: [0.72, 1.0, 1.3],
    abdomen: [1.55, 0.46],
    claw: 0.08,
    palm: 1,
    arm: 0.3,
    crusher: 0,
    leg: 0.5,
    antenna: 1.2,
    eye: 1.7,
    rostrum: 0.25,
    tail: [1.0, 1.5, 2.0, 1.3, 0.25],
  },
  {
    // Most of the animal is shield. Behind it a tapering body and three spines.
    name: "Ceratiocaris",
    age: "Silurian",
    scale: 0.82,
    carapace: [1.3, 1.4, 1.5],
    abdomen: [0.95, 0.6],
    claw: 0.06,
    palm: 1,
    arm: 0.25,
    crusher: 0,
    leg: 0.4,
    antenna: 0.4,
    eye: 0.9,
    rostrum: 1.2,
    tail: [0.5, 0.2, 2.0, 0.3, 3.0],
  },
  {
    // A shrimp's build: slender, long-legged, long feelers, a tail fan, small pincers.
    name: "Palaeopalaemon",
    age: "Devonian",
    scale: 0.8,
    carapace: [1.0, 0.82, 0.9],
    abdomen: [1.25, 0.82],
    claw: 0.3,
    palm: 0.8,
    arm: 0.8,
    crusher: 0,
    leg: 1.1,
    antenna: 1.45,
    eye: 1.3,
    rostrum: 1.6,
    tail: [1.05, 1.5, 1.6, 1.6, 1.7],
  },
  {
    // A lobster's body with long, slim, even claws.
    name: "Eryma",
    age: "Jurassic",
    scale: 0.88,
    carapace: [1.0, 1.0, 1.0],
    abdomen: [1.0, 0.95],
    claw: 0.74,
    palm: 0.62,
    arm: 1.1,
    crusher: 0,
    leg: 1.0,
    antenna: 1.1,
    eye: 1.1,
    rostrum: 1.0,
    tail: [1.0, 1.5, 1.6, 1.6, 1.7],
  },
  {
    // Heavy, with a crusher and a cutter.
    name: "Homarus",
    age: "today",
    scale: 1,
    carapace: [1, 1, 1],
    abdomen: [1, 1],
    claw: 1,
    palm: 1,
    arm: 1,
    crusher: 0.3,
    leg: 1,
    antenna: 1,
    eye: 1,
    rostrum: 1,
    tail: [1.0, 1.5, 1.6, 1.6, 1.7],
  },
];

/** The animal of that name. */
export function stage(name: string): Stage {
  const found = STAGES.find((s) => s.name === name);
  if (!found) throw new Error(`No animal called ${name}.`);
  return found;
}

// The adult's body from head to tail: where each ring sits and its half
// width and half height. The first four rings are the carapace.
const BODY: P3[] = [
  [4.7, 0.42, 0.34],
  [3.8, 1.0, 0.78],
  [2.4, 1.3, 1.0],
  [1.0, 1.22, 0.95],
  [0.45, 0.98, 0.72],
  [-0.6, 1.04, 0.66],
  [-1.7, 0.95, 0.6],
  [-2.8, 0.84, 0.52],
  [-3.8, 0.72, 0.43],
  [-4.6, 0.56, 0.3],
];
const WAIST = 0.45;
const LEGS = [3.0, 2.35, 1.7, 1.05];

/** Builds one animal. Each is built by the same steps, so they share a topology. */
export function build(s: Stage): Mesh {
  const v: number[] = [];
  const tris: number[] = [];
  const edges: number[] = [];
  const add = (p: P3) => v.push((p[0] - 1.7) * s.scale, p[1] * s.scale, p[2] * s.scale) / 3 - 1;
  const plus = (p: P3, d: P3): P3 => [p[0] + d[0], p[1] + d[1], p[2] + d[2]];

  /** A faceted tube along a path: one ring of `k` corners per point. */
  function tube(path: P3[], radii: [number, number][], k: number, phase = Math.PI / 2): number[][] {
    const rings = path.map((p, i) => {
      const a = path[Math.max(0, i - 1)];
      const b = path[Math.min(path.length - 1, i + 1)];
      const length = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      const [ux, uy] = [-(b[1] - a[1]) / length, (b[0] - a[0]) / length];
      return Array.from({ length: k }, (_, j) => {
        const angle = phase + (j / k) * Math.PI * 2;
        const side = Math.cos(angle) * radii[i][0];
        return add([p[0] + ux * side, p[1] + uy * side, p[2] + Math.sin(angle) * radii[i][1]]);
      });
    });
    for (let i = 0; i < rings.length; i++) {
      for (let j = 0; j < k; j++) {
        const [a, b] = [rings[i][j], rings[i][(j + 1) % k]];
        edges.push(a, b);
        if (i === rings.length - 1) continue;
        const [c, d] = [rings[i + 1][(j + 1) % k], rings[i + 1][j]];
        tris.push(a, b, c, a, c, d);
        edges.push(a, d);
      }
    }
    return rings;
  }
  /** Closes a ring to a point. */
  function cap(ring: number[], tip: P3): number {
    const t = add(tip);
    ring.forEach((a, j) => {
      tris.push(a, ring[(j + 1) % ring.length], t);
      edges.push(a, t);
    });
    return t;
  }

  // ---- body, spike and tail ----
  const [carLength, carWidth, carHeight] = s.carapace;
  const [abLength, abWidth] = s.abdomen;
  const along = (x: number) => WAIST + (x - WAIST) * (x > WAIST ? carLength : abLength);
  const rings = BODY.map(([x, ry, rz], i): [P3, [number, number]] => {
    const front = i < 4 ? 1 : i === 4 ? 0.5 : 0;
    const wide = front * carWidth + (1 - front) * abWidth;
    const tall = front * carHeight + (1 - front) * Math.sqrt(abWidth);
    return [
      [along(x), 0, rz * tall],
      [ry * wide, rz * tall],
    ];
  });
  const body = tube(
    rings.map((r) => r[0]),
    rings.map((r) => r[1]),
    6,
  );
  const head = rings[0][0];
  cap(body[0], [head[0] + 0.9 * s.rostrum, 0, head[2] + 0.12]);
  const tail = rings[rings.length - 1][0];
  const root = cap(body[body.length - 1], [tail[0] - 0.15, 0, tail[2]]);
  // Seven points round the tail's root. Equal reaches make a fan; a long
  // middle and blades with short notches between make spines; a short
  // middle makes two flaps.
  const [spread, edge, blade, notch, middle] = s.tail;
  const reach = [edge, blade, notch, middle, notch, blade, edge];
  const lobes = reach.map((r, j) => {
    const angle = (j / 6 - 0.5) * 2.1 * spread;
    return add([tail[0] - 0.15 - Math.cos(angle) * r, Math.sin(angle) * r, 0.1]);
  });
  for (let j = 0; j < 6; j++) {
    tris.push(root, lobes[j], lobes[j + 1]);
    edges.push(root, lobes[j], lobes[j], lobes[j + 1]);
  }
  edges.push(root, lobes[6]);

  for (const side of [1, -1]) {
    // ---- eye ----
    const eye: P3 = [head[0] - 0.2, side * (rings[0][1][0] + 0.14), head[2] + 0.28];
    const size = 0.17 * s.eye;
    tube([plus(eye, [size, 0, 0]), eye, plus(eye, [-size, 0, 0])], [[0.01, 0.01], [size, size], [0.01, 0.01]], 4);

    // ---- feeler ----
    const base: P3 = [head[0] + 0.1, side * 0.3, head[2] + 0.2];
    const bend = plus(base, [2.4 * s.antenna, side * 1.3 * s.antenna, 0.5]);
    const end = plus(bend, [2.4 * s.antenna, side * 2.3 * s.antenna, -0.5]);
    const feeler = tube([base, bend, end], [[0.09, 0.09], [0.07, 0.07], [0.04, 0.04]], 3);
    cap(feeler[2], plus(end, [0.5 * s.antenna, side * 0.6 * s.antenna, -0.1]));

    // ---- claw: arm, palm with its fixed finger, and the finger that moves ----
    const c = s.claw * (side > 0 ? 1 + s.crusher : 1 - s.crusher * 0.35);
    const thick = 0.45 + 0.55 * s.claw;
    const shoulder: P3 = [along(3.3), side * 0.95 * carWidth, 0.45];
    const elbow = plus(shoulder, [1.2 * s.arm, side * 1.5 * s.arm, 0.1]);
    const wrist = plus(elbow, [1.6 * s.arm, side * -0.1 * s.arm, 0]);
    const palm = plus(wrist, [1.2 * c, side * -0.2 * c, 0]);
    const knuckle = plus(palm, [0.95 * c, side * 0.05 * c, 0]);
    const arm = tube(
      [shoulder, elbow, wrist, palm, knuckle],
      [
        [0.27 * thick, 0.25 * thick],
        [0.31 * thick, 0.27 * thick],
        [0.3 * thick, 0.26 * thick],
        [0.95 * c * s.palm, 0.62 * c * s.palm],
        [0.56 * c * s.palm, 0.4 * c * s.palm],
      ],
      4,
    );
    cap(arm[0], plus(shoulder, [-0.3, side * -0.35, 0]));
    cap(arm[4], plus(knuckle, [1.1 * c, side * 0.3 * c, 0]));
    const hinge = plus(palm, [0.6 * c, side * -0.72 * c * s.palm, 0.05]);
    const nail = plus(hinge, [0.95 * c, side * -0.02 * c, 0]);
    const finger = tube([hinge, nail], [[0.3 * c, 0.25 * c], [0.2 * c, 0.16 * c]], 3);
    cap(finger[0], plus(hinge, [-0.35 * c, side * 0.1 * c, 0]));
    cap(finger[1], plus(nail, [0.55 * c, side * 0.25 * c, 0]));

    // ---- walking legs ----
    LEGS.forEach((x, i) => {
      const hip: P3 = [along(x), side * 0.9 * carWidth, 0.35];
      const knee = plus(hip, [(0.3 - 0.2 * i) * s.leg, side * 1.5 * s.leg, 0.55 * s.leg]);
      const foot = plus(knee, [(-0.1 - 0.12 * i) * s.leg, side * 1.1 * s.leg, -0.85 * s.leg]);
      const leg = tube([hip, knee, foot], [[0.15, 0.15], [0.12, 0.12], [0.06, 0.06]], 3);
      cap(leg[2], plus(foot, [0, side * 0.25 * s.leg, -0.1]));
    });
  }
  return { v: new Float32Array(v), tris, edges };
}

// ---- the view ---------------------------------------------------------------

const TILT = 0.62; // how far the view leans from straight overhead
const LIGHT: P3 = [-0.42, -0.5, 0.76];
const clamp = (t: number) => Math.min(1, Math.max(0, t));

/** How far a model has turned from its resting angle, `life` seconds after it first showed. */
export const sway = (life: number) => 0.22 * Math.sin((life * Math.PI * 2) / 11);
/** The furthest it turns either way. */
export const SWAY = 0.22;

/**
 * Turns a mesh by `yaw` (0 points the head right), leans it to the view and
 * lays it out at `unit` pixels to one model unit: x, y and depth into `out`.
 */
export function project(v: Float32Array, yaw: number, unit: number, cx: number, cy: number, out: Float32Array) {
  const [cos, sin, tc, ts] = [Math.cos(yaw), Math.sin(yaw), Math.cos(TILT), Math.sin(TILT)];
  for (let i = 0; i < v.length; i += 3) {
    const x = v[i] * cos - v[i + 1] * sin;
    const y = v[i] * sin + v[i + 1] * cos;
    out[i] = cx + x * unit;
    out[i + 1] = cy + (y * tc - v[i + 2] * ts) * unit;
    out[i + 2] = (y * ts + v[i + 2] * tc) * unit;
  }
}

/** The room an animal takes on the page, in model units, over every angle in `yaws`. */
export function extent(v: Float32Array, yaws: number[]): Box {
  const out = new Float32Array(v.length);
  const box = { left: Infinity, right: -Infinity, top: Infinity, bottom: -Infinity };
  for (const yaw of yaws) {
    project(v, yaw, 1, 0, 0, out);
    for (let i = 0; i < out.length; i += 3) {
      box.left = Math.min(box.left, out[i]);
      box.right = Math.max(box.right, out[i]);
      box.top = Math.min(box.top, out[i + 1]);
      box.bottom = Math.max(box.bottom, out[i + 1]);
    }
  }
  return box;
}

/** A CSS colour token as red, green and blue, read off the element in `root` marked `data-<name>`. */
export function tone(root: HTMLElement, name: string): number[] {
  const text = getComputedStyle(root.querySelector<HTMLElement>(`[data-${name}]`)!).color;
  return (text.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
}

/**
 * The colour of one lit facet, from `shade` in shadow to `ink` in the light.
 * `a`, `b` and `c` are where its corners start in `screen` (as `project` lays
 * them out).
 */
export function facet(screen: Float32Array, a: number, b: number, c: number, ink: number[], shade: number[]): string {
  const [ux, uy, uz] = [screen[b] - screen[a], screen[b + 1] - screen[a + 1], screen[b + 2] - screen[a + 2]];
  const [wx, wy, wz] = [screen[c] - screen[a], screen[c + 1] - screen[a + 1], screen[c + 2] - screen[a + 2]];
  let [nx, ny, nz] = [uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx];
  const length = Math.hypot(nx, ny, nz) || 1;
  if (nz < 0) [nx, ny, nz] = [-nx, -ny, -nz]; // both sides of a facet take the light
  const lit = clamp((nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]) / length);
  const light = 0.04 + 0.96 * lit;
  const glint = clamp((lit - 0.78) * 1.6);
  const channel = (i: number) => Math.round((shade[i] + (ink[i] - shade[i]) * light) * (1 - glint) + 255 * glint);
  return `rgb(${channel(0)},${channel(1)},${channel(2)})`;
}

/** Draws meshes of one topology on a canvas: `place` one, then paint it `solid` or as a `shell`. */
export function painter(ctx: CanvasRenderingContext2D, { v, tris, edges }: Mesh) {
  const screen = new Float32Array(v.length);
  const order = Array.from({ length: tris.length / 3 }, (_, i) => i);
  const depth = new Float32Array(order.length);

  return {
    place(mesh: Float32Array, yaw: number, unit: number, cx: number, cy: number) {
      project(mesh, yaw, unit, cx, cy, screen);
    },

    /** Lit facets, from `shade` in shadow to `ink` in the light. */
    solid(ink: number[], shade: number[]) {
      for (let f = 0; f < order.length; f++) {
        const [a, b, c] = [tris[f * 3] * 3, tris[f * 3 + 1] * 3, tris[f * 3 + 2] * 3];
        depth[f] = screen[a + 2] + screen[b + 2] + screen[c + 2];
      }
      order.sort((p, q) => depth[p] - depth[q]);
      ctx.lineWidth = 0.75;
      ctx.lineJoin = "round";
      for (const f of order) {
        const [a, b, c] = [tris[f * 3] * 3, tris[f * 3 + 1] * 3, tris[f * 3 + 2] * 3];
        ctx.fillStyle = ctx.strokeStyle = facet(screen, a, b, c, ink, shade);
        ctx.beginPath();
        ctx.moveTo(screen[a], screen[a + 1]);
        ctx.lineTo(screen[b], screen[b + 1]);
        ctx.lineTo(screen[c], screen[c + 1]);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
    },

    /** The edges alone: an empty shell. */
    shell(colour: string, alpha: number) {
      if (alpha <= 0) return;
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = colour;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let e = 0; e < edges.length; e += 2) {
        ctx.moveTo(screen[edges[e] * 3], screen[edges[e] * 3 + 1]);
        ctx.lineTo(screen[edges[e + 1] * 3], screen[edges[e + 1] * 3 + 1]);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    },
  };
}
