# 有效行数（Effective Lines）定义与过滤规则

统计开发工作量时，物理增删行数会被大量"非工作性"改动污染。以下改动**不计入**有效行数：

## 1. 依赖锁文件（自动生成，无思考量）
- `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`
- `Gemfile.lock` / `Pipfile.lock` / `composer.lock` / `poetry.lock`

## 2. 编译产物 / 压缩文件
- `*.min.js` / `*.min.css` / `*.map` / `*.bundle.js`
- `dist/` / `build/` / `.next/` / `out/` / `target/` / `bin/` / `node_modules/` 目录下所有文件

## 3. 二进制文件
- 编译产物：`.jar` / `.war` / `.class` / `.pyc` / `.pyo` / `.o` / `.so` / `.dll` / `.exe`
- 图片：`.png` / `.jpg` / `.jpeg` / `.gif` / `.bmp` / `.ico` / `.webp` / `.avif` / `.heic` 等
- 字体：`.woff` / `.woff2` / `.ttf` / `.eot` / `.otf`
- 文档/压缩包：`.pdf` / `.docx` / `.xlsx` / `.pptx` / `.zip` / `.tar` / `.gz` / `.rar` / `.7z`
- 媒体：`.mp3` / `.mp4` / `.wav` / `.avi` / `.mov` / `.flv`

## 4. 纯格式化改动
- 仅含空白字符/缩进变化的行（去掉 `+`/`-` 前缀后 strip 为空）不计入。

## 5. 文件重命名 / 搬运
- diff 中 `renamedFile=true` 且无实际内容变化的，不计入。

## 6. Merge 提交
- `parentIds` 长度 ≥ 2 的提交整体剔除（避免把被合并分支的改动重复计入）。

## 7. 去重
- 跨所有分支采集时，按 commit SHA 去重，避免同一提交在多个分支被重复统计。

## 保留项（不排除，因常含真实工作）
- `.svg`：设计师/前端可能手写编辑，保留。
- `.sql`：可能是手写迁移脚本，保留（若团队 SQL 多为自动生成，可自行加入排除列表）。

## 争议点提醒
- 有效行数仍无法衡量"单行难度"。前端（Vue/HTML/CSS）单行难度普遍低于后端（Java/C#），跨职能对比需加折算系数（见 SKILL.md 双轨评分）。
- 大段复制粘贴、AI 生成代码会虚高行数，本规则无法识别，需人工留意。
- 重构、删代码、技术调研等高价值低行数工作会被低估——报告必须保留免责声明。
