// The local API refuses state-changing requests without this header, so a
// page on another origin cannot drive it (see _require_console_json in api/app.py).
const CONSOLE_HEADERS = { "X-DVL-Client": "console" };

export function postJson(url: string, body?: unknown): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: body === undefined ? CONSOLE_HEADERS : { ...CONSOLE_HEADERS, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** An Error carrying the server's `detail`, so the operator sees why it failed. */
export async function responseError(response: Response, label: string): Promise<Error> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  const detail = typeof body?.detail === "string" ? body.detail : null;
  return new Error(detail ? `${label}：${detail}` : `${label} (${response.status})`);
}

/** Ensures a slower, older response never overwrites a newer one. */
export function latestOnly() {
  let latest = 0;
  return {
    begin: () => ++latest,
    isCurrent: (token: number) => token === latest,
  };
}
