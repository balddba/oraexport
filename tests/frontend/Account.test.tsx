import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Account } from "../../frontend/src/pages/Account";
import { api } from "../../frontend/src/api";

vi.mock("../../frontend/src/api", () => ({ api: vi.fn() }));
const mockApi = vi.mocked(api);

describe("password change", () => {
  it("requires matching confirmation, submits credentials, and clears secrets on success", async () => {
    mockApi.mockResolvedValue({ ok: true });
    render(<Account />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Current password"), "old-password");
    await user.type(
      screen.getByLabelText(/^New password/),
      "replacement-password",
    );
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "mismatched-password",
    );
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("do not match");
    expect(mockApi).not.toHaveBeenCalled();
    await user.clear(screen.getByLabelText("Confirm new password"));
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(mockApi).toHaveBeenCalledWith("/auth/password", "POST", {
      current_password: "old-password",
      new_password: "replacement-password",
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Password changed",
    );
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText(/^New password/)).toHaveValue("");
    expect(screen.getByLabelText("Confirm new password")).toHaveValue("");
  });

  it("disables duplicate submissions and displays errors for correction", async () => {
    let rejectRequest!: (error: Error) => void;
    mockApi.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectRequest = reject;
        }),
    );
    render(<Account />);
    const user = userEvent.setup();
    await user.type(
      screen.getByLabelText("Current password"),
      "wrong-password",
    );
    await user.type(
      screen.getByLabelText(/^New password/),
      "replacement-password",
    );
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(
      screen.getByRole("button", { name: "Changing password…" }),
    ).toBeDisabled();
    rejectRequest(new Error("Current password is incorrect"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Current password is incorrect",
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Change password" }),
      ).toBeEnabled(),
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
