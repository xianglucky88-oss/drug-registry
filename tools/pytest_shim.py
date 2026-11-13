"""最小 pytest 兼容垫片：仅供无 pytest 的离线环境运行本仓库测试。

支持本仓库实际用到的子集：模块收集、tmp_path/fixture、pytest.raises。
不是 pytest 的替代品；有 pytest 的环境仍应直接使用 pytest。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import traceback
import types


class Raises:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"预期抛出 {self.expected.__name__}，但没有异常")
        if not issubclass(exc_type, self.expected):
            return False
        if self.match is not None and self.match not in str(exc):
            raise AssertionError(f"异常信息 {str(exc)!r} 不包含 {self.match!r}")
        self.value = exc
        return True


def fixture(func=None):
    def decorate(f):
        f.__pytest_fixture__ = True
        return f

    if func is None:
        return decorate
    return decorate(func)


pytest = types.ModuleType("pytest")
pytest.raises = Raises
pytest.fixture = fixture
sys.modules["pytest"] = pytest


def _load_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(tests_dir: pathlib.Path) -> int:
    failures = []
    passed = 0
    for path in sorted(tests_dir.glob("test_*.py")):
        module = _load_module(path)
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            func = getattr(module, name)
            if not callable(func):
                continue
            kwargs = {}
            generators = []
            fixture_names = list(getattr(func, "__code__", type("c", (), {"co_varnames": ()})).co_varnames[: func.__code__.co_argcount])
            for fixture_name in fixture_names:
                if fixture_name == "tmp_path":
                    kwargs[fixture_name] = pathlib.Path(
                        tempfile.mkdtemp(prefix="pyt-shim-")
                    )
                    continue
                fixture_func = getattr(module, fixture_name, None)
                if fixture_func is None or not getattr(fixture_func, "__pytest_fixture__", False):
                    raise AssertionError(f"{name} 需要未知 fixture: {fixture_name}")
                if "tmp_path" in fixture_func.__code__.co_varnames:
                    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pyt-shim-"))
                    value = fixture_func(_Fixture(tmp))
                else:
                    value = fixture_func()
                if isinstance(value, types.GeneratorType):
                    kwargs[fixture_name] = next(value)
                    generators.append(value)
                else:
                    kwargs[fixture_name] = value
            try:
                func(**kwargs)
                passed += 1
                print(f"PASS {path.name}::{name}")
            except Exception:  # noqa: BLE001
                failures.append((path.name, name, traceback.format_exc()))
                print(f"FAIL {path.name}::{name}")
            finally:
                for gen in generators:
                    try:
                        next(gen)
                    except StopIteration:
                        pass

    print(f"\n{passed} passed, {len(failures)} failed")
    for file_name, name, tb in failures:
        print(f"\n===== FAIL {file_name}::{name} =====\n{tb}")
    return 1 if failures else 0


class _Fixture:
    def __init__(self, path: pathlib.Path):
        self._path = path

    def __getattr__(self, item):
        return getattr(self._path, item)

    def __truediv__(self, other):
        return self._path / other


if __name__ == "__main__":
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("tests")
    sys.exit(main(root))
