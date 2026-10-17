import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import App from "../../frontend/src/App";
import { Databases, ProvisionForm } from "../../frontend/src/pages/Databases";
import { Tasks, TaskEditor } from "../../frontend/src/pages/Tasks";
import { RunTable } from "../../frontend/src/components/ui";
import { defaultTask } from "../../frontend/src/constants";
import { api } from "../../frontend/src/api";
import type { Run, Target, User } from "../../frontend/src/types";
vi.mock("../../frontend/src/api", async () => ({
  ...(await vi.importActual("../../frontend/src/api")),
  api: vi.fn(),
}));
const mockApi = vi.mocked(api);
const admin: User = {
  id: "admin",
  username: "admin",
  role: "admin",
  enabled: true,
};
const registration = {
  id: "db",
  name: "Test database",
  dsn: "db/service",
  kind: "unknown",
  version: null,
};
const target: Target = {
  id: "target",
  registration_id: "db",
  name: "Finance PDB",
  dsn: "db/service",
  directory: "EXPORT_DIR",
  tablespace: "USERS",
  provisioned: true,
  edition: null,
  container_name: "FINANCE",
};
const catalog = {
  schemas: ["FINANCE"],
  directories: [{ name: "EXPORT_DIR", path: "/exports" }],
  tablespaces: ["USERS"],
};
const run = {
  id: "run",
  task_id: "task",
  target_id: "target",
  state: "running",
  oracle_job: "OE_TEST",
  created_at: "2026-10-09T12:00:00Z",
  started_at: null,
  finished_at: null,
  progress: 40,
  errors: [],
  messages: [],
  files: [],
  size_bytes: null,
  duration_seconds: null,
  cancel_requested: false,
  snapshot: {
    ...defaultTask,
    name: "Finance nightly",
    schema_name: "FINANCE",
    target_name: "Finance PDB",
  },
} satisfies Run;
function mount(element: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      {element}
    </QueryClientProvider>,
  );
}

describe("discovered PDB targets", () => {
  it("prefills a discovered PDB, selects its service, and verifies before saving", async () => {
    mockApi.mockClear();
    let saved = false;
    let fail = true;
    mockApi.mockImplementation(async (path, method) => {
      if (path.endsWith("/discover"))
        return {
          kind: "cdb",
          version: "19c",
          pdbs: [
            {
              name: "FINANCE",
              open_mode: "READ WRITE",
              services: [
                { name: "finance.example", dsn: "db:1521/finance.example" },
                { name: "reports.example", dsn: "db:1521/reports.example" },
              ],
            },
          ],
        } as never;
      if (path === "/targets" && method === "POST") {
        if (fail) throw new Error("The service connects to a different PDB");
        saved = true;
        return target as never;
      }
      return (
        path === "/registrations" ? [registration] : saved ? [target] : []
      ) as never;
    });
    mount(<Databases user={admin} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Test & discover" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Add target for FINANCE" }),
    );
    expect(screen.getByLabelText("Database registration")).toHaveValue("db");
    expect(screen.getByLabelText("Display name")).toHaveValue(
      "Test database · FINANCE",
    );
    expect(screen.getByLabelText(/Service DSN/)).toHaveValue(
      "db:1521/finance.example",
    );
    expect(screen.queryByLabelText("DBA password")).not.toBeInTheDocument();
    await user.selectOptions(
      screen.getByLabelText(/Discovered service/),
      "db:1521/reports.example",
    );
    expect(screen.getByLabelText(/Service DSN/)).toHaveValue(
      "db:1521/reports.example",
    );
    await user.click(
      screen.getByRole("button", { name: "Verify & add target" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("different PDB");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mockApi).toHaveBeenCalledWith("/targets", "POST", {
      name: "Test database · FINANCE",
      dsn: "db:1521/reports.example",
      registration_id: "db",
      pdb_name: "FINANCE",
    });
    fail = false;
    await user.click(
      screen.getByRole("button", { name: "Verify & add target" }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await screen.findByText("Finance PDB");
  });

  it("prefills name and registration when no service DSN is available", async () => {
    mockApi.mockClear();
    mockApi.mockImplementation(async (path) => {
      if (path.endsWith("/discover"))
        return {
          kind: "cdb",
          version: "19c",
          pdbs: [{ name: "HR", open_mode: "MOUNTED", services: [] }],
        } as never;
      return (path === "/registrations" ? [registration] : []) as never;
    });
    mount(<Databases user={admin} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Test & discover" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Add target for HR" }),
    );
    expect(screen.getByLabelText("Database registration")).toHaveValue("db");
    expect(screen.getByLabelText("Display name")).toHaveValue(
      "Test database · HR",
    );
    expect(screen.getByLabelText(/Service DSN/)).toHaveValue("");
    expect(
      screen.getByText(/No service DSN could be suggested/),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Service DSN/), "other:1522/hr");
    await user.click(
      screen.getByRole("button", { name: "Verify & add target" }),
    );
    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith("/targets", "POST", {
        name: "Test database · HR",
        dsn: "other:1522/hr",
        registration_id: "db",
        pdb_name: "HR",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Add target" }));
    expect(
      screen.queryByLabelText(/Discovered service/),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Service DSN/)).toHaveValue("");
    expect(screen.getByLabelText("Display name")).toHaveValue("");
  });
});

