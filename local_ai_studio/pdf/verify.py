"""Verificaciones EXACTAS (sin IA) de fórmulas, código y cifras: lo que no se puede garantizar con un modelo, se comprueba con código.

- Cadenas numéricas: «F(1) = 1/3·[27+110+…] = 513/3 = 171» se recalcula; si no cuadra, se marca.
- Cifras de una fórmula: deben existir en el texto vectorial de la página (una cifra inventada por la visión se detecta).
- Operadores: la visión no puede tener más «+», «=», «−»… que el texto de la página (signos inventados o cambiados).
- Código: cada línea debe existir en el texto del PDF y los paréntesis/llaves deben cuadrar (o compilar, en Python).
- Doble lectura: dos lecturas de la visión a distinta resolución deben coincidir; si no, se marca y se ofrecen ambas.
"""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ normalización
_STRIP = re.compile(r"\\(?:left|right|displaystyle|,|;|!|:|quad|qquad)|\s+")


def norm_eq(latex: str) -> str:
    s = latex.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = _STRIP.sub("", s)
    return s.rstrip(".,;")


def _nums(text: str) -> list[str]:
    return [t.replace(",", ".") for t in re.findall(r"\d+(?:[.,]\d+)?", text)]


# ------------------------------------------------------------- cadenas numéricas
_SAFE = re.compile(r"^[\d\s+\-*/().]+$")


