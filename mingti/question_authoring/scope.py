from __future__ import annotations

import re


# Terms that unambiguously identify post-secondary mathematics in this product.
# Keep this list narrow: the boundary must not reject legitimate high-school
# enrichment merely because it uses sophisticated wording.
UNIVERSITY_ONLY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"傅里叶(?:级数|变换|分析)", "傅里叶分析"),
    (r"拉普拉斯变换", "拉普拉斯变换"),
    (r"马尔可夫链", "马尔可夫链"),
    (r"随机过程", "随机过程"),
    (r"测度论", "测度论"),
    (r"泛函分析", "泛函分析"),
    (r"实变函数", "实变函数"),
    (r"复变函数", "复变函数"),
    (r"偏微分方程", "偏微分方程"),
    (r"抽象代数", "抽象代数"),
    (r"(?:群|环|域)的同态", "抽象代数同态"),
    (r"勒贝格(?:积分|测度)", "勒贝格理论"),
)


def university_only_terms(*values: str) -> list[str]:
    combined = "\n".join(value for value in values if value)
    return [
        label
        for pattern, label in UNIVERSITY_ONLY_PATTERNS
        if re.search(pattern, combined, flags=re.IGNORECASE)
    ]


# 高中课程里不学、标准答案里不能用的解题方法。题目可以有"高观点"背景，但解析只能用高中方法。
# 只列明确越界的写法：单独的"积分"可能是比赛积分，"调和级数"常被当作 1+1/2+…+1/n 的名字，都不算。
BEYOND_CURRICULUM_METHODS: tuple[tuple[str, str], ...] = (
    (r"∫|\\i{1,3}nt(?![a-z])|定积分|不定积分|积分(?:比较|估计|放缩|中值|上限|下限|区间|求和|不等式)"
     r"|牛顿\s*[-—–]?\s*莱布尼[茨兹]|微积分基本定理", "定积分"),
    (r"洛必达|L'?\s*H[oô]spital", "洛必达法则"),
    (r"泰勒(?:展开|公式|级数|多项式)|麦克劳林", "泰勒展开"),
    (r"拉格朗日中值|柯西中值|罗尔定理", "微分中值定理"),
    (r"夹逼(?:定理|准则)", "夹逼定理"),
    (r"(?:无穷|幂)级数|级数(?:收敛|发散)", "级数"),
    (r"向量积|叉积|叉乘|外积", "向量积（叉乘）"),
    (r"矩阵|行列式", "矩阵与行列式"),
    (r"琴生不等式|Jensen", "琴生不等式"),
)

# 写进解题员的要求里，从源头避免；查出来退回重写时也用这段话说明该怎么改
HIGH_SCHOOL_METHODS = (
    "解析只能用中国普通高中数学课程里的方法：不得使用定积分、洛必达法则、泰勒展开、微分中值定理、"
    "夹逼定理、级数、向量积（叉乘）、矩阵与行列式、琴生不等式等。"
    "估计和式、比较大小时，用构造函数并求导、ln(1+x)<x 一类常用不等式和放缩；"
    "求平面法向量用待定系数列方程组。"
)


def beyond_curriculum_methods(*values: str) -> list[str]:
    combined = "\n".join(value for value in values if value)
    return [
        label
        for pattern, label in BEYOND_CURRICULUM_METHODS
        if re.search(pattern, combined, flags=re.IGNORECASE)
    ]


def scope_violations(*values: str) -> list[str]:
    """大学才有的概念和高中不用的解法合在一起查。出题流程里每一处范围检查都用它，
    和交付前的最终检查（authoring._validate_high_school_scope）是同一套规则，流程里放过的，最后也不会拦下。"""
    return university_only_terms(*values) + beyond_curriculum_methods(*values)
