# 📋 更新日志

所有重要变更都记录在此文件。版本号遵循 [语义化版本](https://semver.org/)。

## [Unreleased] · 当前开发版

### 🔐 安全加固（重大）
- **JWT 启动校验**：检测默认 secret 或长度 < 32 字符时启动告警
- **CORS 白名单化**：移除 `allow_origins=["*"]`，改用 `CORS_ORIGINS` 环境变量配置
- **SSRF 防护**：订阅同步拒绝私网/loopback/云 metadata（`127.0.0.0/8`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16`、IPv6 私网）
- **登录限速**：5 次/分钟/IP 滑动窗口，超限返回 429
- **JWT 撤销机制**：新增 `revoked_tokens` 表，登出后 jti 入黑名单，旧 token 立即失效

### ✨ 功能
- **独立管理员登录页** (`/admin/login`)：玻璃态背景 + 渐变 logo + 单独路由
- **国旗自动识别**：节点名启发式 → yaml 解析 → GeoIP（ip-api.com）三层 fallback
- **国家多选筛选**：公开页可勾选多个国家组合筛选
- **节点名过滤正则**：外部订阅支持 `name_filter_regex` 字段，自动排除营销节点
- **预览导入**：外部订阅 modal 加 🔍 预览按钮（不入库）
- **订阅独立定时**：interval（每 N 分钟）/ daily（每天 HH:MM）/ weekly（每周 D HH:MM）/ off 四种模式
- **节点管理**（重构）：CRUD / TCPing / 延迟探测 / 复制 / 打标签 / 删除
- **流量统计**：自动解析 `subscription-userinfo` header（upload/download/total/expire）

### 🎨 UI 美化
- **浅色主题全面重写**：玻璃态顶部 + 渐变 logo + 实时点 + 按钮化导航
- **浅色节点表格**：协议 chip（彩色）/ 状态圆点 / 圆形操作按钮
- **5 个统计卡片** + 彩色竖条状态指示
- **按钮变体系统**：`.btn.primary` 蓝色 / `.btn.success` 绿色 / `.btn.warn` 橙色 / `.btn.danger` 红色 / `.btn.purple` 紫色 / `.btn.subtle` 灰
- **开关精致化**：40×22 胶囊开关，渐变蓝激活 + 阴影
- **统一 8px 圆角** + 柔和阴影体系
- **响应式布局**：桌面/平板/手机自适应

### 🔧 后端重构
- `probes` 表去除外键约束（避免 mihomo/custom id 冲突）
- `database.py` 自动 schema 迁移（新增 `_ensure_columns` 工具）
- 配置支持本地/Docker 双路径（`/app/data` 与 `BASE_DIR/data`）
- 调度器探测覆盖 mihomo + custom 两类节点
- custom 节点探测用 `tcp_probe`（不依赖 mihomo API）

---

## [0.1.0] · 2026-09-30 · 初始发布

首个公开版本。基础功能：
- Mihomo External Controller API 集成
- 持续轮询 + 历史聚合
- 自定义节点 + 外部订阅管理
- 标签系统
- 浅色主题 UI
- 基础鉴权（JWT + bcrypt）
