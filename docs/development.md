# 开发指南

[返回 README](../README.md)

## 本地开发

- 前端是原生 ES 模块，没有构建步骤：改完刷新页面即可，页面发现界面文件更新过也会提示刷新。
- 后端改了需要重启 `tetra`。
- 模块划分见[架构说明](architecture.md#项目结构)。

## 界面翻译

界面代码照常写中文。英文对照表 `web/js/i18n-en.js` 由 `tools/build_i18n.py` 生成：它从代码里提取中文字符串，请模型翻译，结果缓存在 `tools/i18n_en.json`，以后只翻新增的。改了界面文字后运行一次：

```bash
.venv\Scripts\python -X utf8 tools\build_i18n.py
```

- 加 `--dry-run` 只统计还有几条要翻。
- 想改某一条的译法，写进 `tools/i18n_overrides.json` 再运行一次。
- 题目、解析和模型的回答不翻译。

## 示例命题要求

`data/blueprint_specs.json` 由 `tools/build_blueprint_specs.py` 生成。每条有两种写法：`topic` 是纯文本，用于输入框、搜索和发给模型；`topic_tex` 是排版写法，公式写在 `$…$` 里，手写在 `tools/blueprint_tex.json`。两者的中文对不上时，构建会报错。

## 运行数据

运行数据都在 `data/runtime/`，不进仓库：

| 文件 | 内容 |
|---|---|
| `questions.sqlite3` | 命题记录（含各方案进度和工作记录）；`messages` 表存聊天、组卷、识题和上下文摘要，`session_flags` 表存对话的置顶和名称 |
| `model-runs.sqlite3` | 每次模型调用的明细 |
| `bank.sqlite3` | 题库和试卷 |
| `profile.json` `models.json` | 个人设置、模型服务 |
| `figure-cache/` | 导出 Word 时配图转成的 PNG，可以随时删，用到时会重新生成 |

`.env` 和 `data/runtime/models.json` 里有 API Key，两者都已写进 `.gitignore`；打包代码时也不要带上它们。

## 流程评测

`eval/` 用同一批命题要求跑不同的流程配置，比较交付率、答案正确率、调用次数、Token、用时和费用，用来判断每个环节值不值得保留。

- **结构对比**：A 只有命题员 → B 加审题员 → C 加解题员、复核员（完整闭环，就是界面上默认的出题方式）→ D 再加验算员。
- **机制对比**：在 C 上每次关掉一个机制，即思考模式、先构造后成题、答案闸门、关键结果共享、审题答案交叉核对。开关在 `mingti/question_authoring/providers.py` 的 `PipelineSwitches` 里，默认全开，和界面出题一样。用 `--configs C-think,C-gate` 选择要跑的配置。
- **测试集**：从 `data/blueprint_specs.json` 按题型和难度分层抽题。`pilot` 10 道，`small` 30 道（4、5 星 16 道），`full` 60 道；小的集合都包含在大的里。
- **判分**：评判模型只看题面独立解 3 次，多数表决判答案对错，同时看考点和难度是否符合要求。评判默认用 `deepseek-flash`；它和出题是同一个模型时可能犯同样的错，所以另抽题目人工核对，报告两者的一致率。

在项目目录下运行（出题和评判用项目的 `.venv`，汇总用装了 matplotlib 的 Python）：

```bash
.venv\Scripts\python eval\ablation.py small --budget 2.5
.venv\Scripts\python eval\judge.py small
python eval\report.py small --audit 30
```

依次是出题、评判出成的题、汇总出表和图，并抽 30 道题给人工核对：`human_audit.md` 看题，结论填进 `human_audit.csv`，再运行一次 `report.py` 就会算出一致率。

- `--budget 2.5` 表示花到 2.5 美元就不再开始新的出题。
- 结果都在 `eval/results/`，不写应用自己的命题记录和题库。
- 中途停了（包括模型账户余额不足、返回 HTTP 402 时脚本自己停下），再运行同一条命令会接着跑没完成的。
- 评测脚本的 Key 读的是 `.env`。
