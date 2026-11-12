# Mark · M2 / M3 工程交付记录

日期：2026-11-11
状态：后端实现与离线验收就绪；真实 PostgreSQL / 容器重启 / 线上部署尚未验收。

## 1. 本轮做了什么

- M2：在实际 Mark 入口 `coach_server.py` 增加匿名设备注册、身份校验。注册插入采用数据库冲突处理，避免并发首次注册生成多套身份。
- M3：增加用户隔离的云摘要创建、批量同步、列表、读取、删除。
- 同步采用整批事务：完全相同的数据返回 duplicate；同 ID 不同内容返回 409，整批回滚，不覆盖既有记录。
- 同一条本地 ride ID 可以属于不同用户。数据库内部 ID 为 `user_id:local_ride_id`；对外仍返回原本地 ID。所有读写 SQL 均限定已认证 user_id。
- 新云接口只接受摘要白名单；完整 session、series、坐标、原始传感器数据、客户端注入的 user_id 都被拒绝。
- 原有本地 JSON 保存、导入、对话、导航、TTS 和 UI 不改。云失败返回明确错误，不假装已经同步，也不清空本地记录。
- 配置 DATABASE_URL 时服务启动先初始化 PostgreSQL；失败则不绑定端口，错误不回显连接串。不配置时，原本的本地功能继续使用，云接口返回 503。
- PostgreSQL 驱动已在开发虚拟环境安装并锁定版本，Dockerfile 也会安装同一依赖。没有购买数据库实例或发起付费模型调用。

## 2. 接口契约

全部为 POST + application/json；必须通过既有 Host、Origin、私测登录门禁。
新接口不依赖进程级 X-Coach-Token，但云记录操作必须提供有效的匿名身份 token。

| 路径 | 请求字段 | 结果 |
|---|---|---|
| /api/auth/register | `{}` 或 `{device_id}` | `{user_id, token}` |
| /api/auth/whoami | `{token}` | `{user_id}` |
| /api/rides/create | `{token, ride}` | 单条保存结果，使用与 sync 相同的去重规则 |
| /api/rides/sync | `{token, rides}` | `{storage, results, saved, duplicates}` |
| /api/rides/list | `{token}` 或 `{token, limit}` | `{storage, rides}` |
| /api/rides/get | `{token, id}` | `{storage, ride}` |
| /api/rides/delete | `{token, id}` | `{storage, id, deleted}`；重复删除返回 false |

- rides 批次为 1–20 条；list 默认 20，范围 1–100；不会无限返回全部历史。
- create / sync 请求体最大 2 MiB；其他新接口最大 8 KiB。
- 非法结构 400；无效身份 401；不存在 404；同 ID 内容冲突 409；限流 429；存储不可用 503。
- 新接口拒绝额外字段，尤其不接受外部传入的 user_id。
- sync 的 rides 是摘要数组，不是完整 session 数组。云端不能为了生成摘要而先接收原始轨迹。

### 摘要字段

`{id, started_at, ended_at, summary, source, activity, coach_notes}`

`ride_memory.to_cloud_summary(session)` 是无 I/O 的本地投影器：先用原 validate_session 校验完整记录，再仅输出上述字段。
兼容 v1/v2/v3；保留 null、source 与已有活动类型；旧记录缺失 activity 时按已有契约用 ride。
数据库单独标记 `luma-ride-cloud/1`，并不把摘要谎称为可复核原始采样的完整 session。

注意：云端只能校验摘要结构、数值范围和内部一致性，不能在没有采样的情况下重新证明平均心率等数值真实。
coach_notes 依约保留自由文本，**不代表已经对文字里的地址/身份信息自动脱敏**；公开产品必须做明确授权与告知。

## 3. 当前仍然不能宣称完成的事项

