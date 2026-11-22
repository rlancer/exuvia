import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/inter/wght.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
// Astryx: its reset, its components' prebuilt styles, then the exuvia theme
// (built from theme/exuviaTheme.ts by `bun run theme`). The app's own styles
// are StyleX (`stylex.create`), compiled by vite.config.ts.
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "./theme/exuvia.css";
import "@xyflow/react/dist/style.css";
import App from "./App";
import Hub from "./Hub";
import { Root } from "./Root";

// A hub serves this page at / as its home (exuvia/server.py HUB_HOME_META);
// everywhere else it is a project's workbench.
const hubHome = document.querySelector('meta[name="exuvia-hub"]')?.getAttribute("content") === "home";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root>{hubHome ? <Hub /> : <App />}</Root>
  </StrictMode>,
);
