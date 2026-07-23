"""
Ежедневная синхронизация статусов сделок из Битрикс24.

Проходит по всем сделкам, у которых есть оценки (и по кэшу сделок), заново
запрашивает актуальную стадию и дату закрытия из Битрикс и обновляет:
  - deal_cache.stage / close_date / last_synced_at
  - evaluations.stage (у всех оценок этой сделки)

Так отчёты показывают текущий итог сделки, а не снимок на момент оценки.

Запуск:
  python3 scripts/sync_deal_stages.py --dry-run   # только показать, что изменится
  python3 scripts/sync_deal_stages.py             # применить изменения
"""
import argparse
import sys
from datetime import datetime, date, time

# Запуск из корня проекта
sys.path.insert(0, ".")

from app.database import SessionLocal
from app.models import Evaluation, DealCache
from app.bitrix import get_deal_stages_bulk


def _as_dt(d):
    if isinstance(d, datetime):
        return d
    if isinstance(d, date):
        return datetime.combine(d, time.min)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="только показать изменения, ничего не записывать")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        # 1. Собираем все deal_id из оценок и из кэша
        eval_deal_ids = {
            e.deal_id for e in db.query(Evaluation.deal_id).filter(Evaluation.deal_id.isnot(None)).all()
            if e.deal_id
        }
        cache_deal_ids = {c.deal_id for c in db.query(DealCache.deal_id).all() if c.deal_id}
        all_ids = sorted(eval_deal_ids | cache_deal_ids, key=lambda x: (len(x), x))
        print(f"Сделок к синхронизации: {len(all_ids)} "
              f"(в оценках: {len(eval_deal_ids)}, в кэше: {len(cache_deal_ids)})")

        # 2. Актуальные стадии из Битрикс (пакетно)
        print("Запрос актуальных стадий из Битрикс…")
        fresh = get_deal_stages_bulk(list(all_ids))
        print(f"Получено из Битрикс: {len(fresh)}")
        not_found = [d for d in all_ids if d not in fresh]
        if not_found:
            print(f"Не найдены в Битрикс (пропускаем): {len(not_found)}")

        # 3. Обновляем кэш
        cache_map = {c.deal_id: c for c in db.query(DealCache).all()}
        now = datetime.utcnow()
        cache_stage_changes = 0
        for did, info in fresh.items():
            dc = cache_map.get(did)
            if dc is None:
                dc = DealCache(deal_id=did)
                if not args.dry_run:
                    db.add(dc)
                cache_map[did] = dc
            if (dc.stage or "") != info["stage"]:
                cache_stage_changes += 1
            if not args.dry_run:
                dc.stage = info["stage"]
                dc.close_date = _as_dt(info["close_date"])
                dc.last_synced_at = now

        # 4. Обновляем стадию в оценках
        evals = db.query(Evaluation).filter(Evaluation.deal_id.isnot(None)).all()
        transitions = []  # (deal_id, старая стадия, новая стадия)
        eval_changes = 0
        for ev in evals:
            info = fresh.get(ev.deal_id)
            if not info:
                continue
            new_stage = info["stage"]
            if (ev.stage or "") != new_stage:
                transitions.append((ev.deal_id, ev.stage, new_stage))
                eval_changes += 1
                if not args.dry_run:
                    ev.stage = new_stage

        # 5. Итоги
        print("\n── Итог ─────────────────────────────────────────")
        print(f"Изменений стадии в кэше:   {cache_stage_changes}")
        print(f"Изменений стадии в оценках: {eval_changes}")

        # Сводка переходов по типам
        from collections import Counter
        trans_counter = Counter((old or "нет", new) for _, old, new in transitions)
        if trans_counter:
            print("\nПереходы (старая → новая) в оценках:")
            for (old, new), n in sorted(trans_counter.items(), key=lambda x: -x[1]):
                print(f"  {old!r:20} → {new!r:20}  ×{n}")

        # Особо важное: новые проваленные / новые успешные
        new_lost = [t for t in transitions if t[2] == "не смог продать"]
        if new_lost:
            print(f"\nНовые ПРОВАЛЕННЫЕ сделки ({len(new_lost)}):")
            for did, old, _ in new_lost[:30]:
                print(f"  сделка {did}: было {old!r} → провал")

        if args.dry_run:
            print("\n[DRY-RUN] Изменения НЕ записаны. Запустите без --dry-run, чтобы применить.")
            db.rollback()
        else:
            db.commit()
            print("\n[OK] Изменения записаны в базу.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
