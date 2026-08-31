---
name: yunxiao-code-audit
description: 从阿里云云效（Yunxiao/Codeup）拉取代码仓库提交数据，统计每位开发者的"有效代码行数"并做多维工作效能评估，最终产出可视化 HTML 报告。当用户想要"统计开发工作量/工作情况""代码审计""评估开发效能""云效代码统计""按有效行数排名"等需求时使用。核心是有效行数（过滤锁文件/编译产物/二进制/纯格式化），支持按职能（前端/后端）加系数公平对比。关键词：云效、Codeup、代码审计、工作量统计、有效行数、开发效能、commit 统计。
agent_created: true
---

# 云效代码审计与开发效能评估

从云效 Codeup 拉取提交数据，计算有效代码行数，做多维效能评分，产出 HTML 报告。

## 何时使用

用户想统计开发团队的工作量 / 工作情况、做代码审计、评估开发效能，且代码托管在**阿里云云效（Codeup）**。典型诉求：
- "统计一下大家的工作情况""看看谁干得多"
- "按有效代码行数排名，提交次数和活跃天数不算真实工作量"
- "评估开发的工作效能，多维度分析"

## 核心理念（务必先读）

1. **有效行数 ≠ 物理行数**：必须过滤锁文件、编译产物、二进制、纯格式化改动、文件搬运。commit 的 stats 字段云效**始终返回 null**，必须用 Compare API 拉 diff 文本手动解析。
2. **提交次数/活跃天数不代表工作量**：用户通常明确拒绝用这两个当主指标，它们只能做辅助维度。
3. **前后端不可直接用行数对比**：前端（Vue/HTML/CSS）代码天然膨胀约 1.5 倍，跨职能比较需要折算系数（默认 0.65），并用"组内排名（铁数据）+ 全员折算排名（参考值）"双轨展示。
4. **缺陷要看密度不看绝对数**：写得多的人缺陷绝对数自然多。用「缺陷密度 = 缺陷数 / 千有效行(KLOC)」作**负向质量维度**（密度越低评分越高）才公平。缺陷来自云效工作项(projex)，按 `assignedTo`（指派负责人=谁修复=谁对该块质量负责）聚合，其 name 直接是中文姓名，可直接匹配名单，无需邮箱映射。
5. **工时是"投入"不是"产出"**：任务工时反映投入而非成果（工时多可能是效率低），故只作**低权重（10%）正向维度**，刻意低于产出类维度（总有效产出30%/缺陷密度30%）。工时为固定 7 维之一，只要存在工时数据即纳入评分，填报率偏低仅作透明提示、不再自动剔除（早前的 40% 填报率门槛已取消，改为 7 维固定）。
6. **评分只反映代码产出+质量维度**，不能替代综合工作价值评估——报告里必须保留这条免责声明。

## 前置条件

- 云效 MCP 已配置，或有一个具备 **Codeup 读取权限**的 Personal Access Token（`pt-` 开头）。
- Token 通常在 `~/.workbuddy/mcp.json` 的 yunxiao server 配置里，键名可能是 `YUNXIAO_TOKEN` 或 `YUNXIAO_ACCESS_TOKEN`（两个都要试）。
- 需要用户提供**组织 ID（orgId）**。若不知道，可让用户在云效后台 URL 里找，或用 token 调 `/oapi/v1/platform/organizations`。
- Python 需 `requests`。

## API 速查（关键，避免踩坑）

- Base URL：`https://openapi-rdc.aliyuncs.com`（**不是** devops.cn-hangzhou，那个会 404）
- 认证头：`x-yunxiao-token: <TOKEN>`
- 列仓库：`GET /oapi/v1/codeup/organizations/{orgId}/repositories?perPage=100&page=N`
- 列分支：`GET /oapi/v1/codeup/organizations/{orgId}/repositories/{repoId}/branches?perPage=100`
- 列提交：`GET /oapi/v1/codeup/organizations/{orgId}/repositories/{repoId}/commits?refName=X&since=ISO&until=ISO&perPage=100&page=N`
- 取 diff：`GET /oapi/v1/codeup/organizations/{orgId}/repositories/{repoId}/compares?from={parentSha}&to={commitSha}` → 返回 `{diffs:[{newPath, diff, binary, renamedFile...}]}`
- **所有列表接口直接返回数组**，不是 `{result:[...]}`，解析时注意。
- 合并提交：`parentIds` 长度 ≥ 2，需剔除。
- **缺陷(Bug)相关**（projex 项目协作，非 codeup）：
  - 列项目：`POST /oapi/v1/projex/organizations/{orgId}/projects:search`，body `{"page":1,"perPage":100}` → 返回项目数组，取 `id` 作为 spaceId。
  - 搜缺陷：`POST /oapi/v1/projex/organizations/{orgId}/workitems:search`，body 必须含 `category:"Bug"`、`spaceId`、`spaceType:"Project"`、`page`/`perPage`/`orderBy:"gmtCreate"`/`sort:"desc"`。缺 spaceId 会报 400「项目id不能为空」。
  - 缺陷归属：`assignedTo.name`（指派负责人，中文名）；`creator` 多为测试/产品，不用于归属。`gmtCreate` 是毫秒时间戳，按此做时间窗过滤。
