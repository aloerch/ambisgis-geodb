# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend-only bounded snapshot jobs; policy references confer no permission.

A reservation is durable operation identity, not a captured source. The copy
transaction establishes its source head/schema. Connections are dedicated and
idle, and worker credentials never leave the backend.
"""
import json
from .database import identity, require_idle


def _call(connection, name, parameters):
    require_idle(connection)
    with connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'")
        cursor.execute("SELECT managed." + name, parameters)
        return cursor.fetchone()[0]


def configure_quotas(connection, group_id, limits):
    """Schema-owner operation; limits concern named-branch storage only."""
    return _call(connection, "configure_branch_quotas(%s,%s::jsonb)",
                 (identity(group_id), json.dumps(limits, allow_nan=False)))


def reserve_branch(connection, *, group_id, branch_id, operation_id, name,
                   owner_id, visibility="private", editor_policy_refs=()):
    return _call(connection, "branch_reserve(%s,%s,%s,%s,%s,%s::uuid[],%s)",
                 (identity(group_id), identity(branch_id), name, identity(owner_id),
                  visibility, [identity(v) for v in editor_policy_refs], identity(operation_id)))


def get_branch(connection, branch_id, *, operational=False):
    if type(operational) is not bool:
        raise ValueError("operational must be boolean")
    return _call(connection, "branch_get(%s,%s)", (identity(branch_id), operational))


def list_branches(connection, group_id):
    return _call(connection, "branch_list(%s)", (identity(group_id),))


def populate_branch(connection, reservation, *, attempts=3, timeout_ms=30000, lock_timeout_ms=5000):
    """Retry only whole RR transactions; never replace the expected revision."""
    require_idle(connection)
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("copy attempts must be between one and three")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000 or type(lock_timeout_ms) is not int or not 1 <= lock_timeout_ms <= 5000:
        raise ValueError("copy deadlines must remain within 30s statement / 5s lock bounds")
    for attempt in range(attempts):
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute(f"SET LOCAL lock_timeout='{lock_timeout_ms}ms'; SET LOCAL statement_timeout='{timeout_ms}ms'")
                # The function's first data read acquires the group SHARE guard.
                cursor.execute("SELECT managed.branch_populate(%s,%s)",
                               (identity(reservation["branch_id"]), identity(reservation["revision"])))
                return cursor.fetchone()[0]
        except Exception as error:
            if getattr(error, "pgcode", None) != "40001" or attempt + 1 == attempts:
                raise


def create_branch(connection, *, copy_timeout_ms=30000, lock_timeout_ms=5000, **request):
    """Reserve a stable caller-supplied operation ID, then copy or resolve retry."""
    reservation = reserve_branch(connection, **request)
    if reservation["state"] == "active":
        return reservation
    if reservation["state"] != "creating":
        return reservation  # caller receives an explicit failed/completed status
    try:
        return populate_branch(connection, reservation, timeout_ms=copy_timeout_ms, lock_timeout_ms=lock_timeout_ms)
    except BaseException as original:
        # A lost connection leaves the private reservation discoverable through
        # the same operation ID. A fresh authorized worker can recover it.
        if not connection.closed:
            try:
                current = get_branch(connection, reservation["branch_id"], operational=True)
                if current["state"] == "active":
                    return current  # concurrent worker completed this same operation
                reason = "cancelled" if getattr(original, "pgcode", None) == "57014" else "copy_failed"
                _call(connection, "branch_fail(%s,%s,%s)",
                      (reservation["branch_id"], reservation["revision"], reason))
            except Exception:
                pass  # preserve the original failure; recovery never overwrites a newer revision
        raise


def cancel_branch(connection, reservation):
    return _call(connection, "branch_cancel(%s,%s)",
                 (identity(reservation["branch_id"]), identity(reservation["revision"])))


def recover_branch(connection, reservation):
    return _call(connection, "branch_recover(%s,%s)",
                 (identity(reservation["branch_id"]), identity(reservation["revision"])))


def update_branch(connection, branch, *, name, visibility, editor_policy_refs=()):
    return _call(connection, "branch_update(%s,%s,%s,%s,%s::uuid[])",
                 (identity(branch["branch_id"]), identity(branch["revision"]), name,
                  visibility, [identity(v) for v in editor_policy_refs]))


def transition_branch(connection, branch, state):
    return _call(connection, "branch_transition(%s,%s,%s)",
                 (identity(branch["branch_id"]), identity(branch["revision"]), state))
