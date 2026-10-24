"""Хранилище состояния (SQLite): заказы, обработанные события, очередь шагов.

Три таблицы решают три задачи:
  orders  источник правды по заказу (статус, номера сделки и счёта);
  events  уже обработанные события провайдера: повтор того же события ничего не меняет;
  steps   очередь внешних действий (CRM, письма): шаг записан до вызова и помечается выполненным после,
          поэтому сбой CRM не теряет работу, а откладывает её до следующей сверки.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref TEXT UNIQUE,
  lead_id TEXT UNIQUE NOT NULL,
  fingerprint TEXT NOT NULL,
  name TEXT NOT NULL, email TEXT NOT NULL,
  product TEXT NOT NULL, title TEXT NOT NULL,
  amount_kop INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'RUB',
  status TEXT NOT NULL DEFAULT 'NEW',
  crm_deal_id TEXT, invoice_id TEXT, pay_url TEXT, payment_id TEXT, receipt TEXT, access_url TEXT,
  created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, type TEXT, order_ref TEXT, outcome TEXT, received_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_ref TEXT NOT NULL, key TEXT NOT NULL, system TEXT NOT NULL, payload TEXT,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  UNIQUE(order_ref, key)
);
"""
ORDER_FIELDS = {'crm_deal_id', 'invoice_id', 'pay_url', 'payment_id', 'receipt', 'access_url'}


