"""Compile process-model-schema expressions into Pyomo expressions."""

from __future__ import annotations

from typing import Any, Mapping

from puran_mcp_support.schema.expression import Binary, Constant, Expression, Nary, SaturationRatio, Symbol, Unary
from pyomo.environ import exp, log, sqrt


def compile_pyomo(expression: Expression, symbols: Mapping[str, Any]) -> Any:
    if isinstance(expression, Constant):
        return expression.value
    if isinstance(expression, Symbol):
        if expression.name not in symbols:
            raise KeyError(f"missing Pyomo symbol {expression.name!r}")
        return symbols[expression.name]
    if isinstance(expression, Unary):
        value = compile_pyomo(expression.value, symbols)
        if expression.op == "exp":
            return exp(value)
        if expression.op == "log":
            return log(value)
        raise ValueError(f"unknown unary op {expression.op!r}")
    if isinstance(expression, Binary):
        left = compile_pyomo(expression.left, symbols)
        right = compile_pyomo(expression.right, symbols)
        if expression.op == "power":
            return left**right
        if expression.op == "monod":
            return left / (left + right)
        if expression.op == "noncompetitive_inhibition":
            return right / (left + right)
        if expression.op == "smooth_positive":
            return 0.5 * (left + sqrt(left**2 + right**2))
        raise ValueError(f"unknown binary op {expression.op!r}")
    if isinstance(expression, Nary):
        values = [compile_pyomo(value, symbols) for value in expression.values]
        if expression.op == "add":
            return sum(values)
        if expression.op == "multiply":
            product = 1.0
            for value in values:
                product *= value
            return product
        if expression.op == "smooth_minimum":
            left, right, epsilon = values
            return 0.5 * (left + right - sqrt((left - right) ** 2 + epsilon**2))
        raise ValueError(f"unknown n-ary op {expression.op!r}")
    if isinstance(expression, SaturationRatio):
        iap = 1.0
        for species, coefficient in expression.stoichiometry.items():
            if species not in expression.activities:
                raise KeyError(f"missing activity expression for {species!r}")
            iap *= compile_pyomo(expression.activities[species], symbols) ** coefficient
        return iap / compile_pyomo(expression.equilibrium_constant, symbols)
    raise TypeError(f"unsupported expression type {type(expression).__name__}")


__all__ = ["compile_pyomo"]
