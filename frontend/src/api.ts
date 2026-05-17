// Thin fetch wrapper for the MeetingMind backend. Extracted from main.tsx
// in v0.1.4. All helpers throw a plain Error whose message is either the
// FastAPI `detail` field or the HTTP status line — App-level error handlers
// pattern-match on the message text.

async function responseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return body.detail || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

export const api = {
  async get<T>(path: string): Promise<T> {
    const response = await fetch(path);
    if (!response.ok) throw new Error(await responseError(response));
    return response.json() as Promise<T>;
  },
  async post<T>(path: string): Promise<T> {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json() as Promise<T>;
  },
  async postJson<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json() as Promise<T>;
  },
  async delete<T>(path: string): Promise<T> {
    const response = await fetch(path, { method: "DELETE" });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json() as Promise<T>;
  },
  async patch<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json() as Promise<T>;
  },
  async put<T>(path: string): Promise<T> {
    const response = await fetch(path, { method: "PUT" });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json() as Promise<T>;
  },
  async upload<T>(
    path: string,
    file: File,
    onProgress?: (uploadedBytes: number, totalBytes: number) => void,
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      xhr.upload.addEventListener("progress", (event) => {
        if (onProgress && event.lengthComputable) {
          onProgress(event.loaded, event.total);
        }
      });
      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as T);
          } catch (err) {
            reject(err instanceof Error ? err : new Error("Bad upload response"));
          }
        } else {
          let detail = `${xhr.status} ${xhr.statusText}`;
          try {
            const body = JSON.parse(xhr.responseText);
            if (body && body.detail) detail = body.detail;
          } catch {
            /* ignore */
          }
          reject(new Error(detail));
        }
      });
      xhr.addEventListener("error", () => reject(new Error("Network error during upload")));
      xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));
      xhr.send(form);
    });
  },
};

// Browser network-failure detection. Used by App to suppress expected
// "failed to fetch" toasts during user-initiated restart/upgrade windows.
export function isNetworkFailureMessage(text: string): boolean {
  if (!text) return false;
  const lowered = text.toLowerCase();
  return (
    lowered.includes("failed to fetch") ||
    lowered.includes("load failed") ||
    lowered.includes("networkerror") ||
    lowered.includes("network request failed")
  );
}
