"""
Логика расчёта баллов оценки.
Воспроизводит формулы из logic_summary.md §3.
"""
from __future__ import annotations
from app.models import Checklist, EvaluationItem


def calculate_scores(
    items: list[EvaluationItem] | list[tuple],
    checklist: Checklist,
) -> tuple[float, dict[int, float]]:
    """
    Считает итоговый балл и баллы по блокам.

    items — список EvaluationItem или (criterion_id, value, comment).
    N/A исключается из числителя и знаменателя.

    Возвращает (total_score 0-100, {block_id: score_pct}).
    """
    # Нормализуем формат items → {criterion_id: value}
    value_by_crit: dict[int, str] = {}
    for item in items:
        if isinstance(item, EvaluationItem):
            value_by_crit[item.criterion_id] = item.value
        else:
            # tuple: (criterion_id, value, comment)
            value_by_crit[item[0]] = item[1]

    # Автофейл: если критерий с флагом получает "no" → итог 0
    if getattr(checklist, 'autofail_enabled', False):
        for block in checklist.blocks:
            for crit in block.criteria:
                if getattr(crit, 'is_autofail', False):
                    if value_by_crit.get(crit.id) == "no":
                        return 0.0, {}

    total_num = 0.0
    total_den = 0.0
    block_scores: dict[int, float] = {}

    for block in checklist.blocks:
        b_num = 0.0
        b_den = 0.0
        for crit in block.criteria:
            val = value_by_crit.get(crit.id, "na")
            if val == "na":
                continue
            b_den += crit.weight
            score_type = getattr(crit, 'score_type', 'binary')
            if score_type == 'range':
                try:
                    score_max = getattr(crit, 'score_max', 5) or 5
                    b_num += (int(val) / score_max) * crit.weight
                except (ValueError, TypeError):
                    b_den -= crit.weight  # отменяем добавление в знаменатель
            else:
                if val == "yes":
                    b_num += crit.weight
        block_scores[block.id] = round(b_num / b_den * 100, 1) if b_den > 0 else 0.0

    calculation = getattr(checklist, 'calculation', 'weighted')
    if calculation == "average":
        valid_scores = [s for s in block_scores.values() if s is not None]
        total_score = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else 0.0
    else:
        # Взвешенная сумма по весам блоков
        w_num = 0.0
        w_den = 0.0
        for block in checklist.blocks:
            if block.id in block_scores:
                w_num += block_scores[block.id] * block.weight
                w_den += block.weight
        total_score = round(w_num / w_den, 1) if w_den > 0 else 0.0

    return total_score, block_scores


MONTH_NAMES = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def score_color(score: float) -> str:
    """Bootstrap цвет (danger/warning/success) по значению 0-100."""
    if score < 40:
        return "danger"
    if score < 60:
        return "warning"
    return "success"
