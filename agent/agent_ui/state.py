"""Persistent UI state with optimistic concurrency across workers/instances."""

import json
import time
import uuid

from sqlalchemy import (
    Column,
    Float,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError


class StateConflict(Exception):
    pass


class StateStore:
    def __init__(self, url: str, ttl: int = 86400):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.ttl = ttl
        metadata = MetaData()
        self.table = Table(
            "ui_sessions",
            metadata,
            Column("session_id", String(128), primary_key=True),
            Column("owner", String(128), nullable=False),
            Column("version", String(32), nullable=False),
            Column("expires", Float, nullable=False),
            Column("payload", Text, nullable=False),
        )
        metadata.create_all(self.engine)

    def read(self, sid, owner):
        with self.engine.begin() as conn:
            conn.execute(delete(self.table).where(self.table.c.expires < time.time()))
            row = (
                conn.execute(select(self.table).where(self.table.c.session_id == sid))
                .mappings()
                .first()
            )
        if row is None:
            return None
        if row["owner"] != owner:
            raise StateConflict("Session belongs to another browser.")
        return {"version": row["version"], **json.loads(row["payload"])}

    def write(self, sid, owner, previous, payload):
        version = uuid.uuid4().hex
        values = dict(
            owner=owner,
            version=version,
            expires=time.time() + self.ttl,
            payload=json.dumps(payload),
        )
        try:
            with self.engine.begin() as conn:
                if previous is None:
                    conn.execute(self.table.insert().values(session_id=sid, **values))
                else:
                    result = conn.execute(
                        update(self.table)
                        .where(
                            self.table.c.session_id == sid,
                            self.table.c.owner == owner,
                            self.table.c.version == previous,
                        )
                        .values(**values)
                    )
                    if result.rowcount != 1:
                        raise StateConflict(
                            "Session changed; retry using the current company selection."
                        )
        except IntegrityError as exc:
            raise StateConflict("Session changed; retry the request.") from exc
        return {"version": version, **payload}
