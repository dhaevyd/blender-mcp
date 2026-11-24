import React from "react";
import ReactDOM from "react-dom/client";
import { ErrorBoundary } from "./common/ErrorBoundary";
import StudioDashboard from "./pages/studio-dashboard";
import StudioLogin from "./pages/studio-login";
import "./index.css";

// Diagnostic: Log any unhandled runtime errors that might cause a black screen
window.addEventListener("error", (event) => {
  console.error("Runtime error caught:", event.error);
});

window.addEventListener("unhandledrejection", (event) => {
  console.error("Unhandled promise rejection:", event.reason);
});

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Failed to find the root element");

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <ErrorBoundary>
      {window.location.pathname.replace(/\/+$/, "") === "/login" ? <StudioLogin /> : <StudioDashboard />}
    </ErrorBoundary>
  </React.StrictMode>,
);
