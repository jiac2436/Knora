// @ts-check
import { readStorage, writeStorage } from "./storage.js";
export const LOCAL_BINDING = "pka.local.v1";
export const LOCAL_CATALOG = "pka.local-catalog.v1";
export const NATIVE_HOST = "local.pka.knowledge_inbox";

// A popup can load new files while Chrome still runs the previous worker and
// permission set. Never interpret an absent/rejected worker response as offline.
export async function connectLocalApp(launch = false) {
  try {
    const granted = await chrome.permissions.contains({ permissions: ["nativeMessaging"] });
    if (!granted) return { online: false, requiresReload: true,
      message: "插件尚未加载本地连接权限，请点击“重新加载插件”，然后重新打开插件。" };
    const result = await chrome.runtime.sendMessage({ type: "CONNECT_LOCAL", launch });
    if (result?.ok !== true || typeof result.online !== "boolean") {
      return { online: false, requiresReload: true,
        message: result?.error || "插件后台尚未更新，请点击“重新加载插件”，然后重新打开插件。" };
    }
    return result;
  } catch (error) {
    return { online: false, requiresReload: true,
      message: "插件连接组件不可用，请重新加载插件后重试。" };
  }
}

export async function nativeMessage(message) {
  let response;
  try { response = await chrome.runtime.sendNativeMessage(NATIVE_HOST, message); }
  catch { throw Object.assign(new Error("本地桥接未安装或不可用，请在设置中检查连接组件；无需账户或访问令牌。"), { status: 503 }); }
  if (!response?.ok) throw Object.assign(new Error(response?.message || "本地 App 暂不可用"), { status: response?.status ?? 503 });
  return response.data;
}

export async function discoverLocal(launch = false) {
  const cached = await readStorage(LOCAL_BINDING, null);
  try {
    const binding = await nativeMessage({ action: "connect", launch });
    if (binding?.kind !== "local" || typeof binding.accountId !== "string" || !binding.accountId) throw new Error("本地程序返回无效的知识库标识");
    // No token or owner credential is returned by the native host.
    const local = { kind: "local", accountId: binding.accountId, displayName: "本地知识库", apiBaseUrl: binding.apiBaseUrl };
    if (JSON.stringify(cached) !== JSON.stringify(local)) await writeStorage(LOCAL_BINDING, local);
    return { binding: local, online: true };
  } catch (error) {
    return { binding: cached?.kind === "local" ? cached : null, online: false,
      message: error instanceof Error ? error.message : "本地 App 未启动" };
  }
}
