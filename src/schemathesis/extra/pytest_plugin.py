from functools import partial
from typing import Any, Callable, Generator, List, Optional, Type, TypeVar, cast

import pytest
from _pytest import fixtures, nodes
from _pytest.config import hookimpl
from _pytest.fixtures import FuncFixtureInfo
from _pytest.nodes import Node
from _pytest.python import Class, Function, FunctionDefinition, Metafunc, Module, PyCollector
from hypothesis.errors import InvalidArgument
from hypothesis_jsonschema._canonicalise import HypothesisRefResolutionError
from packaging import version

from .. import DataGenerationMethod
from .._hypothesis import create_test
from ..constants import RECURSIVE_REFERENCE_ERROR_MESSAGE
from ..models import Endpoint
from ..utils import is_schemathesis_test

USE_FROM_PARENT = version.parse(pytest.__version__) >= version.parse("5.4.0")

T = TypeVar("T", bound=Node)


def create(cls: Type[T], *args: Any, **kwargs: Any) -> T:
    if USE_FROM_PARENT:
        return cls.from_parent(*args, **kwargs)  # type: ignore
    return cls(*args, **kwargs)


class SchemathesisFunction(Function):  # pylint: disable=too-many-ancestors
    def __init__(
        self,
        *args: Any,
        test_func: Callable,
        test_name: Optional[str] = None,
        data_generation_method: DataGenerationMethod,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.test_function = test_func
        self.test_name = test_name
        self.data_generation_method = data_generation_method

    def _getobj(self) -> partial:
        """Tests defined as methods require `self` as the first argument.

        This method is called only for this case.
        """
        return partial(self.obj, self.parent.obj)  # type: ignore


class SchemathesisCase(PyCollector):
    def __init__(self, test_function: Callable, *args: Any, **kwargs: Any) -> None:
        self.test_function = test_function
        self.schemathesis_case = test_function._schemathesis_test  # type: ignore
        self.given_args = getattr(test_function, "_schemathesis_given_args", ())
        self.given_kwargs = getattr(test_function, "_schemathesis_given_kwargs", {})
        super().__init__(*args, **kwargs)

    def _get_test_name(self, endpoint: Endpoint, data_generation_method: DataGenerationMethod) -> str:
        return f"{self.name}[{endpoint.method.upper()}:{endpoint.full_path}][{data_generation_method.as_short_name()}]"

    def _gen_items(
        self, endpoint: Endpoint, data_generation_method: DataGenerationMethod
    ) -> Generator[SchemathesisFunction, None, None]:
        """Generate all items for the given endpoint.

        Could produce more than one test item if
        parametrization is applied via ``pytest.mark.parametrize`` or ``pytest_generate_tests``.

        This implementation is based on the original one in pytest, but with slight adjustments
        to produce tests out of hypothesis ones.
        """
        name = self._get_test_name(endpoint, data_generation_method)
        funcobj = create_test(
            endpoint=endpoint,
            test=self.test_function,
            _given_args=self.given_args,
            _given_kwargs=self.given_kwargs,
            data_generation_method=data_generation_method,
        )

        cls = self._get_class_parent()
        definition: FunctionDefinition = create(FunctionDefinition, name=self.name, parent=self.parent, callobj=funcobj)
        fixturemanager = self.session._fixturemanager
        fixtureinfo = fixturemanager.getfixtureinfo(definition, funcobj, cls)

        metafunc = self._parametrize(cls, definition, fixtureinfo)

        if not metafunc._calls:
            yield create(
                SchemathesisFunction,
                name=name,
                parent=self.parent,
                callobj=funcobj,
                fixtureinfo=fixtureinfo,
                test_func=self.test_function,
                originalname=self.name,
                data_generation_method=data_generation_method,
            )
        else:
            fixtures.add_funcarg_pseudo_fixture_def(self.parent, metafunc, fixturemanager)
            fixtureinfo.prune_dependency_tree()
            for callspec in metafunc._calls:
                subname = f"{name}[{callspec.id}]"
                yield create(
                    SchemathesisFunction,
                    name=subname,
                    parent=self.parent,
                    callspec=callspec,
                    callobj=funcobj,
                    fixtureinfo=fixtureinfo,
                    keywords={callspec.id: True},
                    originalname=name,
                    test_func=self.test_function,
                    data_generation_method=data_generation_method,
                )

    def _get_class_parent(self) -> Optional[Type]:
        clscol = self.getparent(Class)
        return clscol.obj if clscol else None

    def _parametrize(
        self, cls: Optional[Type], definition: FunctionDefinition, fixtureinfo: FuncFixtureInfo
    ) -> Metafunc:
        parent = self.getparent(Module)
        module = parent.obj if parent is not None else parent
        metafunc = Metafunc(definition, fixtureinfo, self.config, cls=cls, module=module)
        methods = []
        if hasattr(module, "pytest_generate_tests"):
            methods.append(module.pytest_generate_tests)
        if hasattr(cls, "pytest_generate_tests"):
            cls = cast(Type, cls)
            methods.append(cls().pytest_generate_tests)
        self.ihook.pytest_generate_tests.call_extra(methods, {"metafunc": metafunc})
        return metafunc

    def collect(self) -> List[Function]:  # type: ignore
        """Generate different test items for all endpoints available in the given schema."""
        try:
            return [
                item
                for data_generation_method in self.schemathesis_case.data_generation_methods
                for endpoint in self.schemathesis_case.get_all_endpoints()
                for item in self._gen_items(endpoint, data_generation_method)
            ]
        except Exception:
            pytest.fail("Error during collection")


@hookimpl(hookwrapper=True)  # type:ignore # pragma: no mutate
def pytest_pycollect_makeitem(collector: nodes.Collector, name: str, obj: Any) -> Generator[None, Any, None]:
    """Switch to a different collector if the test is parametrized marked by schemathesis."""
    outcome = yield
    if is_schemathesis_test(obj):
        outcome.force_result(create(SchemathesisCase, parent=collector, test_function=obj, name=name))
    else:
        outcome.get_result()


@hookimpl(hookwrapper=True)  # pragma: no mutate
def pytest_pyfunc_call(pyfuncitem):  # type:ignore
    """It is possible to have a Hypothesis exception in runtime.

    For example - kwargs validation is failed for some strategy.
    """
    outcome = yield
    try:
        outcome.get_result()
    except InvalidArgument as exc:
        pytest.fail(exc.args[0])
    except HypothesisRefResolutionError:
        pytest.skip(RECURSIVE_REFERENCE_ERROR_MESSAGE)
