"""Per-user preferences endpoint.

Opaque JSON blob owned by the client. Backend never inspects keys —
stores and returns whatever the client writes. Used today for tool
toggle persistence (`enabled_tools: ["module.method", ...]`); any
future per-user UI setting goes here without a backend change.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, db


router = APIRouter(tags=["preferences"])


async def _read_user_prefs(user_id: int) -> dict:
    async with db.get_conn() as c:
        row = await (await c.execute(
            "SELECT preferences FROM users WHERE id = ?", (user_id,),
        )).fetchone()
    if not row:
        return {}
    raw = row["preferences"] or "{}"
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _write_user_prefs(user_id: int, prefs: dict) -> None:
    payload = json.dumps(prefs, separators=(",", ":"))
    async with db.get_conn() as c:
        await c.execute(
            "UPDATE users SET preferences = ? WHERE id = ?",
            (payload, user_id),
        )
        await c.commit()


@router.get("/me/preferences")
async def get_preferences(user: dict = Depends(auth.current_user)):
    """Return the user's preference blob. Always a dict — empty when unset."""
    return {"preferences": await _read_user_prefs(user["id"])}


@router.patch("/me/preferences")
async def patch_preferences(
    body: dict, user: dict = Depends(auth.current_user),
):
    """Shallow-merge the supplied dict into the user's preference blob.
    To delete a key, send `{"key": null}` — null values are stripped on
    write so they don't accumulate. Returns the merged result so the
    client doesn't need to round-trip a follow-up GET."""
    if not isinstance(body, dict):
        raise HTTPException(400, "preferences body must be a JSON object")
    current = await _read_user_prefs(user["id"])
    for k, v in body.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    await _write_user_prefs(user["id"], current)
    return {"preferences": current}
