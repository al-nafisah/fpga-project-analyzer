"""Compact text views of a design for the agent (summary, show, findings)."""
from __future__ import annotations

from . import model as M
from .lint import _size

SEV = {"high": "H", "med": "M", "low": "L", "info": "i"}
SEV_ORDER = {"high": 0, "med": 1, "low": 2, "info": 3}


def _bd(design):
    p = design["meta"].get("bd_path", "")
    d, _ = M.resolve(design, p) if p else (design["defs"][design["top"]], None)
    return p, M.body(design, d) if d else None


def _peers(design, body, inst, pin):
    defs = design["defs"]
    for n in body["nets"]:
        if [inst, pin] in n["ends"]:
            return n, [e for e in n["ends"] if e != [inst, pin]]
    return None, []


def _pin_desc(p):
    bits = [p["dir"], p["name"]]
    if p["kind"] == "bus":
        bits.append(p.get("bus", "bus") + (" " + p["proto"] if p.get("proto") else ""))
    elif p["kind"] != "data":
        bits.append(p["kind"] + ("(%s)" % p["pol"] if p.get("pol") == "low" and p["kind"] == "rst" else ""))
    if p.get("w") and p["w"] > 1:
        bits.append("%db" % p["w"])
    if p.get("freq"):
        bits.append("%g MHz" % (p["freq"] / 1e6))
    if p.get("unc"):
        bits.append("unconnected")
    return " ".join(bits)


def _irq_map(design, bd_path, body):
    defs = design["defs"]
    out = []
    ps = next((i for i in body["insts"] if defs[i["def"]]["kind"] == "ps"), None)
    if not ps:
        return out
    pins = [p["name"] for p in defs[ps["def"]]["pins"] if p["name"] in ("IRQ_F2P", "pl_ps_irq0", "pl_ps_irq1")]
    for pn in pins:
        n, peers = _peers(design, body, ps["name"], pn)
        if not n:
            continue
        for inst, pin in peers:
            idef = defs[next(i["def"] for i in body["insts"] if i["name"] == inst)] if inst else None
            if idef and idef["name"] == "xlconcat":
                params = next(i for i in body["insts"] if i["name"] == inst).get("params", {})
                bit = 0
                k = 0
                while True:
                    ipin = "In%d" % k
                    if ipin not in M.pin_index(idef):
                        break
                    w = int(params.get("IN%d_WIDTH" % k, 1) or 1)
                    _, src = _peers(design, body, inst, ipin)
                    if src:
                        out.append((pn, bit, "%s/%s" % tuple(src[0])))
                    bit += w
                    k += 1
            else:
                out.append((pn, 0, "%s/%s" % (inst, pin)))
    lines = []
    for pn, bit, src in out:
        if pn == "IRQ_F2P":
            gic = 61 + bit if bit < 8 else 84 + bit - 8
        else:
            gic = (121 if pn == "pl_ps_irq0" else 136) + bit
        lines.append("%s → %s[%d] GIC %d (DT <0 %d 4>)" % (src, pn, bit, gic, gic - 32))
    return lines


