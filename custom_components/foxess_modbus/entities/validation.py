"""Validation"""
from .base_validator import BaseValidator
from .modbus_charge_period_sensors import is_time_value_valid


class Range(BaseValidator):
    """Range validator"""

    def __init__(self, min_value: float, max_value: float):
        """Init"""
        self._min = min_value
        self._max = max_value

    def validate(self, data) -> bool:
        """Validate a value against a set of rules"""

        return data >= self._min and data <= self._max


class Min(BaseValidator):
    """Min validator"""

    def __init__(self, min_value: float):
        """Init"""
        self._min = min_value

    def validate(self, data) -> bool:
        """Validate a value against a set of rules"""

        return data >= self._min


class Max(BaseValidator):
    """Max validator"""

    def __init__(self, max_value: float):
        """Init"""
        self._max = max_value

    def validate(self, data) -> bool:
        """Validate a value against a set of rules"""

        return data <= self._max


class Time(BaseValidator):
    """Time validator"""

    def validate(self, data) -> bool:
        """Validate a value against a set of rules"""
        return is_time_value_valid(data)
