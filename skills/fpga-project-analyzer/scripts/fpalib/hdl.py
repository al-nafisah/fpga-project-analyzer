"""Lightweight Verilog/SystemVerilog/VHDL structure reader (no elaboration).

Extracts modules/entities, ports, parameters, instances with connections and,
for the lowest drill-down level, one node per always/process block (register
or combinational) plus continuous assignments. Good enough for hierarchy,
connectivity and clock-domain views; exact netlists come from Vivado.
"""
from __future__ import annotations

import bisect
import re

from . import model as M

# ----------------------------------------------------------------------------
# shared helpers

def _lines(text):
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _eval(expr: str, env: dict):
    """Evaluate a simple integer expression ("W-1", "2*N+1") with parameters."""
    e = expr.strip()
    if not e:
        return None
    e = re.sub(r"\b\d+'[sS]?[dD](\d+)", r"\1", e)
    def sub(m):
        v = env.get(m.group(0), env.get(m.group(0).lower()))
        return str(v) if isinstance(v, int) else "X"
    e = re.sub(r"[A-Za-z_][A-Za-z0-9_$]*", sub, e)
    e = e.replace("$clog2", "").replace("**", "^")
    if "X" in e or not re.fullmatch(r"[\d\s+\-*/()%^]+", e):
        return None
    e = e.replace("^", "**").replace("/", "//")
    try:
        v = eval(e, {"__builtins__": {}}, {})  # digits and operators only (checked above)
        return int(v)
    except Exception:
        return None


def _int_env(params: dict) -> dict:
    env = {}
    for _ in range(3):
        for k, v in params.items():
            if k not in env:
                r = _eval(str(v), env)
                if r is not None:
                    env[k] = r
    return env


# ----------------------------------------------------------------------------
# Verilog / SystemVerilog

_V_STRIP = re.compile(r'("(?:\\.|[^"\\])*")|(//[^\n]*)|(/\*.*?\*/)', re.S)
_V_TOK = re.compile(r"""
 (?P<ws>\s+)
|(?P<id>[A-Za-z_][A-Za-z0-9_$]*(?:::[A-Za-z_][A-Za-z0-9_$]*)*)
|(?P<esc>\\\S+)
|(?P<sys>\$[A-Za-z_][A-Za-z0-9_$]*)
|(?P<num>\d*\s*'[sS]?[bBoOdDhH]\s*[0-9a-fA-F_xXzZ?]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|'[01xXzZ])
|(?P<mac>`[A-Za-z_]\w*)
|(?P<op><<=|>>=|===|!==|<=|>=|==|!=|&&|\|\||<<|>>|->|::|\*\*|\+:|-:|[-+*/%&|^~!<>=?:;,.\#@()\[\]{}'"])
""", re.X)

_V_KW = set("""
always always_ff always_comb always_latch and assign assert assume automatic begin bit buf bufif0 bufif1 byte case casex
casez cell class clocking cmos config const constraint context continue cover covergroup deassign default defparam design
disable do edge else end endcase endclass endclocking endconfig endfunction endgenerate endgroup endinterface endmodule
endpackage endprimitive endprogram endproperty endspecify endsequence endtable endtask enum event export extends
extern final for force foreach forever fork function generate genvar if iff ifnone import incdir include initial inout
input int integer interface join join_any join_none large liblist library local localparam logic longint macromodule
modport module nand negedge nmos nor not notif0 notif1 null or output package packed parameter pmos posedge primitive
priority program property protected pull0 pull1 pulldown pullup rand randc rcmos real realtime ref reg release repeat
return rnmos rpmos rtran rtranif0 rtranif1 scalared sequence shortint shortreal signed small specify specparam static
string strong0 strong1 struct super supply0 supply1 table task this time timeprecision timeunit tran tranif0 tranif1 tri
tri0 tri1 triand trior trireg type typedef union unique unique0 unsigned var vectored virtual void wait wand weak0
weak1 while wire wor xnor xor
""".split())
_V_TYPES = {"wire", "reg", "logic", "bit", "integer", "int", "tri", "wand", "wor", "var", "signed", "unsigned", "byte",
            "shortint", "longint", "genvar", "real", "time", "supply0", "supply1", "tri0", "tri1", "uwire", "string",
            "event", "realtime", "shortreal"}
_V_GATES = {"and", "or", "nand", "nor", "xor", "xnor", "not", "buf", "bufif0", "bufif1", "notif0", "notif1"}
_V_SKIP_BLOCKS = {"function": "endfunction", "task": "endtask", "specify": "endspecify", "property": "endproperty",
                  "sequence": "endsequence", "covergroup": "endgroup", "clocking": "endclocking",
                  "class": "endclass", "primitive": "endprimitive", "table": "endtable"}
_RST_NAME = re.compile(r"(rst|reset)", re.I)


class _T:
    __slots__ = ("k", "v", "line")

    def __init__(self, k, v, line):
        self.k, self.v, self.line = k, v, line

    def __repr__(self):
        return self.v


_INCLUDE = re.compile(r'^\s*`include\s+"([^"]+)"')
MISSING_INCLUDES = []  # (file, line, name) seen by the last parse


def _v_clean(text, file="", incdirs=(), depth=0):
    if depth < 8 and "`include" in text:
        lines = text.split("\n")
        for k, line in enumerate(lines):
            m = _INCLUDE.match(line)
            inc = _find_include(m.group(1), file, incdirs) if m else None
            if m and not inc:
                MISSING_INCLUDES.append((file, k + 1, m.group(1)))
            if inc:
                try:
                    with open(inc, encoding="utf-8", errors="replace") as f:
                        body = _v_clean(f.read(), inc, incdirs, depth + 1)
                    lines[k] = " ".join(body.split("\n"))  # one line keeps the outer line numbers
                except OSError:
                    pass
        text = "\n".join(lines)

    def rep(m):
        if m.group(1):
            return '""'
        return re.sub(r"[^\n]", " ", m.group(0))
    text = _V_STRIP.sub(rep, text)
    out = []
    cont = False
    for line in text.split("\n"):
        s = line.lstrip()
        if cont or re.match(r"`(define|include|timescale|ifdef|ifndef|else|elsif|endif|default_nettype|resetall|"
                            r"celldefine|endcelldefine|undef|pragma|line|begin_keywords|end_keywords|unconnected_drive|"
                            r"nounconnected_drive)\b", s):
            cont = line.rstrip().endswith("\\")
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def _find_include(name, file, incdirs):
    import os
    for d in [os.path.dirname(file)] + list(incdirs):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _v_tokens(text):
    starts = _lines(text)
    toks = []
    for m in _V_TOK.finditer(text):
        k = m.lastgroup
        if k == "ws":
            continue
        toks.append(_T(k, m.group(0), bisect.bisect_right(starts, m.start())))
    return toks


