import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { Recover, startRecovery } from "./recovery";
import { initTheme } from "./theme";
import "./styles.css";

initTheme(); // before the first paint, so it never flashes the wrong theme
startRecovery();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Recover>
      <App />
    </Recover>
  </React.StrictMode>,
);
