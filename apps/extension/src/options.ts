// @ts-check
import { getBinding, getQueue } from "./storage.js";
import { connectLocalApp } from "./local-connection.js";
const element = id => document.getElementById(id);
async function connect(launch = false) {
  element("connect").disabled = true;
  element("status").textContent = launch ? "正在启动本地 App…" : "正在查找本地 App…";
  try {
    const result = await connectLocalApp(launch);
    element("connect").dataset.reload = String(Boolean(result.requiresReload));
    element("connect").textContent = result.requiresReload ? "重新加载插件" : "启动本地 App 并连接";
    const binding = result?.binding ?? await getBinding();
    element("current-account").textContent = binding ? `本地知识库 · ${binding.accountId}` : "尚未完成首次本地连接";
    element("status").textContent = result?.online ? "已自动连接，无需账户和 Token。" : result?.message || "请检查本地桥接安装，然后启动 App 重试。";
    const queue = await getQueue();
    element("queue-info").textContent = `${queue.filter(item => item.accountId === binding?.accountId).length} 条资料等待同步；App 关闭时保留连接和队列。旧账户任务保持隔离，不会自动写入当前知识库。`;
  } catch { element("status").textContent = "连接失败，请重新加载插件并打开新版客户端。"; }
  finally { element("connect").disabled = false; }
}
element("connect").addEventListener("click", () => {
  if (element("connect").dataset.reload === "true") { chrome.runtime.reload(); return; }
  connect(true);
});
connect();