class Store:
    def __init__(self, path: str = ':memory:'):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    @contextmanager
    def tx(self):
        """Группа изменений целиком: либо всё записалось, либо ничего (вложенные вызовы tx не поддерживаются)."""
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            self.db.execute('COMMIT')

    def _one(self, sql: str, args: Iterable = ()) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: Iterable = ()) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, tuple(args)).fetchall()]

    # ------------------------------------------------------------------ заказы
    def create_order(self, lead_id: str, fingerprint: str, name: str, email: str, product: str, title: str,
                     amount_kop: int, steps: Iterable[Tuple[str, str]] = ()) -> Tuple[dict, bool]:
        """Возвращает (заказ, создан_ли_сейчас). Повторная заявка с тем же lead_id возвращает прежний заказ.
        Шаги записываются в той же транзакции, что и заказ: заказа без плана работ не бывает."""
        now = int(time.time())
        with self.tx():
            old = self._one('SELECT * FROM orders WHERE lead_id=?', (lead_id,))
            if old:
                return old, False
            cur = self.db.execute(
                'INSERT INTO orders (lead_id, fingerprint, name, email, product, title, amount_kop, created_ts, updated_ts)'
                ' VALUES (?,?,?,?,?,?,?,?,?)', (lead_id, fingerprint, name, email, product, title, amount_kop, now, now))
            ref = f'ORD-{cur.lastrowid:04d}'
            self.db.execute('UPDATE orders SET ref=? WHERE id=?', (ref, cur.lastrowid))
            for key, system in steps:
                self.add_step(ref, key, system)
            return self._one('SELECT * FROM orders WHERE id=?', (cur.lastrowid,)), True

    def order(self, ref: str) -> Optional[dict]:
        return self._one('SELECT * FROM orders WHERE ref=?', (ref,))

    def update_order(self, ref: str, **fields) -> None:
        assert set(fields) <= ORDER_FIELDS, f'неизвестные поля: {set(fields) - ORDER_FIELDS}'
        if not fields:
            return
        sets = ', '.join(f'{k}=?' for k in fields)
        with self.lock:
            self.db.execute(f'UPDATE orders SET {sets}, updated_ts=? WHERE ref=?', (*fields.values(), int(time.time()), ref))

    def set_status(self, ref: str, new: str, allowed_from: Iterable[str]) -> bool:
        """Смена статуса только из допустимых состояний (compare-and-set). False, если заказ уже в другом состоянии."""
        allowed = tuple(allowed_from)
        marks = ','.join('?' * len(allowed))
        with self.lock:
            cur = self.db.execute(f'UPDATE orders SET status=?, updated_ts=? WHERE ref=? AND status IN ({marks})',
                                  (new, int(time.time()), ref, *allowed))
        return cur.rowcount == 1

    def awaiting(self, older_than_s: int) -> List[dict]:
        limit = int(time.time()) - older_than_s
        return self._all("SELECT * FROM orders WHERE status IN ('AWAITING_PAYMENT','PAYMENT_FAILED') "
                         "AND invoice_id IS NOT NULL AND updated_ts <= ? ORDER BY id", (limit,))

    # ------------------------------------------------------------------ события провайдера
    def record_event(self, event_id: str, etype: str, ref: str) -> bool:
        """True, если событие новое. False: такой event_id уже обрабатывался (повторная доставка)."""
        with self.lock:
            cur = self.db.execute('INSERT OR IGNORE INTO events (event_id, type, order_ref, outcome, received_ts)'
                                  " VALUES (?,?,?,'received',?)", (event_id, etype, ref, int(time.time())))
        return cur.rowcount == 1

    def event_outcome(self, event_id: str) -> Optional[str]:
        row = self._one('SELECT outcome FROM events WHERE event_id=?', (event_id,))
        return row['outcome'] if row else None

    def set_event_outcome(self, event_id: str, outcome: str) -> None:
        with self.lock:
            self.db.execute('UPDATE events SET outcome=? WHERE event_id=?', (outcome, event_id))

    # ------------------------------------------------------------------ очередь шагов
    def add_step(self, ref: str, key: str, system: str, payload: Optional[dict] = None) -> None:
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO steps (order_ref, key, system, payload) VALUES (?,?,?,?)',
                            (ref, key, system, json.dumps(payload, ensure_ascii=False) if payload else None))

    def dead_systems(self, ref: str) -> List[str]:
        """Системы, по которым у заказа есть «мёртвые» шаги (лимит попыток исчерпан, нужен человек)."""
        rows = self._all("SELECT DISTINCT system FROM steps WHERE order_ref=? AND status='dead'", (ref,))
        return [r['system'] for r in rows]

    def pending_steps(self, ref: Optional[str] = None) -> List[dict]:
        sql, args = "SELECT * FROM steps WHERE status='pending'", ()
        if ref:
            sql, args = sql + ' AND order_ref=?', (ref,)
        rows = self._all(sql + ' ORDER BY id', args)
        for r in rows:
            r['payload'] = json.loads(r['payload']) if r['payload'] else {}
        return rows

    def refs_with_pending(self) -> List[str]:
        return [r['order_ref'] for r in self._all("SELECT DISTINCT order_ref FROM steps WHERE status='pending' "
                                                  'ORDER BY order_ref')]

    def finish_step(self, step_id: int) -> None:
        with self.lock:
            self.db.execute("UPDATE steps SET status='done', last_error=NULL WHERE id=?", (step_id,))

    def fail_step(self, step_id: int, error: str, max_attempts: int) -> str:
        """Записывает неудачную попытку. Возвращает 'pending' или 'dead' (лимит исчерпан, нужен человек)."""
        with self.lock:
            self.db.execute('UPDATE steps SET attempts=attempts+1, last_error=? WHERE id=?', (error[:300], step_id))
            row = self.db.execute('SELECT attempts FROM steps WHERE id=?', (step_id,)).fetchone()
            status = 'dead' if row['attempts'] >= max_attempts else 'pending'
            self.db.execute('UPDATE steps SET status=? WHERE id=?', (status, step_id))
        return status

    # ------------------------------------------------------------------ сводка
    def stats(self) -> Dict[str, Dict[str, int]]:
        def count(table: str, col: str) -> Dict[str, int]:
            return {r[col]: r['n'] for r in self._all(f'SELECT {col}, COUNT(*) AS n FROM {table} GROUP BY {col}')}
        return {'orders': count('orders', 'status'), 'events': count('events', 'outcome'), 'steps': count('steps', 'status')}
