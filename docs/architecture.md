# 架构说明

[返回 README](../README.md)

## 总体结构

Tetra 是一个本机运行的 Web 应用：后端是 FastAPI，前端是不需要构建的原生 JavaScript，数据存在 SQLite 和 JSON 文件里。

```text
浏览器  web/（原生 HTML / CSS / JS，KaTeX 渲染公式）
  │  REST + 轮询进度
  ▼
FastAPI  mingti/server.py
  ├─ 对话助手    assistant.py ── 分流：聊天 / 出题 / 组卷 / 存题；前文由 context.py 整理
  ├─ 出题任务    jobs.py → team.py → question_authoring/（命题流水线）
  │              └─ figures.py 配图、answer_key.py 整理解析
  ├─ 自动组卷    assemble.py ── 从 bank.py 挑题，缺的交给 jobs.py 现场出
  ├─ 识题导入    importer.py ── 附件由 attachments.py 暂存
  ├─ 题库试卷    bank.py、export.py（打印页 / Word / PDF）
  └─ 模型调用    deepseek.py（兼容 OpenAI 的接口）、claude.py（Anthropic SDK）
                 tracking.py 记录每一次调用的耗时和 Token
                 abort.py 点停止时当场断开进行中的请求
  ▼
存储  data/runtime/ 下的 SQLite 和 JSON
```

## 出题流水线

| 角色 | 职责 |
|---|---|
| 命题员 | 先构造、后成题，参考 PromptCoT 的“概念 → 思路 → 题目”：先选定数学对象和数据，把每一问的答案算出来，再写题干。构造出的答案只给系统核对，不进题面，也不给解题员看 |
| 审题员 | 检查条件是否充分、是否超纲、表述是否严谨，并只看题面独立求解。答案先和构造答案对一次，对不上就在写解析之前先改题干 |
| 解题员 | 先规划思路，定下坐标、法向量、方程的解这些关键中间结果，再分段写解析，各段统一使用这些结果。只能用高中方法：用了定积分、洛必达、泰勒展开等会被退回重写 |
| 复核员 | 从题面重新求解，核对解析和答案：题面有问题退回命题员，解析有问题退回解题员。解析的答案最后还要和审题员独立算出的对一次，对不上就重新独立求解，两次一致才采用 |
| 验算员（可选） | 题干定稿后另开线程，只看题面独立解题，和解题员同时进行，最后核对两边的答案 |
| 整理员 | 按“解析详略”把完整推导改写成标准答案式的解析（【分析】【详解】）。最后的“故选：A”由程序按核对过的答案补上；完整推导保留，可以展开看 |
| 配图员 | 只写“图纸”：点的坐标、连线、函数表达式或统计数据，由程序照着画。立体图按斜二测画，被挡住的线自动画成虚线；画完按坐标核对题干里的中点、垂直、平行和长度，对不上就带着问题重画一次 |

- 可以同时开 1～3 个方案并行出题，谁先交出全部通过的题就用谁的，其余方案随即停下。
- 一道题调用模型超过 70 次或超过 8 分钟还没出成，就先停下并说明原因。
- 出题方式有三种预设：快速出题（全用快速模型）、多一道把关（审题员、复核员换成推理更强的模型）、最严格（再加验算员）；也可以自定义。

## 公式显示

题干、选项、答案和解析步骤由模型按 LaTeX 写在 `$…$` 里，用 KaTeX 渲染。易错点、命题意图、过程记录这些说明文字常常是纯文本公式（`V=πr²h`、`a_{n+1}`），`web/js/automath.js` 在显示时把它们转成 LaTeX，不改存储的内容；排不出来的保持原样。

## 项目结构

