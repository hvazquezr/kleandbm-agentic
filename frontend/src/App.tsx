import { useAuth0 } from "@auth0/auth0-react";
import { LogIn, LogOut } from "lucide-react";
import { useEffect, useState } from "react";
import { Toaster } from "sonner";
import { Link, Route, Switch } from "wouter";
import { setTokenGetter } from "./api/client";
import { Button } from "./components/ui/button";
import { TooltipProvider } from "./components/ui/tooltip";
import Home from "./pages/Home";
import ModelEditor from "./pages/ModelEditor";
import ProjectPage from "./pages/ProjectPage";

export default function App() {
  const { isLoading, isAuthenticated, error, loginWithRedirect, getAccessTokenSilently } = useAuth0();
  const [tokenReady, setTokenReady] = useState(false);

  useEffect(() => {
    if (isAuthenticated) {
      setTokenGetter(async () => {
        const token = await getAccessTokenSilently();
        if (!token) throw new Error("no access token");
        return token;
      });
      setTokenReady(true);
    }
  }, [isAuthenticated, getAccessTokenSilently]);

  if (isLoading) return <Centered>Loading…</Centered>;
  if (error) return <Centered>Sign-in failed: {error.message}</Centered>;
  if (!isAuthenticated) {
    return (
      <Centered>
        <img src="/kleandbm-logo.svg" alt="" className="h-12 mb-4" />
        <p className="text-muted-foreground mb-6">Analytical data model design</p>
        <Button onClick={() => loginWithRedirect()}>
          <LogIn className="size-4" /> Sign in
        </Button>
      </Centered>
    );
  }
  if (!tokenReady) return <Centered>Loading…</Centered>;

  return (
    <TooltipProvider>
      <Toaster theme="dark" richColors position="bottom-right" />
      <Switch>
        <Route path="/t/:tenantId/p/:projectId/m/:modelId" component={ModelEditor} />
        <Route path="/t/:tenantId/p/:projectId">
          <Shell>
            <ProjectPage />
          </Shell>
        </Route>
        <Route>
          <Shell>
            <Home />
          </Shell>
        </Route>
      </Switch>
    </TooltipProvider>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth0();
  return (
    <div className="min-h-screen">
      <header className="border-b px-6 py-3 flex items-center gap-3">
        <Link href="/" className="flex items-center gap-2 font-semibold">
          <img src="/kleandbm-logo.svg" alt="KleanDBM" className="h-6" />
        </Link>
        <div className="ml-auto flex items-center gap-3 text-sm text-muted-foreground">
          {user?.name ?? user?.email}
          <Button variant="ghost" size="sm" onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}>
            <LogOut className="size-4" /> Sign out
          </Button>
        </div>
      </header>
      <main className="container py-8">{children}</main>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen flex flex-col items-center justify-center">{children}</div>;
}
