"""Module beta — imports alpha, completing the circular dependency."""

import mod_alpha.alpha


def beta_func() -> str:
    return "beta"


def beta_uses_alpha() -> str:
    return mod_alpha.alpha.alpha_func()
