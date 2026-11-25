// Typed fetch wrappers for the FastAPI backend (same origin; Vite proxies /api in dev).
import type { LoginResponse, Overview, Recommendation } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the API server");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  login: (username: string, password: string) =>
    request<LoginResponse>("/api/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  me: (token: string) => request<{ username: string }>("/api/me", {}, token),
  overview: () => request<Overview>("/api/overview"),
  approve: (id: number, token: string) =>
    request<{ recommendation: Recommendation }>(`/api/recommendations/${id}/approve`, { method: "POST" }, token),
  decline: (id: number, token: string) =>
    request<{ recommendation: Recommendation }>(`/api/recommendations/${id}/decline`, { method: "POST" }, token),
};