def _close(toks, i, o="(", c=")"):
    d = 0
    for j in range(i, len(toks)):
        v = toks[j].v
        if v == o:
            d += 1
        elif v == c:
            d -= 1
            if d == 0:
                return j
    return len(toks) - 1


def _split_top(toks, sep=","):
    parts, cur, d = [], [], 0
    for t in toks:
        if t.v in ("(", "[", "{"):
            d += 1
        elif t.v in (")", "]", "}"):
            d -= 1
        if t.v == sep and d == 0:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return parts


def _text(toks):
    out = ""
    for t in toks:
        if out and (t.k in ("id", "num", "sys") and out[-1:].isalnum()):
            out += " "
        out += t.v
    return out


def _ids(toks, skip_index=False):
    out, d = [], 0
    for idx, t in enumerate(toks):
        if t.v == "[":
            d += 1
        elif t.v == "]":
            d -= 1
        elif t.k == "id" and t.v not in _V_KW and not (skip_index and d):
            if idx and toks[idx - 1].v == ".":
                continue  # the member part of bus.member (emitted with its base below)
            if idx + 2 < len(toks) and toks[idx + 1].v == "." and toks[idx + 2].k == "id":
                out.append(t.v + "." + toks[idx + 2].v)  # interface/struct member: one signal per member
                continue
            if idx + 1 < len(toks) and toks[idx + 1].v == "(" and idx and toks[idx - 1].v != ".":
                continue  # function call name
            if "::" in t.v:
                continue
            out.append(t.v)
    return out


def _range_of(toks, env):
    """First packed range [a:b] in toks -> (text, width)."""
    for i, t in enumerate(toks):
        if t.v == "[":
            j = _close(toks, i, "[", "]")
            inner = toks[i + 1:j]
            colon = [k for k, x in enumerate(inner) if x.v == ":"]
            if colon:
                a = _eval(_text(inner[:colon[0]]), env)
                b = _eval(_text(inner[colon[0] + 1:]), env)
                return "[" + _text(inner) + "]", (abs(a - b) + 1) if a is not None and b is not None else 0
            return "[" + _text(inner) + "]", 0
    return "", 1


def _param_items(toks, out):
    for item in _split_top(toks):
        eq = [k for k, t in enumerate(item) if t.v == "="]
        if not eq:
            continue
        names = [t.v for t in item[:eq[0]] if t.k == "id" and t.v not in _V_KW and t.v not in _V_TYPES]
        if names:
            out[names[-1]] = _text(item[eq[0] + 1:])


class _VModule:
    def __init__(self, name, file, line):
        self.name, self.file, self.line = name, file, line
        self.params, self.ports, self.order = {}, {}, []
        self.insts, self.procs, self.assigns = [], [], []
        self.signals = {}
        self.lang = "verilog"


INTERFACES = set()


def parse_verilog(text: str, file: str, incdirs=()) -> list:
    toks = _v_tokens(_v_clean(text, file, incdirs))
    mods, i, n = [], 0, len(toks)
    while i < n:
        v = toks[i].v
        if v in ("module", "macromodule") and i + 1 < n and toks[i + 1].k == "id":
            j = i + 1
            if toks[j].v in ("automatic", "static"):
                j += 1
            end = j
            while end < n and toks[end].v != "endmodule":
                end += 1
            m = _VModule(toks[j].v, file, toks[j].line)
            _v_module(m, toks[j + 1:end])
            mods.append(m)
            i = end + 1
        elif v in ("interface", "package", "program", "class") and i + 1 < n and toks[i + 1].k == "id" \
                and (i == 0 or toks[i - 1].v not in (".",)):
            endkw = {"interface": "endinterface", "package": "endpackage", "program": "endprogram",
                     "class": "endclass"}[v]
            if v == "interface":
                INTERFACES.add(toks[i + 1].v)
            while i < n and toks[i].v != endkw:
                i += 1
            i += 1
        else:
            i += 1
    return mods


def _v_module(m: _VModule, toks):
    i, n = 0, len(toks)
    while i < n and toks[i].v == "import":
        while i < n and toks[i].v != ";":
            i += 1
        i += 1
    if i < n and toks[i].v == "#":
        j = _close(toks, i + 1)
        _param_items(toks[i + 2:j], m.params)
        i = j + 1
    env = _int_env(m.params)
    if i < n and toks[i].v == "(":
        j = _close(toks, i)
        _v_ports(m, toks[i + 1:j], env)
        i = j + 1
    while i < n and toks[i].v != ";":
        i += 1
    _v_body(m, toks[i + 1:], env)


def _v_ports(m, toks, env):
    items = _split_top(toks)
    ansi = any(t.v in ("input", "output", "inout", "ref") or t.v in _V_TYPES or t.v == "." for it in items for t in it)
    if not ansi:
        for it in items:
            ids = [t.v for t in it if t.k == "id"]
            if ids:
                m.order.append(ids[0])
                m.ports[ids[0]] = {"name": ids[0], "dir": "inout", "range": "", "w": 1}
        return
    cur_dir, cur_range = "in", ("", 1)
    for it in items:
        eq = next((k for k, t in enumerate(it) if t.v == "="), len(it))
        it = it[:eq]
        ids = [t for t in it if t.k == "id" and t.v not in _V_TYPES and t.v not in
               ("input", "output", "inout", "ref", "signed", "unsigned", "const", "var")]
        dirs = [t.v for t in it if t.v in ("input", "output", "inout", "ref")]
        kind = None
        if len(it) >= 3 and it[1].v == "." and it[0].k == "id":  # interface port: bus_if.modport name
            kind, d = "bus", "inout"
            name = it[-1].v
            m.ports[name] = {"name": name, "dir": d, "range": "", "w": 0, "kind": "bus", "bus": it[0].v}
            m.order.append(name)
            continue
        if dirs:
            cur_dir = {"input": "in", "output": "out"}.get(dirs[0], "inout")
            cur_range = _range_of(it, env)
        elif any(t.v == "[" for t in it):
            cur_range = _range_of(it, env)
        if not ids:
            continue
        if not dirs and len(ids) == 2 and not any(t.v in _V_TYPES for t in it):
            name = ids[-1].v  # generic interface port: axi_if name
            m.ports[name] = {"name": name, "dir": "inout", "range": "", "w": 0, "kind": "bus", "bus": ids[0].v}
            m.order.append(name)
            continue
        name = ids[-1].v
        rng = _range_of(it, env) if any(t.v == "[" for t in it) else cur_range
        m.ports[name] = {"name": name, "dir": cur_dir, "range": rng[0], "w": rng[1]}
        m.order.append(name)


