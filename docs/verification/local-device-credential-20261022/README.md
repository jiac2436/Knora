# 启动钥匙串密码框修复

用户截图中的 `local.pka.knowledge-inbox.local-device` 是本机接口的设备凭证，不是已移除的用户账户登录。上一轮免登录仍通过钥匙串读取它，所以系统授权框仍会出现。本次处理实际启动路径。

## 改动

- 新增 `MacDeviceCredentialStore`：本机 Owner 令牌保存到当前用户 `local.pka.knowledge-inbox-state/device-credentials/` 中，以服务命名空间 SHA-256 命名。目录 0700、文件 0600；仅有文件确实不存在时才迁移或生成，存在但损坏／权限异常时保留并报错，不悄悄更换凭证。
- 首次迁移只允许静默读取旧值。可读时原值迁移，兼容旧绑定；需要密码、拒绝或不可用时生成一次随机新值并持久化，后续重启复用。不删除旧钥匙串、数据库或模型配置。
- 本机 API 的 Bearer 校验、回环监听和私有父子进程管道保持。知识库 Owner／library／device 身份不因设备令牌存储变化而重建。
- 备份恢复中的设备令牌也走同一私有存储，探测服务及新库打开不再写入 Owner 钥匙串。
- 模型密钥仍在钥匙串，但自动启动只进行静默读取。需要授权时返回不可用状态，基础知识库仍可打开；用户在模型设置中主动应用配置时保留原授权流程。
- 旧 file-based login keychain 对现代请求选项的兼容存在实际差异，最终静默读取在适配器内串行执行，临时禁止进程交互并恢复原策略，同时保留请求级策略。所有该适配器的读、写、删统一通过递归锁，写后验证可重入；不全局永久禁止授权。

## 真实系统验收

[KeychainNoInteractionTest.swift](../../../apps/macos/scripts/KeychainNoInteractionTest.swift) 分别编译两个代码身份：写入程序创建只信任自己的随机命名合成项，新读取程序调用生产 `readWithoutInteraction`。三次均在 5 秒限时内立即拒绝读取，没有请求用户授权；同时断言调用前后的系统交互策略一致，原写入程序在 finally 清理合成项。完整记录见 keychain-no-interaction.log（历史产物已清理）。未读取真实用户凭证。

最初仅采用 `LAContext.interactionNotAllowed`，以及叠加请求级拒绝 UI 标志的测试仍出现超时，均未作为通过结果；最终改为上述序列化的旧钥匙串兼容策略后才通过。测试曾有一处把受保护项的 `missing` 结果误当作未形成拒绝，现以“已创建且新身份无法取得数据、及时返回、策略恢复”为断言。失败／初始构建记录保留在同目录，不以它们冒充最终通过。

参考 Apple 的 [SecItem 实践说明](https://developer.apple.com/forums/thread/724013)（file-based shim 对查询属性的兼容边界）及 [interactionNotAllowed](https://developer.apple.com/documentation/localauthentication/lacontext/interactionnotallowed)。本次以实际系统 ACL 回归确定行为，不只依赖 API 文档或界面隐藏。

## 存储及集成验收

- unit-test.log（历史产物已清理）：完整单测连续 5 轮通过，每轮 16 路首次创建，共 80 路并发；验证原值迁移、文件优先、拒绝／锁定回退、损坏／超限／非 ASCII 拒绝、目录／文件权限、软硬链接／FIFO、库隔离、定向清理、模型静默不可用／可读及主动授权路径。
- 并发首次 `openat(O_CREAT)` 曾复现短暂 ENOENT，增加仅对此错误的 4 次、每次 1ms 有限重试；其余路径错误不吞掉。验证为普通文件后清除 nonblocking 再加锁，保留特殊文件不阻塞和凭证不覆写保证。
- host-test.log（历史产物已清理）：当前完整原生 Sources + 真实随包 API／Worker；纯合成凭证替身禁止任何交互读／写／删。覆盖旧值迁移、跨新宿主实例重启、旧值拒绝后的新令牌稳定、旧令牌 401、身份／资料／原 Agent 不变，以及真实备份恢复 probe、切库和新实例重开。恢复备份按原有契约不复制旧设备的 Agent 授权。
- service-test.log（历史产物已清理）：随包 Python、动态端口、私有文件凭证、真实采集、退出和重启持久化通过。
- backup-test.log（历史产物已清理）：模型停服导出、自动重连及草稿保留，已有输出目标不覆盖，独立目录恢复后的身份／证据／原件和损坏包保护通过。
- LocalAgentConnectionsTest.log（历史产物已清理）：原独立凭证跨重启、授权隔离、端点更新、撤销立即 401、stdio 工具和临时宿主配置安装／卸载通过。
- KernelCaptureTest.log（历史产物已清理）：当前原生 Sources 对可信同版正式 runtime 执行，覆盖回执落盘失败保留原件、成功释放旧副本、重启接续归档、草稿、幂等、合成飞书图文、队列移除保留知识。

所有资料、模型标识、CLI 及宿主配置均为临时合成值，零真实模型调用，不操作用户知识库。当前可信 runtime 与候选使用相同 manifest，未改 API／Worker 或 schema。

## 兼容影响

- 无数据库迁移，schema16、插件1.0.5和随包内核保持。应用导出的备份不包含私有状态目录里的 Owner 文件；这不等同于从系统备份中排除该目录。
- 能静默取到旧值时手工浏览器绑定保持；取不到时仅首次轮换 Owner Token，使用旧 Owner Token 的浏览器插件／手工接口连接需更新绑定，后续 Token 稳定。独立 Agent/MCP 凭证不因这一轮换失效。
- 首次不能迁移旧钥匙串而生成新令牌后，旧 Owner 会话会失效；直接降级到仍只读旧钥匙串的历史 App 不保证能启动。保留本版私有凭证文件与原资料，切回同样使用私有文件的新版本不受影响；本次不修改后台去重新启用旧会话。
- Owner 文件以当前 macOS 用户的文件权限保护，不能隔离同一系统账户下的其他进程。模型 API Key、Agent 凭证继续由钥匙串保护，未转为普通文件。
- 当前编译有为旧 file-based keychain 兼容保留的 Security API 弃用警告；新旧两种钥匙串策略均明确恢复原状态。未新增依赖，未在本轮做清洁设备、Developer ID 或公证验收。

## 测试过程中修正的 fixture

历史测试需为临时 home 建立 0700 目录，并在建立后统一路径别名；否则 Foundation 对 `/private/var` 与 `/var` 的标准化时机差异会让既有 `MacLibraryStore` 前缀检查拒绝。仅修测试初始化，未放宽生产路径保护。旧备份测试仍假定 `send()` 同步完成，现改为有上限地刷新队列等待归档后再保留原断言，不把异步回执当最终成功。

完整候选构建、严格签名和 19,957 个运行包文件／链接校验已通过，运行包摘要保持 `cb14c1db218292b512853a19130c97dd0e3f17959a067970eef41c96f603aa3e`。结果与源码摘要见 [runtime-device-credential.json](runtime-device-credential.json)。`LocalDomainServiceTest` 只更新兼容断言，未在本轮运行；其他最终退出码见 [credential-test-results.json](credential-test-results.json)、[device-regression-results.json](device-regression-results.json)。

## 正式发布

发布预检确认客户端已退出。已更新唯一正式 App `apps/macos/dist/知识收集箱.app`，核对唯一入口、源码／二进制摘要、严格签名及 release 验收来源一致。旧包归档于 `apps/macos/archive/20261022-074212`；未启动用户正式 App 或操作真实用户资料。
