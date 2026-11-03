# -*- coding: utf-8 -*-
"""折角 v0.3 —— 交互式 + 记忆落盘 + 著作彩蛋
用法：
  python 折角.py            # 进入 REPL，一句一句聊
  python 折角.py --demo     # 跑一段脚本化多轮对话，展示记忆累积与彩蛋
命令（REPL 内）：/记忆 查看记忆，/退出 保存并离开
"""
import json, urllib.request, urllib.error, re, sys, os, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))

# ================= 配置 =================
CRED = r"C:\Users\27884\.dsh\.credentials.yaml"
MEM_FILE = "记忆.json"          # 交互模式用的记忆文件
DEMO_MEM = "记忆_demo.json"     # 演示模式用的记忆文件（不污染真实记忆）
彩蛋 = True                      # 著作彩蛋开关

def load_key():
    key = os.getenv("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    try:
        with open(CRED, encoding="utf-8") as f:
            m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", f.read())
    except FileNotFoundError:
        raise SystemExit(
            "找不到 API key：请设置环境变量 DEEPSEEK_API_KEY，或把 key 写进 "
            f"{CRED}（格式：DEEPSEEK_API_KEY: sk-xxx）。"
        ) from None
    if not m:
        raise SystemExit(
            f"在 {CRED} 里没找到 DEEPSEEK_API_KEY，请检查格式：DEEPSEEK_API_KEY: sk-xxx"
        )
    return m.group(1)

API_KEY = load_key()
BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"        # DeepSeek-V4.1-Flash（快模型：读状态、守门）
MODEL_BIG = "deepseek-v4-pro"   # DeepSeek-V4-Pro（大模型）
DEEP = False                    # True = 换位置用大模型（更稳，但首字慢 3-4 倍）

LEX = json.load(open(os.path.join(ROOT, "data", "词库.json"), encoding="utf-8"))
LENSES = {l["id"]: l for l in LEX["lenses"]}
LENS_ORDER = [l["id"] for l in LEX["lenses"]]   # 五个位置的固定顺序（用户自己选先后）
DOMAINS = {w["domain"]: w for w in LEX["words"]}

# 彩蛋（原 data/词库_导入.json）已下线：与"名字必须来自本轮画面"冲突，见 docs/交接文档.md

SYSTEM = """你是"折角"的一个节点。铁律（违反即失败）：
1. 不讨好、不站队、不评判对错。
2. 不分析原因、不解释、不诊断（绝不出现"你其实是在……""这是因为……"）。
3. 不替用户下结论、不替用户命名，命名权永远留给用户。
4. 语气温暖但不顺从：好奇、有距离感，像一个对你世界感兴趣但绝不附和你的他者。
5. 如果你"记得"关于这个用户的什么，可以自然地带一句（比如他以前给情绪起过的名字），但不要生硬。"""


SYSTEM_ESTRANGE = """你是"折角"的"换位置"节点。你的输出是一幅**有距离的场景**，不是提问，也不是特写。

铁律（违反即失败）：
1. **事实不编**：不许断言他生活里没提过的事实（住哪、几个人、什么房间）。
2. **隐喻可编**：可以引入一个"新框架"里的物件或规则（一条街、一间铺子、一个窗口、一场雨、一件旧物、一趟末班车），但要让读者知道那是"一个看法"，不是"他的生活"。
3. **禁止毫米级特写**：不要写手指怎么动、缝隙多宽、光晕多大、温度、呼吸、心跳；也不要 0.5 倍速慢动作。他原话里的物件（电话、听筒）可以出现，但只作为场景里的一件东西，不特写。
4. **画面要有距离**：能看见一个"场景"（谁在哪、那边在做什么），不是一个"局部"。
5. 人称固定用"你"，不输出问号。
6. 三段：先 1-2 句正常接住他，空一行写画面（画面里要有一件**现实不可能、却逻辑通顺**的事，这一下是"折角"），再空一行用 1 句**点题**——把折角的落点说出来（不是解释画面，是给一个看法）。
7. 不要解释这个画面是什么意思；不分析、不评判、不诊断、不替他命名。
8. 句子通顺、说得过去。
9. **只输出最终的画面**：不要自我点评、不要写"（...）"批注、不要写"改："、不要输出草稿或两版——直接给最终那一版。
10. **不要沿用【之前的对话】里用过的场景/物件**（上一段用了"窗口/黄线/叫号"，这次就换掉）。
11. **每根透镜要给出不同的结论**：镜子=执念/姿势；异乡客=规则错位；彼岸=界限；多年以后=时间；高处=尺度。不要五根都落到"你给自己判的"同一个道理上。

两个反例：
① 编事实：原话只有"妈/电话"，却写"你住在出租屋、桌角翘着旧漆"。
② 拍特写：整段都在写拇指、听筒、缝隙、尾音，镜头怼到毫米级——这没有"换位置"。"""


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连 DeepSeek，不走系统代理（国内直连更快）


def _open(req, timeout=90, retries=3):
    """带重试的打开：DNS 抖动 / 连接被重置时重试，给网络一点恢复时间。"""
    last = None
    for i in range(retries):
        try:
            return _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except OSError as e:
            last = e
        if i < retries - 1:
            time.sleep(1.2 * (i + 1))
    raise last


def llm(system, user, temperature=0.8, model=None):
    body = {"model": model or MODEL, "effort": "low", "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], "temperature": temperature}
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=data, method="POST")
    req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("Content-Type", "application/json")
    with _open(req, timeout=90) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"].strip()


def llm_stream(system, user, temperature=0.8, model=None):
    """逐 token 产出（流式）。"""
    body = {"model": model or MODEL, "stream": True, "effort": "low", "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], "temperature": temperature}
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=data, method="POST")
    req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("Content-Type", "application/json")
    with _open(req, timeout=120) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                delta = obj["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except Exception:
                continue


# ================= 记忆（可落盘） =================
class Memory:
    def __init__(self):
        self.emotions, self.lens_stats, self.recurring_words, self.guard_log = [], {}, {}, []

    def to_dict(self):
        return {"emotions": self.emotions, "lens_stats": self.lens_stats,
                "recurring_words": self.recurring_words, "guard_log": self.guard_log}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.emotions = d.get("emotions", [])
        m.lens_stats = d.get("lens_stats", {})
        m.recurring_words = d.get("recurring_words", {})
        m.guard_log = d.get("guard_log", [])
        return m

    def save(self, path):
        json.dump(self.to_dict(), open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        if os.path.exists(path):
            return cls.from_dict(json.load(open(path, encoding="utf-8")))
        return cls()

    def hit(self, lens, named):
        s = self.lens_stats.setdefault(lens, {"used": 0, "returned": 0, "floated": 0})
        s["used"] += 1; s["returned"] += 1
        self.recurring_words[named] = self.recurring_words.get(named, 0) + 1

    def miss(self, lens):
        s = self.lens_stats.setdefault(lens, {"used": 0, "returned": 0, "floated": 0})
        s["used"] += 1; s["floated"] += 1

    def remember(self, named, raw, domain, lens):
        self.emotions.append({"raw": raw, "named_as": named, "domain": domain, "lens": lens})

    def log(self, signal, ev):
        self.guard_log.append({"signal": signal, "evidence": ev})

    def recall(self):
        """把记忆变成一句可注入 prompt 的提示。"""
        parts = []
        if self.recurring_words:
            top = sorted(self.recurring_words.items(), key=lambda x: -x[1])[:3]
            parts.append("反复出现的词：" + "、".join(f"{w}×{n}" for w, n in top))
        if self.emotions:
            last = self.emotions[-1]
            parts.append(f"上次他给一个情绪起的名字是「{last['named_as']}」")
        return "；".join(parts) if parts else "（暂无）"


# ================= 四个节点 =================
def sensing_fallback(text):
    hi = ["一无是处", "崩溃", "完蛋", "要死", "废", "绝望", "活不下去"]
    lo = ["有点", "还好", "无所谓", "没多", "就这样"]
    domain_map = [
        ("loss",    ["分手", "失去", "离开", "没了", "告别"]),
        ("anger",   ["气", "愤怒", "委屈", "火", "不爽"]),
        ("anxiety", ["焦虑", "紧张", "担心", "无力", "慌", "怕"]),
        ("shame",   ["一无是处", "我不行", "丢人", "羞耻", "废物", "差劲"]),
        ("aimless", ["迷茫", "没意义", "空转", "不知道", "没方向"]),
    ]
    intensity = "high" if any(k in text for k in hi) else ("low" if any(k in text for k in lo) else "medium")
    domain = next((d for d, ks in domain_map if any(k in text for k in ks)), "loss")
    m = re.search(r"(一无是处|难过|焦虑|愤怒|迷茫|崩溃|委屈|空)", text)
    word = m.group(1) if m else "这件事"
    entry_lens = "mirror" if intensity == "high" else ("stranger" if intensity == "medium" else "culture")
    return {"intensity": intensity, "domain": domain, "word": word, "entry_lens": entry_lens, "raw": text}


def sensing(text):
    """用 DeepSeek 分类读状态；失败时回退到关键词规则。"""
    user = f"""读这句话，输出一个 JSON：
「{text}」

只输出一个 JSON 对象，不要任何其他文字：
{{"intent":"emotional|meta|question|chat","intensity":"high|medium|low","domain":"loss|anger|anxiety|shame|aimless","word":"他对自己下的那个判断或感受（如'很差劲''不配''一无是处'），必须来自原句；没有自我判断时才取最重的词","meaning":"他真正可能在意的那个东西（一句话，像旁观者的理解，不要下诊断）","need":"被听见|被安放|被轻轻反驳 三选一","entry_lens":"mirror|stranger|culture"}}

判定规则：
- intent：讲自己的情绪/经历→emotional；评价你刚才递的卡片或流程（"这几个都不对""换一个""卡片不合理"）→meta；问你这是什么/你是干嘛的→question；打招呼/闲聊→chat。
- intensity：强烈自我否定/崩溃/绝望→high；明显轻松/无所谓→low；其余→medium。
- domain：分手/失去/离别→loss；愤怒/委屈→anger；焦虑/紧张/担心/无力→anxiety；自我否定/羞耻/我不行→shame；迷茫/无意义/空转→aimless；拿不准就选最接近的。
- meaning：这句话底下，他真正在意的那个东西（比如"怕自己不值得被留下"）。不要写成诊断。
- need：他此刻更需要被听见、被安放，还是被轻轻反驳一下。
- entry_lens：high→mirror（先造地板）；medium→stranger；low→culture。"""
    try:
        out = llm(SYSTEM, user, 0.1)
        m = re.search(r"\{.*\}", out, re.S)
        d = json.loads(m.group(0))
        d["raw"] = text
        d.setdefault("word", "这件事")
        d.setdefault("domain", "loss")
        d.setdefault("entry_lens", "mirror")
        d.setdefault("intensity", "medium")
        d.setdefault("meaning", "")
        d.setdefault("need", "")
        d.setdefault("intent", "emotional")
        return d
    except Exception:
        return sensing_fallback(text)


_ESTRANGE_MOVES = {
    "mirror": "位置：此时此地，站在镜前。任务：让你看见自己正在做一件滑稽或多余的事。结论方向：你在维持一个没必要的姿势/执念。",
    "stranger": "位置：一个具体的、日常的陌生地方——从他的原话里长出来（他提'电话'，可以是深夜电话亭；提'加班'，可以是末班公交；提'分手'，可以是他一个人收拾东西的房间）。**不要再用'海关/边检/入境大厅'这类**。规则：这地方有一条和你认知相反的规矩（比如在这儿，伤心要登记；道歉要排队；不说话才叫礼貌）。任务：让你原本觉得'自己有错'的那件事，在这里是'无罪、甚至被鼓励'的。结论方向：你带来的那个'错'，在这个地方根本不成立。",
    "culture": "位置：隔着一层物理分割（水/玻璃/深渊），你在这一边，对方在另一边。任务：让你看见两岸各在过自己的日子。结论方向：你在这边可以有自己的生活，不必替对岸活。",
    "history": "位置：时间拉远（十年后/老年）。任务：让'老年的你'回看此刻。结论方向：这件事在那么长的时间里，只是一个点。",
    "cosmic": "位置：空间拉远（俯视/微缩模型/卫星视角）。任务：把你缩成一个点。结论方向：你把自己放得太大，其实你只是其中一个点。",
}

_ESTRANGE_EXAMPLES = """风格参照（只体会这种"具体 + 荒诞"的分量，不要照抄，要另起炉灶）：
- "如果这句话要刻在你家楼下那棵树的树皮上，只许刻五个字，刻哪五个？"
- "假设有个部门专门给人类的痛苦编号，你希望自己这条被编在第几号档案？"
- "在某个岛上，人们难过时要坐船到海上喊一声再回来。你要喊的那一声，是给谁听的？"""


def _estrange_prompt(s, mem, lens, history="", avoid=None):
    L = LENSES[lens]
    hist = f"【之前的对话】\n{history}\n\n" if history else ""
    ban = ""
    if avoid:
        ban = ("\n- **这些场景刚才已经用过，这次必须换掉**："
               + "、".join(sorted(avoid)) + "。另起一个完全不同的地方。")
    return f"""{hist}用户刚说了：「{s['raw']}」

这一轮要折的，是**他对自己下的那个判断**：「{s.get('word','')}」——不是事件里的其他细节（比如别人说了什么、改了几版）。

（我的粗略理解，可能不准，一切以他的原话为准：他可能在意的是「{s.get('meaning','')}」，此刻更需要「{s.get('need','')}」。）

用「{L['name']}」透镜，把他的话"折"一下。
这一轮怎么看：{_ESTRANGE_MOVES[lens]}

要求：
- 输出三段，空一行分隔：
  ① 接住（1-2 句）：先正面接住他（问句要先回答）。
  ② 画面（2-4 句）：按"这一轮的位置和任务"写一幅有距离的场景，里面加一件现实不可能、却逻辑通顺的事——这是"折角"。
  ③ 点题（1 句）：把**这根透镜特有的那个结论**说出来（不是解释画面，是给一个看法）。
- **每根透镜必须给出它自己的结论**（看上面"结论方向"）；不要五根都落到"你给自己判的"这同一个道理上。
- 点题可以用"不是…而是…""X 是 Y"这类有思辨性的句式（不用刻意回避）；关键是**每根透镜给出不同的结论**，不要五根都下同一个判断。
- **事实不编**（不许断言他没提过的生活细节）；隐喻可编。
- 人称全程用"你"，不问号。
- 不要沿用【之前的对话】里用过的场景/物件。{ban}
直接输出。"""


# "异乡客"最容易滑回去的几个套路场景。prompt 里禁了还不够，
# 这里做代码级兜底：命中就换一批词再生成一次。
_STALE_SCENES = ["海关", "边检", "入境", "关口", "报关", "申报", "违禁品",
                 "传送带", "边境", "检票", "安检"]


def _stale_hits(text):
    return [w for w in _STALE_SCENES if w in (text or "")]


def _strip_md(text):
    """模型偶尔会输出 Markdown 粗体/标题。前端按纯文本渲染，
    这些符号会原样露出来，所以在引擎层就清掉。"""
    t = text or ""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)   # **粗体**
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", t, flags=re.S)  # *斜体*
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)         # 行首 # 标题
    t = re.sub(r"^\s*[-－—]{3,}\s*$", "", t, flags=re.M)  # 分隔线
    t = t.replace("**", "")                               # 落单的残留
    return t.strip()


def estranging(s, mem, lens=None, history=""):
    lens = lens or s["entry_lens"]
    L = LENSES[lens]
    mdl = MODEL_BIG if DEEP else MODEL
    flesh = llm(SYSTEM_ESTRANGE, _estrange_prompt(s, mem, lens, history), 0.9, model=mdl)
    hits = _stale_hits(flesh)
    if hits:
        flesh = llm(SYSTEM_ESTRANGE,
                    _estrange_prompt(s, mem, lens, history, avoid=hits), 1.0, model=mdl)
    flesh = _strip_md(flesh)
    return {"lens": lens, "direction": L["move"],
            "constraints": ["只输出问句/画面", "不评判、不站队、不解释"], "flesh": flesh}


def estranging_stream(s, mem, lens=None, history=""):
    """流式换位置。

    先非流式探一版，命中套路场景（海关/边检/入境…）就带禁令重写一次，
    再把最终那一版逐字吐出去。代价是首字稍慢，换来的是不再每次都"过海关"。
    逐字吐之前先清掉 Markdown 符号——前端按纯文本渲染，** 会原样露出来。
    """
    lens = lens or s["entry_lens"]
    mdl = MODEL_BIG if DEEP else MODEL
    first = llm(SYSTEM_ESTRANGE, _estrange_prompt(s, mem, lens, history), 0.9, model=mdl)
    hits = _stale_hits(first)
    if not hits:
        for ch in _strip_md(first):
            yield ch
        return
    second = llm(SYSTEM_ESTRANGE,
                 _estrange_prompt(s, mem, lens, history, avoid=hits), 1.0, model=mdl)
    for ch in _strip_md(second):
        yield ch


def offering(s, mem, picture=""):
    pic = f"刚才那幅画面：{picture}\n" if picture else ""
    user = f"""用户刚说了：「{s['raw']}」
{pic}
给他 2-3 个"名字"——像他自己会给这段感受起的外号。每个 2-10 个字，读起来要能接上"对，就是「___」"。
要求：
- 名字要扣住刚才那幅画面的**意思**，不是它的道具。比如画面是"举着一个早就挂断的听筒"，名字可以是"早就没人接的姿势""替她守夜的那只手""还在应答的那个你"。
- **不要直接拿道具当名字**（"空盘子""票根""秤""体检单"这种是道具，不是名字）。
- 不要心理学术语（"课题分离""原生家庭""自我价值"）。
- 多候选、平级，以问句结尾（"哪个更贴近？还是你自己起一个"）。
先输出一句"递词"的话，然后另起一行输出 JSON 数组：["名字1","名字2","名字3"]"""
    out = llm(SYSTEM, user, 0.85)
    m = re.search(r"\[.*\]", out, re.S)
    names = []
    sentence = out
    if m:
        try:
            names = json.loads(m.group(0))
        except Exception:
            names = []
        sentence = out[:m.start()].strip()
    if not names:
        names = [e["word"] for e in DOMAINS[s["domain"]]["entries"][:3]]
        sentence = out
    return {"direction": f"递 {DOMAINS[s['domain']]['domain_name']} 域候选",
            "constraints": ["多候选、平级", "以问句结尾、留白", "绝不替用户命名"],
            "candidates": names, "flesh": sentence}


def guarding(raw, reply, history=""):
    hist = f"【之前的对话】\n{history}\n\n" if history else ""
    user = f"""{hist}用户在经历一次"认知重构"。上一轮机器把他话里的情绪"变陌生"并递了几个新名字，现在他回复了。
用户原话：「{raw}」
用户回复：「{reply}」

判断他是"回来了并重新命名"还是"飘走了(解离)"：
- 回来+重命名：主语回到第一人称"我"，情绪被指认/安放；常出现"其实/我发现/我懂了/我难过的是/我是被/原来"。
- 飘走(解离)：**只有**当他明确在把情绪关掉/抹平（第一人称消失，且出现"无所谓/算了/看开了/都是假的/人总要…的/没什么/不重要"这类话）时才判。只是正常回应、描述、提问、开玩笑 → 判 "unclear"，不要硬判 float。
- 对递的名字或流程有意见（"这几个都不对""卡片不合理""换个方式"）：signal "meta"。
- 未明：都不是。

若判定"回来"，提取他给这个情绪起的"新名字"。
只输出 JSON，不要任何其他文字：
{{"signal": "return|float|meta|unclear", "named": "名字或null", "evidence": "一句话证据"}}"""
    out = llm(SYSTEM, user, 0.1)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"signal": "unclear", "named": None, "evidence": out[:80]}


