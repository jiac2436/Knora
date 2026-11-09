const normalizeBaseUrl = (value: string): string => value.replace(/\/+$/, "");

export const environment = {
  apiBaseUrl: normalizeBaseUrl(
    process.env.EXPO_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/v1",
  ),
  devBearerToken: process.env.EXPO_PUBLIC_DEV_BEARER_TOKEN?.trim() ?? "",
};
