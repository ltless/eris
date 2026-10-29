"""Who is making this call.

Two scopes exist because two kinds of client do. A browser holds a session
cookie and may do anything the account can do. A script, an editor, or an agent
speaking MCP carries a token, and a token is read scope: it may ask the app
questions, and it may not rewrite the account that issued it.
"""

from fastapi import Cookie, Header, HTTPException

from . import auth, tokens


def db_user(user_id):
    row = auth.db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return row


def require_user(session: str | None = Cookie(None),
                 authorization: str | None = Header(None)):
    """The account making this call, from a cookie or a bearer token.

    A browser holds a session cookie. A script, an editor, or an agent speaking
    MCP cannot, so it presents a token instead; `tokens.resolve` is the only
    place the plaintext is ever compared.
    """
    u = auth.get_session(session)
    if u:
        return u
    if authorization and authorization.lower().startswith("bearer "):
        user_id = tokens.resolve(authorization.split(" ", 1)[1].strip())
        row = db_user(user_id) if user_id is not None else None
        if row:
            return auth.profile(row)
    raise HTTPException(401, "Login required")


def require_session(session: str | None = Cookie(None)):
    """The account, from a session cookie only.

    Every route that changes the account itself — its name, its credentials,
    its preferences, its tokens — stands behind this instead, so a leaked token
    cannot be escalated into a takeover.
    """
    u = auth.get_session(session)
    if not u:
        raise HTTPException(401, "Login required")
    return u