def _close_prompt(named):
    return f"用户刚给自己的情绪起名「{named}」。请用一句话温柔收尾：确认这个名字，但不评价、不表扬、不总结道理。不要承诺你会记得、下次认得（这个练习是无记忆的）。直接输出那句话。"


def close(named):
    return llm(SYSTEM, _close_prompt(named), 0.8)


def close_stream(named):
    return llm_stream(SYSTEM, _close_prompt(named), 0.8)


def _pullback_prompt(text=""):
    return f"用户刚才说：「{text}」。他似乎在用'算了/无所谓'把情绪推开。请用一句话把他拉回第一人称：不评判他，只温柔地把镜头从'人/都'拉回'你'。直接输出那句话。"


def pullback(text=""):
    return llm(SYSTEM, _pullback_prompt(text), 0.8)


def pullback_stream(text=""):
    return llm_stream(SYSTEM, _pullback_prompt(text), 0.8)


def _reoffer_prompt(s):
    return "用户对你的递词没接住（信号不明）。请再轻轻问一次：他此刻的感受，更贴近哪个名字，或者他自己想叫什么。不要重复同样的句子。直接输出那句话。"


def reoffer(s):
    return llm(SYSTEM, _reoffer_prompt(s), 0.8)


def reoffer_stream(s):
    return llm_stream(SYSTEM, _reoffer_prompt(s), 0.8)


