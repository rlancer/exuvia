import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/inter/wght.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import App from "./App";
import Hub from "./Hub";

// A hub serves this page at / as its home (exuvia/server.py HUB_HOME_META);
// everywhere else it is a project's workbench.
const hubHome = document.querySelector('meta[name="exuvia-hub"]')?.getAttribute("content") === "home";

createRoot(document.getElementById("root")!).render(<StrictMode>{hubHome ? <Hub /> : <App />}</StrictMode>);