describe("registration editing", () => {
  it("prefills details, cancels, and saves without replacing credentials", async () => {
    mockApi.mockClear();
    let current = { ...registration };
    mockApi.mockImplementation(async (path, method, body) => {
      if (path === "/registrations/db" && method === "PUT") {
        current = { ...current, ...(body as object) };
        return current as never;
      }
      return (path === "/registrations" ? [current] : [target]) as never;
    });
    mount(<Databases user={admin} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Edit database Test database",
      }),
    );
    expect(screen.getByLabelText("Display name")).toHaveValue(
      registration.name,
    );
    expect(screen.getByLabelText(/Service DSN/)).toHaveValue(registration.dsn);
    expect(screen.queryByLabelText("DBA password")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockApi).not.toHaveBeenCalledWith(
      "/registrations/db",
      "PUT",
      expect.anything(),
    );
    await user.click(
      screen.getByRole("button", { name: "Edit database Test database" }),
    );
    await user.clear(screen.getByLabelText("Display name"));
    await user.type(screen.getByLabelText("Display name"), "Updated database");
    await user.clear(screen.getByLabelText(/Service DSN/));
    await user.type(screen.getByLabelText(/Service DSN/), "new/service");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith("/registrations/db", "PUT", {
        name: "Updated database",
        dsn: "new/service",
      }),
    );
    await screen.findByRole("button", {
      name: "Edit database Updated database",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("replaces credentials only when selected and retains the form on failure", async () => {
    mockApi.mockClear();
    mockApi.mockImplementation(async (path, method) => {
      if (method === "PUT") throw new Error("Could not save registration");
      return (path === "/registrations" ? [registration] : [target]) as never;
    });
    mount(<Databases user={admin} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Edit database Test database",
      }),
    );
    await user.click(screen.getByLabelText(/Replace DBA credentials/));
    expect(screen.getByLabelText("DBA password")).toHaveValue("");
    await user.type(screen.getByLabelText("DBA username"), "SYS");
    await user.type(screen.getByLabelText("DBA password"), "replacement");
    await user.click(screen.getByLabelText("Connect as SYSDBA"));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save registration",
    );
    expect(mockApi).toHaveBeenCalledWith("/registrations/db", "PUT", {
      name: registration.name,
      dsn: registration.dsn,
      username: "SYS",
      password: "replacement",
      sysdba: true,
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("DBA username")).toHaveValue("SYS");
    await user.click(screen.getByLabelText(/Replace DBA credentials/));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(mockApi).toHaveBeenLastCalledWith("/registrations/db", "PUT", {
        name: registration.name,
        dsn: registration.dsn,
      }),
    );
  });

  it.each(["viewer", "operator"] as const)(
    "hides editing for %s",
    async (role) => {
      mockApi.mockImplementation(
        async (path) =>
          (path === "/registrations" ? [registration] : [target]) as never,
      );
      mount(<Databases user={{ ...admin, role }} />);
      await screen.findByText("Test database");
      expect(
        screen.queryByRole("button", { name: /Edit database/ }),
      ).not.toBeInTheDocument();
    },
  );
});

