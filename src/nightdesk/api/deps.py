from __future__ import annotations

from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def get_engine_dep(engine: Engine):
    def _dep() -> Engine:
        return engine
    return _dep


def get_session_dep(engine: Engine):
    def _dep() -> Session:
        with Session(engine) as s:
            yield s
    return _dep