def _stmt_end(toks, i):
    """Index of the last token of the procedural statement starting at i."""
    n = len(toks)
    if i >= n:
        return n - 1
    v = toks[i].v
    if v == "begin" or v == "fork":
        d, j = 0, i
        while j < n:
            if toks[j].v in ("begin", "fork"):
                d += 1
            elif toks[j].v in ("end", "join", "join_any", "join_none"):
                d -= 1
                if d == 0:
                    if j + 2 < n and toks[j + 1].v == ":":
                        j += 2
                    return j
            j += 1
        return n - 1
    if v in ("unique", "unique0", "priority"):
        return _stmt_end(toks, i + 1)
    if v in ("case", "casez", "casex"):
        d, j = 0, i
        while j < n:
            if toks[j].v in ("case", "casez", "casex"):
                d += 1
            elif toks[j].v == "endcase":
                d -= 1
                if d == 0:
                    return j
            j += 1
        return n - 1
    if v == "if":
        j = _close(toks, i + 1)
        j = _stmt_end(toks, j + 1)
        if j + 1 < n and toks[j + 1].v == "else":
            j = _stmt_end(toks, j + 2)
        return j
    if v in ("for", "while", "repeat", "foreach"):
        j = _close(toks, i + 1)
        return _stmt_end(toks, j + 1)
    if v == "forever":
        return _stmt_end(toks, i + 1)
    if v == "@" or v == "#":
        j = i + 1
        if j < n and toks[j].v == "(":
            j = _close(toks, j)
        return _stmt_end(toks, j + 1)
    j = i
    while j < n and toks[j].v != ";":
        j += 1
    return j


_BOUNDARY = {";", "begin", "end", "else", "fork", "join"}


def _proc_rw(toks):
    """Writes (LHS bases) and reads (all other identifiers) of a procedural block."""
    writes, reads = [], []
    stmt_start, d = 0, 0
    lhs_spans = []
    for k, t in enumerate(toks):
        if t.v in ("(", "[", "{"):
            d += 1
        elif t.v in (")", "]", "}"):
            d -= 1
            if d == 0 and t.v == ")":
                stmt_start = k + 1
        if d == 0 and (t.v in _BOUNDARY or (t.v == ":" and k > 0 and toks[k - 1].v not in ("]",))):
            stmt_start = k + 1
        elif d == 0 and t.v in ("<=", "="):
            lhs = toks[stmt_start:k]
            base = _ids(lhs, skip_index=True)
            if base:
                writes.extend(base)
                lhs_spans.append((stmt_start, k))
            stmt_start = k + 1
    skip = set()
    for a, b in lhs_spans:
        dd = 0
        for k in range(a, b):
            if toks[k].v == "[":
                dd += 1
            elif toks[k].v == "]":
                dd -= 1
            elif dd == 0:
                skip.add(k)
    sub = [t for k, t in enumerate(toks) if k not in skip]
    reads = _ids(sub)
    return _uniq(writes), _uniq(r for r in reads if r not in ("posedge", "negedge"))


def _rst_pol(cond, name):
    """'low' if the reset is tested active-low (!rst, ~rst, rst == 0, rst = '0'); None if not found."""
    vs = []
    for t in cond:  # fold bus.member into one token
        if len(vs) >= 2 and vs[-1] == "." and t.k == "id":
            vs[-2:] = [vs[-2] + "." + t.v]
        else:
            vs.append(t.v)
    for k, v in enumerate(vs):
        if v != name:
            continue
        if k and vs[k - 1] in ("!", "~", "not"):
            return "low"
        if k + 2 < len(vs) and vs[k + 1] in ("==", "=", "==="):
            lit = vs[k + 2].replace(" ", "")
            return "low" if lit in ("0", "'0'", "1'b0", "'b0", "1'h0", "false") else "high"
        return "high"
    return None


