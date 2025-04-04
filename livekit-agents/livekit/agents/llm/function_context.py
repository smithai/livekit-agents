# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import enum
import functools
import inspect
import json
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from ..log import logger


class _UseDocMarker:
    pass


METADATA_ATTR = "__livekit_ai_metadata__"
USE_DOCSTRING = _UseDocMarker()


@dataclass(frozen=True, init=False)
class TypeInfo:
    description: str
    choices: tuple

    def __init__(self, description: str, choices: tuple | list[Any] = tuple()) -> None:
        object.__setattr__(self, "description", description)

        if isinstance(choices, list):
            choices = tuple(choices)

        object.__setattr__(self, "choices", choices)


@dataclass(frozen=True)
class FunctionArgInfo:
    name: str
    description: str
    type: type
    default: Any
    choices: tuple | None


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    description: str
    auto_retry: bool
    callable: Callable
    arguments: dict[str, FunctionArgInfo]
    wait_for_response: bool = True


@dataclass(frozen=True)
class FunctionCallInfo:
    tool_call_id: str
    function_info: FunctionInfo
    raw_arguments: str
    arguments: dict[str, Any]

    def execute(self) -> CalledFunction:
        function_info = self.function_info
        func = functools.partial(function_info.callable, **self.arguments)
        if asyncio.iscoroutinefunction(function_info.callable):
            task = asyncio.create_task(func())
        else:
            task = asyncio.create_task(asyncio.to_thread(func))

        called_fnc = CalledFunction(call_info=self, task=task)

        def _on_done(fut):
            try:
                called_fnc.result = fut.result()
            except BaseException as e:
                called_fnc.exception = e

        task.add_done_callback(_on_done)
        return called_fnc


@dataclass
class CalledFunction:
    call_info: FunctionCallInfo
    task: asyncio.Task[Any]
    result: Any | None = None
    exception: BaseException | None = None


def ai_callable(
    *,
    name: str | None = None,
    description: str | _UseDocMarker = USE_DOCSTRING,
    auto_retry: bool = False,
    wait_for_response: bool = True,
) -> Callable:
    def deco(f):
        _set_metadata(
            f,
            name=name,
            desc=description,
            auto_retry=auto_retry,
            wait_for_response=wait_for_response,
        )
        return f

    return deco


class FunctionContext:
    def __init__(self) -> None:
        self._fncs = dict[str, FunctionInfo]()

        for _, member in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(member, METADATA_ATTR):
                self._register_ai_function(member)

    def ai_callable(
        self,
        *,
        name: str | None = None,
        description: str | _UseDocMarker = USE_DOCSTRING,
        auto_retry: bool = True,
    ) -> Callable:
        def deco(f):
            _set_metadata(f, name=name, desc=description, auto_retry=auto_retry)
            self._register_ai_function(f)

        return deco

    def _register_ai_function(self, fnc: Callable) -> None:
        if not hasattr(fnc, METADATA_ATTR):
            logger.warning(f"function {fnc.__name__} does not have ai metadata")
            return

        metadata: _AIFncMetadata = getattr(fnc, METADATA_ATTR)
        fnc_name = metadata.name
        if fnc_name in self._fncs:
            raise ValueError(f"duplicate ai_callable name: {fnc_name}")

        sig = inspect.signature(fnc)

        # get_type_hints with include_extra=True is needed when using Annotated
        # using typing.get_args with param.Annotated is returning an empty tuple for some reason
        type_hints = typing.get_type_hints(
            fnc, include_extras=True
        )  # Annotated[T, ...] -> T
        args = dict[str, FunctionArgInfo]()

        for name, param in sig.parameters.items():
            if param.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise ValueError(f"{fnc_name}: unsupported parameter kind {param.kind}")

            inner_th, type_info = _extract_types(type_hints[name])

            if not is_type_supported(inner_th):
                raise ValueError(
                    f"{fnc_name}: unsupported type {inner_th} for parameter {name}"
                )

            desc = type_info.description if type_info else ""
            choices = type_info.choices if type_info else ()

            if (
                isinstance(inner_th, type)
                and issubclass(inner_th, enum.Enum)
                and not choices
            ):
                # the enum must be a str or int (and at least one value)
                # this is verified by is_type_supported
                choices = tuple([item.value for item in inner_th])
                inner_th = type(choices[0])

            args[name] = FunctionArgInfo(
                name=name,
                description=desc,
                type=inner_th,
                default=param.default,
                choices=choices,
            )

        self._fncs[metadata.name] = FunctionInfo(
            name=metadata.name,
            description=metadata.description,
            auto_retry=metadata.auto_retry,
            callable=fnc,
            arguments=args,
            wait_for_response=metadata.wait_for_response,
        )

    @property
    def ai_functions(self) -> dict[str, FunctionInfo]:
        return self._fncs


@dataclass(frozen=True)
class _AIFncMetadata:
    name: str
    description: str
    auto_retry: bool
    wait_for_response: bool