def summary(design: dict) -> str:
    defs = design["defs"]
    meta = design["meta"]
    top = defs[design["top"]]
    out = []
    srcs = "+".join(dict.fromkeys(s["kind"] for s in meta.get("sources", [])))
    out.append("%s · %s · Vivado %s · from %s" % (top["name"], meta.get("part") or "part?",
                                                   meta.get("tool") or meta.get("vivado") or "?", srcs))
    if meta.get("incomplete"):
        out.append("NOTE " + meta["incomplete"])
    allp = list(M.walk(design))
    depth = max((p.count("/") for p, _, _ in allp), default=0)
    out.append("instances %d, depth %d, top=%s" % (len(allp), depth, design["top"]))
    bd_path, body = _bd(design)
    addr = {}
    if body is not None:
        for a in body.get("addr", []):
            if not a.get("excluded") and "DDR" not in (a.get("block") or "").upper():
                addr[(a["slave"], a["intf"])] = a
    level = M.body(design, top)
    for inst in level["insts"]:
        d = defs[inst["def"]]
        if d["kind"] in ("reg", "comb", "latch"):
            continue
        p = "/" + inst["name"]
        extra = []
        for (slave, intf), a in addr.items():
            if slave == bd_path + p or slave == p:
                extra.append("%s @%s %s" % (intf, a["base"], a.get("range") or ""))
        sub = M.body(design, d)
        kids = [i["name"] for i in sub["insts"] if defs[i["def"]]["kind"] not in ("reg", "comb", "latch")]
        logic = len(sub["insts"]) - len(kids)
        if d.get("impl"):
            extra.append("→ " + d["impl"])
        if kids:
            extra.append("{" + ", ".join(kids[:6]) + (" …" if len(kids) > 6 else "") + "}")
        if logic:
            extra.append("+%d logic" % logic)
        out.append("  %-26s %-7s %-22s %s" % (p, d["kind"], d["name"], "  ".join(extra)))
    doms = design.get("domains", {})
    if doms:
        cnt = {}
        for k, v in design.get("netdom", {}).items():
            for dd in v:
                cnt[dd] = cnt.get(dd, 0) + 1
        ranked = sorted(doms.items(), key=lambda kv: -cnt.get(kv[0], 0))
        out.append("clock domains: " + ", ".join(
            "%s%s (%d nets)" % (v["name"], " %g MHz" % (v["freq"] / 1e6) if v.get("freq") else "", cnt.get(k, 0))
            for k, v in ranked[:10]) + (" +%d more" % (len(ranked) - 10) if len(ranked) > 10 else ""))
    if body is not None:
        streams, mm = [], []
        for n in body["nets"]:
            if n["kind"] != "bus":
                continue
            ends = n["ends"]
            kinds = []
            for inst, pn in ends:
                t = body if inst == "" else defs[next(i["def"] for i in body["insts"] if i["name"] == inst)]
                kinds.append(M.pin_index(t).get(pn, {}))
            bus = next((k.get("bus") for k in kinds if k.get("bus")), "")
            src = [("%s/%s" % (e[0], e[1]) if e[0] else "port:" + e[1]) for e, k in zip(ends, kinds)
                   if (k.get("dir") == "out") == bool(e[0])]
            dst = [("%s/%s" % (e[0], e[1]) if e[0] else "port:" + e[1]) for e, k in zip(ends, kinds)
                   if (k.get("dir") == "out") != bool(e[0])]
            line = "%s → %s" % (",".join(src) or "?", ",".join(dst) or "?")
            (streams if bus == "axis" else mm if bus == "aximm" else []).append(line)
        if streams:
            out.append("streams: " + "; ".join(streams))
        if mm:
            out.append("axi-mm: " + "; ".join(mm[:12]) + (" …" if len(mm) > 12 else ""))
        irq = _irq_map(design, bd_path, body)
        if irq:
            out.append("interrupts: " + "; ".join(irq))
        if addr:
            out.append("address map: " + ", ".join("%s/%s %s+%s" % (s.rsplit("/", 1)[-1], i, a["base"], a.get("range") or "")
                                                   for (s, i), a in sorted(addr.items(), key=lambda kv: kv[1]["base"])))
    io = [_pin_desc(p).replace("in ", "", 1).replace("out ", "", 1) for p in top["pins"]]
    if io:
        out.append("top ports: " + "; ".join(io[:14]) + (" …" if len(io) > 14 else ""))
    ann = design.get("annotations", [])
    if ann:
        by = {}
        for a in ann:
            by.setdefault(a["file"], {}).setdefault(a["path"], 0)
            by[a["file"]][a["path"]] += 1
        out.append("software: " + "; ".join("%s → %s" % (f, ", ".join("%s×%d" % (p.rsplit("/", 1)[-1], n)
                                                                         for p, n in m.items()))
                                             for f, m in by.items()))
    rep = design.get("reports")
    if rep:
        from .reports import format_facts
        out.append(format_facts({k: {"file": v["file"], "data": v["data"]} for k, v in rep.items()}))
    if meta.get("logs"):
        out.append("logs: " + ", ".join(meta["logs"][:6]) + "  (fpa.py logs <file>)")
    fs = design.get("findings", [])
    if fs:
        cnt = {}
        for f in fs:
            cnt[f["sev"]] = cnt.get(f["sev"], 0) + 1
        out.append("findings: " + ", ".join("%d %s" % (cnt[s], s) for s in ("high", "med", "low", "info") if s in cnt))
        out.append(findings(design, limit=12, header=False))
    return "\n".join(x for x in out if x)


