import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Self-hosted faces, bundled with the app: an install on a LAN with no internet still gets them.
import "@fontsource-variable/source-sans-3";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";

import App from "./App";
import "./index.css";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root is missing from index.html");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