SYSTEM_CHAT = """你是"折角"这个练习的引导者。用户这句话不是在讲他自己的情绪，而是在问你、评价这个练习、或者闲聊。
请自然地、简短地回应他（1-2 句），像一个安静、诚实的人。不卖弄、不诊断、不为难他。
- 他问"这是什么/你是干嘛的"：简短说明这是个每天一次的练习，帮你把一句话换个位置看，再自己给它起个名字。
- 他评价卡片/流程（比如"这几个都不对""换一个"）：接住他的评价，把选择权还给他（"那我们换一批""你直接说它在你心里更像什么"）。
- 闲聊：正常回。
只输出你要说的话，不要前缀。"""


def chat_reply(text, context=""):
    user = (context + "\n" if context else "") + f"用户说：「{text}」请自然回应（1-2 句）。"
    return llm_stream(SYSTEM_CHAT, user, 0.7)


SYSTEM_RESPOND = """你是"折角"这个练习的对象。用户刚回了一句话。
请自然地回应（1-2 句），像一个安静、诚实、在认真听的人：
- 先接住他说的话（复述/确认其中的一个细节），让他觉得你听懂了。
- 然后可以轻轻问一句，把话往下带；不要急着让他"起名字"。
- 不分析、不诊断、不评判、不说教、不替他下结论。
- 不要另起话题，不要编造他没提过的事。
只输出你要说的话。"""