def _to_py(side: str) -> str | None:
    s = side
    s = re.sub(r"\\(?:left|right|displaystyle)", "", s)
    s = re.sub(r"\\[,;!:]|\\q?quad", "", s)
    s = s.replace("\\cdot", "*").replace("\\times", "*").replace("×", "*").replace("·", "*").replace("−", "-")
    s = s.replace("^\\circ", "").replace("\\%", "").replace("{}", "")
    for _ in range(8):
        m = re.search(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", s)
        if not m:
            break
        s = s[:m.start()] + f"(({m.group(1)})/({m.group(2)}))" + s[m.end():]
    s = s.replace("{", "(").replace("}", ")").replace("[", "(").replace("]", ")")
    s = re.sub(r"\^\s*\(", "**(", s)
    s = re.sub(r"\^\s*(-?\d+(?:\.\d+)?)", r"**(\1)", s)
    s = re.sub(r"(\d|\))\s*\(", r"\1*(", s)
    s = re.sub(r"\)\s*(\d)", r")*\1", s)
    s = s.strip().rstrip(".,;")
    return s if s and _SAFE.match(s.replace("**", "*")) else None


def _safe_eval(expr: str) -> float | None:
    """Evalúa SOLO aritmética (números, + − * / ** y paréntesis) recorriendo el AST; nunca usa eval()."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            if isinstance(n.op, ast.Pow) and abs(b) <= 12 and abs(a) <= 1e6:
                return a ** b
        raise ValueError("expresión no permitida")
    try:
        return ev(tree)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def numeric_chain_error(latex: str) -> str | None:
    """Si la fórmula es una cadena de igualdades numéricas, comprueba que sean ciertas. Devuelve el error o None."""
    parts = re.split(r"(=|\\approx|≈)", latex)
    sides = parts[0::2]
    ops = parts[1::2]
    vals: list[tuple[float | None, str]] = []
    for sd in sides:
        py = _to_py(sd)
        vals.append((_safe_eval(py) if py else None, sd.strip()))
    for i in range(len(vals) - 1):
        (a, sa), (b, sb) = vals[i], vals[i + 1]
        if a is None or b is None:
            continue
        approx = ops[i] != "="
        decimals = re.search(r"\.(\d+)\s*$", sb.rstrip(".,; "))
        dec = len(decimals.group(1)) if decimals else 0
        tol = 0.5 * 10 ** (-dec) if dec else 1e-9 * max(1.0, abs(a))
        if approx:
            tol = max(tol, 0.01 * max(abs(a), abs(b), 1e-9))
        if abs(a - b) > tol + 1e-12:
            return f"no cuadra: «{sa}» = {a:g} pero «{sb}» = {b:g}"
    return None


def chain_error(latex: str) -> str | None:
    """Comprueba una cadena de igualdades: primero aritmética exacta y después equivalencia simbólica (sympy)."""
    err = numeric_chain_error(latex)
    if err:
        return err
    try:
        from .symcheck import symbolic_chain_error
        return symbolic_chain_error(latex)
    except Exception:
        return None


# ------------------------------------------------------------ fórmula vs texto vectorial
def formula_vs_raw(latex: str, raw: str) -> list[str]:
    """Cifras de la fórmula que NO existen en el texto vectorial de la página (posible cifra inventada)."""
    raw_nums = set(_nums(raw))
    clean = re.sub(r"_\{?[A-Za-z0-9]+\}?|\^\{?[A-Za-z0-9]+\}?", " ", latex)     # subíndices/exponentes simbólicos
    missing = [n for n in _nums(clean) if n not in raw_nums and not (len(n) == 1 and n in "0123456789")]
    return sorted(set(missing))


def operator_audit(eqs: list[str], raw: str) -> list[str]:
    """La visión no puede tener MÁS operadores que el texto de la página (signos o igualdades inventados)."""
    joined = "\n".join(eqs)
    out = []
    for name, rx in (("+", r"\+"), ("=", r"="), ("−", r"[−\-]"), ("≈", r"≈|\\approx"), ("≤", r"≤|\\leq?\b"), ("≥", r"≥|\\geq?\b")):
        n_eq = len(re.findall(rx, joined))
        n_raw = len(re.findall(rx.replace(r"\\approx", "≈").replace(r"\\leq?\b", "≤").replace(r"\\geq?\b", "≥"), raw))
        if n_eq > n_raw + 1 and name in ("+", "=", "−"):
            out.append(f"la lectura visual tiene {n_eq} «{name}» y el texto de la página {n_raw}")
    return out


# --------------------------------------------------------------------- doble lectura
@dataclass
class EqCheck:
    ok: bool = True
    why: list[str] = field(default_factory=list)
    alt: str = ""


def compare_readings(eqs1: list, eqs2: list | None, raw: str) -> list[EqCheck]:
    """Verifica cada fórmula de la 1.ª lectura: coincidencia con la 2.ª, cifras presentes en el texto y cadena numérica."""
    out: list[EqCheck] = []
    norm2 = [norm_eq(e) for _, e in eqs2] if eqs2 is not None else None
    for _, e in eqs1:
        chk = EqCheck()
        n1 = norm_eq(e)
        if norm2 is not None and n1 not in norm2:
            best, ratio = "", 0.0
            for (_, e2), n2 in zip(eqs2 or [], norm2, strict=True):
                r = difflib.SequenceMatcher(None, n1, n2).ratio()
                if r > ratio:
                    best, ratio = e2, r
            chk.ok = False
            if ratio >= 0.6:
                chk.why.append("las dos lecturas visuales no coinciden")
                chk.alt = best
            else:
                chk.why.append("no aparece en la 2.ª lectura visual")
        miss = formula_vs_raw(e, raw)
        if miss:
            chk.ok = False
            chk.why.append("cifras que no están en el texto del PDF: " + ", ".join(miss[:6]))
        err = chain_error(e)
        if err:
            chk.ok = False
            chk.why.append(err)
        out.append(chk)
    return out


def diff_lines(a: str, b: str, threshold: float = 0.92) -> list[str]:
    """Líneas de la lectura 'a' que no tienen una casi idéntica en la lectura 'b' (OCR dudoso)."""
    lines_b = [re.sub(r"\s+", " ", ln).strip() for ln in b.splitlines() if ln.strip()]
    out = []
    for ln in a.splitlines():
        s_ = re.sub(r"\s+", " ", ln).strip()
        if len(s_) < 4:
            continue
        if s_ in lines_b:
            continue
        if max((difflib.SequenceMatcher(None, s_, x).ratio() for x in lines_b), default=0.0) < threshold:
            out.append(ln)
    return out


# ------------------------------------------------------------------------- código
def _balanced(code: str, lang: str = "") -> str | None:
    s = code
    # 1.º literales de expresión regular (/[A-Z]/, /[!@#$%]/): pueden contener # ' " [ ( que no son sintaxis
    s = re.sub(r"(?<=[(,=:!&|?\s])/(?![/*\s])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[]){1,80}/[gimsuy]*", '""', s)
    # 2.º comentarios (solo // y /* */ salvo en Python/shell, donde también # )
    s = re.sub(r"/\*.*?\*/|//[^\n]*", "", s, flags=re.S)
    if lang in ("python", "sh", "bash", "ruby"):
        s = re.sub(r"#[^\n]*", "", s)
    s = re.sub(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`", '""', s)
    stack, pairs = [], {")": "(", "]": "[", "}": "{"}
    for ch in s:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}" and (not stack or stack.pop() != pairs[ch]):
            return f"delimitador «{ch}» sin pareja"
    return f"{len(stack)} delimitador(es) sin cerrar" if stack else None


def check_code_block(code: str, lang: str, raw: str) -> list[str]:
    """Problemas de un bloque de código: líneas que no existen en el PDF, delimitadores descuadrados, sintaxis Python."""
    problems = []
    raw_n = {re.sub(r"\s+", "", ln) for ln in raw.splitlines() if ln.strip()}
    raw_blob = re.sub(r"\s+", "", raw)
    lines = [ln for ln in code.splitlines() if ln.strip()]
    missing = [ln for ln in lines if re.sub(r"\s+", "", ln) not in raw_n and re.sub(r"\s+", "", ln) not in raw_blob]
    if lines and len(missing) / len(lines) > 0.02:
        problems.append(f"{len(missing)} línea(s) no encontradas en el texto del PDF (p. ej. «{missing[0].strip()[:50]}»)")
    bal = _balanced(code, lang)
    if bal:
        problems.append(bal)
    if lang == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            problems.append(f"sintaxis Python no válida: {exc.msg}")
    return problems


def fenced_blocks(md: str) -> list[tuple[str, str]]:
    return [(m.group(1) or "", m.group(2)) for m in re.finditer(r"```([A-Za-z0-9_+#.-]*)\n(.*?)```", md, flags=re.S)]


# ------------------------------------------------------------------ marcas en el Markdown
_MATH = re.compile(r"\$\$(.+?)\$\$|\$([^$\n]+?)\$", re.S)


def mark_numeric_errors(md: str) -> tuple[str, list[str]]:
    """Añade « ⚠» a las fórmulas cuya cadena numérica no cuadra. Devuelve (md, lista de errores)."""
    errors: list[str] = []

    def rep(m):
        body = m.group(1) if m.group(1) is not None else m.group(2)
        err = chain_error(body)
        if err:
            errors.append(err)
            return m.group(0) + " ⚠"
        return m.group(0)
    return _MATH.sub(rep, md), errors


# ----------------------------------------------------------------------- informe
@dataclass
class Fidelity:
    formulas_total: int = 0
    formulas_ok: int = 0
    warnings: list[tuple[int, str]] = field(default_factory=list)       # (página, texto)
    code_blocks: int = 0
    code_ok: int = 0
    double_read: bool = False

    def warn(self, page: int, msg: str):
        self.warnings.append((page, msg))

    @property
    def pages_to_review(self) -> list[int]:
        return sorted({p for p, _ in self.warnings if p})

    def summary(self) -> str:
        bits = []
        if self.formulas_total:
            bits.append(f"fórmulas verificadas {self.formulas_ok}/{self.formulas_total}")
        if self.code_blocks:
            bits.append(f"bloques de código correctos {self.code_ok}/{self.code_blocks}")
        if self.warnings:
            pg = self.pages_to_review
            bits.append(f"⚠ {len(self.warnings)} a revisar" + (f" (págs. {', '.join(map(str, pg[:8]))})" if pg else ""))
        return " · ".join(bits)

    def markdown(self) -> str:
        if not (self.formulas_total or self.code_blocks or self.warnings):
            return ""
        out = ["## Informe de fidelidad", "", f"- {self.summary() or 'sin hallazgos'}"]
        if self.double_read:
            out.append("- Las fórmulas se leyeron dos veces con la visión (distinta resolución) y se contrastaron con el texto del PDF.")
        for p, msg in self.warnings[:25]:
            out.append(f"- ⚠ {'pág. ' + str(p) + ': ' if p else ''}{msg}")
        if len(self.warnings) > 25:
            out.append(f"- … y {len(self.warnings) - 25} avisos más")
        return "\n".join(out)

    def as_dict(self) -> dict:
        return {"formulas_total": self.formulas_total, "formulas_ok": self.formulas_ok, "code_blocks": self.code_blocks,
                "code_ok": self.code_ok, "warnings": len(self.warnings), "pages_to_review": self.pages_to_review,
                "summary": self.summary()}
