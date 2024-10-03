# Owner(s): ["module: dynamo"]
from unittest.mock import patch

import torch
import torch._dynamo.test_case
import torch._dynamo.testing
from torch._C import (
    _len_torch_function_stack,
    _pop_torch_function_stack,
    _push_on_torch_function_stack,
)
from torch.overrides import _get_current_function_mode_stack, BaseTorchFunctionMode
from torch.utils._device import DeviceContext
from torch.utils._python_dispatch import TorchDispatchMode


class TestMode(BaseTorchFunctionMode):
    def __torch_function__(self, func, types, args, kwargs=None):
        if not kwargs:
            kwargs = {}

        if func == torch.add:
            return torch.zeros(2, 2)

        return super().__torch_function__(func, types, args, kwargs)


class TorchDispatchModeTests(torch._dynamo.test_case.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def test_skip_torch_dispatch_modes(self):
        class RewriteAddToMul(TorchDispatchMode):
            def __torch_dispatch__(self, func, types, args=(), kwargs=None):
                if func is torch.ops.aten.add.Tensor:
                    func = torch.ops.aten.mul.Tensor
                return func(*args, **kwargs)

        def fn(x):
            return x + x

        cnt = torch._dynamo.testing.CompileCounter()

        x = torch.tensor([3.0])
        with RewriteAddToMul():
            eager_res = fn(x)
            compiled_res = torch._dynamo.optimize(cnt)(fn)(x)

        self.assertEqual(eager_res, compiled_res)
        self.assertEqual(cnt.frame_count, 0)


class TorchFunctionModeTests(torch._dynamo.test_case.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default_device_old = torch.get_default_device()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        torch.set_default_device(cls.default_device_old)
        super().tearDownClass()

    def setUp(self):
        torch.set_default_device(None)
        torch._dynamo.reset()

    def tearDown(self):
        torch.set_default_device(None)
        torch._dynamo.reset()

    def _run_torch_function_mode_guard_test(self):
        class TestMode1(BaseTorchFunctionMode):
            pass

        class TestMode2(BaseTorchFunctionMode):
            pass

        cnt = torch._dynamo.testing.CompileCounter()

        @torch.compile(backend=cnt.__call__)
        def fn(x):
            return x + 1

        inp = torch.ones(2, 2)
        fn(inp)
        self.assertEqual(cnt.frame_count, 1)

        with TestMode1():
            fn(inp)
        self.assertEqual(cnt.frame_count, 2)

        with TestMode1(), TestMode2():
            fn(inp)
        self.assertEqual(cnt.frame_count, 3)

        with TestMode2(), TestMode1():
            fn(inp)
        self.assertEqual(cnt.frame_count, 4)

        with TestMode1():
            fn(inp)
        self.assertEqual(cnt.frame_count, 4)

    def _run_ignored_mode_types_test(self):
        class IgnoredMode(BaseTorchFunctionMode):
            pass

        cnt = torch._dynamo.testing.CompileCounter()

        @torch.compile(backend=cnt.__call__, fullgraph=True)
        def fn(x):
            return x + 1

        inp = torch.ones(2, 2)

        with patch(
            "torch._dynamo.variables.torch_function.IGNORED_MODES", {IgnoredMode}
        ):
            # initial compile
            fn(inp)

            # no recompile, mode ignored
            # note: the ref stack is length 0, and the stack we are checking against has length 2
            # we want to check both ref stack len > runtime stack, and ref stack len < runtime stack
            with IgnoredMode(), IgnoredMode():
                fn(inp)

            self.assertEqual(cnt.frame_count, 1)

            # recompile due to new mode on the stack
            with BaseTorchFunctionMode(), BaseTorchFunctionMode(), BaseTorchFunctionMode():
                fn(inp)

            self.assertEqual(cnt.frame_count, 2)

            # recompile
            # tests both ref stack len > runtime stack len for the above guard check
            # and ref stack len < runtime stack len for the initial zero mode case
            with BaseTorchFunctionMode(), IgnoredMode(), BaseTorchFunctionMode():
                fn(inp)

            self.assertEqual(cnt.frame_count, 3)

            # no recompile
            with IgnoredMode(), IgnoredMode(), BaseTorchFunctionMode(), BaseTorchFunctionMode():
                fn(inp)

            self.assertEqual(cnt.frame_count, 3)

        # This is tricky, basically the ignored modes are baked into the guard
        # IgnoredMode will be ignored forever by that guard.
        # This is okay since we don't expect to be modifying IGNORED_MODES
        # in the middle of execution except for the purposes of testing.
        torch._dynamo.reset()

        with IgnoredMode():
            fn(inp)

        self.assertEqual(cnt.frame_count, 4)

    @torch._dynamo.config.patch("enable_cpp_guard_manager", False)
    def test_torch_function_mode_guards_ignored_types_py(self):
        self._run_ignored_mode_types_test()

    def test_torch_function_mode_guards_ignored_types_cpp(self):
        self._run_ignored_mode_types_test()

    @torch._dynamo.config.patch("enable_cpp_guard_manager", False)
    def test_torch_function_mode_guards_py(self):
        self._run_torch_function_mode_guard_test()

    def test_torch_function_mode_guards_cpp(self):
        self._run_torch_function_mode_guard_test()

    def test_stack_state_mutation_default_device(self):
        m = BaseTorchFunctionMode()
        m1 = BaseTorchFunctionMode()
        with m, m1:

            @torch.compile(fullgraph=True)
            def fn(x):
                torch.set_default_device("cpu")
                _pop_torch_function_stack()

            fn(torch.ones(2, 2))
            _push_on_torch_function_stack(m1)

            stack = _get_current_function_mode_stack()
            self.assertIsInstance(stack[0], DeviceContext)
            self.assertEqual(stack[0].device, torch.device("cpu"))
            self.assertIs(stack[1], m)
            self.assertIs(stack[2], m1)

    def test_stack_state_clear_default_device(self):
        @torch.compile(fullgraph=True)
        def fn(x):
            torch.set_default_device(None)
            return x + 1

        fn(torch.ones(2, 2))
        stack = _get_current_function_mode_stack()
        self.assertEqual(len(stack), 0)

        m = BaseTorchFunctionMode()
        m1 = BaseTorchFunctionMode()

        # Stack populated, add device
        with m, m1:

            @torch.compile(fullgraph=True)
            def fn(x):
                torch.set_default_device("cpu")
                torch.set_default_device(None)
                torch.set_default_device("cpu")
                return x + 1

            fn(torch.ones(2, 2))
            stack = _get_current_function_mode_stack()
            self.assertEqual(stack[0].device, torch.device("cpu"))
            self.assertIs(stack[1], m)
            self.assertIs(stack[2], m1)

        # Stack populated, remove device
        torch.set_default_device("cpu")
        with m, m1:

            @torch.compile(fullgraph=True)
            def fn(x):
                torch.set_default_device(None)
                return x + 1

            fn(torch.ones(2, 2))
            stack = _get_current_function_mode_stack()
            self.assertIs(stack[0], m)
            self.assertIs(stack[1], m1)

        @torch.compile(fullgraph=True)
        def fn(x):
            torch.set_default_device("cpu")
            torch.set_default_device("cpu")
            return x + 1

        fn(torch.ones(2, 2))
        stack = _get_current_function_mode_stack()
        self.assertEqual(stack[0].device, torch.device("cpu"))
        torch.set_default_device(None)

    def test_pop_torch_function_mode(self):
        m = BaseTorchFunctionMode()
        with m:

            @torch.compile(fullgraph=True)
            def fn(x):
                _pop_torch_function_stack()
                return x + 1

            fn(torch.ones(2, 2))

            self.assertEqual(_len_torch_function_stack(), 0)
            # reset stack so __exit__ doesn't crash
            _push_on_torch_function_stack(m)

        self.assertEqual(_len_torch_function_stack(), 0)

    def test_error_empty_stack_pop_torch_function_mode(self):
        @torch.compile(fullgraph=True)
        def fn(x):
            _pop_torch_function_stack()
            return x + 1

        self.assertRaisesRegex(
            torch._dynamo.exc.Unsupported,
            "Popping from an empty torch function mode stack",
            lambda: fn(torch.ones(2, 2)),
        )

    def test_push_torch_function_mode(self):
        m = BaseTorchFunctionMode()
        with m:

            @torch.compile(fullgraph=True)
            def fn(x, m):
                _push_on_torch_function_stack(m)
                return x + 1

            fn(torch.ones(2, 2), m)

            self.assertEqual(_len_torch_function_stack(), 2)
            # reset stack state
            _pop_torch_function_stack()

        self.assertEqual(_len_torch_function_stack(), 0)

    def test_len_torch_function_mode(self):
        m = BaseTorchFunctionMode()
        with m:

            @torch.compile(fullgraph=True)
            def fn(x):
                z = _len_torch_function_stack()
                return x + z

            res = fn(torch.ones(2, 2))
            self.assertEqual(res, torch.ones(2, 2) + 1)
            self.assertEqual(_len_torch_function_stack(), 1)

    def test_intermedate_torch_function_mode_construction_mutation(self):
        class TestMode(BaseTorchFunctionMode):
            def __init__(self, x):
                self.x = x

        @torch.compile(fullgraph=True)
        def fn(x):
            z = TestMode(2)
            z.y = 2
            return x + 1, z

        fn(torch.ones(2, 2))

    def test_torch_function_mode_enabled_guard(self):
        cnt = torch._dynamo.testing.CompileCounter()
        inp = torch.ones(2, 2)

        @torch.compile(backend=cnt.__call__)
        def fn(x):
            return x + 1

        with BaseTorchFunctionMode(), torch._C.DisableTorchFunctionSubclass():
            with torch._C.DisableTorchFunction():
                fn(inp)
            fn(inp)
        self.assertEqual(cnt.frame_count, 2)

    def test_nested_torch_function_mode(self):
        mode_1_called = False
        mode_2_called = False

        def reset_state():
            nonlocal mode_1_called
            nonlocal mode_2_called
            mode_1_called = False
            mode_2_called = False

        ones = torch.ones(2, 2)
        zeros = torch.zeros(2, 2)

        class TestMode1(BaseTorchFunctionMode):
            def __torch_function__(self, func, types, args, kwargs=None):
                if not kwargs:
                    kwargs = {}

                nonlocal mode_1_called

                mode_1_called = True

                if func == torch.add:
                    return zeros

                return super().__torch_function__(func, types, args, kwargs)

        class TestMode2(BaseTorchFunctionMode):
            def __torch_function__(self, func, types, args, kwargs=None):
                if not kwargs:
                    kwargs = {}

                nonlocal mode_2_called

                mode_2_called = True

                if func == torch.mul:
                    return ones

                return super().__torch_function__(func, types, args, kwargs)

        def fn(x):
            return torch.add(x, 3)

        def fn_2(x):
            return torch.mul(x, 3) + torch.add(x, 3)

        inp = torch.ones(2, 2) + 1

        for fn_i in [fn, fn_2]:
            fn_opt = torch.compile(fn_i, fullgraph=True)
            with TestMode1(), TestMode2():
                expected = fn_i(inp), mode_1_called, mode_2_called
                reset_state()
                actual = fn_opt(inp), mode_1_called, mode_2_called
                reset_state()

            self.assertEqual(expected, actual)

    def test_torch_function_mode_disable(self):
        class TestSubclass(torch.Tensor):
            @classmethod
            def __torch_function__(cls, func, types, args, kwargs=None):
                if not kwargs:
                    kwargs = {}
                if func == torch.add:
                    return torch.ones(2, 2)
                return super().__torch_function__(func, types, args, kwargs)

        class TestMode(BaseTorchFunctionMode):
            def __torch_function__(self, func, types, args, kwargs=None):
                if not kwargs:
                    kwargs = {}

                if func == torch.add:
                    return torch.zeros(2, 2)

                return super().__torch_function__(func, types, args, kwargs)

        def fn(x):
            return torch.add(x, 3)

        inp = (torch.ones(2, 2) + 1).as_subclass(TestSubclass)

        fn_opt = torch.compile(fn, fullgraph=True)
        with TestMode(), torch._dynamo.config.patch(
            "traceable_tensor_subclasses", {TestSubclass}
        ):
            with torch._C.DisableTorchFunctionSubclass():
                expected = fn(inp)
                actual = fn_opt(inp)

            self.assertEqual(expected, actual)

            with torch._C.DisableTorchFunction():
                expected = fn(inp)
                actual = fn_opt(inp)

            self.assertEqual(expected, actual)

    def test_torch_function_mode_highest_priority(self):
        class TestSubclass(torch.Tensor):
            @classmethod
            def __torch_function__(cls, func, types, args, kwargs=None):
                if not kwargs:
                    kwargs = {}
                if func == torch.add:
                    return torch.ones(2, 2)
                return super().__torch_function__(func, types, args, kwargs)

        def fn(x):
            return torch.add(x, 3)

        inp = (torch.ones(2, 2) + 1).as_subclass(TestSubclass)

        fn_opt = torch.compile(fn, fullgraph=True)
        with TestMode(), torch._dynamo.config.patch(
            "traceable_tensor_subclasses", {TestSubclass}
        ):
            expected = fn(inp)
            actual = fn_opt(inp)

        self.assertEqual(expected, actual)


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()
