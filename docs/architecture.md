# 架构说明

[返回 README](../README.md)

## 总体结构

Tetra 是一个本机运行的 Web 应用：后端是 FastAPI，前端是不需要构建的原生 JavaScript，数据存在 SQLite 和 JSON 文件里。

```text
浏览器  web/（原生 HTML / CSS / JS，KaTeX 渲染公式）
  │  REST + 轮询进度
  ▼
FastAPI  mingti/server.py
  ├─ 对话助手    assistant.py ── 分流：聊天 / 出题 / 组卷 / 存题 / 整理题库（organize.py）；前文由 context.py 整理
  ├─ 出题任务    jobs.py → team.py → question_authoring/（命题流水线）；几个对话的任务同时跑（默认最多 8 道）
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
| 解题员 | 先规划思路，定下坐标、法向量、方程的解这些关键中间结果，再分段写解析，各段统一使用这些结果。只能用高中方法：用了定积分、洛必达、泰勒展开等，先由复核员判断有没有高中解法（见下文“超纲的处理”） |
| 复核员 | 从题面重新求解，核对解析和答案：题面有问题退回命题员，解析有问题写明错在哪里，退回解题员修改。解析的答案最后还要和审题员独立算出的对一次，对不上就重新独立求解，两次一致才采用 |
| 验算员（可选） | 题干定稿后另开线程，只看题面独立解题，和解题员同时进行，最后核对两边的答案 |
| 整理员 | 按“解析详略”把完整推导改写成标准答案式的解析（【分析】【详解】）。最后的“故选：A”由程序按核对过的答案补上；完整推导保留，可以展开看 |
| 配图员 | 只写“图纸”：点的坐标、连线、函数表达式或统计数据，由程序照着画。立体图按斜二测画，被挡住的线自动画成虚线；画完按坐标核对题干里的中点、垂直、平行和长度，对不上就带着问题重画一次 |

- 可以同时开 1～3 个方案并行出题，谁先交出全部通过的题就用谁的，其余方案随即停下。
- 复核退回解析时不重写：解题员拿到复核意见和编号的解析，只返回要改的步骤，没提到的步骤由程序原样保留，再交给复核员核对。解题员认为意见不成立时可以提出异议，由复核员重新核算。最多改 4 轮，还不行就整份重写一次。
- 解析的答案和审题员独立算的不一致时不走修改，而是再独立解一遍，两次一致才采用。
- 交付保证：一道题调用模型超过 70 次或超过 8 分钟（极高档 20 分钟）时，手里有写完的解析就交付最好的一版；答案没有分歧的优先，其次意见最少。这样的题标“待核对”，写明还没解决的意见，不会被自动组卷选用，老师改过以后标记去掉。手里还什么都没有时，上限放宽到 1.5 倍。题面被判定不成立的版本不会交付。
- 模型输出的小毛病在流程里自己消化，不会让任务失败：段号、子段号写重写乱了按顺序重编；某一段几次都没写成，先拆成小段再写，还不行就整份重写解析（解题员知道上一份哪里没写成）；题面格式和题型对不上（选择题选项不全或重复、非选择题多给了选项），写解析之前就修好。交付时的全部检查（结构、高中范围、可信检查）在交出之前先跑一遍，题面的问题退回命题员，解析和答案的问题让解题员重写。只有接口出错（网络、余额、Key）才会停下。
- 退回都带着原因：整道题作废重出时，命题员拿到前两版被退回的题面和具体原因（审题意见、复核意见、超纲判断、验算对不上），针对这些原因重新设计；整份重写解析时，解题员拿到上一份被退回的原因。只有“和审题员的答案对不上”这一种，重解时只说有分歧、不说哪边的答案，新的一份仍然独立求解。
- 给写好的题选知识点时，没指定考点的题先由专题识别模型（`mingti/topic_model.py`，按字切片的 TF-IDF 加逻辑回归，分 9 个专题）判断专题，只把这个专题的知识点交给模型，这一步的输入从约 9 千 Token 降到 3 千左右。拿不准（最高概率不到 0.9）时照旧给全部 302 个知识点；复数、平面向量两章模型认不出来，总是带上。它只影响题目挂哪个知识点，不影响题目和答案。
- 出题方式有三种预设：快速出题（全用快速模型）、多一道把关（审题员、复核员换成推理更强的模型）、最严格（再加验算员）；也可以自定义。

### 超纲的处理

程序按 `mingti/question_authoring/scope.py` 的规则查高中课程以外的内容（大学的概念，以及定积分、洛必达法则、泰勒展开、级数等方法）。查出来不会直接判失败，而是按出现的位置交给能处理它的角色：

- **题面**：退回命题员改一次。只是说法超纲就换成高中说法；题目确实要用大学方法才能解，就改成高中方法能解的题。还改不掉，这一版作废，带着原因重出。
- **知识点、考查方法、易错点这些标签**：多半是照抄了要求里的说法（比如“随机过程中的递推”），直接去掉这一条，不影响题目。
- **解析**：先由复核员判断（阶段 `solution-scope-review`），分三种：
  1. 只是说法超纲，数学上仍是高中方法：退回解题员换成高中说法；
  2. 能用高中方法做：复核员写清楚第几步改用什么方法，退回解题员按这个做法修改；
  3. 只能用大学方法：先请解题员再确认一次。解题员找到高中方法就照样修改；也认为做不出来，就写明理由。
  解题员和复核员都认为只能用大学方法时，退回命题员改题，复核员的判断、改题建议和解题员的理由都交给命题员。解题员对复核员给的做法有异议时，复核员会核算他的理由；两轮之后还改不掉，同样退回命题员。

交付前最后还有一次同样规则的检查，正常不会拦下。

## 公式显示

题干、选项、答案和解析步骤由模型按 LaTeX 写在 `$…$` 里，用 KaTeX 渲染。易错点、命题意图、过程记录这些说明文字常常是纯文本公式（`V=πr²h`、`a_{n+1}`），`web/js/automath.js` 在显示时把它们转成 LaTeX，不改存储的内容；排不出来的保持原样。

模型偶尔会在这些本该写成 `$…$` 的内容里漏写 `$`（答案写成裸的 `\frac{2}{5}`），或者把换行多转义一次，留下字面上的 `\n`。新出的题在服务端就整理好（`mingti/math_content.py` 的 `tidy_math_text`）；以前存下的内容在显示和导出时按同一套规则整理（`automath.js` 的 `tidyMath`、`export.py`），不改存储的内容。

## 项目结构

| 路径 | 内容 |
|---|---|
| `mingti/server.py` | Web 接口（FastAPI） |
| `mingti/assistant.py` | 对话助手：判断一句话是聊天、出题、组卷、存题还是整理题库；聊天直接回答，看图解题 |
| `mingti/organize.py` | 整理题库：模型按老师的话写出文件夹操作（建、改名、挪、删，按编号或条件挑题），程序检查后执行，记下撤销办法 |
| `mingti/context.py` | 对话的上下文：按 Token 预算整理前文、估算用量、压缩成摘要 |
| `mingti/assemble.py` | 自动组卷：定方案、从题库挑题、现场出题补缺、组成试卷 |
| `mingti/attachments.py` | 聊天附件的暂存（内存里，4 小时），PDF 按页渲染成图片 |
| `mingti/importer.py` | 识题导入：逐页识别题目、接上跨页内容、截配图（照片按模型给的位置截，再对准图上的墨迹）、补写解析；也能从对话里把讨论过的题和解答整理出来 |
| `mingti/question_authoring/` | 命题核心：多阶段提示词流程（`providers.py`）与规则检查（`authoring.py`） |
| `mingti/team.py` | 多智能体：方案并行、按角色指定模型、审核意见上报、独立验算员 |
| `mingti/jobs.py` `mingti/tracking.py` | 后台出题任务和实时进度，每次模型调用的记录 |
| `mingti/abort.py` | 点停止时当场断开进行中的模型请求（出题、批量、组卷、导入都用它） |
| `mingti/answer_key.py` | 整理员：把完整推导改写成标准答案式的解析 |
| `mingti/figures.py` `mingti/figlib.py` | 配图员与绘图库（斜二测立体图、函数图象、统计图） |
| `mingti/topic_model.py` | 专题识别：没指定考点的题，给题目选知识点时只交出所属专题的知识点（见“出题流水线”） |
| `mingti/bank.py` | 题库、文件夹和试卷的存储 |
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
| `data/blueprint_specs.json` | 171 条示例命题要求，首页的示例从这里抽 |
| `tools/` | `build_i18n.py` 生成英文界面对照表；`build_blueprint_specs.py` 生成上面的 JSON（公式写法在 `blueprint_tex.json`） |

## API 概览

服务启动后，完整的接口文档在 http://127.0.0.1:8765/docs （FastAPI 自动生成）。

| 分组 | 接口 |
|---|---|
| 对话 | `POST /api/assistant`；`GET /api/sessions`、`GET /api/sessions/{id}`；`POST /api/sessions/{id}/rename`、`/pin`；`DELETE /api/sessions/{id}` |
| 上下文 | `GET /api/sessions/{id}/context`、`POST /api/sessions/{id}/compress` |
| 附件与识题 | `POST /api/uploads`、`POST /api/chat-imports`（不带附件时从对话里整理）、`POST /api/messages/{id}/import` |
| 整理题库 | 由 `POST /api/assistant` 当场做完；撤销：`POST /api/messages/{id}/undo` |
| 出题 | `POST /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/stop`；一次出几道：`/api/batches` |
| 组卷 | `POST /api/assemblies`、`GET /api/assemblies/{id}`、`POST /api/assemblies/{id}/stop` |
| 题库 | `GET/POST /api/bank`、`GET/PUT /api/bank/{id}`、`POST /api/bank/delete`、`POST /api/bank/{id}/figure`、`/tidy`；导入：`/api/imports` |
| 文件夹 | `GET/POST /api/bank/folders`、`PATCH/DELETE /api/bank/folders/{id}`、`POST /api/bank/move` |
| 试卷 | `GET/POST /api/papers`、`GET/PUT/DELETE /api/papers/{id}`、`GET /api/papers/{id}/print`、`/export` |
| 设置与模型 | `GET/PATCH /api/profile`、`POST /api/profile/reset`、`GET /api/profile/export`；`/api/model-services`、`GET /api/models` |
| 其他 | `GET /api/health`、`/api/curriculum`、`/api/blueprints`、`/api/counts`、`/api/questions` |
