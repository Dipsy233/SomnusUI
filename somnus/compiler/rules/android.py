"""Android compiler rules: label promotion, wrapper folding, window typing.

SomnusDroid doc §5. The pipeline is the shared one
(normalize -> simplify -> caps -> match -> relations); this module is what the
Android provider's raw trees run through *before* the shared compiler, and the
rules the shared simplify stage is too conservative for on Android's tree
shapes.

Android's specific disease: the clickable element is an outer
``LinearLayout`` and its text lives in inner ``TextView``s, so the shared
"fold anonymous wrappers" rule would leave an unnamed clickable group with
unaddressable children. Here the children's texts are *promoted onto* the
clickable container (name = main title, ``sub=`` attribute = secondary text),
which is what makes a settings row render as one addressable line.
"""

from __future__ import annotations

import re

from somnus.ir.node import RawNode, RawTree

#: Roles whose name may be synthesised from descendant texts.
_CLICKABLE_ROLES = frozenset({"button", "listitem", "tab", "menuitem", "group", "switch", "checkbox", "radio"})

#: Class fragments of packages/containers that are pure noise at every level.
_NOISE_CLASSES = re.compile(
    r"viewstub|include|nullpane|decorview|screen decorated|"
    r"actionbaroverlaylayout|contentframeoverlay|fitwindows",
    re.IGNORECASE,
)

#: Class fragments of system chrome: keep, but only their meaningful entries.
_CHROME_CLASSES = re.compile(r"statusbar|navigationbar|inputmethod", re.IGNORECASE)


def prepare_android_tree(tree: RawTree) -> RawTree:
    """Pre-compile pass for one Android window's raw tree.

    Mutates in place (the same contract as the shared ``normalize``) and
    returns the tree for chaining.
    """
    _promote_labels(tree.root)
    _fold_android_noise(tree.root)
    return tree


def _promote_labels(root: RawNode) -> None:
    """Clickable-but-nameless containers take their texts as their name.

    Doc §5.1: the container gets ``name`` = first descendant text (the main
    title) and keeps the remaining texts as children (they still render, and
    the projection folds them). The texts are *not* removed: the compiler's
    own name-repeat rule drops the duplicate.
    """
    for node in root.walk():
        if node.name or node.role not in _CLICKABLE_ROLES:
            continue
        if "invoke" not in node.patterns and node.role not in (
            "checkbox", "switch", "radio",
        ):
            continue
        texts = [
            c for c in node.walk()
            if c is not node and c.role == "text" and c.name and not c.children
        ]
        if not texts:
            continue
        node.name = texts[0].name
        if len(texts) > 1:
            # Secondary text becomes a plain attribute (renders as ``sub=``
            # on the same line); the primary text child is dropped - the
            # shared simplify's name-repeat rule would otherwise leave the
            # row title-less after removing the duplicate child and let the
            # heuristic row detector pick the *subtitle* as the name.
            node.description = texts[1].name
        node.children = [
            c for c in node.children if not (
                c.role == "text" and c.name and not c.children
            )
        ]


def _fold_android_noise(root: RawNode) -> None:
    """Drop framework containers the shared rules keep but Android always has.

    A DecorView / FitWindowsLinearLayout wrapper carries no semantics and sits
    above every screen; folding it here keeps the fingerprint's ancestor path
    short and stable.
    """
    def walk(node: RawNode) -> None:
        kept: list[RawNode] = []
        for child in node.children:
            walk(child)
            cls = (child.native.class_name or "").lower()
            if (
                child.role in ("group", "pane", "unknown")
                and not child.name
                and not child.value
                and not (child.patterns - {"long_click"})
                and (_NOISE_CLASSES.search(cls) or cls in ("android.view.view",))
                and child.children
            ):
                kept.extend(child.children)
            else:
                kept.append(child)
        node.children = kept

    walk(root)
