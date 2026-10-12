from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

from .models import encode


def dumps(data):
    return json.dumps(data, ensure_ascii=False, default=encode, sort_keys=True, allow_nan=False)


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "journal.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL,
              payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, time INTEGER, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS inputs (hash TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS consumed (id TEXT PRIMARY KEY);
        """)
        self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, values):
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO kv VALUES (?,?)", [(key, dumps(value)) for key, value in values.items()])

    def decision(self, data, frames):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO inputs VALUES (?,?)", (data["data_hash"], dumps(frames)))
            self.db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?)", (data["id"], data["asof"], dumps(data)))

    def consume(self, key):
        with self.lock, self.db:
            return self.db.execute("INSERT OR IGNORE INTO consumed VALUES (?)", (key,)).rowcount == 1

    def record(self, kind, data, state=None, updates=None):
        payload = dumps(data)
        with self.lock, self.db:
            row = self.db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            previous = row[0] if row else "0" * 64
            stamp = time.time()
            hashed = hashlib.sha256(f"{previous}|{stamp}|{kind}|{payload}".encode()).hexdigest()
            cursor = self.db.execute("INSERT INTO events(time,kind,payload,previous_hash,hash) VALUES (?,?,?,?,?)", (stamp, kind, payload, previous, hashed))
            if state is not None:
                self.db.execute("INSERT OR REPLACE INTO kv VALUES ('account',?)", (dumps(state),))
            for key, value in (updates or {}).items():
                self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, dumps(value)))
            event_id = cursor.lastrowid
        if kind in {"ENTRY", "EXIT", "ORDER_UNKNOWN", "RECOVERY"}:
            self.export_event(event_id)
        return event_id

    def events(self, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT id,time,kind,payload,hash FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "time": r[1], "kind": r[2], "data": json.loads(r[3]), "hash": r[4]} for r in rows]

    def export_event(self, event_id):
        with self.lock:
            row = self.db.execute("SELECT time,kind,payload,hash FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("기록을 찾을 수 없습니다.")
        stamp, kind, payload, hashed = row
        data = json.loads(payload)
        decision = data.get("decision") or data.get("entry_decision") or {}
        selection = decision.get("selection") or {}
        lines = [f"# Elliott 거래 기록 {event_id}: {kind}", "",
                 f"- 기록 시각(UTC): {datetime.fromtimestamp(stamp, timezone.utc).isoformat()}",
                 f"- 실행 모드: {data.get('mode', 'paper')}",
                 f"- 사유: {data.get('reason', decision.get('reason', ''))}",
                 f"- 방향: {data.get('direction', decision.get('action', ''))}",
                 f"- 실제/모의 체결 가격: {data.get('price', '')}", f"- 수량(BTC): {data.get('quantity', '')}",
                 f"- 전략 버전: {decision.get('version', '')}", f"- 결정 ID: {decision.get('id', '')}",
                 f"- 선택 파동: {selection.get('timeframe', '')} {selection.get('pattern', '')}",
                 f"- 적합도(승률 아님): {selection.get('score', '')}",
                 f"- 카운트 무효화: {selection.get('invalidation', '')}", f"- 거래 손절: {selection.get('stop', '')}",
                 f"- 파동 목표: {selection.get('target', '')}", "", "## 시간봉별 카운트", ""]
        for tf, frame in decision.get("frames", {}).items():
            lines.append(f"- {tf}: 방향 {frame['bias']}, 상태 {frame['state']}, 선택 {frame['selected']}")
            for c in frame.get("candidates", []):
                lines.append(f"  - {c['pattern']}: 관측 {c.get('observed_direction_name', '미기록')}, 이후 예상 {c['direction_name']}: {c['score']}점, 구조 자격 {c['eligible']}, 후보 {c['id']}")
        if decision.get("scenarios"):
            lines += ["", "## 진입 후보 비교 (승률 추정 아님)", "",
                      f"- 선택 위험 배수: {decision.get('risk_multiplier')}",
                      f"- 돌파 근거: {decision.get('trigger')}"]
            for r in decision["scenarios"]:
                failed = ", ".join(c["detail"] for c in r["checks"] if not c["passed"]) or "진입 조건 통과"
                lines.append(f"- {r['timeframe']} {r['pattern']} {r['direction']}: 순위 {r['priority']}, 위험 배수 {r['risk_multiplier']}, 비용 후 RR {r['rr']}, {failed}")
        lines += ["", "## 우선순위 검사", ""]
        lines += [f"- {c['id']} {'통과' if c['passed'] else '미통과'}: {c['detail']}" for c in decision.get("checks", [])]
        lines += ["", "## 원본 근거와 체결", "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```", "", f"기록 연결 해시: `{hashed}`", ""]
        directory = self.root / "trades"
        directory.mkdir(exist_ok=True)
        path = directory / f"{event_id:08d}-{kind.lower()}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def close(self):
        self.db.close()
