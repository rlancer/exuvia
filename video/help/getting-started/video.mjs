/**
 * Getting started: the tutorial in site/src/docs/getting-started.md, step by
 * step, on a project made from nothing in a fresh home folder. Everything on
 * screen is real: the commands run in bash (help/tools/terminal.py), the
 * workbench is `uvx exuvia serve`, and the agent is the built-in one, on
 * OpenRouter.
 */
import fs from "node:fs";
import path from "node:path";

const DOCS = "exuvia.lobster.mp/docs/getting-started";
const APP = "http://127.0.0.1:7420/";

// The view's three files, as the tutorial has them.
const VIEW_JSON = `{
  "title": "Overview",
  "reads": ["squares", "summary"]
}
`;
const APP_TSX = `import { rows, useQuery, useTable } from "exuvia";
import "./styles.css";

interface Square {
  x: number;
  y: number;
}

export default function App() {
  const squares = useTable("squares");
  const totals = useQuery("select n, total from summary");
  const [summary] = rows<{ n: number; total: number }>(totals.data);

  if (squares.error) return <p>{squares.error}</p>;
  return (
    <main>
      <h1>{summary ? \`\${summary.n} rows, total \${summary.total}\` : "Loading"}</h1>
      <ul>
        {rows<Square>(squares.data).map((r) => (
          <li key={r.x} style={{ width: \`\${r.y * 2}rem\` }}>
            {r.x} squared is {r.y}
          </li>
        ))}
      </ul>
    </main>
  );
}
`;
const STYLES_CSS = `main {
  padding: 1.5rem;
  color: var(--text);
  font-family: var(--sans);
}
ul {
  display: grid;
  gap: 0.5rem;
  padding: 0;
  list-style: none;
}
li {
  padding: 0.4rem 0.6rem;
  background: var(--accent);
  color: var(--bg);
  white-space: nowrap;
}
`;
const heredoc = (file, body) => `cat > ${file} <<'EOF'\n${body}EOF\n`;