1. 当前进程没有配置 DATABASE_URL。真实数据库建表、数据库断连恢复、跨进程读取和容器重启持久化尚未跑通。
2. 新增的真实数据库验收用例只有显式设置 MARK_TEST_DATABASE_URL（一次性测试库）才会运行；不会自动拿生产库做测试。
3. 本轮不改前端，因此用户在现有 Mark 页面点操作，不会自动开始身份注册、云同步或跨设备恢复。
4. 不做 M4 用户画像、M5 记忆编排或 L3/L4 智能升级；避免在持久化尚未真实验收时继续叠能力。
5. 没有 push、线上发布、重启现有 8794 服务，也没有购买云资源。

## 4. NEEDS DECISION / 发布阻塞

### 匿名身份还不是公开账号系统

按参考包，已知 device_id 会取回既有身份及 token。**device_id 若非秘密，不能单凭它安全证明设备所有权**。
因此本实现只能留在现有私测登录门禁内，不能当作公开注册/找回账号直接发布。
公开发布前必须设计已有 token 的持有证明，或由原设备授权的新设备配对流程；不能只靠可猜设备名恢复账号。

无参数 `{}` 会创建新设备身份；要恢复同一设备，应保留稳定随机 device_id 和 token。它不会自动识别另一部手机属于同一个人。

### 实际入口与参考文档有偏差

参考包提到 ride_server.py；它实际是另一套隔离复盘服务。Mark 8794 的真实入口是 coach_server.py。
本轮新增云摘要路由位于真实入口，不强行把旧复盘服务的 SQLite 或凭证槽改成用户云存储。
原 SQLite 凭证仍与 PostgreSQL 摘要隔离，API key 不进入 db.py 或 rides 数据集。

开发目录没有 Git 元数据，不能声称已创建独立 commit。部署目录改动在本地同步；未推送。
部署目录原有 DEPLOY.md 的备选模型配置说明保留，不用开发目录旧文档覆盖它。

## 5. 验证方式

开发虚拟环境 Python：
`E:\luma测试\.code\ai_companion\.venv\Scripts\python.exe`

- 运行全部单测：`python -m unittest discover -s tests -p "test_*.py" -q`
- 定向：`python -m unittest tests.test_auth tests.test_db tests.test_cloud_rides tests.test_cloud_http tests.test_coach_server tests.test_coach_config -q`
- 真实数据库验收（仅测试库）：`python -m unittest tests.test_cloud_postgres -v`
- test_cloud_http 使用真实本机临时 HTTP 端口，验证私测登录、注册、身份校验、同步、重复同步、查询、拒绝原始数据；身份与数据库仍是合成测试替身，不等于真 PostgreSQL 验收。
- test_cloud_postgres 用于真实数据库并发注册、并发幂等同步、用户隔离、冲突回滚、连接池关闭后重连；连接池重连也不能替代真正的容器重启验收。
- 冻结文件 ride_tts.py、ride_nav.py、llm_review.py 和 static/mark.html 与本轮开始的部署版本一致；validate_session 函数源码不变。

额外检查：pip check 发现已有 opencv-python-headless 缺少 numpy。这是现有视觉环境问题，本轮云摘要模块不依赖 OpenCV，未顺手安装或升级视觉依赖。为安装数据库驱动，仅用 ensurepip 修复了当前虚拟环境中无法在 Python 3.12 运行的旧 pip。

最终测试数量、日志和同步校验见同目录 verification-M2-M3-20261111.json。

## 6. 下一步最短路径

1. 先准备 PostgreSQL 实例并安全配置 DATABASE_URL；无需更换 qwen-max，也无需再购买一个 AI 模型 API。
2. 用隔离测试库跑真实数据库验收，再在私测部署做一次“写入→重启容器→重新查询”的持久化验证。
3. 解决匿名身份持有证明后，再安排前端显式云同步与设备配对；公开发布前还需要 M7 的导出/删除和数据授权。
4. 上述验证通过后，进入 M4/M5，让 Mark 的回答真正用上可核验历史记忆。