def _uniq(it):
    seen, out = set(), []
    for x in it:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _v_body(m: _VModule, toks, env):
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        v = t.v
        if v == "(" and i + 1 < n and toks[i + 1].v == "*":  # (* attribute *)
            j = i + 2
            while j + 1 < n and not (toks[j].v == "*" and toks[j + 1].v == ")"):
                j += 1
            i = j + 2
            continue
        if v in ("input", "output", "inout"):
            j = i
            while j < n and toks[j].v != ";":
                j += 1
            seg = toks[i + 1:j]
            d = {"input": "in", "output": "out"}.get(v, "inout")
            rng = _range_of(seg, env) if any(x.v == "[" for x in seg) else ("", 1)
            for item in _split_top(seg):
                ids = [x.v for x in item if x.k == "id" and x.v not in _V_TYPES]
                if ids:
                    nm = ids[-1] if "=" not in [x.v for x in item] else ids[0]
                    p = m.ports.setdefault(nm, {"name": nm})
                    p.update({"dir": d, "range": rng[0], "w": rng[1]})
                    if nm not in m.order:
                        m.order.append(nm)
            i = j + 1
            continue
        if v in _V_TYPES or (v in ("parameter", "localparam")):
            j = i
            dep = 0
            while j < n and not (toks[j].v == ";" and dep == 0):
                if toks[j].v in ("{", "("):
                    dep += 1
                elif toks[j].v in ("}", ")"):
                    dep -= 1
                j += 1
            seg = toks[i:j]
            if v in ("parameter", "localparam"):
                _param_items(seg[1:], m.params)
                env.update(_int_env(m.params))
            else:
                rng = _range_of(seg, env) if any(x.v == "[" for x in seg) else ("", 1)
                for item in _split_top(seg):
                    eq = next((k for k, x in enumerate(item) if x.v == "="), len(item))
                    head = item[:eq]
                    dd, ids = 0, []
                    for x in head:
                        if x.v == "[":
                            dd += 1
                        elif x.v == "]":
                            dd -= 1
                        elif dd == 0 and x.k == "id" and x.v not in _V_TYPES and x.v not in _V_KW:
                            ids.append(x.v)
                    if ids:
                        m.signals[ids[-1]] = rng[1]
                        if eq < len(item):  # wire x = expr;
                            m.assigns.append({"w": [ids[-1]], "r": _uniq(_ids(item[eq + 1:])), "line": item[0].line})
            i = j + 1
            continue
        if v == "assign":
            j = i
            while j < n and toks[j].v != ";":
                j += 1
            for item in _split_top(toks[i + 1:j]):
                eq = next((k for k, x in enumerate(item) if x.v == "="), None)
                if eq is None:
                    continue
                m.assigns.append({"w": _uniq(_ids(item[:eq], skip_index=True)),
                                  "r": _uniq(_ids(item[eq + 1:]) + [x for x in _ids(item[:eq]) if x not in _ids(item[:eq], True)]),
                                  "line": t.line})
            i = j + 1
            continue
        if v in ("always", "always_ff", "always_comb", "always_latch"):
            j = i + 1
            edges = []
            if j < n and toks[j].v == "@":
                if j + 1 < n and toks[j + 1].v == "(":
                    k = _close(toks, j + 1)
                    sens = toks[j + 2:k]
                    for k2, (a, b) in enumerate(zip(sens, sens[1:])):
                        if a.v in ("posedge", "negedge") and b.k == "id":
                            member = k2 + 3 < len(sens) and sens[k2 + 2].v == "."
                            # interface member clock (posedge lb.clk): keep the dotted name; it stays unresolved
                            edges.append((a.v, b.v + "." + sens[k2 + 3].v if member else b.v))
                    j = k + 1
                else:
                    j += 2  # @*
            end = _stmt_end(toks, j)
            blk = toks[j:end + 1]
            w, r = _proc_rw(blk)
            clk = rst = pol = None
            if edges:
                rsts = [e for e in edges if _RST_NAME.search(e[1])]
                clks = [e for e in edges if e not in rsts] or edges
                clk = clks[0]
                if rsts:
                    rst, pol = rsts[0][1], "low" if rsts[0][0] == "negedge" else "high"
                head = [x.v for x in blk[:12]]
                if "if" in head:
                    k0 = head.index("if")
                    ctoks = blk[k0 + 1:_close(blk, k0 + 1) + 1]
                    rr = [c for c in _ids(ctoks) if _RST_NAME.search(c)]
                    if rr and (rst is None or rr[0] == rst):
                        rst, pol = rr[0], _rst_pol(ctoks, rr[0])
            m.procs.append({"kind": "ff" if clk else ("latch" if v == "always_latch" else "comb"),
                            "clk": clk[1] if clk else None, "edge": clk[0][:3] if clk else None, "rst": rst,
                            "pol": pol,
                            "w": w, "r": [x for x in r if x != (clk[1] if clk else None)], "line": t.line})
            i = end + 1
            continue
        if v in ("initial", "final"):
            i = _stmt_end(toks, i + 1) + 1
            continue
        if v in _V_SKIP_BLOCKS:
            endkw = _V_SKIP_BLOCKS[v]
            if v in ("property", "sequence") and i > 0 and toks[i - 1].v in ("assert", "cover", "assume"):
                while i < n and toks[i].v != ";":
                    i += 1
                i += 1
                continue
            while i < n and toks[i].v != endkw:
                i += 1
            i += 1
            continue
        if v in ("typedef", "import", "export", "defparam", "modport", "assert", "assume", "cover", "bind",
                 "timeunit", "timeprecision", "genvar", "specparam", "let", "nettype"):
            dep, j = 0, i
            while j < n and not (toks[j].v == ";" and dep == 0):
                if toks[j].v in ("{", "("):
                    dep += 1
                elif toks[j].v in ("}", ")"):
                    dep -= 1
                j += 1
            i = j + 1
            continue
        if v in ("generate", "endgenerate", "begin", "end", "else", "endcase", "default"):
            i += 1
            if i < n and toks[i].v == ":":
                i += 2
            continue
        if v in ("for", "if", "case") and i + 1 < n and toks[i + 1].v == "(":
            i = _close(toks, i + 1) + 1
            continue
        if t.k == "id" and i + 1 < n and toks[i + 1].v == ":" and i + 2 < n and toks[i + 2].k == "id":
            i += 2  # label:
            continue
        if t.k == "id" and (v not in _V_KW or v in _V_GATES):
            j = _v_instance(m, toks, i, env)
            if j:
                i = j
                continue
        while i < n and toks[i].v != ";":
            i += 1
        i += 1


def _v_instance(m, toks, i, env):
    n = len(toks)
    typ = toks[i].v
    j = i + 1
    params = {}
    if j < n and toks[j].v == "#":
        if j + 1 < n and toks[j + 1].v == "(":
            k = _close(toks, j + 1)
            for pos, item in enumerate(_split_top(toks[j + 2:k])):
                if len(item) >= 2 and item[0].v == "." and item[1].k == "id":
                    inner = item[3:-1] if len(item) > 3 else []
                    params[item[1].v] = _text(inner)
                else:
                    params["#%d" % pos] = _text(item)
            j = k + 1
        else:
            j += 2
    found = False
    while j < n:
        name = None
        if toks[j].k in ("id", "esc") and toks[j].v not in _V_KW:
            name = toks[j].v
            j += 1
            while j < n and toks[j].v == "[":
                j = _close(toks, j, "[", "]") + 1
        elif typ in _V_GATES and toks[j].v == "(":
            name = "%s_%d" % (typ, len(m.insts))
        if j >= n or toks[j].v != "(" or name is None:
            return None if not found else j
        k = _close(toks, j)
        conns, pos = {}, []
        for item in _split_top(toks[j + 1:k]):
            if not item:
                continue
            if item[0].v == "." and len(item) >= 2:
                if item[1].v == "*":
                    conns[".*"] = []
                elif len(item) >= 3 and item[2].v == "(":
                    conns[item[1].v] = item[3:-1]
                else:
                    conns[item[1].v] = [item[1]]
            else:
                pos.append(item)
        m.insts.append({"name": name, "type": typ, "params": params, "conns": conns, "pos": pos,
                        "line": toks[i].line})
        found = True
        j = k + 1
        if j < n and toks[j].v == ",":
            j += 1
            continue
        if j < n and toks[j].v == ";":
            return j + 1
        return j
    return j if found else None


# ----------------------------------------------------------------------------
# VHDL

_H_STRIP = re.compile(r'("(?:[^"\n]|"")*")|(--[^\n]*)|(/\*.*?\*/)', re.S)
_H_TOK = re.compile(r"""
 (?P<ws>\s+)
|(?P<id>[A-Za-z][A-Za-z0-9_]*)
|(?P<esc>\\[^\\\n]+\\)
|(?P<num>\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|[bBoOxX]"[0-9a-fA-F_]*")
|(?P<chr>'.'(?!\w))
|(?P<op><=|=>|:=|/=|>=|\*\*|<>|[-+*/&|<>=;:,.()'\[\]])
""", re.X)