- **工时(Effort)相关**（projex 项目协作，非 codeup）：
  - 工时 API 是**按单个工作项**查询，有两个端点：
    · 实际工时 `GET /oapi/v1/projex/organizations/{orgId}/workitems/{workitemId}/effortRecords` → 数组，每条含 `actualTime`(小时)、`creator`(登记人)、`owner`、`workType`、`gmtStart/gmtEnd`。
    · 预计工时 `GET /oapi/v1/projex/organizations/{orgId}/workitems/{workitemId}/estimatedEfforts` → 字段名可能是 `estimatedTime`/`spentTime`/`actualTime`（脚本已兼容）。注意 `estimateRecords`/`estimate` 端点无效。
  - 取值规则：每个工作项有效工时 = **实际工时之和(>0优先)，为0则用预计工时之和兜底**。
  - 归属：有效工时归到工作项的**任务负责人 `assignedTo.name`**（工作项级归口，与缺陷维度统一）。中文名可直接匹配名单。
  - 因此统计须**先列出周期内所有工作项**（`workitems:search`，category 取 Req/Task/Bug 三类），**再逐个查工时明细**。工作项多时较慢（几百个逐个查，且实际为0还要补查预计），建议后台运行。
  - **务必统计填报率**（有有效工时的工作项占比）与预计兜底占比。工时填报率普遍很低，需据此决定是否纳入评分。

## 执行流程

按顺序运行 `scripts/` 下的脚本。每步产物是下一步的输入。**所有脚本都用命令行参数/环境变量传配置，不要把 token 硬编码。**

### Step 1 — 采集数据 + 计算有效行数（最慢，几百个 commit 逐个拉 diff）
```bash
python3 scripts/collect.py \
  --token "$YUNXIAO_TOKEN" \
  --org-id "<orgId>" \
  --days 30 \
  --out <workdir>
```
产出 `audit_data.json`。默认 `--days 30`（最近一个月）。这一步很慢，**用后台运行**，几百 commit 需要几分钟。

### Step 2 — 拉提交时间戳（用于时间/趋势维度，快）
```bash
python3 scripts/fetch_timestamps.py --token "$YUNXIAO_TOKEN" --org-id "<orgId>" --workdir <workdir>
```
产出 `commit_timestamps.json`（时间范围跟随 audit_data.json）。

### Step 3 — 拉缺陷数据（用于缺陷密度质量维度，快；可选但推荐）
```bash
python3 scripts/fetch_defects.py --token "$YUNXIAO_TOKEN" --org-id "<orgId>" --days 30 --workdir <workdir>
```
产出 `defects_data.json`，按 `assignedTo` 中文姓名聚合缺陷数。**`--days` 必须与 Step 1 一致**，否则密度分子(缺陷)分母(行数)时间窗不对齐。缺陷按中文姓名关联到开发者（需 compute_metrics 里的 `DISPLAY_NAME_OVERRIDES` 把 git 名改成与云效缺陷一致的中文名）。

### Step 4 — 拉工时数据（用于工时投入维度，可选；较慢）
```bash
python3 scripts/fetch_effort.py --token "$YUNXIAO_TOKEN" --org-id "<orgId>" --days 30 --workdir <workdir>
```
产出 `effort_data.json`，逐个工作项查工时（实际优先、无实际则查预计兜底）后按**任务负责人(`assignedTo`)**中文姓名聚合有效工时。**`--days` 必须与 Step 1 一致**。因需逐个工作项查询（几百个，实际为0还要补查预计），比缺陷采集慢，建议**后台运行**。产出的 metadata 含**填报率(fill_rate)**、预计兜底项数(items_by_estimate)、归口(attribution=assignedTo)——compute_metrics 会据填报率决定是否纳入评分（<40% 自动剔除）。工时靠中文姓名关联，同样依赖 `DISPLAY_NAME_OVERRIDES`。可用 `--by creator|owner` 改回记录级归口、`--no-estimate-fallback` 关闭预计兜底。

