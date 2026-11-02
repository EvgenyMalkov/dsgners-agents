"""Веб-сервер ИИ-офиса: страница, API и живая лента (WebSocket).

Доступ по секретному токену: первый вход по ссылке ...?token=XXX, дальше токен живёт в cookie.
"""
import asyncio
import json
import secrets
from pathlib import Path

from aiohttp import WSMsgType, web

from core import config, knowledge, runner, spend
from core.office import AGENTS, office

PAGE = config.ROOT / "web" / "office.html"
COOKIE = "dsg_office"


def _authed(req: web.Request) -> bool:
    tok = req.query.get("token") or req.cookies.get(COOKIE) or req.headers.get("X-Token", "")
    return bool(config.OFFICE_TOKEN) and secrets.compare_digest(tok, config.OFFICE_TOKEN)


@web.middleware
async def auth(req: web.Request, handler):
    if not _authed(req):
        if req.path == "/":
            return web.Response(text=LOGIN_HTML, content_type="text/html")
        return web.json_response({"error": "unauthorized"}, status=401)
    resp = await handler(req)
    if req.query.get("token"):
        resp.set_cookie(COOKIE, config.OFFICE_TOKEN, max_age=3600 * 24 * 180, httponly=True, samesite="Strict")
    return resp


LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ИИ-офис dsgners</title><body style="font:16px system-ui;background:#16181C;color:#fff;display:grid;place-items:center;height:100vh;margin:0">
<form style="display:flex;gap:8px;padding:16px"><input name=token placeholder="Токен офиса" style="padding:12px;border-radius:10px;border:0;min-width:0">
<button style="padding:12px 18px;border-radius:10px;border:0;background:#D5F839;font-weight:700">Войти</button></form>"""


def _agent(req) -> str:
    a = req.match_info["agent"]
    if a not in AGENTS:
        raise web.HTTPNotFound(text="Нет такого агента")
    return a


async def page(req):
    return web.FileResponse(PAGE, headers={"Cache-Control": "no-store"})


async def state(req):
    return web.json_response({
        "agents": [{**office.status[a], "title": m["title"], "active": m["active"]} for a, m in AGENTS.items()],
        "pending_by_agent": office.pending_by_agent(),
        "events": office.recent_events(),
        "spend": round(spend.this_month(), 2),
        "limit": config.MONTHLY_LIMIT_USD,
        "spend_by_agent": {k: round(v, 2) for k, v in spend.by_agent_this_month().items()},
    })


async def ws(req):
    sock = web.WebSocketResponse(heartbeat=25)
    await sock.prepare(req)
    q = office.subscribe()

    async def pump():
        while True:
            await sock.send_str(json.dumps(await q.get(), ensure_ascii=False))

    task = asyncio.create_task(pump())
    try:
        async for msg in sock:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        task.cancel()
        office.unsubscribe(q)
    return sock


async def ask(req):
    a = _agent(req)
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="Пустая задача")
    try:
        jid = runner.start_job(a, text, source="office")
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"job_id": jid})


async def jobs(req):
    return web.json_response(office.list_jobs(req.query.get("agent") or None, int(req.query.get("limit", 60))))


async def job(req):
    j = office.get_job(req.match_info["jid"])
    if not j:
        raise web.HTTPNotFound()
    j["children"] = [x["id"] for x in office.list_jobs(j["agent"], 200) if x.get("parent") == j["id"]]
    return web.json_response(j)


async def approve(req):
    try:
        return web.json_response(runner.approve(req.match_info["jid"]))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


async def redo(req):
    reason = ((await req.json()).get("reason") or "").strip()
    if not reason:
        return web.json_response({"error": "Напиши, что переделать"}, status=400)
    try:
        return web.json_response({"job_id": runner.redo(req.match_info["jid"], reason, source="office")})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


def _safe_file(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if base.resolve() not in p.parents or not p.is_file():
        raise web.HTTPNotFound()
    return p


async def outbox_file(req):
    return web.FileResponse(_safe_file(config.OUTBOX, req.match_info["tail"]))


async def kfile(req):
    return web.FileResponse(_safe_file(knowledge.kdir(_agent(req)), req.match_info["tail"]))


async def k_overview(req):
    return web.json_response(knowledge.overview(_agent(req)))


async def k_save(req):
    a = _agent(req)
    body = await req.json()
    try:
        if body.get("reset"):
            knowledge.reset_text(a, req.match_info["name"])
        else:
            knowledge.save_text(a, req.match_info["name"], body.get("content", ""))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    office.emit(a, "info", f"Знания обновлены: {knowledge.TEXTS.get(req.match_info['name'], req.match_info['name'])}")
    return web.json_response(knowledge.overview(a))


async def k_upload(req):
    a = _agent(req)
    form = await req.post()
    f = form.get("file")
    if f is None or not hasattr(f, "file"):
        return web.json_response({"error": "Нет файла"}, status=400)
    data = f.file.read()
    if len(data) > 15 * 1024 * 1024:
        return web.json_response({"error": "Файл больше 15 МБ"}, status=400)
    try:
        knowledge.save_upload(a, form.get("kind", ""), f.filename, data, form.get("note", ""))
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    labels = {"good": "референс «хорошо»", "bad": "антиреференс", "doc": "документ"}
    office.emit(a, "info", f"Загружен {labels.get(form.get('kind'), 'файл')}: {f.filename}")
    return web.json_response(knowledge.overview(a))


async def k_delete(req):
    a = _agent(req)
    try:
        knowledge.delete_file(a, (await req.json()).get("path", ""))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(knowledge.overview(a))


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth], client_max_size=16 * 1024 * 1024)
    app.router.add_get("/", page)
    app.router.add_get("/api/state", state)
    app.router.add_get("/ws", ws)
    app.router.add_post("/api/agent/{agent}/ask", ask)
    app.router.add_get("/api/jobs", jobs)
    app.router.add_get("/api/jobs/{jid}", job)
    app.router.add_post("/api/jobs/{jid}/approve", approve)
    app.router.add_post("/api/jobs/{jid}/redo", redo)
    app.router.add_get("/files/{tail:.+}", outbox_file)
    app.router.add_get("/api/agent/{agent}/knowledge", k_overview)
    app.router.add_put("/api/agent/{agent}/knowledge/{name}", k_save)
    app.router.add_post("/api/agent/{agent}/upload", k_upload)
    app.router.add_post("/api/agent/{agent}/delete", k_delete)
    app.router.add_get("/kfiles/{agent}/{tail:.+}", kfile)
    return app


async def start_web():
    runner_ = web.AppRunner(make_app(), access_log=None)
    await runner_.setup()
    site = web.TCPSite(runner_, config.OFFICE_HOST, config.OFFICE_PORT)
    await site.start()
    return runner_
