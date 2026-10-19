"""Restricted Python execution for the optional ``exec`` compatibility mode.

This is an additional guard for trusted teams, not a security boundary. Run
workers in containers without host mounts, credentials or network access if a
program may be untrusted.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import io
import math
import signal
import threading
import traceback
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from .errors import ExecutionTimeout

MAX_CODE_LENGTH = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
CODE_CHARSET = frozenset(
    "".join(chr(code) for code in range(32, 127)) + "\n\r\t"
)


def restricted_numpy() -> SimpleNamespace:
    names = [
        "abs", "all", "allclose", "any", "arange", "arccos", "arcsin", "arctan",
        "arctan2", "argmax", "argmin", "argsort", "array", "asarray", "bool_",
        "clip", "concatenate", "cos", "cross", "deg2rad", "diag", "diff", "dot",
        "empty", "exp", "eye", "float32", "float64", "full", "hstack", "hypot",
        "identity", "inf", "int32", "int64", "interp", "isclose", "isfinite",
        "isnan", "linspace", "log", "max", "maximum", "mean", "median",
        "meshgrid", "min", "minimum", "nan", "ndarray", "newaxis", "nonzero",
        "ones", "ones_like", "pi", "rad2deg", "ravel", "repeat", "reshape",
        "round", "sign", "sin", "sort", "sqrt", "stack", "std", "sum", "tan",
        "tile", "transpose", "uint8", "unique", "vstack", "where", "zeros",
        "zeros_like",
    ]
    values = {name: getattr(np, name) for name in names}
    values["linalg"] = SimpleNamespace(
        **{
            name: getattr(np.linalg, name)
            for name in [
                "norm", "inv", "pinv", "solve", "lstsq", "svd", "eig", "eigh",
                "det", "matrix_rank",
            ]
        }
    )
    values["random"] = SimpleNamespace(
        **{
            name: getattr(np.random, name)
            for name in ["default_rng", "uniform", "normal", "randint", "choice", "seed"]
        }
    )
    return SimpleNamespace(**values)


NUMPY = restricted_numpy()


def _numerical_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in {"numpy", "math"}:
        raise ImportError("Only numpy and math are available in robot programs")
    return NUMPY if name == "numpy" else math


SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in [
        "abs", "all", "any", "bool", "dict", "enumerate", "Exception", "float",
        "int", "isinstance", "len", "list", "map", "max", "min", "next", "print",
        "range", "reversed", "round", "RuntimeError", "set", "slice", "sorted",
        "str", "sum", "tuple", "ValueError", "zip", "ZeroDivisionError",
        "TypeError", "KeyError", "IndexError", "AssertionError",
    ]
}
SAFE_BUILTINS["__import__"] = _numerical_import

FORBIDDEN_NAMES = frozenset(
    {
        "env", "APIS", "INPUTS", "open", "eval", "exec", "compile", "globals",
        "locals", "getattr", "setattr", "vars", "dir", "breakpoint", "input",
        "help", "memoryview",
    }
)
FORBIDDEN_ATTRIBUTES = frozenset({"tofile", "dump", "reset"})


def validate_snippet(code: str) -> str:
    if not isinstance(code, str):
        raise TypeError("program must be a string")
    if not code.strip():
        raise ValueError("program must contain non-empty Python source")
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError(f"program exceeds {MAX_CODE_LENGTH} characters")
    if any(character not in CODE_CHARSET for character in code):
        raise ValueError("program contains unsupported characters")
    tree = ast.parse(code, filename="<robot-program>")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES or (node.id.startswith("_") and node.id != "_")
        ):
            raise ValueError(f"Unavailable name in robot program: {node.id}")
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRIBUTES
        ):
            raise ValueError(f"Unavailable attribute in robot program: {node.attr}")
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
            )
            if any(not name or name not in {"numpy", "math"} for name in names) or getattr(
                node, "level", 0
            ):
                raise ValueError("Only numpy and math imports are supported")
    return code


@dataclass
class ProgramResult:
    ok: bool
    stdout: str
    stderr: str
    error: str | None = None


@contextlib.contextmanager
def _deadline(seconds: float):
    if seconds <= 0:
        raise ValueError("execution timeout must be positive")
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    timed_out = False

    def expired(signum, frame):
        nonlocal timed_out
        timed_out = True
        raise ExecutionTimeout("Execution timed out; the session cannot be resumed")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
        if timed_out:
            raise ExecutionTimeout("Execution timed out; the session cannot be resumed")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run_program(
    code: str,
    namespace: dict[str, Any],
    primitives: dict[str, Callable],
    *,
    timeout: float,
) -> ProgramResult:
    """Execute source with persistent globals and only the four primitives."""

    validate_snippet(code)
    stdout, stderr = io.StringIO(), io.StringIO()
    ok = True
    error = None
    namespace["__builtins__"] = dict(SAFE_BUILTINS)
    namespace["np"] = NUMPY
    namespace.update(primitives)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            with _deadline(timeout):
                exec(compile(code, "<robot-program>", "exec"), namespace)
        except ExecutionTimeout:
            raise
        except BaseException as exc:  # Program failures are returned, not raised.
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
    return ProgramResult(
        ok=ok,
        stdout=stdout.getvalue()[-MAX_OUTPUT_BYTES:],
        stderr=stderr.getvalue()[-MAX_OUTPUT_BYTES:],
        error=error,
    )
