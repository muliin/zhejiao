# -*- coding: utf-8 -*-
"""折角 · 本地服务
用法：python server.py  →  浏览器打开 http://127.0.0.1:8765/

多卡片：每张卡片是一个独立会话，历史与名字各自落盘在 卡片/<id>.json。
不同卡片之间不共享记忆。
"""
import json, sys, os, time, uuid, threading, importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))

# 载入核心模块（中文文件名，用 importlib 加载）
spec = importlib.util.spec_from_file_location("core", os.path.join(ROOT, "折角.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

CARD_DIR = os.path.join(ROOT, "卡片")
os.makedirs(CARD_DIR, exist_ok=True)


class Session:
    """一张卡片 = 一个独立会话。"""

    def __init__(self, cid=None, name=None):
        self.id = cid or uuid.uuid4().hex[:8]
        self.name = name or "新的折角"
        self.created = time.time()
        self.updated = time.time()
        self.history = []      # [(role, text)]
        self.names = []        # [{"word": ..., "when": ...}]
        self.mem = core.Memory()
        self.phase = "opening"
        self.ctx = None
        # 合上（不可逆）。sealed 是硬状态：服务端据此拒绝 open/reply/lens。
        self.sealed = False
        self.sealed_at = None
        # 走过的位置（透镜 id），每根只能去一次，顺序由用户决定。
        self.lenses_used = []
        # 每个位置看到的点题，给"刻度"浮层做对照用。
        self.points = []
        # 用户是否自己起过名字（起过就不再自动覆盖）
        self.named_by_user = False
        self.lock = threading.Lock()

    # ---- 位置（透镜） ----
    def lens_left(self):
        return [l for l in core.LENS_ORDER if l not in self.lenses_used]

    def use_lens(self, lens_id):
        if lens_id not in self.lenses_used:
            self.lenses_used.append(lens_id)

    # ---- 走过的位置 + 各自的点题（给"刻度"用） ----
    def add_point(self, lens_id, full):
        """记录某个位置看到的那段画面，点题 = 最后一段。"""
        parts = [p.strip() for p in (full or "").split("\n\n") if p.strip()]
        point = parts[-1] if parts else ""
        self.points = [p for p in self.points if p.get("lens") != lens_id]
        self.points.append({"lens": lens_id, "point": point, "full": full or ""})

    def auto_name(self, text):
        """用用户自己的第一句话起个短名（不额外调 LLM）。
        优先在第一个标点处断开，断不出来再硬截。
        用户双击改名优先，自动名只是兜底。"""
        if self.named_by_user:
            return
        t = " ".join((text or "").split())
        if not t:
            return
        # 在第一个句读处断开，取前半句
        for i, ch in enumerate(t):
            if ch in "，。！？；,.!?;、":
                head = t[:i].strip()
                if 2 <= len(head) <= 12:
                    self.name = head
                    return
                break
        self.name = t[:10].strip() if len(t) > 10 else t

    def to_public(self):
        last = ""
        for role, tx in reversed(self.history):
            if role == "machine":
                last = tx
                break
        raw = (self.ctx or {}).get("raw", "")
        if not raw:
            for role, tx in self.history:
                if role == "user":
                    raw = tx
                    break
        return {"id": self.id, "name": self.name, "created": self.created,
                "updated": self.updated, "names": self.names, "last": last,
                "has_ctx": bool(self.ctx), "raw": raw,
                "sealed": self.sealed, "sealed_at": self.sealed_at,
                "lenses_used": list(self.lenses_used),
                "lenses_left": self.lens_left(),
                "points": list(self.points),
                "named_by_user": self.named_by_user,
                "lens_names": {k: v["name"] for k, v in core.LENSES.items()}}

    def _lens_name(self, lens_id):
        return core.LENSES[lens_id]["name"]

    def _history_text(self, n=10):
        out = []
        for role, tx in self.history[-n:]:
            out.append(("用户" if role == "user" else "你") + "：" + tx)
        return "\n".join(out)


# ============ 卡片落盘 ============
def _card_path(cid):
    return os.path.join(CARD_DIR, cid + ".json")


def _save_card(s):
    s.updated = time.time()
    data = {"id": s.id, "name": s.name, "created": s.created, "updated": s.updated,
            "history": s.history, "names": s.names, "ctx": s.ctx,
            "sealed": s.sealed, "sealed_at": s.sealed_at,
            "lenses_used": s.lenses_used, "points": s.points,
            "named_by_user": s.named_by_user}
    tmp = _card_path(s.id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _card_path(s.id))


def _load_cards():
    out = {}
    for fn in os.listdir(CARD_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CARD_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            s = Session(cid=d["id"], name=d.get("name"))
            s.created = d.get("created", s.created)
            s.updated = d.get("updated", s.updated)
            s.history = d.get("history", [])
            s.names = d.get("names", [])
            s.ctx = d.get("ctx")
            # 旧卡片没有这些字段：默认未合上、未走过任何位置。
            s.sealed = bool(d.get("sealed", False))
            s.sealed_at = d.get("sealed_at")
            used = d.get("lenses_used")
            if not isinstance(used, list):
                used = []
            s.lenses_used = [l for l in used if l in core.LENS_ORDER]
            pts = d.get("points")
            s.points = pts if isinstance(pts, list) else []
            # 旧卡片名字不是默认值，就当作用户自己起过，不去覆盖它
            s.named_by_user = bool(d.get("named_by_user",
                                        d.get("name") and d.get("name") != "新的折角"))
            out[s.id] = s
        except Exception:
            continue
    return out


def _prune_empty(keep_id=None):
    """清掉没说过话的空卡，避免"每次打开都新建"堆出一堆空房间。"""
    for cid, s in list(SESSIONS.items()):
        if cid == keep_id or s.sealed or s.history or s.names:
            continue
        SESSIONS.pop(cid, None)
        try:
            os.remove(_card_path(cid))
        except OSError:
            pass


SESSIONS = _load_cards()


def _get_or_create(cid):
    """有 id 就用它；没有就新建一张空卡。

    不退回"最近更新的卡片"——入口永远是一张空白的卡，
    打开产品后的第一个动作应该是"说话"，不是"选择"。
    """
    if cid and cid in SESSIONS:
        return SESSIONS[cid]
    s = Session()
    SESSIONS[s.id] = s
    _save_card(s)
    return s


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        try:
            html = open(path, encoding="utf-8").read()
        except Exception:
            self._json({"error": "html not found"}, 404)
            return
        b = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._html(os.path.join(ROOT, "界面预览.html"))
        elif self.path == "/api/cards":
            cards = sorted(SESSIONS.values(), key=lambda s: -s.updated)
            self._json({"cards": [s.to_public() for s in cards]})
        elif self.path == "/api/memory":
            self._json({"memory": _get_or_create(None).mem.to_dict()})
        else:
            self._json({"error": "not found"}, 404)

    def _refuse_sealed(self, sess, sse=False):
        """已合上的卡片：在调用 LLM 之前就拒绝，不花额度、不产生新内容。"""
        if not sess.sealed:
            return False
        if sse:
            self._sse_start()
            self._sse("sealed", {"sealed_at": sess.sealed_at})
            self._sse("error", "这张卡片已合上")
        else:
            self._json({"error": "card sealed", "code": "CARD_SEALED",
                        "sealed_at": sess.sealed_at}, 409)
        return True

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        text = (body.get("text") or "").strip()
        try:
            if self.path == "/api/card/new":
                s = Session()
                SESSIONS[s.id] = s
                _prune_empty(keep_id=s.id)
                _save_card(s)
                self._json({"ok": True, "card": s.to_public()})
            elif self.path == "/api/card/rename":
                s = SESSIONS.get(body.get("id"))
                if not s:
                    self._json({"error": "no such card"}, 404)
                    return
                if s.sealed:
                    self._json({"error": "card sealed", "code": "CARD_SEALED"}, 409)
                    return
                name = (body.get("name") or "").strip()[:40]
                if name:
                    s.name = name
                    s.named_by_user = True
                _save_card(s)
                self._json({"ok": True, "card": s.to_public()})
            elif self.path == "/api/card/delete":
                cid = body.get("id")
                s = SESSIONS.pop(cid, None)
                if s is None:
                    self._json({"error": "no such card"}, 404)
                    return
                try:
                    os.remove(_card_path(cid))
                except OSError:
                    pass
                self._json({"ok": True, "deleted": cid})
            elif self.path == "/api/card/seal":
                self._handle_seal(body)
            elif self.path == "/api/card/delete":
                self._handle_delete(body)
            elif self.path == "/api/lens/stream":
                sess = _get_or_create(body.get("card"))
                if self._refuse_sealed(sess, sse=True):
                    return
                self._handle_lens_stream(sess, body.get("lens") or "mirror")
            elif self.path == "/api/open/stream":
                if not text:
                    self._json({"error": "empty"}, 400)
                    return
                sess = _get_or_create(body.get("card"))
                if self._refuse_sealed(sess, sse=True):
                    return
                self._handle_open_stream(sess, text)
            elif self.path == "/api/reply/stream":
                if not text:
                    self._json({"error": "empty"}, 400)
                    return
                sess = _get_or_create(body.get("card"))
                if self._refuse_sealed(sess, sse=True):
                    return
                self._handle_reply_stream(sess, text)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_delete(self, body):
        """删掉一张卡片：连同磁盘文件一起，不保留副本。

        这是真的删除，不是归档——"说完就忘"也包括"你可以把它丢掉"。
        同样由前端按住触发，不接受一次点击。
        """
        cid = body.get("id")
        s = SESSIONS.get(cid)
        if not s:
            # 已经不在了也算成功，避免重发变成报错
            self._json({"ok": True, "already": True})
            return
        SESSIONS.pop(cid, None)
        try:
            os.remove(_card_path(cid))
        except OSError:
            pass
        self._json({"ok": True, "id": cid})

    def _handle_seal(self, body):
        """合上一张卡片：不可逆。

        只能由用户显式触发（前端要求按住）。重复提交按幂等成功处理，
        不让网络重发变成一个失败提示。
        """
        s = SESSIONS.get(body.get("id"))
        if not s:
            self._json({"error": "no such card"}, 404)
            return
        with s.lock:
            if s.sealed:
                self._json({"ok": True, "already": True, "card": s.to_public()})
                return
            s.sealed = True
            s.sealed_at = time.time()
            s.phase = "sealed"
            # 纵深防御：ctx 一并清掉。即使将来漏了某处 sealed 检查，
            # reply/lens 都依赖 ctx，没有 ctx 就续不下去。
            s.ctx = None
            _save_card(s)
        self._json({"ok": True, "card": s.to_public()})

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _sse(self, ev, data):
        try:
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def _handle_open_stream(self, sess, text):
        self._sse_start()
        try:
            with sess.lock:
                self._sse("status", "正在读你这句话…")
                s = core.sensing(text)
                if s.get("intent") != "emotional":
                    sess.ctx = None
                    sess.phase = "opening"
                    hist = sess._history_text()
                    sess.history.append(("user", text))
                    acc = ""
                    for tok in core.chat_reply(text, context=hist):
                        acc += tok
                        self._sse("token", tok)
                    sess.history.append(("machine", acc))
                    _save_card(sess)
                    self._sse("done", {})
                    return
                sess.ctx = s
                hist = sess._history_text()
                sess.history.append(("user", text))
                self._sse("status", "在换位置…")
                acc = ""
                for tok in core.estranging_stream(s, sess.mem, history=hist):
                    acc += tok
                    self._sse("token", tok)
                sess.history.append(("machine", acc))
                self._sse("status", "在挑名字…")
                o = core.offering(s, sess.mem, picture=acc)
                sess.phase = "awaiting"
                # 入口透镜也算走过一个位置，否则用户会以为还有五个。
                sess.use_lens(s["entry_lens"])
                sess.add_point(s["entry_lens"], acc)
                sess.auto_name(text)
                _save_card(sess)
                self._sse("names", {"lens": sess._lens_name(s["entry_lens"]),
                                    "lens_id": s["entry_lens"],
                                    "names": o.get("candidates", []),
                                    "left": sess.lens_left(),
                                    "used": list(sess.lenses_used),
                                    "points": list(sess.points),
                                    "card_name": sess.name})
                self._sse("done", {"memory": sess.mem.to_dict()})
        except Exception as e:
            self._sse("error", str(e))

    def _handle_lens_stream(self, sess, lens_id):
        self._sse_start()
        try:
            with sess.lock:
                if lens_id not in core.LENS_ORDER:
                    self._sse("error", "no such lens")
                    return
                # 每个位置只能去一次：去过就不再生成（这不是 reroll）。
                if lens_id in sess.lenses_used:
                    self._sse("spent", {"lens": sess._lens_name(lens_id),
                                        "lens_id": lens_id,
                                        "left": sess.lens_left()})
                    self._sse("done", {"left": sess.lens_left()})
                    return
                if not sess.ctx:
                    # 旧卡片没有 ctx：用最初那句话重新读一次
                    raw = ""
                    for role, tx in sess.history:
                        if role == "user":
                            raw = tx
                            break
                    if not raw:
                        self._sse("error", "no active topic")
                        return
                    sess.ctx = core.sensing(raw)
                self._sse("status", "在换位置…")
                acc = ""
                for tok in core.estranging_stream(sess.ctx, sess.mem, lens=lens_id, history=sess._history_text()):
                    acc += tok
                    self._sse("token", tok)
                sess.history.append(("machine", acc))
                o = core.offering(sess.ctx, sess.mem, picture=acc)
                sess.use_lens(lens_id)
                sess.add_point(lens_id, acc)
                _save_card(sess)
                self._sse("names", {"lens": sess._lens_name(lens_id),
                                    "lens_id": lens_id,
                                    "names": o.get("candidates", []),
                                    "left": sess.lens_left(),
                                    "used": list(sess.lenses_used),
                                    "points": list(sess.points)})
                self._sse("done", {"lens": sess._lens_name(lens_id),
                                   "lens_id": lens_id,
                                   "left": sess.lens_left(),
                                   "used": list(sess.lenses_used),
                                   "points": list(sess.points)})
        except Exception as e:
            self._sse("error", str(e))

    def _handle_reply_stream(self, sess, text):
        self._sse_start()
        try:
            with sess.lock:
                if not sess.ctx:
                    self._sse("error", "no active topic")
                    return
                self._sse("status", "在守门…")
                hist = sess._history_text()
                sess.history.append(("user", text))
                END_MARKERS = ["差不多了", "好多了", "结束了", "先这样", "就到这", "不聊了", "结束"]
                SWITCH_MARKERS = ["换个话题", "换话题", "说点别的", "聊点别的", "换一个话题", "新的话题"]
                if any(m in text for m in END_MARKERS):
                    # 短语匹配只用来"问"，不用来"做"：
                    # 合上是不可逆的，必须由用户按住那个动作才真的发生。
                    self._sse("signal", {"signal": "offer_seal"})
                    acc = ""
                    for tok in core.farewell_stream():
                        acc += tok
                        self._sse("token", tok)
                    sess.history.append(("machine", acc))
                    _save_card(sess)
                    self._sse("done", {"offer_seal": True})
                    return
                if any(m in text for m in SWITCH_MARKERS):
                    self._sse("signal", {"signal": "switch"})
                    msg = "好，来说点别的——你想说什么？"
                    self._sse("token", msg)
                    sess.history.append(("machine", msg))
                    sess.phase, sess.ctx = "opening", None
                    _save_card(sess)
                    self._sse("done", {})
                    return
                g = core.guarding(sess.ctx["raw"], text, history=hist)
                sig = g.get("signal")
                FLOAT_MARKERS = ["无所谓", "算了", "没意义", "看开", "都是假", "人总要", "不重要", "没必要", "随便", "不想说"]
                if sig == "float" and not any(m in text for m in FLOAT_MARKERS):
                    sig = "unclear"
                if sig == "return":
                    named = g.get("named") or sess.ctx["word"]
                    sess.mem.hit(sess.ctx["entry_lens"], named)
                    sess.mem.remember(named, sess.ctx["raw"], sess.ctx["domain"], sess.ctx["entry_lens"])
                    sess.mem.log("return", g.get("evidence"))
                    sess.names.append({"word": named, "when": time.time()})
                    self._sse("signal", {"signal": "return", "named": named})
                    acc = ""
                    for tok in core.after_named_stream(named):
                        acc += tok
                        self._sse("token", tok)
                    sess.history.append(("machine", acc))
                    _save_card(sess)
                    self._sse("done", {"memory": sess.mem.to_dict()})
                elif sig == "float":
                    sess.mem.miss(sess.ctx["entry_lens"])
                    sess.mem.log("float", g.get("evidence"))
                    self._sse("signal", {"signal": "float"})
                    acc = ""
                    for tok in core.pullback_stream(text):
                        acc += tok
                        self._sse("token", tok)
                    sess.history.append(("machine", acc))
                    _save_card(sess)
                    self._sse("done", {"memory": sess.mem.to_dict()})
                elif sig == "meta":
                    self._sse("signal", {"signal": "meta"})
                    ctx = f"【之前的对话】\n{hist}\n\n刚才你基于他说的「{sess.ctx['raw']}」递了几个候选名字。"
                    acc = ""
                    for tok in core.chat_reply(text, context=ctx):
                        acc += tok
                        self._sse("token", tok)
                    sess.history.append(("machine", acc))
                    _save_card(sess)
                    self._sse("done", {})
                else:
                    self._sse("signal", {"signal": "unclear"})
                    acc = ""
                    for tok in core.respond(text, history=hist, raw=sess.ctx["raw"]):
                        acc += tok
                        self._sse("token", tok)
                    sess.history.append(("machine", acc))
                    _save_card(sess)
                    self._sse("done", {"memory": sess.mem.to_dict()})
        except Exception as e:
            self._sse("error", str(e))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"折角正在运行：http://127.0.0.1:{port}/")
    print(f"卡片目录：{os.path.abspath(CARD_DIR)}")
    print("按 Ctrl+C 停止。")
    srv.serve_forever()
