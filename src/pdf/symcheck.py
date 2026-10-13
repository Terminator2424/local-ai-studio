"""Verificación simbólica de cadenas de igualdades en LaTeX con sympy (sin eval).

La comprobación aritmética de verify.py solo entiende números. Aquí se leen también símbolos, raíces, potencias,
fracciones y funciones, y se comprueba la EQUIVALENCIA de cada paso: (x+1)^2 = x^2 + 2x + 1 es cierto aunque no haya números.

Principio: ante la menor ambigüedad el verificador se abstiene (no opina). Solo avisa cuando los dos lados se leen sin
dudas y se DEMUESTRA que no son iguales (diferencia no nula en varios puntos de prueba). Ejemplos de abstención:
«F(1)» (¿función o producto?), unidades («m^2», «^\\circ»), comas, desigualdades, integrales, sumatorios.
"""
from __future__ import annotations

import re

import sympy as sp

MAX_LEN = 400
_TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?|\\[A-Za-z]+|[A-Za-z]|[-+*/^_(){}\[\]=<>|!,.;:&%'\"])")
_FUNCS = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "ln": sp.log, "exp": sp.exp, "log": lambda x: sp.log(x, 10)}
_SKIP = {"left", "right", "displaystyle", "textstyle", "quad", "qquad", ",", ";", "!", ":", "big", "Big"}


class Ambiguous(Exception):
    """La expresión es ambigua o no soportada: no se emite ningún juicio."""


def _tokens(s: str) -> list[str]:
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", " ").replace("\\:", " ")
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            if s[pos:].strip() == "":
                break
            raise Ambiguous(f"carácter no reconocido: {s[pos:pos + 5]!r}")
        pos = m.end()
        t = m.group(1)
        if t.startswith("\\") and t[1:] in _SKIP:
            continue
        out.append(t)
    return out


