import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { Recover, startRecovery } from "./recovery";
import "./styles.css";

startRecovery();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Recover>
      <App />
    </Recover>
  </React.StrictMode>,
);
