import os
from dotenv import load_dotenv
import httpx
from mcp.server.mcpserver import MCPServer 
import uuid
from supabase import create_client, Client
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from starlette.responses import PlainTextResponse
from starlette.routing import Route
import jwt
from jwt import PyJWKClient
from datetime import date

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")


# mcp = MCPServer("task-tracker-mcp-server")

transport_mode = os.getenv("MCP_TRANSPORT", "stdio")


def _load_known_tokens() -> dict[str, str]:
    """Parses MCP_AUTH_TOKENS, formatted as 'token1:name1,token2:name2'."""
    tokens = {}
    for pair in os.getenv("MCP_AUTH_TOKENS", "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        token, _, name = pair.partition(":")
        if token and name:
            tokens[token] = name
    return tokens


# class StaticTokenVerifier(TokenVerifier):
#     async def verify_token(self, token: str) -> AccessToken | None:
#         name = _load_known_tokens().get(token)
#         if name is None:
#             return None
#         return AccessToken(token=token, client_id=name, scopes=["mcp:use"], subject=name)

class SupabaseJWTVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = _jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256"],
                audience="authenticated",
            )
        except jwt.PyJWTError:
            return None

        user_id = claims.get("sub")
        if not user_id:
            return None

        return AccessToken(
            token = token,
            client_id=claims.get("email", user_id),
            scopes=["mcp:use"],
            subject=user_id,
        )
        

if transport_mode == "http":
    resource_url = os.getenv("MCP_RESOURCE_URL", "http://127.0.0.1:8000/mcp")
    mcp = MCPServer(
        "task-tracker-mcp-server",
        token_verifier=SupabaseJWTVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(resource_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=["mcp:use"],
        ),
    )
else:
    mcp = MCPServer("task-tracker-mcp-server")

async def health_check(request):
    return PlainTextResponse("ok")

def _fetch_schema() -> dict:
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    resp.raise_for_status()
    return resp.json()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

VALID_STATUSES = {"todo", "in_progress", "done"}

def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False 


@mcp.tool()
def describe_schema() -> dict:
    """List every table in the database with its columns, each column's type, and whether it's required."""
    spec = _fetch_schema()
    tables = [p.strip("/") for p in spec.get("paths", {}) if p != "/" and not p.startswith("/rpc/")]
    definitions = spec.get("definitions", {})

    result = {}
    for table in tables:
        schema = definitions.get(table, {})
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        result[table] = {
            col: {"type": meta.get("format", meta.get("type", "unknown")), "required": col in required}
            for col, meta in props.items()
        }
    return result

@mcp.tool()
def list_tasks(status: str | None=None, limit: int=20) -> list[dict] | dict:
    """list tasks, most recently created first. Optionally fitler by status (todo, in_progress, done)."""
    if status is not None and status not in VALID_STATUSES:
        return {"error": f"'{status}' is not a valid status. Use one of: {','. join(sorted(VALID_STATUSES))}."}
    try:
        query = supabase.table("tasks").select("*").order("created_at", desc=True).limit(limit)
        if status is not None:
            query = query.eq("status", status)
        response = query.execute()
    except Exception as e:
        return {"error": f"Database query failed: {e}"}
    return response.data

@mcp.tool()
def get_task(task_id: str)->  dict:
    """Get a single task by its id (UUID)."""
    if not _is_valid_uuid(task_id):
        return {"error":f"'{task_id}' is not a valid uuid"}

    try:
        response = supabase.table("tasks").select("*").eq("id", task_id).maybe_single().execute()
    except Exception as e:
        return {"error": f"Database query failed: {e}"}

    if not response or not response.data:
        return{"error": f"No task found with id '{task_id}'"}

    return response.data

VALID_PRIORITIES = {"low", "medium", "high"}

def _is_valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False

@mcp.tool()
def create_task(
    title: str,
    description: str | None=None,
    priority: str | None=None,
    due_date: str | None=None,
) -> dict:
    """create a new task, priority (if given) must be low, medium, high. due_date (if given) must be an ISO date string, e.g. '2026-.9-30'."""
    if not title or not title.strip():
        return {"error": "title is required and cannot be empty."}
    if priority is not None and priority not in VALID_PRIORITIES:
        return {"error": f"'{priority}' is not a valid priority, Use one of: {','.join(sorted(VALID_PRIORITIES))}."}
    if due_date is not None and not _is_valid_date(due_date):
        return {"error": f"'{due_date}' is noe a valid date. use YYYY-MM-DD"}

    row = {"title": title.strip(), "status": "todo"}
    if description is not None:
        row["description"] = description
    if priority is not None:
        row["priority"] = priority
    if due_date is not None:
        row["due_date"] = due_date

    try:
        response = supabase.table("tasks").insert(row).execute()
    except Exception as e:
        return {"error": f"Database insert failed: {e}"}

    return response.data[0]

@mcp.tool()
def update_task_status(task_id : str, status : str) -> dict:
    """Change task status, must be one of from: todo, in_progress, done."""
    if not _is_valid_uuid(task_id):
        return {"error": f"'{task_id}' is not a valid uuid"}

    if status not in VALID_STATUSES:
        return {"error": f"'{status}' is not valid status. Use one of:{','.join(sorted(VALID_STATUSES))}."}

    try:
        response = supabase.table("tasks").update({"status": status}).eq("id", task_id).execute()
    except Exception as e:
        return {"error": f"database update query failed {e}."}

    if not response.data:
        return {"error": f"No task found with id {task_id}."}

    return response.data[0]

@mcp.tool()
def delete_task(task_id: str, confirm: bool= False) -> dict:
    """Delete a task permanently. This requires confirm=True — calling it with
    confirm=False (the default) will NOT delete anything; it just describes
    what would be deleted so you can double-check before confirming."""
    if not _is_valid_uuid(task_id):
        return {"error": f"'{task_id}' is not valid uuid."}
    try:
        existing = supabase.table("tasks").select("*").eq("id", task_id).maybe_single().execute()
    except Exception as e:
        return {"error": f"Database query failed: {e}"}

    if not existing or not existing.data:
        return {"error": f"No task found with id '{task_id}'."}

    if not confirm:
        return {
            "status": "confirmation_required",
            "message": "Call delete_task again with confirm=True to actually delete this task.",
            "task": existing.data,
        }

    try:
        supabase.table("tasks").delete().eq("id", task_id).execute()
    except Exception as e:
        return {"error": f"Database delete failed: {e}"}

    return {"status": "deleted", "task": existing.data}

# if __name__ == "__main__":
#     mcp.run(transport="stdio")
if __name__ == "__main__":
    if transport_mode == "http":
        app = mcp.streamable_http_app()
        app.router.routes.append(Route("/", health_check))
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
        # mcp.run(transport="streamable-http", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    else:
        mcp.run(transport="stdio")