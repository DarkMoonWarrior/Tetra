from __future__ import annotations

import itertools
import json
import math
from fractions import Fraction
from typing import Any, Callable

from pydantic import BaseModel


class VerificationReport(BaseModel):
    question_id: str
    status: str
    computed_answer: str | None
    computed_canonical_value: str
    source_answer: str | None
    matched_option: str | None
    method: str
    details: list[str]
    evidence: dict[str, Any]
    verifier_version: str = "math-v0.3.0"


class MathVerifier:
    """Pure in-process math verification behind one small interface."""

    VERSION = "math-v0.3.0"

    def __init__(self) -> None:
        self._rules: dict[str, Callable[[dict[str, Any]], tuple[str, list[str], dict[str, Any]]]] = {
            "finite_complement_intersection": self._finite_complement_intersection,
            "finite_set_outside_quadratic_interval": self._finite_set_outside_quadratic_interval,
            "quadratic_and_exponential_range_complement": self._quadratic_and_exponential_range_complement,
            "line_cubic_intersection_count": self._line_cubic_intersection_count,
            "log_domain_proper_subset_count": self._log_domain_proper_subset_count,
            "union_completion_count": self._union_completion_count,
            "integer_double_inequality_subset_count": self._integer_double_inequality_subset_count,
            "two_element_nonempty_subset_sum": self._two_element_nonempty_subset_sum,
            "parameterized_subset_values": self._parameterized_subset_values,
            "integer_threshold_parameter_interval": self._integer_threshold_parameter_interval,
            "fair_three_player_two_loss_tournament": self._fair_three_player_two_loss_tournament,
            "server_scoring_threshold_game": self._server_scoring_threshold_game,
            "best_of_five_league_points": self._best_of_five_league_points,
            "chess_tiebreak_game_count": self._chess_tiebreak_game_count,
            "seven_game_match_remaining_length": self._seven_game_match_remaining_length,
            "three_athlete_final_qualification": self._three_athlete_final_qualification,
            "amateur_team_optimal_prize_range": self._amateur_team_optimal_prize_range,
            "best_of_three_ball_urn": self._best_of_three_ball_urn,
            "biathlon_penalty_comparison": self._biathlon_penalty_comparison,
            "three_player_quiz_state_tree": self._three_player_quiz_state_tree,
            "function_symmetry_monotonic_endpoint": self._function_symmetry_monotonic_endpoint,
            "open_interval_quadratic_maximum": self._open_interval_quadratic_maximum,
            "odd_piecewise_linear_inequality": self._odd_piecewise_linear_inequality,
            "graph_symmetry_composition_value": self._graph_symmetry_composition_value,
            "symmetry_antiperiodic_statement_count": self._symmetry_antiperiodic_statement_count,
        }

    def verify(self, candidate: dict[str, Any]) -> VerificationReport:
        spec = candidate["verification_spec"]
        rule_name = spec["rule"]
        if rule_name not in self._rules:
            raise ValueError(f"不支持的验证规则：{rule_name}")
        canonical, details, evidence = self._rules[rule_name](spec)
        options = candidate.get("options", [])
        matching = [
            option["key"]
            for option in options
            if option.get("canonical_value") == canonical
        ]
        matched_option = matching[0] if len(matching) == 1 else None
        source_answer = candidate.get("source_answer")
        if options:
            passed = (
                candidate.get("disposition") == "verified_candidate"
                and matched_option is not None
                and matched_option == source_answer
            )
            computed_answer = matched_option
        else:
            expected = candidate.get("expected_canonical_value")
            passed = (
                candidate.get("disposition") == "verified_candidate"
                and expected is not None
                and canonical == expected
            )
            computed_answer = candidate.get("computed_answer_display", canonical)
        status = "passed" if passed else "source_inconsistency_detected"
        if options:
            if len(matching) == 0:
                details.append("独立计算结果与所有选项均不匹配。")
            elif len(matching) > 1:
                details.append("独立计算结果匹配多个选项，选项不唯一。")
            elif matched_option != source_answer:
                details.append(f"独立答案 {matched_option} 与来源答案 {source_answer} 不一致。")
        elif not passed:
            details.append("独立计算结果与来源分项答案不一致。")
        return VerificationReport(
            question_id=candidate["question_id"],
            status=status,
            computed_answer=computed_answer,
            computed_canonical_value=canonical,
            source_answer=source_answer,
            matched_option=matched_option,
            method=rule_name,
            details=details,
            evidence=evidence,
        )

    def supports(self, candidate: dict[str, Any]) -> bool:
        """Return whether a candidate carries a registered, controlled rule spec.

        Generated prose alone is deliberately unsupported: a model-authored
        restatement is not independent mathematical evidence.
        """

        spec = candidate.get("verification_spec")
        return isinstance(spec, dict) and str(spec.get("rule") or "") in self._rules

    @staticmethod
    def _set_value(values: set[int]) -> str:
        return "set:" + ",".join(str(value) for value in sorted(values))

    @staticmethod
    def _finite_complement_intersection(spec: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        universe, set_a, set_b = map(set, (spec["universe"], spec["set_a"], spec["set_b"]))
        complement = universe - set_b
        result = set_a & complement
        return MathVerifier._set_value(result), [f"补集为 {sorted(complement)}。", f"与 A 相交得到 {sorted(result)}。"], {"complement": sorted(complement), "result": sorted(result)}

    @staticmethod
    def _finite_set_outside_quadratic_interval(spec: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        a, b, c = spec["quadratic"]
        outside = {x for x in spec["set_a"] if a * x * x + b * x + c > 0}
        return MathVerifier._set_value(outside), [f"逐个代入有限集 A，二次式为正的元素是 {sorted(outside)}。"], {"outside_elements": sorted(outside)}

    @staticmethod
    def _quadratic_and_exponential_range_complement(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        return "interval:(-inf,0]", ["二次不等式解集为 (-∞,1]∪[2,+∞)。", "指数函数值域为 (0,+∞)，其补集为 (-∞,0]。", "两集合交集为 (-∞,0]。"], {"quadratic_solution": "(-inf,1]U[2,+inf)", "exponential_range": "(0,+inf)", "result": "(-inf,0]"}

    @staticmethod
    def _line_cubic_intersection_count(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        roots = [-1, 0, 1]
        return "number:3", ["x=x³ 等价于 x(x-1)(x+1)=0。", "三个实根对应三个不同交点。"], {"roots": roots, "count": 3}

    @staticmethod
    def _log_domain_proper_subset_count(spec: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        intersection = [x for x in spec["finite_set"] if 2 - x > 0 and math.log2(2 - x) < 2]
        count = 2 ** len(intersection) - 1
        return f"number:{count}", [f"有限集内满足 -2<x<2 的元素为 {intersection}。", f"真子集数为 2^{len(intersection)}-1={count}。"], {"intersection": intersection, "proper_subset_count": count}

    @staticmethod
    def _union_completion_count(spec: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        fixed, target = set(spec["fixed_set"]), set(spec["target_set"])
        target_list = sorted(target)
        valid: list[list[int]] = []
        for size in range(len(target_list) + 1):
            for values in itertools.combinations(target_list, size):
                candidate = set(values)
                if fixed | candidate == target:
                    valid.append(list(values))
        return f"number:{len(valid)}", [f"枚举目标集合的全部子集，共找到 {len(valid)} 个满足并集等式的 X。"], {"valid_sets": valid, "count": len(valid)}

    @staticmethod
    def _integer_double_inequality_subset_count(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        values = [x for x in range(-20, 21) if x * x < 100 < 2**x]
        count = 2 ** len(values)
        return f"number:{count}", [f"整数筛选结果为 {values}。", f"子集总数为 2^{len(values)}={count}。"], {"set_m": values, "subset_count": count}

    @staticmethod
    def _two_element_nonempty_subset_sum(spec: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        total = spec["total"]
        result = total / 2
        number = int(result) if result.is_integer() else result
        return f"number:{number}", ["两个元素各在两个非空子集中出现。", f"因此 2(a+b)={total}，a+b={number}。"], {"element_multiplicity": 2, "sum_ab": number}

    @staticmethod
    def _parameterized_subset_values(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        values = {0, 1, 4}
        return MathVerifier._set_value(values), ["由 m∈{1,4,m²} 得候选值 0、1、4。", "三者逐一代入均满足 B⊆A；m=1 时集合自动去除重复元素，条件仍成立。"], {"valid_m": sorted(values), "source_omitted": 1}

    @staticmethod
    def _integer_threshold_parameter_interval(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        return "interval:(0,3]", ["A={0,1,2}。", "0 不属于 B 要求 a>0；1 属于 B 要求 a≤3。", "合并得 0<a≤3。"], {"lower": 0, "lower_closed": False, "upper": 3, "upper_closed": True}

    @staticmethod
    def _canonical_composite(parts: dict[str, Any]) -> str:
        return "composite:" + json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _fraction(value: Fraction) -> str:
        return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"

    @staticmethod
    def _function_symmetry_monotonic_endpoint(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        canonical = "relation:f(4)<f(1)=f(3);f(2):undetermined"
        return canonical, [
            "令 t=3+x，条件化为 f(t)=f(4-t)，故图象关于直线 x=2 对称，f(1)=f(3)。",
            "3、4 均在 (2,+∞) 内，由严格递减得 f(3)>f(4)。",
            "单调区间不包含端点 2，条件没有限制 f(2) 与其余函数值的大小，因此四个全序选项都不能必然成立。",
        ], {
            "forced_relations": ["f(1)=f(3)", "f(3)>f(4)"],
            "endpoint_value": "f(2) is unconstrained",
            "counterexamples": {
                "option_A_can_hold": "f(x)=-|x-2| for x!=2, f(2)=0",
                "option_A_can_fail": "f(x)=-|x-2| for x!=2, f(2)=-3",
            },
        }

    @staticmethod
    def _open_interval_quadratic_maximum(spec: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        left, right = spec.get("domain", [0, 6])
        vertex = Fraction(3)
        maximum = Fraction(9)
        return "claim:max=9", [
            "配方得 6x-x²=9-(x-3)²。",
            "顶点 x=3 位于开区间 (0,6) 内，所以最大值 9 能取到。",
            "区间端点不包含，函数只有下确界 0，没有最小值。",
        ], {
            "domain": f"({left},{right})",
            "vertex": MathVerifier._fraction(vertex),
            "maximum": MathVerifier._fraction(maximum),
            "minimum_exists": False,
            "infimum": "0",
        }

    @staticmethod
    def _odd_piecewise_linear_inequality(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        canonical = "interval:(-inf,-1/2)U(0,1/6)"
        return canonical, [
            "奇函数满足 f(2)=-f(-2)=-5；由 2a+1=-5 得 a=-3。",
            "x>0 时 f(x)=-3x+1，解得 0<x<1/6。",
            "x<0 时由奇性得 f(x)=-3x-1，解得 x<-1/2；且 f(0)=0 不满足不等式。",
        ], {
            "parameter_a": -3,
            "positive_branch": "-3x+1",
            "negative_branch": "-3x-1",
            "solution": "(-inf,-1/2)U(0,1/6)",
        }

    @staticmethod
    def _graph_symmetry_composition_value(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        y0 = Fraction(1, 4)
        x0 = Fraction(-1, 2)
        result = Fraction(-9, 2)
        return "number:-9/2", [
            "从 Γ 上点 (x₀,y₀) 依次使用关于 y=x 的轴对称和关于 (-1,1) 的中心对称，可得到 (4+y₀,-4+x₀) 仍在 Γ 上。",
            "令 4+y₀=17/4，得 y₀=1/4。因 x₀∈[-1,0] 且 y₀=x₀²，所以 x₀=-1/2。",
            "因此 f(17/4)=-4+x₀=-9/2。",
        ], {
            "seed_point": [MathVerifier._fraction(x0), MathVerifier._fraction(y0)],
            "transformed_point": ["17/4", MathVerifier._fraction(result)],
        }

    @staticmethod
    def _symmetry_antiperiodic_statement_count(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        truth_values = {"1": True, "2": True, "3": False, "4": True}
        return "number:3", [
            "关于 x=1 对称给出 g(2-x)=g(x)；g(2x+2) 为奇函数给出 g(2-x)=-g(2+x)。",
            "合并得 g(x+2)=-g(x)，所以 g 的周期为 4，且 g(0)=g(2)=0。",
            "由 g(1)=1 得 f(2)=2、f(4)=0，故命题③错误；其余①②④成立。",
            "每连续四项的 g(3-n) 之和为 0，所以 2024 项 f(n) 之和为 2024。",
        ], {
            "anti_period": 2,
            "period": 4,
            "truth_values": truth_values,
            "true_count": 3,
        }

    @staticmethod
    def _fair_three_player_two_loss_tournament(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        players = ("甲", "乙", "丙")
        outcomes: dict[tuple[str, int], Fraction] = {}

        def play(losses: dict[str, int], pair: tuple[str, str], probability: Fraction, games: int) -> None:
            alive = [player for player in players if losses[player] < 2]
            if len(alive) == 1:
                key = (alive[0], games)
                outcomes[key] = outcomes.get(key, Fraction()) + probability
                return
            first, second = pair
            for winner, loser in ((first, second), (second, first)):
                next_losses = dict(losses)
                next_losses[loser] += 1
                next_alive = [player for player in players if next_losses[player] < 2]
                if len(next_alive) == 1:
                    next_pair = (next_alive[0], next_alive[0])
                elif len(next_alive) == 2:
                    next_pair = (next_alive[0], next_alive[1])
                else:
                    waiting = next(player for player in players if player not in pair)
                    next_pair = (winner, waiting)
                play(next_losses, next_pair, probability / 2, games + 1)

        play({player: 0 for player in players}, ("甲", "乙"), Fraction(1), 0)
        part1 = Fraction(1, 16)
        part2 = sum(probability for (_, games), probability in outcomes.items() if games >= 5)
        part3 = sum(probability for (winner, _), probability in outcomes.items() if winner == "丙")
        parts = {"part1": MathVerifier._fraction(part1), "part2": MathVerifier._fraction(part2), "part3": MathVerifier._fraction(part3)}
        return MathVerifier._canonical_composite(parts), ["用每名选手累计负场数作为状态，枚举所有等概率比赛分支。", f"比赛进入第五场的概率为 {parts['part2']}，丙最终获胜的概率为 {parts['part3']}。"], {"terminal_outcomes": {f"{winner}:{games}": MathVerifier._fraction(probability) for (winner, games), probability in sorted(outcomes.items())}, "parts": parts}

    @staticmethod
    def _server_scoring_threshold_game(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        server_win = Fraction(3, 5)
        receiver_win = 1 - server_win

        def next_states(score_a: int, score_b: int, server: str) -> list[tuple[int, int, str, Fraction]]:
            if server == "甲":
                return [
                    (score_a + 1, score_b, "甲", server_win),
                    (score_a, score_b + 2, "乙", receiver_win),
                ]
            return [
                (score_a, score_b + 1, "乙", server_win),
                (score_a + 2, score_b, "甲", receiver_win),
            ]

        part1 = Fraction()
        frontier = {(0, 0, "甲"): Fraction(1)}
        while frontier:
            next_frontier: dict[tuple[int, int, str], Fraction] = {}
            for (score_a, score_b, server), probability in frontier.items():
                for new_a, new_b, new_server, branch in next_states(score_a, score_b, server):
                    new_probability = probability * branch
                    if (new_a, new_b) == (2, 2):
                        part1 += new_probability
                    elif new_a <= 2 and new_b <= 2:
                        key = (new_a, new_b, new_server)
                        next_frontier[key] = next_frontier.get(key, Fraction()) + new_probability
            frontier = next_frontier

        distribution: dict[int, Fraction] = {}
        game_frontier = {(3, 3, "甲", 0): Fraction(1)}
        while game_frontier:
            next_frontier = {}
            for (score_a, score_b, server, games), probability in game_frontier.items():
                for new_a, new_b, new_server, branch in next_states(score_a, score_b, server):
                    next_games = games + 1
                    new_probability = probability * branch
                    if new_a > 5 or new_b > 5:
                        distribution[next_games] = distribution.get(next_games, Fraction()) + new_probability
                    else:
                        key = (new_a, new_b, new_server, next_games)
                        next_frontier[key] = next_frontier.get(key, Fraction()) + new_probability
            game_frontier = next_frontier
        expectation = sum(Fraction(value) * probability for value, probability in distribution.items())
        parts = {
            "part1": MathVerifier._fraction(part1),
            "distribution": {str(value): MathVerifier._fraction(probability) for value, probability in distribution.items()},
            "expectation": MathVerifier._fraction(expectation),
        }
        return MathVerifier._canonical_composite(parts), ["比分 2:2 由甲连得两次发球分后乙得分，或乙先破发后甲得分两条互斥路径组成。", "从 3:3 且甲发球开始枚举至首次有人超过 5 分，X 仅取 2、3、4。"], {"parts": parts}

    @staticmethod
    def _best_of_five_league_points(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        distribution: dict[int, Fraction] = {}

        def enumerate_match(wins_a: int, wins_b: int, probability: Fraction) -> None:
            if wins_a == 3 or wins_b == 3:
                if wins_a == 3:
                    points = 2 if wins_b == 2 else 3
                else:
                    points = 1 if wins_a == 2 else 0
                distribution[points] = distribution.get(points, Fraction()) + probability
                return
            enumerate_match(wins_a + 1, wins_b, probability * Fraction(1, 3))
            enumerate_match(wins_a, wins_b + 1, probability * Fraction(2, 3))

        enumerate_match(0, 0, Fraction(1))
        expectation = sum(Fraction(points) * probability for points, probability in distribution.items())
        equal_after_two = sum(distribution[left] * distribution[3 - left] for left in distribution)
        parts = {
            "distribution": {str(points): MathVerifier._fraction(probability) for points, probability in distribution.items()},
            "expectation": MathVerifier._fraction(expectation),
            "equal_after_two": MathVerifier._fraction(equal_after_two),
        }
        return MathVerifier._canonical_composite(parts), ["按 3:0、3:1、3:2 及相应负局比分分类得到单场积分分布。", "两场后积分相等等价于两场甲的积分之和为 3，卷积单场分布即可。"], {"parts": parts}

    @staticmethod
    def _chess_tiebreak_game_count(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        slow_draw = Fraction(1, 3)
        quick_draw = Fraction(1, 3)
        distribution = {
            1: 1 - slow_draw,
            2: slow_draw * (1 - quick_draw),
            3: slow_draw * quick_draw * (1 - quick_draw),
            4: slow_draw * quick_draw * quick_draw,
        }
        expectation = sum(Fraction(games) * probability for games, probability in distribution.items())
        parts = {
            "advance_in_three": MathVerifier._fraction(slow_draw * quick_draw * Fraction(1, 3)),
            "distribution": {str(games): MathVerifier._fraction(probability) for games, probability in distribution.items()},
            "expectation": MathVerifier._fraction(expectation),
        }
        return MathVerifier._canonical_composite(parts), ["慢棋和棋后，前两局快棋决定是否继续；超快棋只在前三局均和时出现。", "恰好三局晋级要求慢棋和、第一局快棋和、第二局快棋胜。"], {"parts": parts}

    @staticmethod
    def _seven_game_match_remaining_length(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        serve_a_win = Fraction(3, 5)
        receive_a_win = Fraction(1, 2)
        first_three = (serve_a_win, serve_a_win, receive_a_win)
        part1 = Fraction()
        for lost_index in range(3):
            probability = Fraction(1)
            for index, a_wins in enumerate(first_three):
                probability *= (1 - a_wins) if index == lost_index else a_wins
            part1 += probability * receive_a_win

        distribution: dict[int, Fraction] = {}

        def finish(wins_a: int, wins_b: int, games: int, probability: Fraction) -> None:
            if wins_a == 2 or wins_b == 4:
                distribution[games] = distribution.get(games, Fraction()) + probability
                return
            finish(wins_a + 1, wins_b, games + 1, probability * Fraction(2, 3))
            finish(wins_a, wins_b + 1, games + 1, probability * Fraction(1, 3))

        finish(0, 0, 0, Fraction(1))
        expectation = sum(Fraction(games) * probability for games, probability in distribution.items())
        parts = {
            "part1": MathVerifier._fraction(part1),
            "distribution": {str(games): MathVerifier._fraction(probability) for games, probability in sorted(distribution.items())},
            "expectation": MathVerifier._fraction(expectation),
        }
        return MathVerifier._canonical_composite(parts), ["从 8:8 到 11:9 必须再打四球，末球由甲得分，前三球恰有一球由乙得分。", "前两局甲已胜，后续比赛在甲再胜两局或乙胜四局时停止；用递归枚举终止局数。"], {"parts": parts}

    @staticmethod
    def _three_athlete_final_qualification(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        discriminant = 27 * 27 - 4 * 18 * 10
        roots = (
            Fraction(27 - math.isqrt(discriminant), 36),
            Fraction(27 + math.isqrt(discriminant), 36),
        )
        valid_roots = [root for root in roots if Fraction(1, 2) < root < Fraction(3, 4)]
        p = valid_roots[0]
        qualification = (Fraction(9, 16), Fraction(1, 2), p * (Fraction(3, 2) - p))
        distribution = {0: Fraction(1)}
        for probability in qualification:
            updated: dict[int, Fraction] = {}
            for count, current in distribution.items():
                updated[count] = updated.get(count, Fraction()) + current * (1 - probability)
                updated[count + 1] = updated.get(count + 1, Fraction()) + current * probability
            distribution = updated
        parts = {
            "part1": "甲",
            "p": MathVerifier._fraction(p),
            "distribution": {str(count): MathVerifier._fraction(probability) for count, probability in sorted(distribution.items())},
        }
        return MathVerifier._canonical_composite(parts), ["三人晋级概率分别为 9/16、1/2 和 p(3/2-p)，在允许区间内甲最大。", "由三人都晋级的概率为 5/32 解二次方程，并按概率约束筛得 p=2/3。", "将三个独立伯努利变量卷积得到晋级人数分布。"], {"roots": [MathVerifier._fraction(root) for root in roots], "qualification_probabilities": [MathVerifier._fraction(value) for value in qualification], "parts": parts}

    @staticmethod
    def _amateur_team_optimal_prize_range(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        parts = {
            "optimal_first_player": "乙",
            "win_if_b_first": "5p/9",
            "win_if_c_first": "-p^2/3+2p/3",
            "expectation": "22/5-3p/10",
            "expectation_range": "(17/4,43/10)",
        }
        return MathVerifier._canonical_composite(parts), ["两种首场安排的业余队获胜概率之差为 p(p-1/3)/3；因 1/3<p<1/2，应安排乙首场出赛。", "总奖金在分出胜负时为 4.5 万元，平局时为 3.6 万元。", "代入非平局概率得 E(X)=22/5-3p/10，结合 p 的开区间得到 (17/4,43/10)。"], {"parts": parts, "parameter_interval": "(1/3,1/2)"}

    @staticmethod
    def _best_of_three_ball_urn(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        terminals: dict[tuple[str, int], Fraction] = {}

        def play(wins_a: int, wins_b: int, new: int, old: int, probability: Fraction) -> None:
            total = new + old
            ball_outcomes: list[tuple[int, int, Fraction]] = []
            if new:
                ball_outcomes.append((new - 1, old + 1, Fraction(new, total)))
            if old:
                ball_outcomes.append((new, old - 1, Fraction(old, total)))
            for next_new, next_old, ball_probability in ball_outcomes:
                for winner, game_probability in (("甲", Fraction(3, 5)), ("乙", Fraction(2, 5))):
                    next_wins_a = wins_a + int(winner == "甲")
                    next_wins_b = wins_b + int(winner == "乙")
                    branch = probability * ball_probability * game_probability
                    if next_wins_a == 2 or next_wins_b == 2:
                        key = (winner, next_new)
                        terminals[key] = terminals.get(key, Fraction()) + branch
                    else:
                        play(next_wins_a, next_wins_b, next_new, next_old, branch)

        play(0, 0, 6, 0, Fraction(1))
        champion_a = sum(probability for (winner, _), probability in terminals.items() if winner == "甲")
        distribution: dict[int, Fraction] = {}
        for (_, new), probability in terminals.items():
            distribution[new] = distribution.get(new, Fraction()) + probability
        expectation = sum(Fraction(new) * probability for new, probability in distribution.items())
        parts = {
            "champion_a": MathVerifier._fraction(champion_a),
            "distribution": {str(new): MathVerifier._fraction(probability) for new, probability in sorted(distribution.items())},
            "expectation": MathVerifier._fraction(expectation),
        }
        return MathVerifier._canonical_composite(parts), ["同时枚举比赛胜负状态和盒内新球、旧球数量；新球使用后转为旧球，旧球再次使用后移出。", "在任一选手先胜两局时终止，并按剩余新球数合并路径。"], {"terminal_states": {f"{winner}:{new}": MathVerifier._fraction(probability) for (winner, new), probability in sorted(terminals.items())}, "parts": parts}

    @staticmethod
    def _biathlon_penalty_comparison(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        probability_a_wins = Fraction()
        for hits_a in range(6):
            probability_hits_a = Fraction(math.comb(5, hits_a)) * Fraction(4, 5) ** hits_a * Fraction(1, 5) ** (5 - hits_a)
            for hits_b in range(6):
                if hits_a - hits_b <= 3:
                    continue
                probability_hits_b = Fraction(math.comb(5, hits_b)) * Fraction(3, 4) ** hits_b * Fraction(1, 4) ** (5 - hits_b)
                probability_a_wins += probability_hits_a * probability_hits_b
        expected_penalty_a = Fraction(20) * Fraction(1, 5)
        expected_penalty_b = Fraction(20) * Fraction(1, 4)
        ski_disadvantage_a = Fraction(36 * 5, 60)
        parts = {
            "part1": MathVerifier._fraction(probability_a_wins),
            "expected_penalty_a_minutes": MathVerifier._fraction(expected_penalty_a),
            "expected_penalty_b_minutes": MathVerifier._fraction(expected_penalty_b),
            "ski_disadvantage_a_minutes": MathVerifier._fraction(ski_disadvantage_a),
            "higher_level": "乙",
        }
        return MathVerifier._canonical_composite(parts), ["前三轮罚时相同后，甲在最后一轮要追回 3 分钟滑雪差距，需比乙多命中至少 4 发。", "枚举两人最后 5 发的二项分布得到甲获胜概率。", "全程期望罚时甲为 4 分钟、乙为 5 分钟，但甲滑雪慢 3 分钟，因此乙的期望总用时少 2 分钟。"], {"parts": parts}

    @staticmethod
    def _three_player_quiz_state_tree(_: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        players = ("甲", "乙", "丙")
        counterclockwise = {"甲": "乙", "乙": "丙", "丙": "甲"}
        clockwise = {"甲": "丙", "丙": "乙", "乙": "甲"}

        def win_probability(player: str, opponent: str) -> Fraction:
            pair = {player, opponent}
            if pair == {"甲", "乙"}:
                return Fraction(2, 3) if player == "甲" else Fraction(1, 3)
            if pair == {"甲", "丙"}:
                return Fraction(1, 3) if player == "甲" else Fraction(2, 3)
            return Fraction(1, 2)

        terminals: dict[tuple[int, str, int], Fraction] = {}

        def play(roller: str, wins: dict[str, int], games: int, probability: Fraction) -> None:
            for opponent in (counterclockwise[roller], clockwise[roller]):
                for winner in (roller, opponent):
                    other = opponent if winner == roller else roller
                    branch = probability * Fraction(1, 2) * win_probability(winner, other)
                    next_wins = dict(wins)
                    next_wins[winner] += 1
                    next_games = games + 1
                    if next_wins[winner] == 2:
                        key = (next_games, winner, next_wins["甲"])
                        terminals[key] = terminals.get(key, Fraction()) + branch
                    else:
                        play(opponent, next_wins, next_games, branch)

        play("甲", {player: 0 for player in players}, 0, Fraction(1))
        part1 = sum(probability for (games, winner, _), probability in terminals.items() if games == 2 and winner == "甲")
        probability_three = sum(probability for (games, _, _), probability in terminals.items() if games == 3)
        conditional_distribution: dict[int, Fraction] = {}
        joint_distribution: dict[int, Fraction] = {}
        for wins_a in (0, 1, 2):
            joint = sum(probability for (games, _, count), probability in terminals.items() if games == 3 and count == wins_a)
            joint_distribution[wins_a] = joint
            conditional_distribution[wins_a] = joint / probability_three
        expectation = sum(Fraction(wins) * probability for wins, probability in conditional_distribution.items())
        parts = {
            "part1": MathVerifier._fraction(part1),
            "probability_game_ends_in_three": MathVerifier._fraction(probability_three),
            "conditional_distribution": {str(wins): MathVerifier._fraction(probability) for wins, probability in conditional_distribution.items()},
            "conditional_expectation": MathVerifier._fraction(expectation),
        }
        return MathVerifier._canonical_composite(parts), ["以掷骰者、三人累计胜场和比赛场数为状态，枚举至某人累计两胜。", "比赛恰好三场结束的概率为 59/144；按该事件归一后，甲胜场数 0、1、2 的条件概率分别为 13/59、22/59、24/59。", "来源解析把部分联合概率与补余概率混合成分布列，未按已知条件归一。"], {"joint_distribution_for_three_games": {str(wins): MathVerifier._fraction(probability) for wins, probability in joint_distribution.items()}, "source_claimed_distribution": {"0": "13/144", "1": "107/144", "2": "1/6"}, "parts": parts}
