"""One effective grant per supported client; old rows remain as history."""
from sqlalchemy import and_, or_, select, update

from .models import AgentConnection, utcnow


MANAGED_CLIENTS = {"Codex", "Claude Code", "Workbuddy"}


def current_clients(db, owner_id):
    rows = db.scalars(select(AgentConnection).where(
        AgentConnection.owner_id == owner_id, AgentConnection.client_name.in_(MANAGED_CLIENTS),
    ).order_by(AgentConnection.created_at.desc(), AgentConnection.id)).all()
    result = {}
    for row in rows:
        result.setdefault(row.client_name, row.id)
    return result


def is_current(db, connection):
    return (connection.client_name not in MANAGED_CLIENTS or
            current_clients(db, connection.owner_id).get(connection.client_name) == connection.id)


def supersede_others(db, connection):
    if connection.client_name in MANAGED_CLIENTS:
        db.execute(update(AgentConnection).where(
            AgentConnection.owner_id == connection.owner_id,
            AgentConnection.client_name == connection.client_name,
            AgentConnection.id != connection.id, AgentConnection.status == "active",
            or_(AgentConnection.created_at < connection.created_at,
                and_(AgentConnection.created_at == connection.created_at, AgentConnection.id > connection.id)),
        ).values(status="revoked", revoked_at=utcnow()))