export default async function film(s, { stage }) {
  const home = stage;
  const project = path.join(home, "my-research");
  const card = await s.card({ kicker: "exuvia tutorial", title: "Getting started", url: DOCS });
  const shell = await s.terminal({ port: 7431, home, title: "~ — exuvia serve" });
  const shell2 = await s.terminal({ port: 7432, home, title: "~" });

  // Where some text sits in a node's file on disk, as [line, column] for the editor.
  const find = (file, text, nth = 0) => {
    const lines = fs.readFileSync(path.join(project, file), "utf8").split("\n");
    let seen = 0;
    for (let i = 0; i < lines.length; i++) {
      const col = lines[i].indexOf(text);
      if (col >= 0 && seen++ === nth) return [i + 1, col];
    }
    throw new Error(`"${text}" is not in ${file}`);
  };

  await s.show(card);
  s.start();
  await s.beat("intro");

  // ---- install, a project, the workbench ---------------------------------
  await s.show(shell.page);
  await s.beat("install", async () => {
    await s.said("put uvx");
    await shell.run("uvx exuvia init my-research");
  });
  await s.beat("init", async () => {
    await s.wait(0.6);
    await shell.run("ls -A my-research");
  });
  await s.beat("hello", async () => {
    await shell.run("cat my-research/nodes/hello.py");
  });
  await s.beat("serve", async () => {
    await shell.run("uvx exuvia serve my-research", { until: /http:\/\/127\.0\.0\.1:7420/ });
  });

  const app = await s.newPage(APP);
  const node = (name) => app.locator(`.react-flow__node[data-id="${name}"]`);
  // The inspector's tabs; Data carries a row count once there is output ("Data 3").
  const tab = (name) => app.getByRole("tablist", { name: /code, data, runs/ }).getByRole("tab", { name: new RegExp(`^${name}`) });
  const button = (name) => app.getByRole("button", { name, exact: true });
  const editor = () => app.locator(".cm-editor").first();
  const save = () => s.shortcut("Control+s", 900);
  // Replaces a span of the open node's code: drag across it, then type.
  const replace = async (file, from, to, text) => {
    await s.selectCode(from, to, editor());
    await s.type(text, { cps: 14 });
  };
  await node("hello").waitFor();
  await s.show(app);

  await s.beat("canvas", async () => {
    await s.hover(node("hello"), 0.4, { dx: 0.3 });
    await s.said("Select it");
    await s.click(node("hello"), { dx: 0.3 });
    await s.said("Then run");
    await s.click(button("Run"));
  });
  await s.beat("output", async () => {
    await node("hello").filter({ hasText: "fresh" }).waitFor();
    await s.click(tab("Data"));
    await s.said("Runs keeps");
    await s.click(tab("Runs"));
  });
  await s.beat("watch", async () => {
    await s.click(tab("Code"));
    await s.hover(node("hello"), 0.5, { dx: 0.7, dy: 0.8 });
  });

  // ---- a Python node that depends on hello, and a SQL node ----------------
  const dialog = app.getByRole("dialog");
  await s.beat("newnode", async () => {
    await s.click(button("New node"));
    await s.click(dialog.getByPlaceholder("rolling_corr"));
    await s.type("squares");
    await s.said("Its input");
    await s.hover(dialog.getByRole("button", { name: "hello", exact: true }), 0.8);
    await s.click(dialog.getByPlaceholder("What this node produces"));
    await s.type("Keep the rows of hello where y is above a threshold.", { cps: 22 });
    await s.click(dialog.getByRole("button", { name: "Create" }));
  });
  await s.beat("param", async () => {
    await editor().waitFor();
    const [line, col] = find("nodes/squares.py", "hello)");
    const def = await s.codeAt(line, col);
    await s.move(def.x + 20, def.y);
    await s.said("Keep the rows");
    const [first] = find("nodes/squares.py", "import pandas");
    const [last] = find("nodes/squares.py", "return");
    await replace("nodes/squares.py", [first, "indent"], [last, "end"], 'return hello[hello["y"] > 1]');
    await s.said("save");
    await save();
  });
  await s.beat("sql", async () => {
    await s.click(button("New node"));
    await s.click(dialog.getByPlaceholder("rolling_corr"));
    await s.type("summary");
    await s.click(dialog.getByText("SQL", { exact: true }));
    await s.click(dialog.getByPlaceholder("What this node produces"));
    await s.type("How many rows survived, and their total.", { cps: 22 });
    await s.click(dialog.getByRole("button", { name: "Create" }));
    await editor().waitFor();
    await s.wait(0.4);
    const [line] = find("nodes/summary.sql", "select");
    await replace("nodes/summary.sql", [line, 0], [line, "end"], "select count(*) as n, sum(y) as total\nfrom squares");
    await save();
  });

  // ---- run it all, then the same from a terminal ---------------------------
  await s.beat("runall", async () => {
    await s.hover(node("hello"), 0.3, { dx: 0.5, dy: 0.2 });
    await s.hover(node("squares"), 0.3, { dx: 0.5, dy: 0.2 });
    await s.hover(node("summary"), 0.3, { dx: 0.5, dy: 0.2 });
    await s.said("Run summary");
    await s.click(button("Run"));
    await node("summary").filter({ hasText: "fresh" }).waitFor({ timeout: 60000 });
  });
  await s.beat("result", async () => {
    await s.click(tab("Data"));
  });
  await s.show(shell2.page);
  await s.beat("cli", async () => {
    await s.said("exuvia run");
    await shell2.run("uvx exuvia run my-research");
  });

  // ---- change something and see what re-runs -------------------------------
  const threshold = async (from, to) => {
    const [line, col] = find("nodes/squares.py", `> ${from}]`);
    await s.selectCode([line, col + 2], [line, col + 3], editor());
    await s.type(String(to));
    await save();
  };
  await s.show(app);
  await s.beat("change", async () => {
    await s.click(node("squares"), { dx: 0.3 });
    await s.click(tab("Code"));
    await s.said("Raise the threshold");
    await threshold(1, 3);
  });
  await s.show(shell2.page);
  await s.beat("status", async () => {
    await shell2.run("uvx exuvia status my-research");
  });
  await s.beat("rerun", async () => {
    await shell2.run("uvx exuvia run my-research");
  });
  await s.show(app);
  await s.beat("four", async () => {
    await threshold(3, 4);
    await s.show(shell2.page);
    await shell2.run("uvx exuvia run my-research");
  });
  await s.show(app);
  await s.beat("history", async () => {
    await s.click(tab("History"));
    await s.wait(0.8);
    await s.hover(app.getByRole("tabpanel").last(), 0.6, { dx: 0.4, dy: 0.15 });
  });

  // ---- a schedule ------------------------------------------------------------
  await s.beat("schedule", async () => {
    await s.click(node("hello"), { dx: 0.3 });
    await s.click(tab("Code"));
    await s.said("Give the decorator");
    const [line, col] = find("nodes/hello.py", "@exuvia.node(");
    await s.clickCode(line, col + "@exuvia.node(".length);
    await s.type('schedule="0 17 * * 1-5', { cps: 12 });
    await s.press("ArrowRight");
    await save();
  });

  // ---- a view ------------------------------------------------------------------
  await s.show(shell2.page);
  await s.beat("view", async () => {
    await shell2.run("clear", { cps: 12 });
    await shell2.run("mkdir -p my-research/views/overview && cd my-research/views/overview");
    await s.said("Let's make one");
    for (const [file, body] of [["view.json", VIEW_JSON], ["App.tsx", APP_TSX], ["styles.css", STYLES_CSS]]) {
      await shell2.paste(heredoc(file, body));
      await s.wait(0.8);
    }
    await shell2.run("ls", { cps: 10 });
    await shell2.run("cd ~");
  });
  await s.show(app);
  await s.beat("viewfiles", async () => {
    await s.click(app.getByRole("tab", { name: "Files" }));
    for (const file of ["view.json", "App.tsx", "styles.css"]) {
      await s.said(file === "view.json" ? "The manifest" : file === "App.tsx" ? "The entry file" : "The stylesheet");
      await s.click(app.getByText(file, { exact: true }).first());
    }
  });
  await s.beat("viewopen", async () => {
    await s.click(app.getByRole("tab", { name: /^Views/ }));
    await s.wait(0.6);
    await s.click(app.getByText("Overview", { exact: true }).first());
    // Opening a view's file from Files showed its code; its View tab draws it.
    await s.click(app.getByRole("tab", { name: "View", exact: true }));
  });
  await s.beat("viewlive", async () => {
    await s.click(app.getByRole("tab", { name: /^Graph/ }));
    await s.click(node("squares"), { dx: 0.3 });
    await s.click(tab("Code"));
    await threshold(4, 1);
    await s.click(app.getByRole("button", { name: /^Run stale/ }));
    await node("summary").filter({ hasText: "fresh" }).waitFor({ timeout: 60000 });
    await s.click(app.getByRole("tab", { name: /^Views/ }));
    await s.wait(1.5);
  });

  // ---- the agent -------------------------------------------------------------------
  await s.show(shell.page);
  await s.beat("agent", async () => {
    await s.said("Give exuvia");
    await shell.interrupt();
    await shell.run('echo "OPENROUTER_API_KEY=sk-or-..." >> my-research/.env');
    await s.said("and restart");
    await shell.run("uvx exuvia serve my-research", { until: /http:\/\/127\.0\.0\.1:7420/ });
  });
  // The address is printed a moment before the server takes connections.
  for (let i = 0; ; i++) {
    try {
      await app.reload();
      break;
    } catch (error) {
      if (i > 40) throw error;
      await new Promise((r) => setTimeout(r, 250));
    }
  }
  await app.getByRole("tab", { name: /^Graph/ }).waitFor(); // it opens where it was: on Views
  await s.show(app);
  const agentBox = app.getByRole("textbox").last();
  await s.beat("ask", async () => {
    await s.click(app.getByRole("tab", { name: /^Graph/ }));
    await s.click(app.getByRole("tab", { name: "Agent" }));
    await s.click(agentBox);
    await s.type("Add a node that ranks the rows of squares by y.", { cps: 20 });
    await s.press("Enter");
  });
  await s.beat("agentwork", async () => {
    await s.fast(4, async () => {
      await node("rank").or(app.locator(".react-flow__node").filter({ hasText: /rank/i })).first().waitFor({ timeout: 300000 });
      await app.getByRole("button", { name: /^Stop/ }).waitFor({ state: "hidden", timeout: 300000 }).catch(() => {});
    });
  });
  await s.beat("agentdone", async () => {
    await s.click(app.locator(".react-flow__node").filter({ hasText: /rank/i }).first(), { dx: 0.3 });
    await s.click(tab("Code"));
  });

  const end = await s.card({ kicker: "exuvia docs", title: "Where to go next", url: "exuvia.lobster.mp/docs" });
  await s.show(end);
  await s.beat("outro");
}
