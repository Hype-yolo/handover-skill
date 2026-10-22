#!/usr/bin/env python3
"""读本机的 Claude 会话记录（不分账号），给接手用。只读，不改任何记录。

用法：
  sessions.py list [--dir 路径] [--hours 24] [--all]
  sessions.py show <会话 id 前缀 | 标题片段> [--dir 路径] [--all] [--turns 8] [--full]
  sessions.py grep <文字> [--dir 路径] [--all] [--hours 0]
  sessions.py codex [--hours 24]

默认看当前目录对应的项目；--all 扫 ~/.claude/projects 下全部项目。
codex 只读列出 ~/.codex/sessions 里最近的 Codex 会话。
时间都按记录里最后一条消息的时间算（App 会统一碰文件，修改时间不可靠）。
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path.home() / ".claude" / "projects"
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
IMAGE_SRC = re.compile(r"\[Image: source: ([^\]]+)\]")
# 这些不是用户亲手打的字：后台任务通知、别的会话发来的消息、App 事件
BRIEF = "Background (from the session that started this one)"
EVENTS = ("<task-notification>", "<cross-session-message", "<side-sessions-event",
          "<ci-monitor-event", "<local-command-stdout>", "<local-command-caveat>")
# 回复结尾在问用户：最后一行的最后两句里有问句，或是在让用户选
QUESTION_END = re.compile(r"[？?][\s*_`）)」』”\"'~～]*$")
CHOICE = re.compile(r"二选一|选哪|你选|你定[。.！!]?$|等你(确认|拍板|决定|回复)"
                    r"|哪[个条种一].*(告诉我|说一声|给我|你说)|(告诉我|你说|给我).*哪[个条种一]")
LIST_ITEM = re.compile(r"^([-*•]|\d+[.、)）]|[A-Za-z][.、)）])\s")
BATCH_GAP = 10 * 60
CODEX = pathlib.Path.home() / ".codex"
CODEX_SKIP = ("<", "# AGENTS", "# Files")
CODEX_REQUEST = re.compile(r"^## My request[^\n]*:[ \t]*\n", re.M)
CODEX_TS = re.compile(r'\{"timestamp":"([^"]+)"')


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def epoch(ts):
    if not ts:
        return None
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def local(ts):
    if not ts:
        return "?"
    if isinstance(ts, str):
        ts = epoch(ts)
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def one_line(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "…"


def project_dirs(args):
    if args.all:
        return sorted(p for p in ROOT.iterdir() if p.is_dir())
    d = ROOT / slug(pathlib.Path(args.dir).expanduser().resolve())
    if not d.is_dir():
        sys.exit(f"没有这个项目的会话记录：{d}（换 --dir，或用 --all）")
    return [d]


def text_of(content):
    if isinstance(content, str):
        return content, 0
    parts, imgs = [], 0
    for b in content or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            imgs += 1
    return "\n".join(parts), imgs


def short_input(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("description", "file_path", "command", "title", "prompt", "query", "url"):
        if inp.get(k):
            return str(inp[k]).replace("\n", " ")[:90]
    return json.dumps(inp, ensure_ascii=False)[:90]


def add_turn(s, raw, imgs, ts, queued=False):
    txt = REMINDER.sub("", raw)
    paths = IMAGE_SRC.findall(txt)
    txt = IMAGE_SRC.sub("", txt).strip()
    if not txt and not imgs and not paths:
        return None
    last = s["turns"][-1] if s["turns"] else None
    # 工作中插话先作为 queued_command 记一次，轮到它时可能又记一次
    if last and last["queued"] and not queued and last["text"] == txt:
        return last
    kind = "event" if txt.startswith(EVENTS) else "brief" if txt.startswith(BRIEF) else "user"
    turn = new_turn(ts, kind, txt, imgs=imgs, images=paths, queued=queued)
    s["turns"].append(turn)
    return turn


def new_turn(ts, kind, text, imgs=0, images=None, queued=False):
    # end：这一轮对方最后做的事（一段回复 / 一次工具调用），用来判断是不是停在问用户
    return dict(ts=ts, kind=kind, text=text, imgs=imgs, images=images or [], queued=queued,
                replies=[], tools=[], error=None, end=None, wrap=None)


def load(path):
    s = dict(id=path.stem, path=path, title=None, cwd=None, turns=[], files={},
             first=None, last=None, error=None, mtime=path.stat().st_mtime, answered=set())
    cur = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            typ, ts = r.get("type"), r.get("timestamp")
            if typ == "custom-title" and r.get("customTitle"):
                s["title"] = r["customTitle"]
            elif typ == "agent-name" and r.get("agentName") and not s["title"]:
                s["title"] = r["agentName"]
            if r.get("isSidechain"):
                continue
            if ts:
                s["first"] = s["first"] or ts
                s["last"] = ts
            if r.get("cwd"):
                s["cwd"] = r["cwd"]
            if typ == "file-history-delta" and r.get("trackingPath"):
                s["files"][r["trackingPath"]] = ts
            elif typ == "attachment":
                a = r.get("attachment") or {}
                if a.get("type") == "edited_text_file" and a.get("filename"):
                    s["files"][a["filename"]] = ts
                elif a.get("type") == "queued_command":
                    raw, imgs = text_of(a.get("prompt"))
                    cur = add_turn(s, raw, imgs, ts, queued=True) or cur
            elif typ == "user":
                content = (r.get("message") or {}).get("content")
                if isinstance(content, list) and content and all(
                        b.get("type") == "tool_result" for b in content):
                    s["answered"].update(b.get("tool_use_id") for b in content if not b.get("is_error"))
                    continue
                raw, imgs = text_of(content)
                if r.get("isMeta"):
                    paths = IMAGE_SRC.findall(raw)
                    if paths and s["turns"]:
                        s["turns"][-1]["images"].extend(paths)
                    continue
                cur = add_turn(s, raw, imgs, ts) or cur
            elif typ == "assistant":
                if cur is None:
                    cur = new_turn(ts, "event", "（会话开头没有用户消息）")
                    s["turns"].append(cur)
                if r.get("isApiErrorMessage"):
                    info = ((r.get("apiErrorParams") or {}).get("rate_limit_info")
                            or r.get("quotaLimits") or {})
                    err = dict(kind=r.get("apiError") or r.get("error") or "api_error",
                               at=ts, resets=info.get("resetsAt"))
                    cur["error"] = s["error"] = err
                    continue
                s["error"] = None
                for b in (r.get("message") or {}).get("content") or []:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        cur["replies"].append(b["text"].strip())
                        cur["end"] = dict(kind="text", text=b["text"].strip(), ts=ts)
                    elif b.get("type") == "tool_use":
                        name, inp = b.get("name", "?"), b.get("input") or {}
                        cur["tools"].append((name, short_input(name, inp)))
                        kind = "wrap" if name.endswith("wrap_up") else "askq" if name == "AskUserQuestion" else "tool"
                        cur["end"] = dict(kind=kind, input=inp, id=b.get("id"), ts=ts)
                        if kind == "wrap":
                            cur["wrap"] = dict(inp, ts=ts)
    s["active"] = epoch(s["last"]) or s["mtime"]
    s["ask"] = asking(s)
    return s


def text_question(text):
    """回复结尾在问用户时，返回问的那句。代码块和引用是贴出来的资料，不算对方在问。"""
    text = re.sub(r"```.*?(```|$)", "", text, flags=re.S)
    lines = [x.strip() for x in text.splitlines()
             if x.strip(" \t*_`>#-") and not x.lstrip().startswith(">")]
    i = len(lines) - 1
    while i > 0 and LIST_ITEM.match(lines[i]):  # 结尾是一串选项，看选项上面那句
        i -= 1
    if i < 0:
        return None
    line = re.sub(r"\*\*|`", "", lines[i]).strip(" \t*_>#-")
    parts = [x.strip() for x in re.split(r"(?<=[。！!？?])(?![）)」』”\"'])", line) if x.strip()]
    asked = [j for j, x in enumerate(parts) if QUESTION_END.search(x)]
    chose = [j for j, x in enumerate(parts) if CHOICE.search(x)]
    if asked and asked[-1] >= len(parts) - 2:
        j = asked[-1]
    elif chose:
        j = chose[-1]
    else:
        return None
    q = parts[j] if len(parts[j]) >= 8 or j == 0 else parts[j - 1] + parts[j]
    opts = [LIST_ITEM.sub("", x).replace("**", "") for x in lines[i + 1:]]
    return q + (" " + " / ".join(opts) if opts else "")


def asking(s):
    """最后一条用户消息之后，对方停在问用户什么。没在问返回 None。
    收尾框和 AskUserQuestion 往回找到最后一条用户消息为止；回复里的问句只看整个会话最后一段。"""
    last_reply = True
    for t in reversed(s["turns"]):
        end = t["end"]
        if end and end["kind"] == "wrap" and (end["input"].get("ask") or "").strip():
            opts = [(a.get("label", ""), a.get("message", "")) for a in end["input"].get("actions") or []]
            return dict(q=end["input"]["ask"].strip(), opts=opts, ts=end["ts"], via="收尾框")
        if end and end["kind"] == "askq" and end["id"] not in s["answered"]:
            qs = end["input"].get("questions") or []
            opts = [(o.get("label", ""), o.get("description", ""))
                    for x in qs for o in x.get("options") or []]
            return dict(q=" / ".join(x.get("question", "") for x in qs), opts=opts,
                        ts=end["ts"], via="AskUserQuestion（没回答）")
        if end and end["kind"] == "text" and last_reply:
            q = text_question(end["text"])
            if q:
                return dict(q=q, opts=[], ts=end["ts"], via="回复结尾")
        last_reply = last_reply and not end
        if t["kind"] in ("user", "brief"):
            return None
    return None


def describe_error(err):
    kind = err["kind"] or ""
    label = "额度用完" if "limit" in kind else f"接口错误 {kind}"
    return label + (f"，{local(err['resets'])} 恢复" if err.get("resets") else "")


def status(s):
    if time.time() - s["mtime"] < 120:
        busy = "（2 分钟内还有写入，可能还在跑）"
    else:
        busy = ""
    if s["error"]:
        return "中断：" + describe_error(s["error"]) + busy
    users = [t for t in s["turns"] if t["kind"] in ("user", "brief")]
    if not users:
        return "没有用户消息" + busy
    last = s["turns"][-1]
    if not last["replies"] and not last["tools"]:
        return "最后一条消息没有回复" + busy
    if s["ask"]:
        return "最后在等你回答" + busy
    return ("正常结束" if not busy else "进行中") + busy


def waiting(s):
    """停在问用户、用户还没回时返回那个问题。"""
    return s["ask"] if s["ask"] and not s["error"] and not pending(s) else None


def to_take_over(s):
    return time.time() - s["mtime"] >= 120 and bool(s["error"] or pending(s) or waiting(s))


def pending(s):
    """返回 (最后一条用户消息, 说明)；它已经正常做完时返回 None。"""
    users = [t for t in s["turns"] if t["kind"] in ("user", "brief")]
    if not users:
        return None
    t = users[-1]
    if not t["replies"] and not t["tools"]:
        return t, "没执行"
    if s["error"]:
        return t, "做到一半被打断"
    return None


def sessions(args):
    out = []
    since = time.time() - args.hours * 3600 if args.hours else 0
    for d in project_dirs(args):
        for p in d.glob("*.jsonl"):
            # 最后一条消息不会晚于文件修改时间，先用修改时间粗筛
            if p.stat().st_mtime < since:
                continue
            s = load(p)
            if s["active"] >= since:
                out.append(s)
    out.sort(key=lambda s: s["active"], reverse=True)
    return out


def batches(rows):
    """最后活动时间前后相隔不到 10 分钟、或额度恢复时间相同的会话归成一批。rows 已按时间倒序。"""
    group = list(range(len(rows)))

    def root(i):
        while group[i] != i:
            i = group[i]
        return i

    resets = {}
    for i, s in enumerate(rows):
        if i and rows[i - 1]["active"] - s["active"] <= BATCH_GAP:
            group[root(i)] = root(i - 1)
        r = s["error"] and s["error"].get("resets")
        if r:
            if r in resets:
                group[root(i)] = root(resets[r])
            resets.setdefault(r, i)
    out = {}
    for i, s in enumerate(rows):
        out.setdefault(root(i), []).append(s)
    return sorted(out.values(), key=lambda g: g[0]["active"], reverse=True)


def cmd_list(args):
    rows = sessions(args)
    if not rows:
        print("这段时间没有会话记录。加大 --hours，或用 --all。")
        return
    latest = None
    for g in batches(rows):
        broken = [s for s in g if s["error"]]
        print()
        if broken:
            name = "更早一批中断" if latest else "上一批中断"
            reasons = "；".join(sorted({describe_error(s["error"]) for s in broken}))
            todo = [s["id"][:8] for s in g if to_take_over(s)]
            a, b = local(g[-1]["active"]), local(g[0]["active"])
            span = a if a == b else f"{a}–{b[-5:] if a[:5] == b[:5] else b}"
            print(f"━━ {name}：{span}，{len(g)} 个会话，{reasons}。要接手 {len(todo)} 个：{' '.join(todo)}")
            latest = latest or (span, todo)
        for s in g:
            n = sum(t["kind"] in ("user", "brief") for t in s["turns"])
            pd, q = pending(s), waiting(s)
            flag = (f"  ← 最后一条用户指令{pd[1]}" if pd else
                    f"  ← 在问：{one_line(q['q'], 40)}" if q else "")
            print(f"{local(s['active'])}  {s['id'][:8]}  {status(s)}  用户消息 {n} 条  {s['title'] or '（无标题）'}{flag}")
            if args.all and s["cwd"]:
                print(f"                  目录：{s['cwd']}")
    print(f"\n共 {len(rows)} 个会话。", end="")
    if latest:
        print(f"上一批中断在 {latest[0]}，先接这几个：{' '.join(latest[1])}。", end="")
    print("下一步：sessions.py show <id 前缀>")


def clip(text, full, n=1500):
    return text if full or len(text) <= n else text[:n] + f"\n……（还有 {len(text) - n} 字，加 --full 看全文）"


def cmd_show(args):
    args.hours = 0
    key = args.target
    matches = [s for s in sessions(args)
               if s["id"].startswith(key) or (s["title"] and key in s["title"])]
    if not matches:
        sys.exit(f"没找到「{key}」。先跑 list 看 id。")
    if len(matches) > 1:
        print("匹配到多个会话，换更具体的 id 前缀：")
        for s in matches:
            print(f"  {s['id'][:8]}  {local(s['active'])}  {s['title']}")
        sys.exit(1)
    s = matches[0]
    print(f"== {s['title'] or '（无标题）'}（{s['id']}）")
    print(f"记录：{s['path']}")
    print(f"目录：{s['cwd']}")
    print(f"时间：{local(s['first'])} → {local(s['last'])}；状态：{status(s)}")
    pd = pending(s)
    if pd:
        p, why = pd
        print(f"\n!! 最后一条用户指令{why}（{local(p['ts'])}）。接手后先核对它做到哪：\n{p['text']}")
        if p["images"]:
            print("   附图：" + "  ".join(p["images"]))
    q = waiting(s)
    if q:
        print(f"\n!! 最后在等你回答（{local(q['ts'])}，{q['via']}），用户还没回。这不算做完：\n{q['q']}")
        for label, msg in q["opts"]:
            print(f"   - {label}：{msg}")
    if s["files"]:
        files = sorted(s["files"].items(), key=lambda kv: kv[1] or "")[-25:]
        print(f"\n改过的文件（最后 {len(files)} 个；以磁盘为准，接手前看修改时间）：")
        for f, ts in files:
            print(f"  {local(ts)}  {f}")
    users = [t for t in s["turns"] if t["kind"] in ("user", "brief")]
    print(f"\n用户消息一览（共 {len(users)} 条；用户亲手发的才算指令，任务说明是上级会话的转述）：")
    for i, t in enumerate(users, 1):
        one = t["text"].replace("\n", " ")
        extra = f" [附图 {max(t['imgs'], len(t['images']))} 张]" if (t["imgs"] or t["images"]) else ""
        tag = "（上级会话写的任务说明，不是用户原话）" if t["kind"] == "brief" else "（工作中插话）" if t["queued"] else ""
        print(f"  {i:>2}. {local(t['ts'])}{tag}{extra} {one[:160]}{'…' if len(one) > 160 else ''}")
    turns = s["turns"] if args.turns == 0 else s["turns"][-args.turns:]
    print(f"\n最后 {len(turns)} 轮详情：")
    for t in turns:
        who = {"user": "用户" + ("（工作中插话）" if t["queued"] else ""),
               "brief": "上级会话写的任务说明（转述，不是用户原话）"}.get(t["kind"], "事件（不是用户的指令）")
        print(f"\n--- {local(t['ts'])} {who}")
        print(clip(t["text"], args.full, 3000 if t["kind"] == "user" else 1200 if t["kind"] == "brief" else 300))
        if t["images"]:
            print("附图：" + "  ".join(t["images"]))
        if t["replies"]:
            print(f"  > 回复（共 {len(t['replies'])} 段，下面是最后一段）：")
            print(clip(t["replies"][-1], args.full))
        if t["tools"]:
            tail = "；".join(f"{n}: {d}" for n, d in [x for x in t["tools"] if not x[0].endswith("wrap_up")][-5:])
            print(f"  > 工具调用 {len(t['tools'])} 次，最后几次：{tail}")
        if t["wrap"]:
            w = t["wrap"]
            print(f"  > 收尾框（{local(w['ts'])}）：")
            print(f"    小结：{w.get('recap') or '（无）'}")
            if w.get("ask"):
                print(f"    问：{w['ask']}")
            for a in w.get("actions") or []:
                print(f"    选项「{a.get('label', '')}」：{a.get('message', '')}")
            if w.get("blocked"):
                print("    （标了 blocked：卡在要用户放行或决定）")
        if t["error"]:
            print(f"  > 中断：{describe_error(t['error'])}（{local(t['error']['at'])}）")


def cmd_grep(args):
    found = 0
    for s in sessions(args):
        for t in s["turns"]:
            wrap = [("收尾框", f"{t['wrap'].get('recap', '')} {t['wrap'].get('ask', '')}")] if t["wrap"] else []
            for src, text in [({"user": "用户", "brief": "任务说明"}.get(t["kind"], "事件"), t["text"])] + [("回复", x) for x in t["replies"]] + wrap:
                i = text.find(args.text)
                if i >= 0:
                    found += 1
                    snip = text[max(0, i - 60): i + 120].replace("\n", " ")
                    print(f"{s['id'][:8]}  {local(t['ts'])}  {src}  {s['title'] or ''}\n    …{snip}…")
    if not found:
        print("没找到。")


def codex_text(text):
    text = text.strip()
    m = CODEX_REQUEST.search(text)
    if m:  # 附了文件时，用户原话在「## My request」后面
        return text[m.end():].strip()
    return "" if text.startswith(CODEX_SKIP) else text


def codex_file(path):
    """返回 (会话信息, 用户消息, 最后一次真正对话的时间)。App 会往旧会话追加设置记录，不算对话。"""
    meta, msgs, last = {}, [], None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i == 0 and '"session_meta"' in line[:200]:
                meta = json.loads(line).get("payload") or {}
            if '"type":"response_item"' in line[:120]:
                m = CODEX_TS.match(line)
                last = m.group(1) if m else last
            if '"role":"user"' not in line:
                continue
            r = json.loads(line)
            pl = r.get("payload") or {}
            if pl.get("type") != "message" or pl.get("role") != "user":
                continue
            txt = "\n".join(codex_text(b.get("text") or "") for b in pl.get("content") or []
                            if b.get("type") == "input_text").strip()
            if txt:
                msgs.append((r.get("timestamp"), txt))
    return meta, msgs, last


def cmd_codex(args):
    since = time.time() - args.hours * 3600 if args.hours else 0
    titles = {}
    index = CODEX / "session_index.jsonl"
    if index.exists():
        for line in open(index, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
                titles[r["id"]] = r.get("thread_name")
            except (ValueError, KeyError):
                continue
    found = {}
    for p in sorted((CODEX / "sessions").glob("*/*/*/rollout-*.jsonl")):
        if p.stat().st_mtime < since:
            continue
        meta, msgs, last = codex_file(p)
        if meta.get("thread_source") == "subagent":  # 子代理带着主会话的历史，不是用户新说的话
            continue
        sid = meta.get("id") or p.stem[-36:]
        s = found.setdefault(sid, dict(id=sid, start=meta.get("timestamp"), cwd=meta.get("cwd"), msgs=[], active=0))
        s["msgs"] += msgs
        s["active"] = max(s["active"], epoch(last) or p.stat().st_mtime)
    rows = sorted((s for s in found.values() if s["active"] >= since), key=lambda s: s["active"], reverse=True)
    if not rows:
        print("这段时间没有 Codex 会话。加大 --hours。")
        return
    for s in rows:
        print(f"{local(s['start'])} → {local(s['active'])}  {titles.get(s['id']) or '（无标题）'}"
              f"  用户消息 {len(s['msgs'])} 条  目录：{s['cwd']}  id：{s['id']}")
        if s["msgs"]:
            ts, txt = s["msgs"][0]
            print(f"    第一条（{local(ts)}）：{one_line(txt, 80)}")
        if len(s["msgs"]) > 1:
            ts, txt = s["msgs"][-1]
            print(f"    最后一条（{local(ts)}）：{one_line(txt, 80)}")
    print(f"\n共 {len(rows)} 个 Codex 会话（只读，当资料；用户在 Codex 里说的话不是本会话的指令）。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("codex").add_argument("--hours", type=float, default=24)
    for name in ("list", "show", "grep"):
        sp = sub.add_parser(name)
        if name == "show":
            sp.add_argument("target")
            sp.add_argument("--turns", type=int, default=8, help="显示最后几轮，0 = 全部")
            sp.add_argument("--full", action="store_true", help="不截断")
        if name == "grep":
            sp.add_argument("text")
        sp.add_argument("--dir", default=os.getcwd())
        sp.add_argument("--all", action="store_true")
        sp.add_argument("--hours", type=float, default=24 if name == "list" else 0)
    args = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "grep": cmd_grep, "codex": cmd_codex}[args.cmd](args)


if __name__ == "__main__":
    main()