| 路径 | 内容 |
|---|---|
| `mingti/server.py` | Web 接口（FastAPI） |
| `mingti/assistant.py` | 对话助手：判断一句话是聊天、出题、组卷还是存题；聊天直接回答，看图解题 |
| `mingti/context.py` | 对话的上下文：按 Token 预算整理前文、估算用量、压缩成摘要 |
| `mingti/assemble.py` | 自动组卷：定方案、从题库挑题、现场出题补缺、组成试卷 |
| `mingti/attachments.py` | 聊天附件的暂存（内存里，4 小时），PDF 按页渲染成图片 |
| `mingti/importer.py` | 识题导入：逐页识别题目、接上跨页内容、截配图、补写解析 |
| `mingti/question_authoring/` | 命题核心：多阶段提示词流程（`providers.py`）与规则检查（`authoring.py`） |
| `mingti/team.py` | 多智能体：方案并行、按角色指定模型、审核意见上报、独立验算员 |
| `mingti/jobs.py` `mingti/tracking.py` | 后台出题任务和实时进度，每次模型调用的记录 |
| `mingti/abort.py` | 点停止时当场断开进行中的模型请求（出题、批量、组卷、导入都用它） |
| `mingti/answer_key.py` | 整理员：把完整推导改写成标准答案式的解析 |
| `mingti/figures.py` `mingti/figlib.py` | 配图员与绘图库（斜二测立体图、函数图象、统计图） |
| `mingti/bank.py` | 题库和试卷的存储 |
| `mingti/export.py` | 试卷的打印页、Word 导出（LaTeX 转 Word 公式）和 PDF 导出 |
| `mingti/store.py` | 命题记录、对话消息、对话的置顶和名称 |
| `mingti/deepseek.py` `mingti/claude.py` | 模型调用：兼容 OpenAI 的接口（含 JSON 修复）、Anthropic SDK |
| `mingti/model_config.py` `mingti/models.py` `mingti/presets.py` | 模型服务的配置、可选模型列表、常用服务的模板 |
| `mingti/profile.py` | 个人设置、偏好学习、导出与删除 |
| `mingti/curriculum/` `mingti/curriculum_tree.py` | 知识树：人教 A 版（5 册 18 章 71 节 302 个知识点）、人教 B 版（7 册 17 章 53 节 302 个知识点） |
| `mingti/math_verifier/` `mingti/model_operations/` | 程序验证规则；模型调用的耗时与 Token 记录 |
| `mingti/launcher.py` `run.py` `pyproject.toml` | `tetra` 启动命令：首次运行时准备 `.venv`，然后启动服务、打开浏览器 |
| `web/` | 界面：`index.html`、`app.css`，`js/` 下按功能分文件；KaTeX 在 `vendor/` |
| `data/curriculum/` | 两版教材的知识树 CSV |
| `data/blueprint_specs.json` | 171 条示例命题要求：首页示例和评测用的测试集 |
| `tools/` | `build_i18n.py` 生成英文界面对照表；`build_blueprint_specs.py` 生成上面的 JSON（公式写法在 `blueprint_tex.json`） |
| `eval/` | 流程评测：`ablation.py` 出题、`judge.py` 评判、`report.py` 汇总出表和图，`common.py` 是共用部分 |

## API 概览

服务启动后，完整的接口文档在 http://127.0.0.1:8765/docs （FastAPI 自动生成）。

| 分组 | 接口 |
|---|---|
| 对话 | `POST /api/assistant`；`GET /api/sessions`、`GET /api/sessions/{id}`；`POST /api/sessions/{id}/rename`、`/pin`；`DELETE /api/sessions/{id}` |
| 上下文 | `GET /api/sessions/{id}/context`、`POST /api/sessions/{id}/compress` |
| 附件与识题 | `POST /api/uploads`、`POST /api/chat-imports`、`POST /api/messages/{id}/import` |
| 出题 | `POST /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/stop`；一次出几道：`/api/batches` |
| 组卷 | `POST /api/assemblies`、`GET /api/assemblies/{id}`、`POST /api/assemblies/{id}/stop` |
| 题库 | `GET/POST /api/bank`、`GET/PUT /api/bank/{id}`、`POST /api/bank/delete`、`POST /api/bank/{id}/figure`、`/tidy`；导入：`/api/imports` |
| 试卷 | `GET/POST /api/papers`、`GET/PUT/DELETE /api/papers/{id}`、`GET /api/papers/{id}/print`、`/export` |
| 设置与模型 | `GET/PATCH /api/profile`、`POST /api/profile/reset`、`GET /api/profile/export`；`/api/model-services`、`GET /api/models` |
| 其他 | `GET /api/health`、`/api/curriculum`、`/api/blueprints`、`/api/counts`、`/api/questions` |