def _h_tokens(text):
    def rep(m):
        if m.group(1):
            return '""'
        return re.sub(r"[^\n]", " ", m.group(0))
    text = _H_STRIP.sub(rep, text)
    starts = _lines(text)
    toks = []
    for m in _H_TOK.finditer(text):
        k = m.lastgroup
        if k == "ws":
            continue
        v = m.group(0)
        toks.append(_T(k, v.lower() if k == "id" else v, bisect.bisect_right(starts, m.start())))
    return toks


_H_KW = set("""
abs access after alias all and architecture array assert attribute begin block body buffer bus case component
configuration constant disconnect downto else elsif end entity exit file for function generate generic group guarded
if impure in inertial inout is label library linkage literal loop map mod nand new next nor not null of on open or
others out package port postponed procedure process pure range record register reject rem report return rol ror select
severity signal shared sla sll sra srl subtype then to transport type unaffected units until use variable wait when
while with xnor xor rising_edge falling_edge std_logic std_logic_vector unsigned signed integer natural positive boolean
std_ulogic std_ulogic_vector bit bit_vector string ieee std work numeric_std std_logic_1164 event
""".split())


def _h_width(type_toks, env):
    tt = [t.v for t in type_toks]
    if not tt:
        return "", 0
    if tt[0] in ("std_logic", "std_ulogic", "bit", "boolean"):
        return "", 1
    if "(" in tt:
        i = tt.index("(")
        j = _close(type_toks, i)
        inner = type_toks[i + 1:j]
        words = [t.v for t in inner]
        for kw in ("downto", "to"):
            if kw in words:
                k = words.index(kw)
                a = _eval(_text(inner[:k]), env)
                b = _eval(_text(inner[k + 1:]), env)
                rng = "(" + _text(inner) + ")"
                return rng, (abs(a - b) + 1) if a is not None and b is not None else 0
    if tt[0] in ("integer", "natural", "positive"):
        return "", 32
    return "", 0


def _h_iface(toks, env, out_params=None):
    """Parse a generic(...) or port(...) list -> list of (name, mode, range, width, default)."""
    res = []
    for item in _split_top(toks, ";"):
        colon = next((k for k, t in enumerate(item) if t.v == ":"), None)
        if colon is None:
            continue
        names = [t.v for t in item[:colon] if t.k in ("id", "esc") and t.v not in ("signal", "constant")]
        rest = item[colon + 1:]
        mode = "in"
        if rest and rest[0].v in ("in", "out", "inout", "buffer", "linkage"):
            mode = rest[0].v
            rest = rest[1:]
        dflt = next((k for k, t in enumerate(rest) if t.v == ":="), None)
        default = _text(rest[dflt + 1:]) if dflt is not None else ""
        rng, w = _h_width(rest[:dflt] if dflt is not None else rest, env)
        for nm in names:
            res.append((nm, mode, rng, w, default))
    return res


def parse_vhdl(text: str, file: str) -> list:
    toks = _h_tokens(text)
    ents, archs = {}, []
    i, n = 0, len(toks)
    while i < n:
        v = toks[i].v
        if v == "entity" and i + 2 < n and toks[i + 2].v == "is" and (i == 0 or toks[i - 1].v != ":"):
            m = _VModule(toks[i + 1].v, file, toks[i + 1].line)
            m.lang = "vhdl"
            j = i + 3
            while j < n and toks[j].v != "end":
                if toks[j].v == "generic" and j + 1 < n and toks[j + 1].v == "(":
                    k = _close(toks, j + 1)
                    for nm, _, _, _, d in _h_iface(toks[j + 2:k], {}):
                        m.params[nm] = d
                    j = k + 1
                elif toks[j].v == "port" and j + 1 < n and toks[j + 1].v == "(":
                    k = _close(toks, j + 1)
                    env = _int_env(m.params)
                    for nm, mode, rng, w, _ in _h_iface(toks[j + 2:k], env):
                        m.ports[nm] = {"name": nm, "dir": {"in": "in", "out": "out", "buffer": "out"}.get(mode, "inout"),
                                       "range": rng, "w": w}
                        m.order.append(nm)
                    j = k + 1
                else:
                    j += 1
            ents[m.name] = m
            i = j
        elif v == "architecture" and i + 3 < n and toks[i + 2].v == "of":
            arch_name, ent = toks[i + 1].v, toks[i + 3].v
            j = i + 4
            # find matching "end [architecture] [name];" at depth 0 of nested blocks
            archs.append((ent, j))
            i = j
        else:
            i += 1
    for ent, j in archs:
        m = ents.get(ent)
        if m is None:
            m = _VModule(ent, file, toks[j].line)
            m.lang = "vhdl"
            ents[ent] = m
        _h_arch(m, toks, j)
    return list(ents.values())


def _h_skip_decl(toks, i):
    """Skip a declarative item; handles function/procedure bodies and components."""
    n = len(toks)
    v = toks[i].v
    if v in ("function", "procedure", "impure", "pure"):
        j = i
        while j < n and toks[j].v not in (";", "is"):
            if toks[j].v == "(":
                j = _close(toks, j)
            j += 1
        if j < n and toks[j].v == ";":
            return j + 1
        d = 0
        while j < n:
            if toks[j].v == "begin":
                d += 1
            elif toks[j].v == "end":
                if toks[j + 1].v in ("if", "loop", "case", "process", "generate", "record", "units"):
                    j += 1
                else:
                    d -= 1
                    if d <= 0:
                        while j < n and toks[j].v != ";":
                            j += 1
                        return j + 1
            j += 1
        return n
    if v in ("component",):
        j = i
        while j < n and not (toks[j].v == "end" and j + 1 < n and toks[j + 1].v == "component"):
            j += 1
        while j < n and toks[j].v != ";":
            j += 1
        return j + 1
    if v == "type" and any(t.v == "record" for t in toks[i:i + 6]):
        j = i
        while j < n and not (toks[j].v == "end" and j + 1 < n and toks[j + 1].v == "record"):
            j += 1
        while j < n and toks[j].v != ";":
            j += 1
        return j + 1
    j, d = i, 0
    while j < n and not (toks[j].v == ";" and d == 0):
        if toks[j].v == "(":
            d += 1
        elif toks[j].v == ")":
            d -= 1
        j += 1
    return j + 1


def _h_arch(m, toks, i):
    n = len(toks)
    env = _int_env(m.params)
    # declarative part
    while i < n and toks[i].v != "is":
        i += 1
    i += 1
    while i < n and toks[i].v != "begin":
        if toks[i].v == "signal":
            j = _h_skip_decl(toks, i)
            for nm, _, rng, w, _ in _h_iface(toks[i + 1:j - 1], env):
                m.signals[nm] = w
            i = j
        elif toks[i].v in ("constant",):
            j = _h_skip_decl(toks, i)
            for nm, _, _, _, d in _h_iface(toks[i + 1:j - 1], env):
                m.params.setdefault(nm, d)
            env = _int_env(m.params)
            i = j
        else:
            i = _h_skip_decl(toks, i)
    i += 1
    _h_stmts(m, toks, i, env)