describe("connection removal", () => {
  it.each([
    ["Remove database Test database", "/registrations/db", "registrations"],
    ["Remove target Finance PDB", "/targets/target", "targets"],
  ])("confirms and removes %s", async (label, endpoint, list) => {
    mockApi.mockClear();
    let removed = false;
    mockApi.mockImplementation(async (path, method) => {
      if (path === endpoint && method === "DELETE") {
        removed = true;
        return { ok: true } as never;
      }
      if (path === "/registrations")
        return (
          removed && list === "registrations" ? [] : [registration]
        ) as never;
      if (path === "/targets")
        return (removed && list === "targets" ? [] : [target]) as never;
      return {} as never;
    });
    mount(<Databases user={admin} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: label }));
    expect(mockApi).not.toHaveBeenCalledWith(endpoint, "DELETE");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockApi).not.toHaveBeenCalledWith(endpoint, "DELETE");
    await user.click(screen.getByRole("button", { name: label }));
    await user.click(screen.getByRole("button", { name: "Remove connection" }));
    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith(endpoint, "DELETE"),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: label }),
      ).not.toBeInTheDocument(),
    );
  });

  it("shows dependency failures in the removal dialog", async () => {
    mockApi.mockImplementation(async (path, method) => {
      if (method === "DELETE")
        throw new Error("Remove this database's export targets first");
      return (path === "/registrations" ? [registration] : [target]) as never;
    });
    mount(<Databases user={admin} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Remove database Test database",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Remove connection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "export targets first",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove database Test database" }),
    ).toBeInTheDocument();
  });

  it.each(["viewer", "operator"] as const)(
    "hides removal controls for %s",
    async (role) => {
      mockApi.mockImplementation(
        async (path) =>
          (path === "/registrations" ? [registration] : [target]) as never,
      );
      mount(<Databases user={{ ...admin, role }} />);
      await screen.findByText("Test database");
      expect(
        screen.queryByRole("button", { name: /Remove/ }),
      ).not.toBeInTheDocument();
    },
  );
});

