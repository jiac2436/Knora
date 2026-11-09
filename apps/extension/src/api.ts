// @ts-check
import { nativeMessage } from "./local-connection.js";

/** @param {string} base @param {string} path */
function endpoint(base, path) {
  return `${base}${path}`;
}

/**
 * @param {{apiBaseUrl: string, token: string}} binding
 * @param {string} path
 * @param {RequestInit} [init]
 */
export async function apiRequest(binding, path, init = {}) {
  if (binding.kind === "local") {
    return nativeMessage({ action: "request", library_id: binding.accountId, path,
      method: init.method ?? "GET", json: init.body ? JSON.parse(String(init.body)) : null,
      key: new Headers(init.headers).get("Idempotency-Key") });
  }
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${binding.token}`);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");

  const response = await fetch(endpoint(binding.apiBaseUrl, path), { ...init, headers });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.message || `服务返回 ${response.status}`);
    // @ts-ignore custom status for error classification
    error.status = response.status;
    // @ts-ignore custom service code
    error.code = payload?.code;
    throw error;
  }
  return payload;
}

/** @param {{apiBaseUrl: string, token: string}} binding */
export const fetchMe = (binding) => apiRequest(binding, "/v1/me");

/** @param {{apiBaseUrl: string, token: string}} binding */
export const fetchDomains = (binding) => apiRequest(binding, "/v1/domains");

/** @param {{apiBaseUrl: string, token: string}} binding */
export const fetchProjects = (binding) => apiRequest(binding, "/v1/projects");

/** @param {{apiBaseUrl: string, token: string}} binding @param {any} item */
export const uploadCapture = (binding, item) => apiRequest(binding, "/v1/captures", {
  method: "POST",
  headers: { "Idempotency-Key": item.idempotencyKey },
  body: JSON.stringify(item.payload)
});
