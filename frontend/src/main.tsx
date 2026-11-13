import { Auth0Provider } from "@auth0/auth0-react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ApiError } from "./api/client";
import "./index.css";

const env = import.meta.env;
const missing = ["VITE_AUTH0_DOMAIN", "VITE_AUTH0_CLIENT_ID", "VITE_AUTH0_AUDIENCE"].filter((k) => !env[k]);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Don't retry authorization or validation failures; they won't fix themselves.
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

const root = createRoot(document.getElementById("root")!);

if (missing.length) {
  root.render(
    <div className="p-8 font-mono text-sm">
      Missing configuration: {missing.join(", ")}. Copy frontend/.env.example to frontend/.env.local.
    </div>,
  );
} else {
  root.render(
    <StrictMode>
      <Auth0Provider
        domain={env.VITE_AUTH0_DOMAIN}
        clientId={env.VITE_AUTH0_CLIENT_ID}
        authorizationParams={{ redirect_uri: window.location.origin, audience: env.VITE_AUTH0_AUDIENCE }}
        // Rotating refresh tokens, kept in memory, so no long-lived token sits in browser storage.
        useRefreshTokens
        useRefreshTokensFallback
      >
        <QueryClientProvider client={queryClient}>
          <App />
        </QueryClientProvider>
      </Auth0Provider>
    </StrictMode>,
  );
}