def respond(text, history="", raw=""):
    user = ""
    if history:
        user += f"【之前的对话】\n{history}\n\n"
    user += f"他最初说：「{raw}」\n他刚才回：「{text}」\n请自然回应（1-2 句）。"
    return llm_stream(SYSTEM_RESPOND, user, 0.8)


def after_named_stream(named):
    return llm_stream(SYSTEM_RESPOND, f"用户刚给自己的情绪起名「{named}」。先用一句话确认这个名字（不评价、不表扬、不总结道理），再轻轻问一句：这个「{named}」一般什么时候最明显、或者跟哪件事有关？两句话，自然点，直接输出。", 0.8)


def farewell_stream():
    return llm_stream(SYSTEM_RESPOND, "用户说他想结束这次了。请用一句话收尾：不煽情、不挽留、不说教，像关掉一盏灯。"
                                      "然后另起一句，平静地告诉他：想合上这张卡片的话，按住右上角那个'合上'；合上之后就不能再往下说了。"
                                      "不要劝他合、也不要劝他别合。直接输出这两句。", 0.8)


# ================= 交互循环 =================
def interactive(mem_path=MEM_FILE):
    mem = Memory.load(mem_path)
    print("=" * 56)
    print("折角 · 交互版")
    print("它会陪你换位置、递词，但绝不替你下结论。")
    print("命令：/记忆 查看记忆 · /退出 保存并离开")
    print("=" * 56)
    phase, ctx = "opening", None
    while True:
        try:
            text = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。"); break
        if not text:
            continue
        if text in ("/退出", "/exit", "/quit", "退出", "q"):
            mem.save(mem_path); print("已保存记忆。再见。"); break
        if text in ("/记忆", "/memory"):
            print(json.dumps(mem.to_dict(), ensure_ascii=False, indent=2)); continue

        if phase == "opening":
            s = sensing(text)
            e = estranging(s, mem)
            o = offering(s, mem)
            print(f"\n机 > {e['flesh']}")
            print(f"机 > {o['flesh']}")
            ctx = s; phase = "awaiting"
        else:
            g = guarding(ctx["raw"], text)
            if g.get("signal") == "return":
                named = g.get("named") or ctx["word"]
                mem.hit(ctx["entry_lens"], named)
                mem.remember(named, ctx["raw"], ctx["domain"], ctx["entry_lens"])
                mem.log("return", g.get("evidence"))
                print(f"机 > {close(named)}")
                phase, ctx = "opening", None
            elif g.get("signal") == "float":
                mem.miss(ctx["entry_lens"]); mem.log("float", g.get("evidence"))
                print(f"机 > {pullback()}")
            else:
                print(f"机 > {reoffer(ctx)}")
        mem.save(mem_path)


