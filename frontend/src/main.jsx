import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { LiveProvider } from "./live";
import "@fontsource/overpass/400.css";
import "@fontsource/overpass/500.css";
import "@fontsource/overpass/600.css";
import "@fontsource/overpass/700.css";
import "@fontsource/barlow-condensed/600.css";
import "@fontsource/barlow-condensed/700.css";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <LiveProvider>
      <App />
    </LiveProvider>
  </StrictMode>
);
