"""Validated profiles persisted as one atomic JSON document."""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FIELDS = ("shooter", "discipline", "count", "latest", "total", "series")


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    background: str = "#f1f5f3"
    tile: str = "#ffffff"
    text: str = "#172e29"
    target: str = "#179c80"
    accent: str = "#077b61"

    @field_validator("*")
    @classmethod
    def color(cls, value):
        import re
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Colors must be #RRGGBB")
        return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    name: str = Field(min_length=1, max_length=64)
    rows: list[list[int]] = Field(min_length=1, max_length=8)
    theme: Theme = Field(default_factory=Theme)
    fields: list[Literal["shooter", "discipline", "count", "latest", "total", "series"]] = Field(default_factory=lambda: list(FIELDS))
    hits: Literal["series", "all"] = "series"
    practice: bool = True
    zoom: Literal["auto", "full"] = "auto"
    discipline_inline: bool = False

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Name must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def ranges(self):
        flat = [lane for row in self.rows for lane in row]
        if any(not 1 <= len(row) <= 12 for row in self.rows):
            raise ValueError("Each row needs 1 to 12 ranges")
        if len(set(flat)) != len(flat) or any(not 1 <= n <= 32767 for n in flat):
            raise ValueError("Range numbers must be unique and between 1 and 32767")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("Display fields must be unique")
        return self


def seeds():
    air, small = list(range(1, 5)), list(range(51, 54))
    return [Profile(id="alles", name="Alles", rows=[air, small]),
            Profile(id="kleinkaliber", name="Kleinkaliber", rows=[small]),
            Profile(id="luftgewehr", name="Luftgewehr", rows=[air])]


class ProfileStore:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.profiles = [Profile.model_validate(p) for p in json.loads(self.path.read_text())]
            if not self.profiles or len({p.id for p in self.profiles}) != len(self.profiles):
                raise ValueError("Profile file must contain unique profiles")
        else:
            self.profiles = seeds()
            self.save(self.profiles)

    def get(self, profile_id):
        return next((p for p in self.profiles if p.id == profile_id), None)

    def save(self, profiles):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".profiles-", suffix=".json")
        try:
            with os.fdopen(descriptor, "w") as file:
                json.dump([p.model_dump() for p in profiles], file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.profiles = profiles
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
