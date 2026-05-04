"""
Скрипт создания тестовой оценки из выгрузки Qolio.
Сотрудник: Балова Екатерина, Отдел продаж 1.
Чек-лист: Чек-лист ОП.
"""
import sys
import os
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import joinedload
from app.database import SessionLocal
from app.models import Checklist, Block, Criterion, Evaluation, EvaluationItem, User
from app.scoring import calculate_scores, MONTH_NAMES

CHECKLIST_NAME = "Чек-лист ОП"
OPERATOR_NAME = "Балова Екатерина"
DEPARTMENT = "Отдел продаж 1"
EVAL_DATE = datetime(2025, 3, 15)
STAGE = "в работе"
GENERAL_COMMENT = "Тестовая оценка на основе выгрузки Qolio март 2025"
FIRST_CRIT_COMMENT = "Тест из выгрузки Qolio"


def main():
    db = SessionLocal()
    try:
        # 1. Найти чек-лист
        cl = (
            db.query(Checklist)
            .options(joinedload(Checklist.blocks).joinedload(Block.criteria))
            .filter(Checklist.name == CHECKLIST_NAME)
            .first()
        )
        if not cl:
            print(f"Чек-лист «{CHECKLIST_NAME}» не найден. Сначала запустите seed_op_checklist.py")
            return

        # 2. Найти первого пользователя
        user = db.query(User).filter(User.id == 1).first()
        if not user:
            user = db.query(User).first()
        if not user:
            print("Нет ни одного пользователя в БД")
            return

        print(f"Чек-лист: {cl.name} (id={cl.id})")
        print(f"Пользователь: {user.username} (id={user.id})")

        # 3. Подготовить items: все "yes", первый критерий каждого блока с комментарием
        items_raw = []
        for block in cl.blocks:
            for crit_idx, crit in enumerate(block.criteria):
                value = "yes"
                comment = FIRST_CRIT_COMMENT if crit_idx == 0 else ""
                items_raw.append((crit.id, value, comment))

        # 4. Посчитать итоговый балл
        total_score, _ = calculate_scores(items_raw, cl)

        # 5. Создать Evaluation
        evaluation = Evaluation(
            checklist_id=cl.id,
            operator_name=OPERATOR_NAME,
            department=DEPARTMENT,
            eval_date=EVAL_DATE,
            week_num=EVAL_DATE.isocalendar()[1],
            week_year=EVAL_DATE.year,
            month=MONTH_NAMES[EVAL_DATE.month - 1],
            stage=STAGE,
            total_score=total_score,
            evaluator_id=user.id,
            general_comment=GENERAL_COMMENT,
        )
        db.add(evaluation)
        db.flush()

        # 6. Создать EvaluationItem для каждого критерия
        for crit_id, value, comment in items_raw:
            db.add(EvaluationItem(
                evaluation_id=evaluation.id,
                criterion_id=crit_id,
                value=value,
                comment=comment or None,
            ))

        db.commit()
        db.refresh(evaluation)
        print(f"Оценка создана. ID = {evaluation.id}")
        print(f"Оператор: {OPERATOR_NAME}, Отдел: {DEPARTMENT}")
        print(f"Дата: {EVAL_DATE.strftime('%Y-%m-%d')}, Стадия: {STAGE}")
        print(f"Итоговый балл: {total_score:.1f}%")
        print(f"Количество критериев: {len(items_raw)}")
        return evaluation.id
    finally:
        db.close()


if __name__ == "__main__":
    main()
