// The favicon: the app icon, drawn from the mark in src/lib/brand.ts.
import type { APIRoute } from "astro";
import { icon } from "../lib/brand";

export const GET: APIRoute = () => new Response(icon(), { headers: { "Content-Type": "image/svg+xml; charset=utf-8" } });
