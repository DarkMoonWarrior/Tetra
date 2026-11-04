"""Shared presentation contract for model-generated mathematical content."""

MATH_DISPLAY_INSTRUCTIONS = (
    "所有数学表达必须使用 KaTeX 兼容的 LaTeX：行内公式用单个 $...$ 包裹，"
    "需要单独成行的公式用 $$...$$ 包裹；中文说明必须放在公式分隔符之外，"
    "不要使用 Markdown 代码块，也不要把 LaTeX 命令作为未分隔的普通文字输出。"
)
