import inspect
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError, create_model


class ToolError(Exception):
    """An expected tool failure with a message safe to return to the caller."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class Tool:
    definition: dict
    model: type[BaseModel]
    handler: object

    @property
    def read_only(self):
        return self.definition['annotations']['readOnlyHint']

    def call(self, user, arguments):
        try:
            values = self.model.model_validate(arguments)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_context=False, include_input=False)
            # Nested field names can contain user-supplied values. Report the declared
            # parameter only so malformed arguments never appear in error responses.
            details = [{'field': error['loc'][0] if error['loc'] and error['loc'][0] in self.model.model_fields
                        else None, 'reason': error['type']} for error in errors]
            raise ToolError('Invalid tool arguments.', details=details) from exc
        return self.handler(user, **dict(values))


class Registry:
    def __init__(self):
        self._tools = {}

    def tool(self, *, name=None, read_only=True, destructive=False, idempotent=False, open_world=False):
        def register(handler):
            tool_name = name or handler.__name__
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', tool_name):
                raise ValueError(f'Invalid tool name: {tool_name}')
            if tool_name in self._tools:
                raise ValueError(f'Duplicate tool name: {tool_name}')

            fields = {}
            signature = inspect.signature(handler, eval_str=True)
            parameters = iter(signature.parameters.values())
            actor = next(parameters, None)
            if actor is None or actor.kind not in (actor.POSITIONAL_ONLY, actor.POSITIONAL_OR_KEYWORD):
                raise TypeError(f'{tool_name} must accept its actor as the first positional argument')
            for parameter in parameters:
                if (parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
                        or parameter.annotation is inspect.Parameter.empty):
                    raise TypeError(f'{tool_name}.{parameter.name} must be a typed keyword argument')
                default = ... if parameter.default is inspect.Parameter.empty else parameter.default
                fields[parameter.name] = (parameter.annotation, default)
            model = create_model(tool_name, __config__=ConfigDict(extra='forbid', strict=True, allow_inf_nan=False),
                                 **fields)
            definition = {
                'name': tool_name,
                'description': inspect.getdoc(handler) or tool_name,
                'inputSchema': model.model_json_schema(),
                'annotations': {
                    'readOnlyHint': read_only,
                    'destructiveHint': destructive,
                    'idempotentHint': idempotent,
                    'openWorldHint': open_world,
                },
            }
            self._tools[tool_name] = Tool(definition, model, handler)
            return handler
        return register

    def list(self, *, can_write):
        return [tool.definition for _, tool in sorted(self._tools.items()) if tool.read_only or can_write]

    def get(self, name, *, can_write):
        tool = self._tools.get(name)
        return tool if tool and (tool.read_only or can_write) else None