### Step 5 — 计算多维效能指标（双轨评分）
先在脚本顶部配置 `ROLE_MAP`（谁是前端/后端，按邮箱）、`IDENTITY_MERGES`/`DEV_KEY_MERGES`（跨域名同一人合并）、`DISPLAY_NAME_OVERRIDES`（git名→云效中文名，缺陷/工时匹配靠它）、`EXCLUDE_DEVS`（要排除的人）。然后：
```bash
python3 scripts/compute_metrics.py --workdir <workdir>
```
产出 `enhanced_audit_data.json`。**固定 7 维评分**：总有效产出(30%) + 缺陷密度(30%) + 日均产出(15%) + 工时投入(10%) + 删行占比(5%) + 提交颗粒度(5%) + 连续性(5%)。维度降级规则：
- 无 `defects_data.json` → 缺陷密度维度自动剔除、权重按比例分摊。
- 无 `effort_data.json`（文件完全缺失）→ 工时投入维度自动剔除、权重分摊。
- 注意：只要存在工时数据即**固定纳入**工时维度（7维固定），不再用填报率门槛剔除；填报率偏低仅作透明提示，不降级。

> **主维度口径**：用「总有效产出」（周期内有效增删行总数）而非「日均产出（÷活跃天）」。因为"活跃天"只统计有提交的天，会让活跃天多的勤奋者日均被摊薄、突击式提交者虚高。固定时间窗、成员全程在职时，总量口径最公平、最难被分母操纵。日均产出作为低权重辅助维度保留。
> **缺陷密度口径**：缺陷数 / 千有效行(KLOC)，负向指标（越低越好），归一化时反转百分位。用比值消除"产出多缺陷绝对数自然多"的偏差，反映单位产出的质量。
> **工时投入口径**：按**任务负责人(assignedTo)归口**，每个工作项取值 = 实际工时(actualTime)优先、无实际则用预计工时兜底，求和(小时)。正向维度，权重 10%（投入非产出，低于产出类维度）。只要存在工时数据即固定纳入 7 维评分，填报率偏低不再自动剔除。

### Step 6 — 生成 HTML 报告
```bash
python3 scripts/generate_report.py --workdir <workdir> --template assets/report_template.html
```
产出 `audit_report.html`。报告会根据实际启用的维度动态渲染（缺陷/工时列自动显隐；工时填报率过低时展示"数据不足未纳入评分"说明卡片）。

### Step 7 — 展示
用 present_files 起本地 server 预览 HTML（`python3 -m http.server`），并把 HTML 文件一并给出。

## 身份归一化注意事项

- 同一人可能有多个 Git identity（不同邮箱域名），脚本按邮箱小写归一化，但**跨域名无法自动合并**。
- 云效常见坑：`accounts_<hash>@mail.teambition.com` 是系统自动账号，displayName 往往是真实邮箱，需在 `IDENTITY_MERGES` 里手动映射到真人。
- **跨邮箱同人合并（重要）**：同一人常用「个人邮箱 + 公司邮箱」或「主邮箱 + teambition 自动账号」提交，是**两个独立 dev key**。仅靠 `IDENTITY_MERGES`（只改显示名）不够，必须用 `DEV_KEY_MERGES`（`{次要邮箱: 主邮箱}`）在统计层累加 commits/行数/活跃天/仓库/needs 并删除次要 key。
- **时间窗拉长（如从1个月改到3个月）时尤其容易暴露新的分身身份**，每次改时间跨度后都要重新核对开发者清单，把新出现的多邮箱身份补进 `DEV_KEY_MERGES`。
- 用 `DISPLAY_NAME_OVERRIDES`（`{主邮箱: "正式姓名"}`）把 git 里的英文名/账号名改成正式中文姓名。
- 完成后应把归一化映射（含跨邮箱合并）列给用户确认。

## 常见陷阱

- token 无 Codeup 权限 → 403，让用户在云效"个人设置→访问令牌"加"代码管理"读权限。
- 用错 base url → 404。
- 直接信 commit 的 stats → 永远是 null，必须走 compare API。
- 数据量少的人（如 <5 次提交）建议排除或单列，避免评分失真。
- 数据/时间戳采集脚本用 `tail -N` 管道时，进度输出会被缓冲到进程结束才一次性出现；90天全量采集耗时可达 10+ 分钟属正常，勿误判为卡死，直接看输出文件 mtime 判断是否写出。
- 详细口径规则见 `references/effective_lines_rules.md`。
