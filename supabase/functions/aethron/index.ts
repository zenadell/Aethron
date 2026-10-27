// Supabase edge entry point. All the logic is in app.ts (plain web APIs),
// so the same code is tested in Bun without the edge runtime.
import { handle } from "./app.ts";

// deno-lint-ignore no-explicit-any
(globalThis as any).Deno.serve((req: Request) => handle(req));