class _Parser:
    def __init__(self, toks: list[str]):
        self.t = toks
        self.i = 0

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, expect: str | None = None) -> str:
        if self.i >= len(self.t):
            raise Ambiguous("fin inesperado")
        tok = self.t[self.i]
        if expect is not None and tok != expect:
            raise Ambiguous(f"se esperaba {expect!r} y hay {tok!r}")
        self.i += 1
        return tok

    # expr := term (('+'|'-') term)*
    def expr(self):
        v = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            r = self.term()
            v = v + r if op == "+" else v - r
        return v

    # term := unary (('*'|'\cdot'|'\times'|'/'|implicit) unary)*
    def term(self):
        v = self.unary()
        while True:
            p = self.peek()
            if p in ("*", "\\cdot", "\\times"):
                self.take()
                v = v * self.unary()
            elif p in ("/", "\\div"):
                self.take()
                v = v / self.unary()
            elif p is not None and self._starts_atom(p):
                if p in ("(", "[") and self._prev_is_letter():
                    raise Ambiguous("símbolo seguido de paréntesis: ¿función o producto?")
                v = v * self.unary()
            else:
                return v

    def _prev_is_letter(self) -> bool:
        return self.i > 0 and re.fullmatch(r"[A-Za-z]", self.t[self.i - 1]) is not None

    @staticmethod
    def _starts_atom(p: str) -> bool:
        return bool(re.fullmatch(r"\d+(?:\.\d+)?|[A-Za-z]|\(|\[|\{", p)) or p in (
            "\\frac", "\\sqrt", "\\pi") or p[1:] in _FUNCS and p.startswith("\\")

    def unary(self):
        p = self.peek()
        if p == "-":
            self.take()
            return -self.unary()
        if p == "+":
            self.take()
            return self.unary()
        return self.power()

    def power(self):
        base = self.atom()
        if self.peek() == "^":
            self.take()
            exp = self.exponent()
            if isinstance(exp, sp.Basic) and exp.is_number and abs(exp) > 12:
                raise Ambiguous("exponente demasiado grande")
            return base ** exp
        return base

    def exponent(self):
        p = self.peek()
        if p == "{":
            self.take("{")
            v = self.expr()
            self.take("}")
            return v
        if p == "-":
            self.take()
            return -self.exponent()
        if p is not None and re.fullmatch(r"\d|[A-Za-z]", p):      # ^2 y ^n: un solo carácter, como en TeX
            self.take()
            return sp.Rational(p) if p.isdigit() else sp.Symbol(p)
        raise Ambiguous("exponente no soportado")

    def group(self, open_: str, close: str):
        self.take(open_)
        v = self.expr()
        if self.peek() == ",":
            raise Ambiguous("coma dentro de un paréntesis")
        self.take(close)
        return v

    def atom(self):
        p = self.peek()
        if p is None:
            raise Ambiguous("expresión incompleta")
        if re.fullmatch(r"\d+(?:\.\d+)?", p):
            self.take()
            return sp.Rational(p)
        if re.fullmatch(r"[A-Za-z]", p):
            self.take()
            return self.symbol(p)
        if p == "(":
            return self.group("(", ")")
        if p == "[":
            return self.group("[", "]")
        if p == "{":
            return self.group("{", "}")
        if p == "\\frac":
            self.take()
            a = self.braced()
            b = self.braced()
            if b == 0:
                raise Ambiguous("división por cero")
            return a / b
        if p == "\\sqrt":
            self.take()
            n = 2
            if self.peek() == "[":
                self.take("[")
                n = self.expr()
                self.take("]")
            return sp.root(self.braced(), n) if n != 2 else sp.sqrt(self.braced())
        if p == "\\pi":
            self.take()
            return sp.pi
        if p.startswith("\\") and p[1:] in _FUNCS:
            self.take()
            if self.peek() == "(":
                arg = self.group("(", ")")
            elif self.peek() == "{":
                arg = self.braced()
            else:
                raise Ambiguous("función sin argumento entre paréntesis")
            return _FUNCS[p[1:]](arg)
        raise Ambiguous(f"no soportado: {p}")

    def braced(self):
        self.take("{")
        v = self.expr()
        self.take("}")
        return v

    def symbol(self, letter: str):
        """Letra con subíndice opcional: x_i, T_{12}. Una letra seguida de otra letra son dos símbolos (xy = x·y)."""
        if self.peek() == "_":
            self.take()
            if self.peek() == "{":
                self.take("{")
                parts = []
                while self.peek() != "}":
                    tok = self.take()
                    if not re.fullmatch(r"[A-Za-z0-9]", tok):
                        raise Ambiguous("subíndice no simple")
                    parts.append(tok)
                self.take("}")
                return sp.Symbol(f"{letter}_{''.join(parts)}")
            tok = self.take()
            if not re.fullmatch(r"[A-Za-z0-9]", tok):
                raise Ambiguous("subíndice no simple")
            return sp.Symbol(f"{letter}_{tok}")
        return sp.Symbol(letter)


def parse(latex: str):
    """LaTeX → expresión de sympy. Lanza Ambiguous si no se puede leer sin dudas."""
    s = latex.strip().rstrip(".,;").strip()
    if not s or len(s) > MAX_LEN:
        raise Ambiguous("vacía o demasiado larga")
    if re.search(r"\\(?:mathrm|text|circ|%|iint|int|sum|prod|lim|infty|le|ge|leq|geq|neq|in|cup|cap|\{|\})|\^\\|%|\|", s):
        raise Ambiguous("unidades, conjuntos, integrales o sumatorios")
    p = _Parser(_tokens(s))
    v = p.expr()
    if p.peek() is not None:
        raise Ambiguous(f"sobran símbolos: {p.peek()!r}")
    return v


_SPLIT = re.compile(r"(=|\\approx|≈)")
_BAD = re.compile(r"\\le|\\ge|\\neq|<|>|\\in\b|≤|≥|≠")


def _free_vs_numeric(a, b):
    return bool(a.free_symbols or b.free_symbols)


def _decimals(text: str) -> int:
    m = re.search(r"\.(\d+)\s*[.,;]?\s*$", text.strip())
    return len(m.group(1)) if m else 0


def _proves_different(a, b) -> bool:
    """Diferencia no nula en TODOS los puntos de prueba donde está definida (y simplify tampoco la anula)."""
    d = sp.simplify(a - b)
    if d == 0:
        return False
    syms = sorted(d.free_symbols, key=lambda s: s.name)
    if not syms:
        return d != 0
    points = [sp.Rational(2), sp.Rational(3), sp.Rational(5, 2), sp.Rational(-7, 3), sp.Rational(11)]
    nonzero = zero = 0
    for k in range(5):
        subs = {s: points[(k + j) % len(points)] for j, s in enumerate(syms)}
        try:
            v = sp.nsimplify(d.subs(subs))
        except Exception:  # noqa: BLE001
            continue
        if not v.is_number or v.has(sp.zoo, sp.nan, sp.oo):
            continue
        if v == 0:
            zero += 1
        else:
            nonzero += 1
    return nonzero >= 3 and zero == 0