def _extract_types(annotation: type) -> tuple[type, TypeInfo | None]:
    """Return inner_type, TypeInfo"""
    if typing.get_origin(annotation) is not typing.Annotated:
        # email: Annotated[
        #    Optional[str], TypeInfo(description="The user address email")
        # ] = None,
        #
        # An argument like the above will return us:
        # `typing.Optional[typing.Annotated[typing.Optional[str], TypeInfo(description='The user address email', choices=())]]`
        # So we ignore the first typing.Optional

        is_optional, optional_inner = _is_optional_type(annotation)
        if is_optional:
            inner_type, info = _extract_types(optional_inner)
            return Optional[inner_type], info  # type: ignore

        return annotation, None

    # assume the first argument is always the inner type the LLM will use
    args = typing.get_args(annotation)
    if len(args) < 2:
        return args[0], None

    for a in args:
        if isinstance(a, TypeInfo):
            return args[0], a

    return args[0], None


def _set_metadata(
    f: Callable,
    name: str | None = None,
    desc: str | _UseDocMarker = USE_DOCSTRING,
    auto_retry: bool = False,
    wait_for_response: bool = True,
) -> None:
    if isinstance(desc, _UseDocMarker):
        docstring = inspect.getdoc(f)
        if docstring is None:
            raise ValueError(
                f"missing docstring for function {f.__name__}, "
                "use explicit description or provide docstring"
            )
        desc = docstring

    metadata = _AIFncMetadata(
        name=name or f.__name__,
        description=desc,
        auto_retry=auto_retry,
        wait_for_response=wait_for_response,
    )

    setattr(f, METADATA_ATTR, metadata)


def is_type_supported(t: type) -> bool:
    if t in (str, int, float, bool):
        return True

    if typing.get_origin(t) is list:
        in_type = typing.get_args(t)[0]
        return is_type_supported(in_type)

    is_optional, ty = _is_optional_type(t)
    if is_optional:
        return is_type_supported(ty)

    if issubclass(t, enum.Enum):
        initial_type = None
        for e in t:
            if initial_type is None:
                initial_type = type(e.value)
            if type(e.value) is not initial_type:
                return False

        return initial_type in (str, int)

    return False


def _is_optional_type(typ) -> Tuple[bool, Any]:
    """return is_optional, inner_type"""
    origin = typing.get_origin(typ)
    if origin is None or origin is list:
        return False, typ

    if origin in {typing.Union, getattr(types, "UnionType", typing.Union)}:
        args = typing.get_args(typ)
        is_optional = type(None) in args
        non_none_args = [a for a in args if a is not type(None)]
        if is_optional and len(non_none_args) == 1:
            # Exactly one non-None type + None means optional
            return True, non_none_args[0]

    return False, None


def _create_ai_function_info(
    fnc_ctx: FunctionContext,
    tool_call_id: str,
    fnc_name: str,
    raw_arguments: str,  # JSON string
) -> FunctionCallInfo:
    if fnc_name not in fnc_ctx.ai_functions:
        raise ValueError(f"AI function {fnc_name} not found")

    parsed_arguments: dict[str, Any] = {}
    try:
        if raw_arguments:  # ignore empty string
            parsed_arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        raise ValueError(
            f"AI function {fnc_name} received invalid JSON arguments - {raw_arguments}"
        )

    fnc_info = fnc_ctx.ai_functions[fnc_name]

    # Ensure all necessary arguments are present and of the correct type.
    sanitized_arguments: dict[str, Any] = {}
    for arg_info in fnc_info.arguments.values():
        if arg_info.name not in parsed_arguments:
            if arg_info.default is inspect.Parameter.empty:
                raise ValueError(
                    f"AI function {fnc_name} missing required argument {arg_info.name}"
                )
            continue

        arg_value = parsed_arguments[arg_info.name]
        is_optional, inner_th = _is_optional_type(arg_info.type)

        if typing.get_origin(inner_th) is not None:
            if not isinstance(arg_value, list):
                raise ValueError(
                    f"AI function {fnc_name} argument {arg_info.name} should be a list"
                )

            inner_type = typing.get_args(inner_th)[0]
            sanitized_value = [
                _sanitize_primitive(
                    value=v,
                    expected_type=inner_type,
                    choices=arg_info.choices,
                )
                for v in arg_value
            ]
        else:
            sanitized_value = _sanitize_primitive(
                value=arg_value,
                expected_type=inner_th,
                choices=arg_info.choices,
            )

        sanitized_arguments[arg_info.name] = sanitized_value

    return FunctionCallInfo(
        tool_call_id=tool_call_id,
        raw_arguments=raw_arguments,
        function_info=fnc_info,
        arguments=sanitized_arguments,
    )


def _sanitize_primitive(
    *, value: Any, expected_type: type, choices: tuple | None
) -> Any:
    if expected_type is str:
        if not isinstance(value, str):
            raise ValueError(f"expected str, got {type(value)}")
    elif expected_type in (int, float):
        if not isinstance(value, (int, float)):
            raise ValueError(f"expected number, got {type(value)}")

        if expected_type is int:
            if value % 1 != 0:
                raise ValueError("expected int, got float")

            value = int(value)
        elif expected_type is float:
            value = float(value)

    elif expected_type is bool:
        if not isinstance(value, bool):
            raise ValueError(f"expected bool, got {type(value)}")

    if choices and value not in choices:
        raise ValueError(f"invalid value {value}, not in {choices}")

    return value
