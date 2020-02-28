from functools import wraps
from inspect import Parameter, signature
from itertools import groupby
from operator import itemgetter
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TypeVar

from . import validator
from .main import BaseConfig, BaseModel, Extra, create_model
from .utils import to_camel

__all__ = ('validate_arguments',)

T = TypeVar('T')


class Config(BaseConfig):
    extra = Extra.forbid


def coalesce(param: Parameter, default: Any) -> Any:
    return param if param != Parameter.empty else default


def make_field(arg: Parameter) -> Dict[str, Any]:
    return {'name': arg.name, 'kind': arg.kind, 'field': (coalesce(arg.annotation, Any), coalesce(arg.default, ...))}


def build_message_helper(keys: Sequence[str]) -> Tuple[str, str]:
    plural = '' if len(keys) == 1 else 's'
    joined = ', '.join(map(repr, keys))
    return plural, joined


def validate_arguments(func: Callable[..., T]) -> Callable[..., T]:
    """
    Decorator to validate the arguments passed to a function.
    """
    sig = signature(func)
    fields = [make_field(p) for p in sig.parameters.values()]

    # Python syntax should already enforce fields to be ordered by kind
    grouped = groupby(fields, key=itemgetter('kind'))
    params = {kind: {field['name']: field['field'] for field in val} for kind, val in grouped}

    # Arguments descriptions by kind
    positional_only = params.get(Parameter.POSITIONAL_ONLY, {})
    positional_or_keyword = params.get(Parameter.POSITIONAL_OR_KEYWORD, {})
    var_positional = params.get(Parameter.VAR_POSITIONAL, {})
    keyword_only = params.get(Parameter.KEYWORD_ONLY, {})
    var_keyword = params.get(Parameter.VAR_KEYWORD, {})

    var_positional = {name: (Tuple[annotation, ...], ()) for name, (annotation, _) in var_positional.items()}
    var_keyword = {
        name: (Dict[str, annotation], {})  # type: ignore
        for name, (annotation, _) in var_keyword.items()
    }

    assert len(var_positional) <= 1
    assert len(var_keyword) <= 1

    vp_name = next(iter(var_positional.keys()), None)
    vk_name = next(iter(var_keyword.keys()), None)

    model = create_model(
        to_camel(func.__name__),
        __config__=Config,
        **positional_only,
        **positional_or_keyword,
        **var_positional,
        **keyword_only,
        **var_keyword,
    )

    sig_pos = tuple(positional_only) + tuple(positional_or_keyword)
    sig_kw = set(positional_or_keyword) | set(keyword_only)

    class SignatureCheck(BaseModel):
        args: Dict[str, Any]
        kwargs: Dict[str, Any]
        positional_only: Optional[Dict[str, Any]] = None
        arguments: Optional[Dict[str, Any]] = None

        @validator('args', pre=True, allow_reuse=True)
        def validate_positional(cls, args: Tuple[Any, ...]) -> Dict[str, Any]:
            try:
                return sig.bind_partial(*args).arguments
            except TypeError:
                raise TypeError(f'{len(sig_pos)} positional arguments expected but {len(args)} given')

        @validator('kwargs', pre=True, allow_reuse=True)
        def validate_keyword(cls, kwargs: Dict[str, Any]) -> Dict[str, Any]:
            try:
                return sig.bind_partial(**kwargs).arguments
            except TypeError:
                unexpected = set(kwargs) - sig_kw - set(positional_only)
                if unexpected:
                    # TODO: use definition order
                    plural, keys = build_message_helper(sorted(unexpected))
                    raise TypeError(f'unexpected keyword argument{plural}: {keys}')
                return kwargs

        @validator('positional_only', pre=True, always=True, allow_reuse=True)
        def validate_positional_only(cls, v: Any, values: Dict[str, Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
            kwargs = values.get('kwargs', {})
            try:
                return sig.bind_partial(**kwargs).arguments
            except TypeError:
                pos_only = set(kwargs) & set(positional_only)
                if pos_only:
                    # TODO: use definition order
                    plural, keys = build_message_helper(sorted(pos_only))
                    raise TypeError(f'positional-only argument{plural} passed as keyword argument{plural}: {keys}')
                return kwargs

        @validator('arguments', pre=True, always=True, allow_reuse=True)
        def validate_multi(cls, v: Any, values: Dict[str, Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
            args = values.get('args', {})
            kwargs = values.get('kwargs', {})
            try:
                return sig.bind(*args.values(), **kwargs).arguments
            except TypeError:
                multi = set(args) & set(kwargs)
                if multi:
                    # TODO: use definition order
                    plural, keys = build_message_helper(sorted(multi))
                    raise TypeError(f'multiple values for argument{plural}: {keys}')
                return {**args, **kwargs}

    @wraps(func)
    def apply(*args: Any, **kwargs: Any) -> T:

        sigcheck = SignatureCheck(args=args, kwargs=kwargs)
        # use dict(model) instead of model.dict() so values stay cast as intended
        instance = dict(model(**sigcheck.args, **sigcheck.kwargs))

        upd_arg = {k: instance.get(k, v) for k, v in sigcheck.args.items() if k != vp_name}
        upd_kw = {k: instance.get(k, v) for k, v in sigcheck.kwargs.items() if k != vk_name}

        return func(
            *upd_arg.values(),
            *sigcheck.args.get(vp_name, ()),  # type: ignore
            **upd_kw,
            **sigcheck.kwargs.get(vk_name, {}),  # type: ignore
        )

    return apply