def _read_sides(latex: str):
    """(lados como texto, operadores, expresiones leídas o None si un lado es ambiguo)."""
    parts = _SPLIT.split(latex)
    sides, ops = parts[0::2], parts[1::2]
    exprs = []
    for sd in sides:
        try:
            exprs.append(parse(sd))
        except (Ambiguous, sp.SympifyError, ValueError, TypeError, ZeroDivisionError, RecursionError):
            exprs.append(None)
    return sides, ops, exprs


def _pair_status(a, b, approx: bool, lhs: str, rhs: str) -> tuple[str, str]:
    """('eq' | 'neq' | 'unk', mensaje) para dos lados ya leídos."""
    try:
        if not _free_vs_numeric(a, b):
            if sp.simplify(a - b) == 0:
                return "eq", ""
            x, y = sp.N(a, 30), sp.N(b, 30)
            dec = _decimals(rhs)
            tol = sp.Rational(1, 2) / 10 ** dec if dec else sp.Integer(0)       # «= 171.33» admite redondeo
            if approx:
                tol = max(tol, max(abs(x), abs(y)) / 100)
            if abs(x - y) <= tol + sp.Float("1e-12"):
                return "eq", ""
            return "neq", f"no cuadra: «{lhs.strip()}» = {float(x):g} pero «{rhs.strip()}» = {float(y):g}"
        # Solo identidades (mismos símbolos a ambos lados). «A = 36» o «h_x = 1» son definiciones, no identidades.
        if not approx and a.free_symbols == b.free_symbols:
            if sp.simplify(a - b) == 0:
                return "eq", ""
            if _proves_different(a, b):
                return "neq", f"no son equivalentes: «{lhs.strip()}» y «{rhs.strip()}»"
    except (TypeError, ValueError, ZeroDivisionError, RecursionError):
        pass
    return "unk", ""


def _statuses(latex: str) -> list[tuple[str, str]] | None:
    if _BAD.search(latex) or len(latex) > MAX_LEN * 3:
        return None
    sides, ops, exprs = _read_sides(latex)
    if len(sides) < 2:
        return None
    out = []
    for i in range(len(exprs) - 1):
        a, b = exprs[i], exprs[i + 1]
        out.append(("unk", "") if a is None or b is None else _pair_status(a, b, ops[i] != "=", sides[i], sides[i + 1]))
    return out


def symbolic_chain_error(latex: str) -> str | None:
    """Si la fórmula es una cadena de igualdades que se puede leer sin ambigüedad, comprueba cada paso.
    Devuelve el motivo del primer paso que NO es equivalente, o None (todo bien o no se puede opinar)."""
    st = _statuses(latex)
    if not st:
        return None
    return next((msg for state, msg in st if state == "neq"), None)


def verify_chain(latex: str) -> tuple[str, str]:
    """Estado de una cadena de igualdades: ('ok', '') si TODOS los pasos están comprobados, ('error', motivo) si alguno es
    falso, ('unknown', motivo) si no se puede comprobar (símbolos ambiguos, definiciones, unidades, funciones…)."""
    st = _statuses(latex)
    if not st:
        return "unknown", "no es una cadena de igualdades que pueda leerse sin ambigüedad"
    bad = next((msg for state, msg in st if state == "neq"), None)
    if bad:
        return "error", bad
    if all(state == "eq" for state, _ in st):
        return "ok", ""
    n = sum(1 for state, _ in st if state != "eq")
    return "unknown", f"{n} paso(s) no se pueden comprobar (definiciones, funciones, unidades o símbolos ambiguos)"


def equivalent(a: str, b: str) -> bool | None:
    """¿Son equivalentes dos expresiones LaTeX? True/False si se puede decidir, None si es ambiguo."""
    try:
        x, y = parse(a), parse(b)
    except (Ambiguous, sp.SympifyError, ValueError, TypeError, ZeroDivisionError, RecursionError):
        return None
    if sp.simplify(x - y) == 0:
        return True
    return False if _proves_different(x, y) else None