# ================= 演示模式 =================
def demo():
    if os.path.exists(DEMO_MEM):
        os.remove(DEMO_MEM)
    mem = Memory.load(DEMO_MEM)
    script = [
        ("opening", "我分手了，好难过，感觉自己一无是处。"),
        ("reply",   "对……我是被留下的。我难过不是因为我不行，是因为我被留在原地了。"),
        ("opening", "我下周要答辩，特别焦虑，总觉得自己会搞砸。"),
        ("reply",   "对，我发现了，我是把明天提前搬来今天住了。"),
    ]
    print("【演示：多轮对话 + 记忆累积 + 彩蛋】")
    phase, ctx = "opening", None
    for kind, text in script:
        print("\n" + "-" * 56)
        print(f"你 > {text}")
        if kind == "opening":
            s = sensing(text)
            e = estranging(s, mem)
            o = offering(s, mem)
            print(f"机 > {e['flesh']}")
            print(f"机 > {o['flesh']}")
            ctx = s; phase = "awaiting"
        else:
            g = guarding(ctx["raw"], text)
            print(f"  (守门: {g.get('signal')} / named={g.get('named')})")
            if g.get("signal") == "return":
                named = g.get("named") or ctx["word"]
                mem.hit(ctx["entry_lens"], named)
                mem.remember(named, ctx["raw"], ctx["domain"], ctx["entry_lens"])
                mem.log("return", g.get("evidence"))
                print(f"机 > {close(named)}")
                phase, ctx = "opening", None
            elif g.get("signal") == "float":
                mem.miss(ctx["entry_lens"]); mem.log("float", g.get("evidence"))
                print(f"机 > {pullback()}")
            else:
                print(f"机 > {reoffer(ctx)}")
        mem.save(DEMO_MEM)
    print("\n" + "=" * 56)
    print("落盘后的记忆（记忆_demo.json）：")
    print(json.dumps(mem.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        interactive()
