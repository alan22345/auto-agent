"""Module alpha — imports beta, forming a circular dependency."""

import mod_beta.beta


def alpha_func() -> str:
    return mod_beta.beta.beta_func()
