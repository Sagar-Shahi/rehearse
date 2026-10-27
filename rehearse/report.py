"""Terminal output: one line per step, then an ASCII timeline and a summary."""

from __future__ import annotations

from .player import StepResult

_CODES = {"red": "31", "green": "32", "yellow": "33", "dim": "2", "bold": "1"}


def paint(text: str, color: str, enabled: bool) -> str:
    return f"\033[{_CODES[color]}m{text}\033[0m" if enabled else text


def describe(r: StepResult) -> str:
    if r.kind == "wait":
        return f"{r.duration:.2f}s"
    bits = []
    if r.error:
        bits.append(r.error)
    elif r.status is not None:
        bits.append(str(r.status))
    if r.attempts > 1:
        bits.append(f"{r.attempts} attempts")
    if r.note:
        bits.append(r.note)
    return ", ".join(bits)


def format_line(r: StepResult, color: bool = False) -> str:
    if r.kind == "info":
        mark = paint("info", "dim", color)
    elif r.ok:
        mark = paint(" ok ", "green", color)
    else:
        mark = paint("FAIL", "red", color)
    indent = "  " * r.depth
    detail = describe(r)
    line = f"[{mark}] {indent}{r.label}" + (f"  ({detail})" if detail else "")
    for failure in r.failures:
        line += "\n" + paint(f"         {indent}- {failure}", "red", color)
    return line


def render_timeline(results: list[StepResult], width: int = 36) -> str:
    """Each step as a bar positioned and sized by when it ran and how long it took."""
    if not results:
        return ""
    total = max(r.start + r.duration for r in results) or 0.001
    lines = [f"Timeline (total {total:.2f}s)"]
    for r in sorted(results, key=lambda r: (r.start, r.depth)):
        if r.kind == "info":
            continue
        col = min(width - 1, int(r.start / total * width))
        span = max(1, min(width - col, round(r.duration / total * width)))
        char = "-" if r.kind == "wait" else ("#" if r.ok else "!")
        bar = (" " * col + char * span).ljust(width)
        label = r.label if len(r.label) <= 34 else r.label[:33] + "…"
        lines.append(f"{r.start:6.2f}s |{bar}| {'  ' * r.depth}{label}")
    return "\n".join(lines)


def summary(results: list[StepResult], color: bool = False) -> str:
    sent = [r for r in results if r.kind == "send"]
    failed = [r for r in sent if not r.ok]
    total = max((r.start + r.duration for r in results), default=0.0)
    if failed:
        text = f"{len(failed)} of {len(sent)} requests failed their expectations in {total:.2f}s"
        return paint(text, "red", color)
    return paint(f"all {len(sent)} requests passed in {total:.2f}s", "green", color)
