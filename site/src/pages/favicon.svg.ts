// The favicon: the mark, drawn from src/lib/brand.ts.
import type { APIRoute } from "astro";
import { icon } from "../lib/brand";

export const GET: APIRoute = () => new Response(icon(), { headers: { "Content-Type": "image/svg+xml; charset=utf-8" } });