def findings(design: dict, limit: int = 100, header: bool = True) -> str:
    fs = sorted(design.get("findings", []), key=lambda f: (SEV_ORDER.get(f["sev"], 9), f.get("by") != "lint"))
    out = ["id   sev rule              path / title"] if header else []
    for f in fs[:limit]:
        out.append("  %-4s %s %-17s %s  %s" % (f.get("id", ""), SEV.get(f["sev"], "?"), f.get("rule", ""), f["path"],
                                              f["title"]))
        if header and f.get("detail"):
            out.append("            %s" % f["detail"])
        if header and f.get("fix"):
            out.append("            fix: %s" % f["fix"])
    if len(fs) > limit:
        out.append("  … %d more (fpa.py findings)" % (len(fs) - limit))
    return "\n".join(out)


def show(design: dict, path: str, nets: bool = False) -> str:
    defs = design["defs"]
    path = "/" + path.strip("/") if path.strip("/") else ""
    if path:
        d, inst = M.resolve(design, path)
        if d is None:
            return "no instance %s" % path
        parent = path.rsplit("/", 1)[0]
        pdef = defs[design["top"]] if not parent else M.resolve(design, parent)[0]
        pbody = M.body(design, pdef)
        iname = path.rsplit("/", 1)[1]
    else:
        d, inst, pbody, iname = defs[design["top"]], None, None, None
    out = ["%s  %s %s%s" % (path or "/", d["kind"], d["name"], ("  " + d["vlnv"]) if d.get("vlnv") else "")]
    if d.get("src"):
        out.append("src %s:%s" % (d["src"].get("file"), d["src"].get("line")))
    if d.get("impl"):
        idef = defs[d["impl"]]
        out.append("impl %s (%s:%s)" % (d["impl"], idef.get("src", {}).get("file"), idef.get("src", {}).get("line")))
    params = dict(d.get("params", {}))
    overrides = inst.get("params", {}) if inst else {}
    for k, v in overrides.items():
        for dk in [x for x in params if x.lower() == k.lower()]:
            params.pop(dk)
        params[k] = v
    if params:
        override_names = {k.lower() for k in overrides}
        items = sorted(params.items(), key=lambda kv: kv[0].lower() not in override_names)
        more = " +%d more" % (len(items) - 24) if len(items) > 24 else ""
        out.append("params " + " ".join("%s=%s" % kv for kv in items[:24]) + more)
    out.append("pins")
    for p in d["pins"]:
        line = "  " + _pin_desc(p)
        if pbody is not None:
            n, peers = _peers(design, pbody, iname, p["name"])
            if n:
                dom = design.get("netdom", {}).get(path.rsplit("/", 1)[0] + "|" + n["name"])
                ps = [("%s/%s" % tuple(e)) if e[0] else "port:" + e[1] for e in peers]
                line += ("  ← " if p["dir"] == "in" else "  → ") + ", ".join(ps[:6]) + (" …" if len(ps) > 6 else "")
                line += "  net=" + n["name"]
                if dom:
                    line += "  [%s]" % ",".join(design["domains"].get(x, {}).get("name", x) for x in dom)
        out.append(line)
    bd_path, bbody = _bd(design)
    if bbody is not None:
        for a in bbody.get("addr", []):
            if a["slave"] in (path, path[len(bd_path):] if bd_path else path):
                out.append("addr %s %s %s from %s" % (a["intf"], a["base"], a.get("range") or a.get("high"), a["master"]))
    if d.get("regs"):
        out.append("regs " + " ".join("%s@%s" % (r["name"], r["off"]) for r in d["regs"][:40]))
    body = M.body(design, d)
    if body["insts"]:
        out.append("children " + ", ".join("%s:%s" % (i["name"], defs[i["def"]]["name"]) for i in body["insts"][:40]))
    if nets:
        lvl = path
        for n in body["nets"]:
            dom = design.get("netdom", {}).get(lvl + "|" + n["name"])
            ends = ", ".join(("%s/%s" % tuple(e)) if e[0] else "port:" + e[1] for e in n["ends"][:6])
            out.append("net %s|%s %s%s  %s%s" % (lvl, n["name"], n["kind"], (" %db" % n["w"]) if n.get("w") else "",
                                                ends, ("  [%s]" % ",".join(design["domains"].get(x, {}).get("name", x)
                                                                            for x in dom)) if dom else ""))
    for a in design.get("annotations", []):
        if a["path"] == path:
            out.append("ref %s %s:%s  %s" % (a["kind"], a["file"], a["line"], a["text"]))
    for f in design.get("findings", []):
        if f["path"] == path or f["path"].startswith(path + "/"):
            out.append("finding %s %s %s  %s" % (f.get("id"), f["sev"], f["path"], f["title"]))
    return "\n".join(out)
