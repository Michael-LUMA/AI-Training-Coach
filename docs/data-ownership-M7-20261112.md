# Mark M7 交付与验收 — 2026-11-12

## 结论

本轮工程与线上数据门禁通过，停在 L3 之前。保持私测，不开放公众注册。
已完成数据所有权、前端云身份、三条云链路、device_id 漏洞修复、双仓分任务提交与生产部署；
并真正重启了容器，用重启前同一对云 token 验证四类数据一致。
这不是“健康检查通过”或“重新创建身份后有数据”的替代验收。

## 已实现范围

- 导出：当前云身份的全部运动聚合摘要、画像、近期对话和云笔记；无分页截断，不导出 token/device_id。
- 硬删除：单事务删除 rides/profiles/dialog_turns/notes/users；先确认提示，再输入“删除云数据”。服务端只接受 literal `confirm=true`。
- 删除安全：所有云写入与删除共用用户行锁，避免异步画像/对话写入复活；旧 token 随身份删除失效，其他用户不受影响。
- 云身份：打开 Mark/运动页后自动注册，凭证保存在当前浏览器 localStorage；刷新复用，不以 device_id 找回旧 token。
- 问答/复盘/实时均携带本人云 token；云历史读取本人摘要，旧共享历史不冒领、不自动迁移。
- 云笔记按用户隔离；本轮未接入模型上下文，不新增未经授权的记忆功能。
- 身份等待结束后，实时链路重新检查当前运动状态与定位新鲜度；进行中的运动、问答、复盘或上传会阻止删除。
- 删除成功保留本地墓碑，刷新不自动注册新身份；需手动确认建立新身份。
- 私测 Basic、同源、X-Coach-Token、云 token、no-store 和限流继续保留；重复 device_id 注册返回 409，不泄露或重置旧 token。

## 线上实测证据

- 正式私测入口：https://ai-ride-coach-production-7887.up.railway.app/mark 。另一个 Railway 域名不在同源白名单，返回 403 是正常门禁，未放宽白名单。
- 构建部署 ID：`7ebd215e-08aa-4319-a31d-fca20c62d9d8`；已部署代码：`096b810df62fecba86f2bcfa375fcb73476677d4`。
- 容器内检查：DATABASE_URL 存在且真正连接 PostgreSQL；私测密码存在；FAST/SMART 均为 qwen-max。
- 合成 A/B 两个隔离身份，通过真实 HTTPS 问答、复盘、实时链路。问答与复盘均返回核验通过的真实模型结果，未使用假成功 mock。
- 实时请求只证明带身份的 API 链路可用；没有触发事件时允许 calls 为空，不冒称已完成真实骑行或实时模型播报体验验收。
- 重启前/后容器指纹：`1080ab27a319af9ddbe9` → `14453416630bd3fcb96b`（hostname + /proc/1/stat 启动标识的脱敏 hash）。
- 使用原 token 逐项比较 rides/profile/dialog/notes；两个身份均一致，SQL 表计数也一致。
- A 硬删除后五表计数归零，旧 token 的 whoami/export 返回 401；B 的四类数据不变。
- 原轮次重启/回读已通过，硬删除亦成功；后续导出响应在客户端超时，Railway 脱敏 HTTP 日志确认服务端实际返回 401（4ms）。因此另用一对专用合成 A/B 身份补验硬删除、旧 token 401、SQL 归零和隔离，不冒称为一次全程无重试的验收。幂等摘要同步/只读查询遇传输超时最多重试一次，未重试不可判定的破坏性写入。
- 仅创建/删除验收专用合成身份，未删除真实用户数据；最终所有本轮合成身份、临时 schema、隧道和临时 SSH key 已清理。

## 回归结果

| 检查 | 结果 |
|---|---|
| 开发仓 Python | 1735 项：1730 通过、5 跳过、0 失败 |
| 部署仓 Python | 1710 项：1705 通过、5 跳过、0 失败 |
| Node 云身份/删除/投影 | 10/10 |
| 真实 PostgreSQL 隔离 schema | 4/4，含删除中途故障整事务回滚、删除后不可复活 |
| M7 真 HTTP 门禁 | 11/11 |
| 生产容器真重启回读 | 通过，见 verification JSON |

开发仓与部署仓有既有代码差异，未为凑齐测试数量覆盖不属于本轮的 ride_live.py 等文件。
真实 PostgreSQL 单独验收补充普通回归中的 opt-in 数据库跳过；不把全部跳过说成已通过。

## 现在如何使用

1. 通过正式私测入口登录（沿用已有私测凭证，不把密码写进文档）。打开 Mark 后等待“云身份有效”。
2. 正常问答、保存运动摘要、复盘；同一浏览器刷新继续使用原身份。
3. 需要备份时点“导出云数据”，确认 JSON 文件实际下载保存。
4. 确需永久清除当前身份时，先结束运动并等待问答/保存完成，再点“删除云数据”，两次确认。删除不可恢复，不要用真实重要数据做破坏性验收。

## 边界与下一道门

- 浏览器 localStorage bearer 身份不是账号系统；清站点数据/换设备可能丢失访问权，device_id 不能恢复凭证。
- 本地原始轨迹/音频/草稿和旧共享记录不在本轮云导出/硬删除范围；未自动迁移，也未宣称删除。
- PostgreSQL 逻辑硬删除不等于云服务商备份或物理介质擦除。
- 保留原模型核验与回退，不为测试通过放宽事实核验；继续使用 qwen-max，未擅自替换 key 或模型。
- Node harness 与真实 HTTP 已测；本轮没有实际操作桌面浏览器和 iPhone，不声称手机权限、下载、扬声器播放与后台能力已验收。
- boss 后续只需用 iPhone 同一正式入口确认：首次云身份、刷新后历史、导出文件保存、前台语音/录音体验。硬删除用专门测试身份。
- 公网发布仍需独立的账号/找回、逐用户风控、合规与备份策略审查；本轮 device_id 漏洞修复不代表可取消私测门禁。
- 本轮不开始 L3。交付后等待下一阶段任务；不增加硬件/感知或额外模型功能。

## 双仓提交

| 任务 | 开发仓 | 部署仓 |
|---|---|---|
| chore(memory): checkpoint validated M2-M6 cloud memory prerequisites | `ef8b9ab` | `a6c848b` |
| feat(privacy): export all owned data and atomically hard-delete cloud identity | `021ffbc` | `bef880a` |
| fix(auth): reject device-id credential recovery and rate-limit registration | `77a9025` | `89be3cf` |
| feat(web): persist cloud identity and wire chat review live plus privacy controls | `5f5452c` | `766e33a` |
| fix(privacy): disclose cloud retention and block deletion during pending work | `b41c661` | `e427e67` |
| test(web): verify identity resolution before live freshness recheck | `f12890c` | `096b810` |


文档与验收证据另作本地 docs commit，不推送部署仓，避免仅为文档再次触发未经本轮重启验收的新部署。
验证 JSON 记录的是已部署代码提交；文档提交号可通过本地 `git log -1` 查看。
