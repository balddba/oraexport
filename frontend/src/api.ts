export const sessionExpiredEvent = "oraexport:session-expired";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    if (response.status === 401 && path !== "/auth/login") {
      window.dispatchEvent(new Event(sessionExpiredEvent));
    }
    const data = await response
      .json()
      .catch(() => ({ detail: "Request failed" }));
    const message = Array.isArray(data.detail)
      ? data.detail
          .map(
            (e: { loc: string[]; msg: string }) =>
              `${e.loc.slice(1).join(".")}: ${e.msg}`,
          )
          .join("; ")
      : data.detail;
    throw new ApiError(response.status, message || "Request failed");
  }
  return response.json();
}
export function bytes(value: number | null): string {
  if (value === null) return "Unavailable";
  if (value === 0) return "0 B";
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KB", "MB", "GB", "TB"][unit]}`;
}
export function time(value: string | null, zone?: string): string {
  return value
    ? new Date(value).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: zone,
      })
    : "—";
}

export function schemaLabel(scope: {
  schema_name?: string;
  schema_names?: string[];
  export_mode?: string;
}): string {
  return scope.export_mode === "FULL"
    ? "Full export"
    : scope.schema_names?.length
      ? scope.schema_names.join(", ")
      : scope.schema_name || "";
}
