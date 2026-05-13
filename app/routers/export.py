from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user
from app.models import Block as BlockModel, Checklist, Evaluation, User
from app.scoring import calculate_scores

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Side, Border
from openpyxl.utils import get_column_letter

router = APIRouter(prefix="/export")

# ── Стили (создаём один раз) ──────────────────────────────────────────────────

_H_FONT  = Font(bold=True, color="FFFFFF", size=10)
_H_FILL  = PatternFill("solid", fgColor="2C3E50")
_H_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)

_FILL_GREEN  = PatternFill("solid", fgColor="D5F5E3")
_FILL_YELLOW = PatternFill("solid", fgColor="FEF9E7")
_FILL_RED    = PatternFill("solid", fgColor="FADBD8")
_FILL_GRAY   = PatternFill("solid", fgColor="F2F3F4")

_FILL_YES = PatternFill("solid", fgColor="D5F5E3")
_FILL_NO  = PatternFill("solid", fgColor="FADBD8")
_FILL_NA  = PatternFill("solid", fgColor="F2F3F4")

_CENTER = Alignment(horizontal="center", vertical="center")


def _score_fill(score):
    if score is None:   return _FILL_GRAY
    if score >= 60:     return _FILL_GREEN
    if score >= 40:     return _FILL_YELLOW
    return _FILL_RED


def _write_header(ws, cols: list[str]):
    ws.append(cols)
    for cell in ws[1]:
        cell.font   = _H_FONT
        cell.fill   = _H_FILL
        cell.alignment = _H_ALIGN
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"


def _autowidth(ws, min_w=8, max_w=38):
    for col_cells in ws.columns:
        letter = get_column_letter(col_cells[0].column)
        w = max(len(str(c.value or "")) for c in col_cells)
        ws.column_dimensions[letter].width = min(max(w + 2, min_w), max_w)


def _result_label(stage):
    return {"сделка успешна": "Успешна",
            "не смог продать": "Не продал",
            "в работе": "В работе"}.get(stage or "", stage or "—")


def _val_label(val):
    return {"yes": "Да", "no": "Нет", "na": "Н/П"}.get(val or "", "—")


# ── Общий запрос ─────────────────────────────────────────────────────────────

