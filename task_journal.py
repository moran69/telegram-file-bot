"""Persistent admissions for one bot process. Never store events, peers, or credentials."""
import json
import sqlite3
from pathlib import Path


FIELDS = ('link', 'status_msg_id', 'chat_id', 'target_chat_id', 'created_at', 'grouped_id',
          'mode', 'message_ids', 'source_chat_id', 'retries', 'ready_at', 'observer', 'verify_edit', 'refused_role')


class TaskJournal:
    def __init__(self, map_path):
        self.path = Path(str(map_path) + '.queue.sqlite3')
        self.db = None

    def open(self):
        if self.db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'state TEXT NOT NULL, payload TEXT NOT NULL)')
            except sqlite3.Error:
                db.close()
                raise
            self.db = db
        return self.db

    def restore(self):
        with self.open() as db:
            return [(jid, state, json.loads(payload)) for jid, state, payload in db.execute(
                "SELECT id,state,payload FROM jobs ORDER BY id")]

    def save(self, task, state='pending'):
        payload = json.dumps({key: getattr(task, key) for key in FIELDS}, separators=(',', ':'))
        with self.open() as db:
            if task.job_id is None:
                jid = db.execute('INSERT INTO jobs(state,payload) VALUES(?,?)', (state, payload)).lastrowid
            else:
                jid = task.job_id
                if not db.execute('UPDATE jobs SET state=?,payload=? WHERE id=?', (state, payload, jid)).rowcount:
                    raise RuntimeError('任务的持久化记录不存在')
        task.job_id = jid

    def discard(self, ids):
        ids = [(jid,) for jid in ids if jid is not None]
        if ids:
            with self.open() as db:
                db.executemany('DELETE FROM jobs WHERE id=?', ids)

    def cancel_waiters(self, current_id, timer_ids, stop_current=False):
        with self.open() as db:
            db.execute("DELETE FROM jobs WHERE state IN ('pending','early','wait_peer') AND id IS NOT ?", (current_id,))
            db.executemany('DELETE FROM jobs WHERE id=?', [(jid,) for jid in timer_ids])
            if stop_current:
                db.execute("DELETE FROM jobs WHERE id=? AND state='pending'", (current_id,))
                db.execute("UPDATE jobs SET state='review' WHERE id=? AND state='sending'", (current_id,))

    def reviews(self):
        db = self.open()
        total = db.execute("SELECT count(*) FROM jobs WHERE state='review'").fetchone()[0]
        recent = [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM jobs WHERE state='review' ORDER BY id DESC LIMIT 5")]
        return total, recent

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
