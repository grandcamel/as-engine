"""Fetch the current version once; never refresh or retry a conflicting write."""

from __future__ import annotations

from typing import Any

from . import Context, Transform
from .values import MISSING, pointer, required, set_target, target_value


class Version(Transform):
    def request(self, context: Context, tag: Any) -> None:
        if target_value(context, tag["target"]) is not MISSING:
            return
        for override in tag.get("overrides", []):
            when = override["when"]
            if pointer(context.body, when["path"]) == when["equals"]:
                set_target(context, tag["target"], override["value"])
                return
        read = context.index.operations.get(tag["operationId"])
        if read is None or read.method.upper() != "GET":
            raise ValueError("version read must name a same-document GET operation")
        parameters = {}
        for name, source in tag["parameters"].items():
            value = target_value(context, source)
            if value is MISSING:
                raise ValueError(f"missing version read input: {name}")
            parameters[name] = value
        response = context.invoke(read.operationId, parameters)
        current = required(response.body, tag["responsePath"])
        increment = tag["increment"]
        if type(current) is not int or type(increment) is not int:
            raise ValueError("current version and increment must be integers")
        set_target(context, tag["target"], current + increment)