def _load_evaluations(db: Session, operator="", checklist_id="", department="",
                      date_from="", date_to="", eval_status="published"):
    q = (
        db.query(Evaluation)
        .options(
            joinedload(Evaluation.evaluator),
            joinedload(Evaluation.items),
            joinedload(Evaluation.checklist).joinedload(Checklist.blocks),
        )
        .filter(Evaluation.status == (eval_status or "published"))
    )
    if operator:
        q = q.filter(Evaluation.operator_name.ilike(f"%{operator}%"))
    if checklist_id:
        q = q.filter(Evaluation.checklist_id == int(checklist_id))
    if department:
        q = q.filter(Evaluation.department == department)
    if date_from:
        try:
            q = q.filter(Evaluation.eval_date >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(Evaluation.eval_date <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            pass
    evs = q.order_by(Evaluation.eval_date.desc().nullslast(), Evaluation.id.desc()).all()

    # Догружаем критерии (нужны для листа 2)
    cl_ids = {ev.checklist_id for ev in evs if ev.checklist_id}
    if cl_ids:
        full_cls = {
            cl.id: cl for cl in
            db.query(Checklist)
              .filter(Checklist.id.in_(cl_ids))
              .options(joinedload(Checklist.blocks).joinedload(BlockModel.criteria))
              .all()
        }
        for ev in evs:
            if ev.checklist_id in full_cls:
                ev.checklist = full_cls[ev.checklist_id]

    return evs


def _ordered_blocks(evs):
    """[(block_id, checklist_name, block_display_name)] в порядке появления."""
    seen = {}
    for ev in evs:
        if not ev.checklist:
            continue
        for b in ev.checklist.blocks:
            if b.id not in seen:
                seen[b.id] = (b.id, ev.checklist.name, b.display_name or b.name)
    return list(seen.values())


def _multi_cl(blocks):
    return len({cl for _, cl, _ in blocks}) > 1


# ── Лист 1: Детальные оценки ─────────────────────────────────────────────────

def _sheet_detail(wb, evs, blocks):
    ws = wb.create_sheet("Детальные оценки")
    multi = _multi_cl(blocks)
    block_hdrs = [f"{cl} / {bn}" if multi else bn for _, cl, bn in blocks]

    _write_header(ws, [
        "#", "Дата звонка", "Дата оценки", "Сотрудник", "Отдел",
        "Сделка", "Чек-лист", "Оценщик", "Итог %", "Результат", "Комментарий",
    ] + block_hdrs)

    SCORE_COL   = 9
    BLOCK_START = 12

    for ev in evs:
        _, bscores = calculate_scores(ev.items, ev.checklist) if ev.checklist else (None, {})
        block_vals = [bscores.get(bid) for bid, _, _ in blocks]

        ws.append([
            ev.id,
            ev.eval_date.strftime("%d.%m.%Y") if ev.eval_date else "—",
            (ev.updated_at or ev.created_at).strftime("%d.%m.%Y") if (ev.updated_at or ev.created_at) else "—",
            ev.operator_name,
            ev.department or "—",
            f"#{ev.deal_id}" if ev.deal_id else "—",
            ev.checklist.name if ev.checklist else "—",
            (ev.evaluator.full_name or ev.evaluator.username) if ev.evaluator else "—",
            ev.total_score,
            _result_label(ev.stage),
            ev.general_comment or "",
        ] + block_vals)

        ri = ws.max_row
        # Красим только ячейки со скором
        ws.cell(ri, SCORE_COL).fill = _score_fill(ev.total_score)
        ws.cell(ri, SCORE_COL).alignment = _CENTER
        for i, bv in enumerate(block_vals):
            c = ws.cell(ri, BLOCK_START + i)
            c.fill = _score_fill(bv)
            c.alignment = _CENTER

    _autowidth(ws)
    ws.column_dimensions["K"].width = 42


# ── Лист 2: По критериям ─────────────────────────────────────────────────────

def _sheet_criteria(wb, evs):
    ws = wb.create_sheet("По критериям")
    _write_header(ws, [
        "# оценки", "Дата звонка", "Дата оценки", "Сотрудник", "Отдел",
        "Сделка", "Чек-лист", "Оценщик",
        "Блок", "Критерий", "Значение", "Комментарий",
    ])

    VAL_COL = 11

    for ev in evs:
        if not ev.checklist:
            continue
        item_map = {it.criterion_id: it for it in ev.items}
        prefix = [
            ev.id,
            ev.eval_date.strftime("%d.%m.%Y") if ev.eval_date else "—",
            (ev.updated_at or ev.created_at).strftime("%d.%m.%Y") if (ev.updated_at or ev.created_at) else "—",
            ev.operator_name,
            ev.department or "—",
            f"#{ev.deal_id}" if ev.deal_id else "—",
            ev.checklist.name,
            (ev.evaluator.full_name or ev.evaluator.username) if ev.evaluator else "—",
        ]
        for block in ev.checklist.blocks:
            for crit in block.criteria:
                item = item_map.get(crit.id)
                val  = item.value   if item else None
                comm = item.comment if item else ""
                ws.append(prefix + [
                    block.display_name or block.name,
                    crit.text,
                    _val_label(val),
                    comm or "",
                ])
                ri = ws.max_row
                vc = ws.cell(ri, VAL_COL)
                vc.alignment = _CENTER
                if val == "yes":   vc.fill = _FILL_YES
                elif val == "no":  vc.fill = _FILL_NO
                elif val == "na":  vc.fill = _FILL_NA

    _autowidth(ws)
    ws.column_dimensions["J"].width = 48
    ws.column_dimensions["L"].width = 42


# ── Лист 3: Сводка по сотрудникам ────────────────────────────────────────────

def _sheet_summary(wb, evs, blocks):
    ws = wb.create_sheet("Сводка по сотрудникам")
    multi = _multi_cl(blocks)
    block_hdrs = [f"{cl} / {bn}" if multi else bn for _, cl, bn in blocks]

    _write_header(ws, [
        "Сотрудник", "Отдел", "Кол-во оценок", "Средний балл %",
        "Зелёных ≥60%", "Жёлтых 40–60%", "Красных <40%",
    ] + block_hdrs)

    SCORE_COL   = 4
    BLOCK_START = 8

    emp = defaultdict(lambda: {"dept": "—", "scores": [], "bscores": defaultdict(list)})

    for ev in evs:
        if ev.total_score is None or not ev.checklist:
            continue
        k = ev.operator_name
        emp[k]["dept"] = ev.department or "—"
        emp[k]["scores"].append(ev.total_score)
        _, bscores = calculate_scores(ev.items, ev.checklist)
        for bid, s in bscores.items():
            if s is not None:
                emp[k]["bscores"][bid].append(s)

    for name in sorted(emp):
        d = emp[name]
        sc = d["scores"]
        n  = len(sc)
        avg    = round(sum(sc) / n, 1) if n else None
        green  = round(sum(1 for s in sc if s >= 60) / n * 100, 1) if n else 0
        yellow = round(sum(1 for s in sc if 40 <= s < 60) / n * 100, 1) if n else 0
        red    = round(sum(1 for s in sc if s < 40) / n * 100, 1) if n else 0

        bavgs = []
        for bid, _, _ in blocks:
            bs = d["bscores"].get(bid, [])
            bavgs.append(round(sum(bs) / len(bs), 1) if bs else None)

        ws.append([name, d["dept"], n, avg, green, yellow, red] + bavgs)
        ri = ws.max_row
        ws.cell(ri, SCORE_COL).fill = _score_fill(avg)
        ws.cell(ri, SCORE_COL).alignment = _CENTER
        for i, bv in enumerate(bavgs):
            c = ws.cell(ri, BLOCK_START + i)
            c.fill = _score_fill(bv)
            c.alignment = _CENTER

    _autowidth(ws)


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.get("")
def export_xlsx(
    operator: str = "",
    checklist_id: str = "",
    department: str = "",
    date_from: str = "",
    date_to: str = "",
    eval_status: str = "published",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    evs = _load_evaluations(
        db, operator=operator, checklist_id=checklist_id,
        department=department, date_from=date_from, date_to=date_to,
        eval_status=eval_status,
    )
    blocks = _ordered_blocks(evs)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _sheet_detail(wb, evs, blocks)
    _sheet_criteria(wb, evs)
    _sheet_summary(wb, evs, blocks)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = f"callreview_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