def _h_stmts(m, toks, i, env):
    n = len(toks)
    while i < n:
        v = toks[i].v
        if v == "end":
            nxt = toks[i + 1].v if i + 1 < n else ""
            if nxt in ("generate", "block"):
                i += 2
                while i < n and toks[i].v != ";":
                    i += 1
                i += 1
                continue
            return
        label = None
        if toks[i].k in ("id", "esc") and i + 1 < n and toks[i + 1].v == ":":
            label = toks[i].v
            i += 2
            v = toks[i].v if i < n else ""
        if v == "process" or (v == "postponed" and i + 1 < n and toks[i + 1].v == "process"):
            i = _h_process(m, toks, i, label)
            continue
        if v in ("for", "if") and label:
            while i < n and toks[i].v != "generate":
                i += 1
            i += 1
            continue
        if v == "block":
            while i < n and toks[i].v != "begin":
                i += 1
            i += 1
            continue
        if v == "begin":
            i += 1
            continue
        if label and (v in ("entity", "component", "configuration") or toks[i].k in ("id", "esc")):
            j = _h_instance(m, toks, i, label)
            if j:
                i = j
                continue
        if v == "with":  # with sel select target <= ...;
            j = i
            while j < n and toks[j].v != ";":
                j += 1
            seg = toks[i:j]
            k = next((x for x, t in enumerate(seg) if t.v == "select"), None)
            le = next((x for x, t in enumerate(seg) if t.v == "<="), None)
            if k is not None and le is not None:
                m.assigns.append({"w": _uniq(_hids(seg[k + 1:le], True)),
                                  "r": _uniq(_hids(seg[1:k]) + _hids(seg[le + 1:])), "line": toks[i].line})
            i = j + 1
            continue
        j = i
        while j < n and toks[j].v != ";":
            j += 1
        seg = toks[i:j]
        le = next((x for x, t in enumerate(seg) if t.v == "<="), None)
        if le is not None and v not in ("assert", "report"):
            m.assigns.append({"w": _uniq(_hids(seg[:le], True)), "r": _uniq(_hids(seg[le + 1:])),
                              "line": toks[i].line})
        i = j + 1


def _hids(toks, skip_index=False):
    out, d = [], 0
    for k, t in enumerate(toks):
        if t.v == "(":
            d += 1
        elif t.v == ")":
            d -= 1
        elif t.k in ("id", "esc") and t.v not in _H_KW:
            if skip_index and d:
                continue
            if k and toks[k - 1].v in ("'", "."):
                continue
            if k + 1 < len(toks) and toks[k + 1].v == "(" and t.v in ("resize", "to_unsigned", "to_signed",
                                                                         "to_integer", "conv_integer", "shift_left",
                                                                         "shift_right"):
                continue
            out.append(t.v)
    return out


def _h_process(m, toks, i, label):
    n = len(toks)
    line = toks[i].line
    j = i
    d = 0
    while j < n:
        if toks[j].v == "end" and j + 1 < n and toks[j + 1].v == "process":
            break
        j += 1
    blk = toks[i:j]
    end = j
    while end < n and toks[end].v != ";":
        end += 1
    words = [t.v for t in blk]
    clk = rst = None
    edge = None
    for k, w in enumerate(words):
        if w in ("rising_edge", "falling_edge") and k + 2 < len(words):
            clk, edge = words[k + 2], "pos" if w == "rising_edge" else "neg"
            break
        if w == "event" and k >= 2 and words[k - 1] == "'":
            clk, edge = words[k - 2], "pos"
            break
    try:
        b = words.index("begin")
    except ValueError:
        b = 0
    body = blk[b + 1:]
    writes, reads = [], []
    stmt = 0
    for k, t in enumerate(body):
        if t.v in (";", "then", "else", "begin", "loop", "=>") or t.v in ("is",):
            stmt = k + 1
        elif t.v == "<=" and _depth(body, stmt, k) == 0:
            writes.extend(_hids(body[stmt:k], True))
            stmt = k + 1
    reads = [x for x in _hids(body) if x != clk]
    if clk:
        bw = [t.v for t in body]
        if "if" in bw:
            k0 = bw.index("if")
            k1 = bw.index("then", k0) if "then" in bw[k0:] else k0
            cond = [x for x in _hids(body[k0:k1]) if x != clk]
            rr = [c for c in cond if _RST_NAME.search(c)]
            rst = rr[0] if rr else None
            pol = _rst_pol(body[k0:k1], rst) if rst else None
    m.procs.append({"kind": "ff" if clk else "comb", "clk": clk, "edge": edge, "rst": rst, "pol": pol if clk else None,
                    "w": _uniq(writes), "r": _uniq(reads), "line": line, "label": label})
    return end + 1


def _depth(toks, a, b):
    d = 0
    for t in toks[a:b]:
        if t.v == "(":
            d += 1
        elif t.v == ")":
            d -= 1
    return d


def _h_instance(m, toks, i, label):
    n = len(toks)
    v = toks[i].v
    j = i
    if v == "entity":
        j += 1
        typ = toks[j].v.split(".")[-1]
        j += 1
        while j < n and toks[j].v == ".":
            typ = toks[j + 1].v
            j += 2
        if j < n and toks[j].v == "(":
            j = _close(toks, j) + 1
    elif v in ("component", "configuration"):
        typ = toks[j + 1].v
        j += 2
    else:
        typ = v
        j += 1
    params, conns, pos = {}, {}, []
    seen_map = False
    while j < n and toks[j].v != ";":
        if toks[j].v in ("generic", "port") and j + 2 < n and toks[j + 1].v == "map":
            which = toks[j].v
            k = _close(toks, j + 2)
            for p, item in enumerate(_split_top(toks[j + 3:k])):
                arrow = next((x for x, t in enumerate(item) if t.v == "=>"), None)
                if which == "generic":
                    if arrow is not None:
                        params[item[0].v] = _text(item[arrow + 1:])
                    else:
                        params["#%d" % p] = _text(item)
                else:
                    if arrow is not None:
                        formal = item[0].v
                        actual = item[arrow + 1:]
                        if not (len(actual) == 1 and actual[0].v == "open"):
                            conns.setdefault(formal, []).extend(actual)
                    else:
                        pos.append(item)
            j = k + 1
            seen_map = True
        else:
            if not seen_map and toks[j].v not in ("generic", "port", "map"):
                return None
            j += 1
    if not seen_map:
        return None
    m.insts.append({"name": label, "type": typ, "params": params, "conns": conns, "pos": pos, "line": toks[i].line})
    return j + 1


