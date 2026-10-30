"""Data containers for demo cases: plain tables plus the story that goes with them."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CellValue = int | float | str
ColumnType = Literal["int", "float", "str"]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _check_name(value: str) -> str:
    if not _NAME_RE.match(value):
        raise ValueError(f"{value!r} must be snake_case (used as a file name and a Jinja variable)")
    return value


def _cell_type(value: CellValue) -> ColumnType:
    if isinstance(value, bool):
        raise ValueError("booleans are not supported; use 0/1")
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


class DemoTable(BaseModel):
    """A small rectangular table; every row has exactly ``columns`` as keys."""

    name: str
    description: str
    columns: list[str]
    units: dict[str, str] = Field(default_factory=dict)
    rows: list[dict[str, CellValue]]

    _check_table_name = field_validator("name")(_check_name)

    @field_validator("columns")
    @classmethod
    def _check_columns(cls, columns: list[str]) -> list[str]:
        for column in columns:
            _check_name(column)
        if len(set(columns)) != len(columns):
            raise ValueError("duplicate column names")
        return columns

    @model_validator(mode="after")
    def _check_rows(self) -> DemoTable:
        expected = set(self.columns)
        for index, row in enumerate(self.rows):
            if set(row) != expected:
                raise ValueError(
                    f"row {index} of {self.name!r} has keys {sorted(row)}, expected {self.columns}"
                )
        self.dtypes()  # raises on mixed column types
        return self

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[CellValue]:
        """All values of one column, in row order."""
        if name not in self.columns:
            raise KeyError(f"{self.name!r} has no column {name!r}; columns: {self.columns}")
        return [row[name] for row in self.rows]

    def dtypes(self) -> dict[str, ColumnType]:
        """Column types inferred from the values; a column may not mix int, float and str."""
        result: dict[str, ColumnType] = {}
        for column in self.columns:
            kinds = {_cell_type(row[column]) for row in self.rows}
            if len(kinds) > 1:
                raise ValueError(f"column {column!r} of {self.name!r} mixes types {sorted(kinds)}")
            result[column] = kinds.pop() if kinds else "str"
        return result


class DemoCase(BaseModel):
    """A ready-made decision case: data tables, a short story and a suggested question."""

    name: str
    title: str
    company: str
    description: str
    question: str
    options: list[str]
    key_facts: dict[str, float]
    tables: list[DemoTable]
    seed: int
    period_start: str
    period_end: str

    _check_case_name = field_validator("name")(_check_name)

    @field_validator("tables")
    @classmethod
    def _unique_tables(cls, tables: list[DemoTable]) -> list[DemoTable]:
        names = [table.name for table in tables]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate table names: {names}")
        return tables

    @property
    def table_names(self) -> list[str]:
        return [table.name for table in self.tables]

    def table(self, name: str) -> DemoTable:
        """Look up a table by name."""
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(f"case {self.name!r} has no table {name!r}; tables: {self.table_names}")

    def has_table(self, name: str) -> bool:
        return name in self.table_names
