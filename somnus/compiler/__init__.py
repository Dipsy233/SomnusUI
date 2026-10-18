"""Semantic compiler (deterministic; never calls an LLM).

Pipeline::

    RawTree -> Normalize -> Primitive caps -> Relations -> Structural simplify
            -> Pattern match -> Entity -> Fingerprint -> Projection
"""

from somnus.compiler.compile import CompileResult, Compiler, compile_tree

__all__ = ["Compiler", "CompileResult", "compile_tree"]