describe("export scope", () => {
  it("searches and selects multiple schemas without losing hidden selections", async () => {
    mockApi.mockClear();
    mockApi.mockImplementation(
      async () =>
        ({ ...catalog, schemas: ["HR", "FINANCE", "ARCHIVE"] }) as never,
    );
    const saved = vi.fn();
    mount(<TaskEditor targets={[target]} close={vi.fn()} saved={saved} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Task name"), "Selected schemas");
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "target",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Schemas" })).toBeEnabled(),
    );
    expect(screen.getByRole("button", { name: "Save task" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Schemas" }));
    await user.type(screen.getByLabelText("Search schemas"), "hr");
    await user.click(screen.getByLabelText("HR"));
    expect(screen.queryByLabelText("FINANCE")).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search schemas"));
    await user.type(screen.getByLabelText("Search schemas"), "finance");
    await user.click(screen.getByRole("button", { name: "Select matching" }));
    expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
      "HR, FINANCE",
    );
    await user.clear(screen.getByLabelText("Search schemas"));
    await user.type(screen.getByLabelText("Search schemas"), "unknown");
    expect(
      screen.getByText("No schemas match your search."),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByLabelText("Search schemas")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Schemas" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Save task" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(mockApi).toHaveBeenCalledWith(
      "/tasks",
      "POST",
      expect.objectContaining({
        export_mode: "SCHEMA",
        schema_names: ["HR", "FINANCE"],
        schema_name: "HR",
      }),
    );
  });

  it("creates a full export without selecting a schema", async () => {
    mockApi.mockClear();
    mockApi.mockImplementation(
      async () => ({ ...catalog, schemas: [] }) as never,
    );
    const saved = vi.fn();
    mount(<TaskEditor targets={[target]} close={vi.fn()} saved={saved} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Task name"), "Full PDB export");
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "target",
    );
    await user.selectOptions(screen.getByLabelText("Export scope"), "FULL");
    expect(
      screen.queryByRole("button", { name: "Schemas" }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save task" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Save task" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(mockApi).toHaveBeenCalledWith(
      "/tasks",
      "POST",
      expect.objectContaining({
        export_mode: "FULL",
        schema_names: [],
        schema_name: "",
      }),
    );
  });

  it("loads an existing multi-schema task and edits its scope, retaining errors for correction", async () => {
    mockApi.mockClear();
    mockApi.mockImplementation(async (path, method) => {
      if (method === "PUT")
        throw new Error("Export account lacks required privileges");
      return { ...catalog, schemas: ["HR", "FINANCE"] } as never;
    });
    mount(
      <TaskEditor
        task={{
          ...defaultTask,
          id: "task",
          name: "Multi export",
          target_id: "target",
          schema_name: "HR",
          schema_names: ["HR", "FINANCE"],
          directory: "EXPORT_DIR",
          next_run_at: null,
        }}
        targets={[target]}
        close={vi.fn()}
        saved={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Schemas" })).toBeEnabled(),
    );
    expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
      "HR, FINANCE",
    );
    await user.click(screen.getByRole("button", { name: "Schemas" }));
    expect(screen.getByLabelText("HR")).toBeChecked();
    expect(screen.getByLabelText("FINANCE")).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.getByRole("button", { name: "Save task" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Done" }));
    await user.selectOptions(screen.getByLabelText("Export scope"), "FULL");
    await user.selectOptions(screen.getByLabelText("Content"), "METADATA_ONLY");
    await user.click(screen.getByRole("button", { name: "Save task" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "required privileges",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("Export scope")).toHaveValue("FULL");
    expect(mockApi).toHaveBeenCalledWith(
      "/tasks/task",
      "PUT",
      expect.objectContaining({
        export_mode: "FULL",
        schema_names: [],
        schema_name: "",
        content: "METADATA_ONLY",
      }),
    );
    await user.selectOptions(screen.getByLabelText("Export scope"), "SCHEMA");
    expect(screen.getByRole("button", { name: "Save task" })).toBeDisabled();
  });
});

describe("export workflows", () => {
  it("creates a task from discovered schema and directory", async () => {
    mockApi.mockImplementation(async (path) =>
      path.includes("/catalog") ? catalog : ({} as never),
    );
    const saved = vi.fn();
    mount(<TaskEditor targets={[target]} close={vi.fn()} saved={saved} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Task name"), "Finance nightly");
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "target",
    );
    await screen.findByRole("button", { name: "Schemas" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
        "FINANCE",
      ),
    );
    expect(screen.getByLabelText("Oracle DIRECTORY")).toHaveValue("EXPORT_DIR");
    await user.click(screen.getByRole("button", { name: "Save task" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(mockApi).toHaveBeenCalledWith(
      "/tasks",
      "POST",
      expect.objectContaining({
        name: "Finance nightly",
        schema_name: "FINANCE",
        directory: "EXPORT_DIR",
        parallel: 1,
        retention_days: 30,
      }),
    );
  });
  it("lists registered targets before provisioning and explains missing export targets", async () => {
    mockApi.mockClear();
    mockApi.mockImplementation(async (path) => {
      if (path === "/registrations")
        return [
          registration,
          { ...registration, id: "other-db", name: "Other database" },
        ] as never;
      if (path === "/targets")
        return [{ ...target, provisioned: false }] as never;
      if (path.endsWith("/catalog")) return catalog as never;
      return [] as never;
    });
    mount(<Tasks user={admin} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New export task" }));
    await screen.findByRole("option", {
      name: "Finance PDB · needs provisioning",
    });
    await waitFor(() =>
      expect(screen.getByLabelText("Database target")).toBeEnabled(),
    );
    expect(
      screen.getByRole("option", {
        name: "Other database · add export target",
      }),
    ).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "target",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
        "FINANCE",
      ),
    );
    expect(screen.getByLabelText("Oracle DIRECTORY")).toHaveValue("EXPORT_DIR");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Provision this target",
    );
    expect(screen.getByRole("button", { name: "Save task" })).toBeDisabled();
    expect(mockApi).toHaveBeenCalledWith("/targets/target/catalog");
  });
  it("refreshes schema and directory choices when switching targets and keeps multiple schemas unselected", async () => {
    mockApi.mockImplementation(async (path) => {
      if (path === "/targets/second/catalog")
        return {
          ...catalog,
          schemas: ["HR", "PAYROLL"],
          directories: [
            { name: "HR_DIR", path: "/hr" },
            { name: "DATA_DIR", path: "/data" },
          ],
        } as never;
      return catalog as never;
    });
    mount(
      <TaskEditor
        targets={[
          target,
          { ...target, id: "second", name: "HR PDB", directory: "HR_DIR" },
        ]}
        close={vi.fn()}
        saved={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "target",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
        "FINANCE",
      ),
    );
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "second",
    );
    await user.click(screen.getByRole("button", { name: "Schemas" }));
    await screen.findByLabelText("PAYROLL");
    expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
      "Select schemas",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Oracle DIRECTORY")).toHaveValue("HR_DIR"),
    );
    expect(screen.queryByLabelText("FINANCE")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("PAYROLL"));
    await user.selectOptions(
      screen.getByLabelText("Database target"),
      "target",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Schemas" })).toHaveTextContent(
        "FINANCE",
      ),
    );
    expect(screen.getByLabelText("Oracle DIRECTORY")).toHaveValue("EXPORT_DIR");
  });
  it("edits a task with pause and export option controls", async () => {
    mockApi.mockImplementation(async (path) =>
      path.includes("/catalog") ? catalog : ({} as never),
    );
    mount(
      <TaskEditor
        task={{
          ...defaultTask,
          id: "task",
          name: "Finance nightly",
          target_id: "target",
          schema_name: "FINANCE",
          directory: "EXPORT_DIR",
          next_run_at: null,
        }}
        targets={[target]}
        close={vi.fn()}
        saved={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Enable scheduled exports"));
    await user.click(screen.getByRole("button", { name: "Save task" }));
    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith(
        "/tasks/task",
        "PUT",
        expect.objectContaining({ enabled: false }),
      ),
    );
  });
  it("requires SQL preview before applying provisioning", async () => {
    mockApi.mockImplementation(async (path) =>
      path.includes("/catalog")
        ? catalog
        : path.endsWith("/preview")
          ? { sql: ["GRANT CREATE SESSION TO ORAEXPORT;"], note: "Reviewed" }
          : ({} as never),
    );
    mount(<ProvisionForm target={target} close={vi.fn()} saved={vi.fn()} />);
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "USERS" });
    expect(
      screen.queryByRole("button", { name: "Apply provisioning" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Preview SQL" }));
    await screen.findByText("GRANT CREATE SESSION TO ORAEXPORT;");
    await user.click(
      screen.getByRole("button", { name: "Apply provisioning" }),
    );
    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith(
        "/targets/target/provision/apply",
        "POST",
        expect.objectContaining({ directory: "EXPORT_DIR" }),
      ),
    );
  });
  it("shows actual size unavailable and current running state", () => {
    mount(<RunTable runs={[run]} onSelect={vi.fn()} />);
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });
  it("hides administration and mutation controls for viewers", async () => {
    mockApi.mockImplementation(async (path) => {
      if (path === "/auth/me")
        return {
          id: "viewer",
          username: "reader",
          role: "viewer",
          enabled: true,
        } satisfies User as never;
      if (path === "/reports/summary")
        return { counts: {}, retained_bytes: 0, tasks: 1, targets: 1 } as never;
      if (path === "/tasks" || path === "/targets") return [] as never;
      return { items: [], total: 0 } as never;
    });
    mount(<App />);
    await screen.findByText("Everything in view.");
    expect(
      screen.queryByRole("button", { name: "Settings" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Manage export tasks/ }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Export tasks" }));
    expect(
      screen.queryByRole("button", { name: "New export task" }),
    ).not.toBeInTheDocument();
  });
  it("filters run history via API and displays matching runs", async () => {
    mockApi.mockImplementation(async (path) => {
      if (path === "/auth/me")
        return {
          id: "viewer",
          username: "reader",
          role: "viewer",
          enabled: true,
        } as never;
      if (path === "/reports/summary")
        return { counts: {}, retained_bytes: 0, tasks: 1, targets: 1 } as never;
      if (path === "/tasks" || path === "/targets") return [] as never;
      return { items: [run], total: 1 } as never;
    });
    mount(<App />);
    await screen.findByText("Everything in view.");
    await userEvent.click(screen.getByRole("button", { name: "Run history" }));
    await userEvent.selectOptions(
      screen.getByLabelText("Filter by status"),
      "running",
    );
    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith("/runs?state=running&page=1"),
    );
    expect(
      screen.getByRole("button", { name: "Finance nightly" }),
    ).toBeInTheDocument();
  });
});

it("updates visible progress when live job data is refreshed", async () => {
  let progress = 25;
  mockApi.mockImplementation(async (path) => {
    if (path === "/auth/me")
      return {
        id: "viewer",
        username: "reader",
        role: "viewer",
        enabled: true,
      } as never;
    if (path === "/reports/summary")
      return { counts: {}, retained_bytes: 0, tasks: 1, targets: 1 } as never;
    return { items: [{ ...run, progress }], total: 1 } as never;
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
  await screen.findByText("25% estimated data progress");
  progress = 75;
  await client.invalidateQueries({ queryKey: ["active-runs"] });
  await screen.findByText("75% estimated data progress");
});

it("clears authenticated data and returns to login when a protected request expires", async () => {
  const { sessionExpiredEvent } = await import("../../frontend/src/api");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(["me"], admin);
  client.setQueryData(["private-settings"], { secret: "cached" });
  mockApi.mockImplementation(async (path) => {
    if (path === "/auth/me") return admin as never;
    if (path === "/reports/summary")
      return { counts: {}, retained_bytes: 0, tasks: 0, targets: 0 } as never;
    return { items: [], total: 0 } as never;
  });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
  await screen.findByText("Everything in view.");
  const { act } = await import("@testing-library/react");
  act(() => window.dispatchEvent(new Event(sessionExpiredEvent)));
  await screen.findByRole("heading", { name: "Sign in to your workspace" });
  expect(client.getQueryData(["private-settings"])).toBeUndefined();
  expect(client.getQueryData(["me"])).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Settings" }),
  ).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Username"), "admin");
  await userEvent.type(screen.getByLabelText("Password"), "password");
  await userEvent.click(screen.getByRole("button", { name: /Sign in/ }));
  await screen.findByText("Everything in view.");
});

it("offers retry for an initial server error instead of showing login", async () => {
  const { ApiError } = await import("../../frontend/src/api");
  mockApi.mockRejectedValue(new ApiError(503, "Metadata database unavailable"));
  mount(<App />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Metadata database unavailable",
  );
  expect(
    screen.getByRole("button", { name: "Retry connection" }),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
});