# ----------------------------------------------------------------------------
# modules -> design defs

_PRIM_OUT = re.compile(r"^(O|OB|Q|P|CO|O\d|LOCKED|DO\w*|DOUT\w*|CLKOUT\d*B?|CLKFBOUT\w*|dest_\w+|dout\w*|full|empty|"
                       r"almost_\w+|prog_\w+|\w+_o|\w+_out|data_valid|wr_ack|overflow|underflow|rd_data_count|"
                       r"wr_data_count|douta|doutb|sbiterr\w*|dbiterr\w*)$", re.I)
CDC_PRIMS = re.compile(r"^(xpm_cdc_\w+|xpm_fifo_async|fifo_generator\w*|axis_clock_converter\w*|"
                       r"axi_clock_converter\w*|axis_data_fifo\w*)$", re.I)
XILINX_PRIMS = re.compile(r"^(BUFG\w*|BUFH\w*|BUFR|BUFIO|IBUF\w*|OBUF\w*|IOBUF\w*|FD[CPRS]E|LUT\d\w*|MMCM\w+|"
                          r"PLLE\d\w*|IDDR\w*|ODDR\w*|ISERDES\w*|OSERDES\w*|IDELAY\w*|ODELAY\w*|RAMB\w+|DSP48\w+|"
                          r"SRL\w+|CARRY\d|MUXF\d|STARTUP\w+|ICAP\w+|xpm_\w+|GT\w+_CHANNEL|GT\w+_COMMON)$")


def _conn_ids(expr_toks, lang):
    """Base signal names in a connection expression ({a, b[3:0]} -> a, b)."""
    if lang == "vhdl":
        return _uniq(_hids(expr_toks, True))
    return _uniq(_ids(expr_toks, skip_index=True))


def build_defs(design: dict, modules: list, rel=lambda p: p):
    """Add rtl:<module> defs (and black boxes) to the design; return the def id map."""
    defs = design["defs"]
    by_name = {}
    for m in modules:
        key = m.name.lower() if m.lang == "vhdl" else m.name
        by_name.setdefault(key, m)
    ids = {}
    for key, m in by_name.items():
        ids[key] = "rtl:" + m.name
    members = {m.name: _members(m) for m in by_name.values()}
    _propagate_members(by_name, members)
    instantiated = set()
    for key, m in by_name.items():
        env = _int_env(m.params)
        d = M.new_def(m.name, "rtl", src={"file": rel(m.file), "line": m.line}, lang=m.lang)
        clocks = {p["clk"] for p in m.procs if p.get("clk")}
        resets = {p["rst"]: p.get("pol") for p in m.procs if p.get("rst")}
        for pn in m.order:
            p = m.ports[pn]
            kind = p.get("kind") or ("clk" if pn in clocks else "rst" if pn in resets else "")
            d["pins"].append(M.pin(pn, p.get("dir", "inout"), kind, w=p.get("w") or None,
                                   range=p.get("range") or None, bus=p.get("bus"),
                                   pol=resets.get(pn) if pn in resets else None))
        if m.params:
            d["params"] = dict(m.params)
        sig = {}  # signal -> list of ends

        def touch(s, end):
            sig.setdefault(s, []).append(end)

        portnames = set(m.order)
        lower = {p.lower(): p for p in portnames} if m.lang == "vhdl" else None
        for pn in m.order:
            touch(pn, ["", pn])
        iface = {pn for pn in m.order if m.ports[pn].get("kind") == "bus"}
        mine = members[m.name]
        for x in mine:
            touch(x, ["", x])
        aliases = []
        for inst in m.insts:
            if inst["type"] in INTERFACES:
                iface.add(inst["name"])
                for port, expr in inst["conns"].items():  # interface ports: bram_if b(.clk(clk))
                    for s2 in _conn_ids(expr, m.lang)[:1]:
                        aliases.append((inst["name"] + "." + port, s2))
                continue
            callee = by_name.get(inst["type"]) or by_name.get(inst["type"].lower())
            if callee:
                cid = ids[callee.name.lower() if callee.lang == "vhdl" else callee.name]
                instantiated.add(cid)
            else:
                cid = "bb:" + inst["type"]
                if cid not in defs:
                    kind = "prim" if XILINX_PRIMS.match(inst["type"]) else "bb"
                    defs[cid] = M.new_def(inst["type"], kind)
                    if CDC_PRIMS.match(inst["type"]):
                        defs[cid]["cdc"] = True
            conns = dict(inst["conns"])
            if inst["pos"]:
                order = callee.order if callee else ["p%d" % k for k in range(len(inst["pos"]))]
                for k, item in enumerate(inst["pos"]):
                    if k < len(order):
                        conns[order[k]] = item
            if ".*" in conns:
                conns.pop(".*")
                if callee:
                    for pn in callee.order:
                        if pn not in conns and (pn in portnames or pn in m.signals):
                            conns[pn] = [_T("id", pn, 0)]
            entry = {"name": inst["name"], "def": cid, "line": inst["line"]}
            if inst["params"]:
                entry["params"] = inst["params"]
            d["insts"].append(entry)
            bbdef = defs.get(cid) if cid.startswith("bb:") else None
            for formal, expr in conns.items():
                if bbdef is not None and formal not in M.pin_index(bbdef):
                    bbdef["pins"].append(M.pin(formal, "out" if _PRIM_OUT.match(formal) else "in"))
                cids = _conn_ids(expr, m.lang)
                for s in cids:
                    if lower is not None:
                        s = lower.get(s.lower(), s)
                    touch(s, [inst["name"], formal])
                    if s in mine and callee and callee.ports.get(formal, {}).get("dir") == "out":
                        mine[s] = "out"  # member driven by a child output
                if callee and callee.ports.get(formal, {}).get("kind") == "bus" and cids:
                    base = cids[0].split(".")[0]
                    for cm in members.get(callee.name, {}):
                        if cm.startswith(formal + "."):
                            touch(base + "." + cm[len(formal) + 1:], [inst["name"], cm])
                            if base + "." + cm[len(formal) + 1:] in mine and members[callee.name][cm] == "out":
                                mine[base + "." + cm[len(formal) + 1:]] = "out"
        _logic_nodes(design, m, d, touch, rel(m.file), iface)
        for a, b in aliases:
            if a in sig:
                sig.setdefault(b, []).extend(sig.pop(a))
        for x, dr in mine.items():
            d["pins"].append(M.pin(x, "out" if dr == "out" else "in", "clk" if dr == "clk" else "", member=True))
        for s, ends in sig.items():
            if len(ends) < 2:
                continue
            w = m.ports[s].get("w") if s in m.ports else m.signals.get(s)
            net = {"name": s, "kind": "bus" if s in iface else "", "ends": ends}
            if w:
                net["w"] = w
            d["nets"].append(net)
        defs[ids[key]] = d
    _finish_kinds(design, [ids[k] for k in by_name])
    roots = [ids[k] for k in by_name if ids[k] not in instantiated]
    return ids, roots


