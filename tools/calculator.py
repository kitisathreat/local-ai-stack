"""
title: Calculator
author: local-ai-stack
description: Perform accurate arithmetic, algebra, and unit conversions. Prevents model hallucination on math.
required_open_webui_version: 0.4.0
requirements: sympy
version: 1.0.0
licence: MIT
"""

import math
import ast
import operator
from typing import Callable, Any, Optional
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()
        self._safe_ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos,
            ast.Mod: operator.mod,
            ast.FloorDiv: operator.floordiv,
        }
        self._safe_names = {
            "abs": abs, "round": round, "min": min, "max": max,
            "sum": sum, "pow": pow, "sqrt": math.sqrt, "log": math.log,
            "log10": math.log10, "log2": math.log2, "exp": math.exp,
            "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "asin": math.asin, "acos": math.acos, "atan": math.atan,
            "atan2": math.atan2, "degrees": math.degrees, "radians": math.radians,
            "ceil": math.ceil, "floor": math.floor, "factorial": math.factorial,
            "pi": math.pi, "e": math.e, "inf": math.inf,
        }

    def _eval(self, node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"Unsupported constant: {node.value}")
        elif isinstance(node, ast.BinOp):
            op = self._safe_ops.get(type(node.op))
            if not op:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op(self._eval(node.left), self._eval(node.right))
        elif isinstance(node, ast.UnaryOp):
            op = self._safe_ops.get(type(node.op))
            if not op:
                raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
            return op(self._eval(node.operand))
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only simple function calls are allowed")
            func = self._safe_names.get(node.func.id)
            if not func:
                raise ValueError(f"Unknown function: {node.func.id}")
            args = [self._eval(a) for a in node.args]
            return func(*args)
        elif isinstance(node, ast.Name):
            val = self._safe_names.get(node.id)
            if val is None:
                raise ValueError(f"Unknown name: {node.id}")
            return val
        raise ValueError(f"Unsupported expression type: {type(node).__name__}")

    def calculate(
        self,
        expression: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Evaluate a mathematical expression and return the exact result.
        Supports: +, -, *, /, **, %, //, sqrt, sin, cos, tan, log, exp, factorial, pi, e.
        :param expression: The math expression to evaluate (e.g. "sqrt(144) + 2**8")
        :return: The computed result as a string
        """
        try:
            tree = ast.parse(expression.strip(), mode="eval")
            result = self._eval(tree.body)
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            return f"{expression} = {result}"
        except ZeroDivisionError:
            return "Error: Division by zero"
        except ValueError as e:
            return f"Error: {e}"
        except SyntaxError:
            return f"Error: Invalid expression syntax — '{expression}'"
        except Exception as e:
            return f"Calculation error: {str(e)}"

    def convert_units(
        self,
        value: float,
        from_unit: str,
        to_unit: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Convert between common units (length, weight, temperature, volume, speed).
        :param value: The numeric value to convert
        :param from_unit: Source unit (e.g. "km", "kg", "celsius", "liters", "mph")
        :param to_unit: Target unit (e.g. "miles", "lbs", "fahrenheit", "gallons", "kph")
        :return: Converted value with units
        """
        conversions = {
            # Length (base: meters)
            ("m", "km"): 0.001, ("m", "miles"): 0.000621371, ("m", "ft"): 3.28084,
            ("m", "inches"): 39.3701, ("m", "cm"): 100, ("m", "mm"): 1000,
            ("km", "m"): 1000, ("km", "miles"): 0.621371, ("km", "ft"): 3280.84,
            ("miles", "km"): 1.60934, ("miles", "m"): 1609.34, ("miles", "ft"): 5280,
            ("ft", "m"): 0.3048, ("ft", "inches"): 12, ("ft", "cm"): 30.48,
            ("inches", "cm"): 2.54, ("inches", "ft"): 0.0833333, ("inches", "m"): 0.0254,
            ("cm", "m"): 0.01, ("cm", "inches"): 0.393701, ("mm", "m"): 0.001,
            # Weight (base: kg)
            ("kg", "lbs"): 2.20462, ("kg", "g"): 1000, ("kg", "oz"): 35.274,
            ("lbs", "kg"): 0.453592, ("lbs", "oz"): 16, ("lbs", "g"): 453.592,
            ("g", "kg"): 0.001, ("g", "oz"): 0.035274, ("oz", "g"): 28.3495,
            ("oz", "lbs"): 0.0625, ("oz", "kg"): 0.0283495,
            # Volume
            ("liters", "gallons"): 0.264172, ("liters", "ml"): 1000,
            ("liters", "cups"): 4.22675, ("liters", "fl_oz"): 33.814,
            ("gallons", "liters"): 3.78541, ("gallons", "cups"): 16,
            ("ml", "liters"): 0.001, ("ml", "cups"): 0.00422675,
            # Speed
            ("mph", "kph"): 1.60934, ("mph", "m/s"): 0.44704,
            ("kph", "mph"): 0.621371, ("kph", "m/s"): 0.277778,
            ("m/s", "mph"): 2.23694, ("m/s", "kph"): 3.6,
            # Data
            ("bytes", "kb"): 0.001, ("bytes", "mb"): 1e-6, ("bytes", "gb"): 1e-9,
            ("kb", "bytes"): 1000, ("kb", "mb"): 0.001, ("mb", "gb"): 0.001,
            ("gb", "mb"): 1000, ("gb", "tb"): 0.001, ("tb", "gb"): 1000,
        }

        from_l = from_unit.lower().rstrip("s")
        to_l = to_unit.lower().rstrip("s")

        # Temperature (special case)
        if from_l in ("celsius", "c") and to_l in ("fahrenheit", "f"):
            result = value * 9 / 5 + 32
            return f"{value}°C = {result:.4g}°F"
        if from_l in ("fahrenheit", "f") and to_l in ("celsius", "c"):
            result = (value - 32) * 5 / 9
            return f"{value}°F = {result:.4g}°C"
        if from_l in ("celsius", "c") and to_l in ("kelvin", "k"):
            return f"{value}°C = {value + 273.15:.4g} K"
        if from_l in ("kelvin", "k") and to_l in ("celsius", "c"):
            return f"{value} K = {value - 273.15:.4g}°C"

        factor = conversions.get((from_l, to_l))
        if factor is None:
            return f"Unknown conversion: {from_unit} → {to_unit}"

        result = value * factor
        return f"{value} {from_unit} = {result:.6g} {to_unit}"
