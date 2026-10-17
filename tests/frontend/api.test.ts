import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, sessionExpiredEvent } from "../../frontend/src/api";

afterEach(() => vi.unstubAllGlobals());

describe("session expiry", () => {
  it("notifies the app about unauthorized protected requests", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Login required" }), {
          status: 401,
        }),
      ),
    );
    const listener = vi.fn();
    window.addEventListener(sessionExpiredEvent, listener);
    try {
      await expect(api("/tasks")).rejects.toBeInstanceOf(ApiError);
      expect(listener).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener(sessionExpiredEvent, listener);
    }
  });
  it.each([401, 503])(
    "preserves the session for login or server errors (%s)",
    async (status) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify({ detail: "Request failed" }), {
            status,
          }),
        ),
      );
      const listener = vi.fn();
      window.addEventListener(sessionExpiredEvent, listener);
      try {
        await expect(
          api(status === 401 ? "/auth/login" : "/tasks", "POST"),
        ).rejects.toBeInstanceOf(ApiError);
        expect(listener).not.toHaveBeenCalled();
      } finally {
        window.removeEventListener(sessionExpiredEvent, listener);
      }
    },
  );
});

describe("API requests and responses", () => {
  it("sends JSON with session credentials and returns the response", async () => {
    const payload = { name: "Finance export" };
    const result = { id: "task", ...payload };
    const fetch = vi.fn().mockResolvedValue(Response.json(result));
    vi.stubGlobal("fetch", fetch);
    await expect(api("/tasks", "POST", payload)).resolves.toEqual(result);
    expect(fetch).toHaveBeenCalledWith("/api/v1/tasks", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  });

  it("defaults to GET without a request body", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json([]));
    vi.stubGlobal("fetch", fetch);
    await expect(api("/tasks")).resolves.toEqual([]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/tasks",
      expect.objectContaining({ method: "GET", body: undefined }),
    );
  });

  it("formats validation failures with nested field paths", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          {
            detail: [
              { loc: ["body", "schedule", "cron"], msg: "Invalid cron" },
              { loc: ["body", "name"], msg: "Field required" },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    await expect(api("/tasks", "POST", {})).rejects.toMatchObject({
      status: 422,
      message: "schedule.cron: Invalid cron; name: Field required",
    });
  });

  it.each(["<html>Proxy error</html>", "{}", '{"detail":""}'])(
    "provides a fallback error message for unusable responses (%s)",
    async (body) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(body, { status: 502 })),
      );
      await expect(api("/tasks")).rejects.toMatchObject({
        status: 502,
        message: "Request failed",
      });
    },
  );

  it("propagates network failures", async () => {
    const failure = new TypeError("Failed to fetch");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(failure));
    await expect(api("/tasks")).rejects.toBe(failure);
  });
});