def _propagate_members(by_name, members):
    """A module that passes an interface port down inherits the members its children use."""
    for _ in range(24):
        changed = False
        for m in by_name.values():
            ports = {pn for pn in m.order if m.ports[pn].get("kind") == "bus"}
            mine = members[m.name]
            for inst in m.insts:
                callee = by_name.get(inst["type"]) or by_name.get(inst["type"].lower())
                if not callee:
                    continue
                for formal, expr in inst["conns"].items():
                    if callee.ports.get(formal, {}).get("kind") != "bus":
                        continue
                    cids = _conn_ids(expr, m.lang)
                    base = cids[0].split(".")[0] if cids else ""
                    if base not in ports:
                        continue
                    for cm, how in list(members[callee.name].items()):
                        if not cm.startswith(formal + "."):
                            continue
                        x = base + "." + cm[len(formal) + 1:]
                        if x not in mine or (how == "out" and mine[x] != "out"):
                            mine[x] = how
                            changed = True
        if not changed:
            return


def _members(m):
    """Interface-port members a module uses ("bus.clk") -> 'clk' | 'out' | 'in'."""
    ports = {pn for pn in m.order if m.ports[pn].get("kind") == "bus"}
    use = {}

    def mark(x, how):
        if "." in x and x.split(".")[0] in ports:
            if use.get(x) not in ("clk",) and (how != "in" or x not in use):
                use[x] = how

    for p in m.procs:
        if p.get("clk"):
            mark(p["clk"], "clk")
        for x in p["r"] + ([p["rst"]] if p.get("rst") else []):
            mark(x, "in")
        for x in p["w"]:
            mark(x, "out")
    for a in m.assigns:
        for x in a["r"]:
            mark(x, "in")
        for x in a["w"]:
            mark(x, "out")
    for inst in m.insts:
        for expr in inst["conns"].values():
            for x in _conn_ids(expr, m.lang):
                mark(x, "in")
    return use


def _logic_nodes(design, m, d, touch, src_file, iface=()):
    """One node per always/process block and per continuous assignment."""
    defs = design["defs"]
    blocks = list(m.procs)
    assigns = m.assigns
    if len(assigns) > 24:
        merged = {"kind": "comb", "w": _uniq(x for a in assigns for x in a["w"]),
                  "r": _uniq(x for a in assigns for x in a["r"]), "line": assigns[0]["line"], "label": "assigns"}
        assigns = [merged]
    for a in assigns:
        blocks.append(dict(a, kind=a.get("kind", "assign")))
    used = {i["name"] for i in d["insts"]}
    known = set(m.order) | set(m.signals)
    bases = known | set(iface)
    ok = lambda x: x in known or ("." in x and x.split(".")[0] in bases)
    for k, b in enumerate(blocks):
        b = dict(b, w=[x for x in b["w"] if ok(x)] or b["w"], r=[x for x in b["r"] if ok(x)])
        if not b["w"]:
            continue
        base = b.get("label") or (b["w"][0] + ("_reg" if b["kind"] == "ff" else ""))
        name = base
        n2 = 1
        while name in used:
            n2 += 1
            name = "%s~%d" % (base, n2)
        used.add(name)
        did = "logic:%s#%d" % (m.name, k)
        kind = {"ff": "reg", "latch": "latch"}.get(b["kind"], "comb")
        title = {"reg": "always_ff" if m.lang != "vhdl" else "process", "latch": "latch",
                 "comb": "always_comb" if b["kind"] == "comb" else "assign"}[kind]
        if m.lang == "vhdl" and kind == "comb":
            title = "process" if b["kind"] == "comb" else "concurrent"
        ld = M.new_def(title, kind, src={"file": src_file, "line": b["line"]})
        if b.get("clk"):
            ld["pins"].append(M.pin(b["clk"], "in", "clk", edge=b.get("edge")))
            touch(b["clk"], [name, b["clk"]])
        if b.get("rst"):
            ld["pins"].append(M.pin(b["rst"], "in", "rst", pol=b.get("pol")))
            touch(b["rst"], [name, b["rst"]])
        skip = {b.get("clk"), b.get("rst")}
        for s in b["r"]:
            if s in skip or s in b["w"]:
                continue
            ld["pins"].append(M.pin(s, "in", "data"))
            touch(s, [name, s])
        for s in b["w"]:
            ld["pins"].append(M.pin(s, "out", "data"))
            touch(s, [name, s])
        if kind == "comb":
            if b.get("label") == "assigns":
                ld["deps"] = {w: a["r"] for a in m.assigns for w in a["w"] if ok(w)}
            else:
                ld["deps"] = {w: b["r"] for w in b["w"]}
        if kind == "reg":
            ld["nregs"] = len(b["w"])
        defs[did] = ld
        d["insts"].append({"name": name, "def": did, "line": b["line"]})


def _finish_kinds(design, rtl_ids):
    defs = design["defs"]
    for rid in rtl_ids:
        d = defs[rid]
        nets = []
        for net in d["nets"]:
            if net["kind"] == "bus":  # SystemVerilog interface bundle
                nets.append(net)
                continue
            kinds = set()
            for inst, p in net["ends"]:
                target = d if inst == "" else defs.get(next((i["def"] for i in d["insts"] if i["name"] == inst), ""))
                pp = M.pin_index(target).get(p) if target else None
                if pp and pp["kind"] != "data":
                    kinds.add(pp["kind"])
            net["kind"] = kinds.pop() if len(kinds) == 1 else ("clk" if "clk" in kinds else "data")
            nets.append(net)
        d["nets"] = nets


def parse_files(paths: list, incdirs=()) -> list:
    import os
    mods = []
    del MISSING_INCLUDES[:]
    incdirs = list(dict.fromkeys(list(incdirs) + [os.path.dirname(p) for p in paths]))
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        low = p.lower()
        if low.endswith((".vhd", ".vhdl")):
            mods.extend(parse_vhdl(text, p))
        else:
            mods.extend(parse_verilog(text, p, incdirs))
    for m in mods:
        for b in m.procs:
            b.setdefault("file", m.file)
    return mods
