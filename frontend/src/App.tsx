import { useEffect, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  CalendarClock,
  Check,
  ChevronRight,
  Database,
  HardDrive,
  LayoutDashboard,
  LogOut,
  Settings,
  ShieldCheck,
} from "lucide-react";
import { api, ApiError, sessionExpiredEvent } from "./api";
import type { Run, User } from "./types";
import { ErrorBox, Field } from "./components/ui";
import { Overview } from "./pages/Overview";
import { Tasks } from "./pages/Tasks";
import { Databases } from "./pages/Databases";
import { Exports } from "./pages/Exports";
import { History } from "./pages/History";
import { SettingsPage } from "./pages/Settings";

export default function App() {
  const client = useQueryClient();
  const user = useQuery({
    queryKey: ["me"],
    queryFn: () => api<User | null>("/auth/me"),
    retry: false,
  });
  const [exportTarget, setExportTarget] = useState("");
  const [screen, setScreen] = useState("Overview");
  useEffect(() => {
    function expireSession() {
      void client.cancelQueries();
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "me",
      });
      client.setQueryData(["me"], null);
      setScreen("Overview");
      setExportTarget("");
    }
    window.addEventListener(sessionExpiredEvent, expireSession);
    return () => window.removeEventListener(sessionExpiredEvent, expireSession);
  }, [client]);
  if (
    !user.data &&
    user.error &&
    !(user.error instanceof ApiError && user.error.status === 401)
  ) {
    return (
      <div className="login-page">
        <div className="login-card">
          <h2>Unable to connect to OraExport</h2>
          <ErrorBox error={user.error} />
          <button
            className="button primary"
            onClick={() => void user.refetch()}
          >
            Retry connection
          </button>
        </div>
      </div>
    );
  }
  if (user.isPending)
    return <div className="loading">Connecting to OraExport…</div>;
  if (!user.data)
    return (
      <Login onLogin={() => client.invalidateQueries({ queryKey: ["me"] })} />
    );
  const admin = user.data.role === "admin";
  const links = [
    { name: "Overview", icon: LayoutDashboard },
    { name: "Export tasks", icon: CalendarClock },
    { name: "Databases", icon: Database },
    { name: "Exports", icon: HardDrive },
    { name: "Run history", icon: Activity },
    ...(admin ? [{ name: "Settings", icon: Settings }] : []),
  ];
  return (
    <div className="shell">
      <aside className="sidebar">
        <a
          className="brand"
          aria-label="OraExport home"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setScreen("Overview");
          }}
        >
          <span className="brand-mark">
            <Database size={22} />
          </span>
          <span>
            ora<span className="brand-light">export</span>
            <small>EXPORT MANAGEMENT</small>
          </span>
        </a>
        <div className="nav-caption">WORKSPACE</div>
        <nav>
          {links.map(({ name, icon: Icon }) => (
            <button
              key={name}
              className={screen === name ? "selected" : ""}
              onClick={() => setScreen(name)}
            >
              <Icon size={19} />
              {name}
              {screen === name && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="oracle-note">
            <ShieldCheck size={18} />
            <div>
              Oracle Data Pump<small>Managed schema exports</small>
            </div>
          </div>
          <div className="profile">
            <span className="avatar">
              {user.data.username.slice(0, 2).toUpperCase()}
            </span>
            <div>
              {user.data.username}
              <small>{user.data.role}</small>
            </div>
            <button
              className="icon-button"
              aria-label="Log out"
              onClick={async () => {
                await api("/auth/logout", "POST");
                client.clear();
                window.location.reload();
              }}
            >
              <LogOut size={18} />
            </button>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            Workspace <ChevronRight size={14} /> <strong>{screen}</strong>
          </span>
          <div className="topbar-right">
            <span className="environment">
              <span /> Oracle 19c+
            </span>
            <span className="topbar-avatar">
              {user.data.username[0].toUpperCase()}
            </span>
          </div>
        </header>
        <main>
          {screen === "Overview" && (
            <Overview navigate={setScreen} user={user.data} />
          )}{" "}
          {screen === "Export tasks" && <Tasks user={user.data} />}{" "}
          {screen === "Databases" && (
            <Databases
              user={user.data}
              onExports={(id) => {
                setExportTarget(id);
                setScreen("Exports");
              }}
            />
          )}{" "}
          {screen === "Exports" && (
            <Exports user={user.data} initialTargetId={exportTarget} />
          )}
          {screen === "Run history" && <History user={user.data} />}{" "}
          {screen === "Settings" && admin && <SettingsPage />}
        </main>
        <footer>
          OraExport{" "}
          <span>
            Schema export management · All run timestamps shown in your browser
            timezone
          </span>
        </footer>
      </div>
    </div>
  );
}

export function Login({ onLogin }: { onLogin: () => void }) {
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      await api("/auth/login", "POST", {
        username: form.get("username"),
        password: form.get("password"),
      });
      onLogin();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <div className="login-intro">
        <div className="brand">
          <span className="brand-mark">
            <Database />
          </span>
          oraexport
        </div>
        <span className="eyebrow">YOUR EXPORTS, UNDER CONTROL</span>
        <h1>
          A clear view of
          <br />
          every export.
        </h1>
        <p>
          Schedule schema exports, follow running jobs, and keep your Oracle
          exports organized.
        </p>
        <div className="login-decoration">
          <Database size={70} />
          <div className="connection-line" />
          <HardDrive size={70} />
          <Check size={24} />
        </div>
      </div>
      <form className="login-card" onSubmit={submit}>
        <span className="eyebrow">WELCOME BACK</span>
        <h2>Sign in to your workspace</h2>
        <p>Use your OraExport account to continue.</p>
        <ErrorBox error={error} />
        <Field label="Username">
          <input name="username" autoComplete="username" required autoFocus />
        </Field>
        <Field label="Password">
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </Field>
        <button className="button primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
          <ArrowRight size={17} />
        </button>
        <small>
          First setup? Create your administrator with the backend bootstrap
          command.
        </small>
      </form>
    </div>
  );
}
