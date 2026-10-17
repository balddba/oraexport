import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../../../frontend/src/api";
import { defaultTask } from "../../../frontend/src/constants";
import type { Run, User } from "../../../frontend/src/types";
import { Exports } from "../../../frontend/src/pages/Exports";

vi.mock("../../../frontend/src/api", async () => ({
  ...(await vi.importActual("../../../frontend/src/api")),
  api: vi.fn(),
}));
const mockApi = vi.mocked(api);
const user: User = {
  id: "operator",
  username: "operator",
  role: "operator",
  enabled: true,
};
const run: Run = {
  id: "run",
  task_id: "task",
  target_id: "one",
  state: "succeeded",
  oracle_job: "OE_TEST",
  created_at: "2026-10-12T12:00:00Z",
  started_at: null,
  finished_at: null,
  progress: 100,
  errors: [],
  messages: [],
  size_bytes: 1024,
  duration_seconds: null,
  cancel_requested: false,
  snapshot: {
    ...defaultTask,
    name: "Finance export",
    target_name: "Finance database",
    schema_names: ["FINANCE"],
  },
  files: [
    {
      id: "file",
      directory: "EXPORT_DIR",
      filename: "backup.dmp",
      kind: "dump",
      size_bytes: 1024,
      deleted_at: null,
      cleanup_attempts: 0,
      cleanup_error: null,
    },
  ],
};
let state: string;
let deleted: boolean;
let fail: boolean;
beforeEach(() => {
  mockApi.mockReset();
  state = "succeeded";
  deleted = false;
  fail = false;
  mockApi.mockImplementation(async (path, method) => {
    if (path === "/targets")
      return [
        { id: "one", name: "Finance database" },
        { id: "two", name: "Other database" },
      ];
    if (method === "DELETE") {
      if (fail) throw new Error("Some files could not be deleted");
      deleted = true;
      return {};
    }
    if (path.includes("/exports"))
      return {
        items: [
          {
            ...run,
            state,
            files: run.files.map((file) => ({
              ...file,
              deleted_at: deleted ? "2026-10-12T13:00:00Z" : null,
            })),
          },
        ],
        total: 26,
        page: path.endsWith("page=2") ? 2 : 1,
        page_size: 25,
      };
    throw new Error(`Unexpected path: ${path}`);
  });
});
function mount(role = user.role, initialTargetId = "") {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <Exports user={{ ...user, role }} initialTargetId={initialTargetId} />
    </QueryClientProvider>,
  );
}

it("lists the current database, paginates and resets the page when the database changes", async () => {
  mount("operator", "two");
  await screen.findByText("Finance export");
  expect(mockApi).toHaveBeenCalledWith("/targets/two/exports?page=1");
  await userEvent.click(screen.getByRole("button", { name: "Next page" }));
  await waitFor(() =>
    expect(mockApi).toHaveBeenCalledWith("/targets/two/exports?page=2"),
  );
  await userEvent.selectOptions(
    screen.getByRole("combobox", { name: "Database" }),
    "one",
  );
  await waitFor(() =>
    expect(mockApi).toHaveBeenCalledWith("/targets/one/exports?page=1"),
  );
});

it("requires confirmation, deletes files and refreshes the list", async () => {
  mount();
  await userEvent.click(
    await screen.findByRole("button", {
      name: "Delete export Finance export run",
    }),
  );
  const dialog = screen.getByRole("dialog", { name: "Delete export files" });
  expect(within(dialog).getByText("backup.dmp")).toBeInTheDocument();
  expect(mockApi.mock.calls.some(([, method]) => method === "DELETE")).toBe(
    false,
  );
  await userEvent.click(
    within(dialog).getByRole("button", { name: "Delete files" }),
  );
  await screen.findByText("Deleted");
  expect(mockApi).toHaveBeenCalledWith("/runs/run/files", "DELETE");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Delete export Finance export run" }),
  ).toBeDisabled();
});

it("shows deletion errors and allows a retry", async () => {
  fail = true;
  mount();
  await userEvent.click(
    await screen.findByRole("button", {
      name: "Delete export Finance export run",
    }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Delete files" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Some files could not be deleted",
  );
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "Delete files" }));
  await screen.findByText("Deleted");
});

it("disables deletion while a job needs reconciliation", async () => {
  state = "reconciliation_required";
  mount();
  expect(
    await screen.findByRole("button", {
      name: "Delete export Finance export run",
    }),
  ).toBeDisabled();
});

it("gives viewers read access without deletion controls", async () => {
  mount("viewer");
  await screen.findByText("Finance export");
  expect(
    screen.queryByRole("button", { name: /Delete export/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Details" })).toBeEnabled();
});
