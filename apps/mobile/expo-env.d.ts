/// <reference types="expo/types" />

declare namespace NodeJS {
  interface ProcessEnv {
    EXPO_PUBLIC_API_BASE_URL?: string;
    EXPO_PUBLIC_DEV_BEARER_TOKEN?: string;
  }
}
