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

## 专题识别模型

`data/topic_model.json.gz` 是离线训练后导出的专题识别模型：词表、idf、各专题的系数和两个阈值，不含任何训练题目，`mingti/topic_model.py` 只用标准库读取和计算。训练数据是讲义题库，不随仓库发布；换了训练数据或者改了文字预处理（`normalize`），要重新导出这个文件，并保证两边的 `normalize` 一致。
