import * as React from "react";
import * as ReactJSXRuntime from "react/jsx-runtime";
import * as ReactDOMClient from "react-dom/client";
import * as UI from "./sdk";

// The shared runtime handed to dynamically-imported plugin modules. The static
// shim files under /assets/sdk/*.js read these off window so that plugins and
// the host resolve react / react-dom / @roxy/dashboard-ui to one instance.
export interface RoxyRuntime {
  React: typeof React;
  ReactJSXRuntime: typeof ReactJSXRuntime;
  ReactDOMClient: typeof ReactDOMClient;
  UI: typeof UI;
}

declare global {
  interface Window {
    __roxyRuntime?: RoxyRuntime;
    __akashicRuntime?: RoxyRuntime;
  }
}

// Publish the runtime before any plugin is imported.
export function exposeRuntime(): void {
  const runtime = { React, ReactJSXRuntime, ReactDOMClient, UI };
  window.__roxyRuntime = runtime;
  window.__akashicRuntime = runtime;
}
