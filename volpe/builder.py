from typing import Callable

from lark.visitors import Interpreter
from llvmlite import ir

from annotate import Unannotated
from builder_utils import write_environment, Closure, free_environment, environment_size, options, \
    read_environment, tuple_assign
from volpe_types import int1, make_bool, pint8, int32, make_flt, flt32
from tree import TypeTree


class LLVMScope(Interpreter):
    def __init__(self, builder: ir.IRBuilder, scope: dict, tree: TypeTree, ret: Callable, old_scope: set, closure: Closure):
        self.builder = builder
        self.scope = scope
        self.old_scope = old_scope
        self.ret = ret
        self.closure = closure

        if tree.data == "code":
            assert len(tree.children) > 0, "code block needs code"

            def evaluate(children):
                if len(children) == 1:
                    self.visit_unsafe(children[0])
                else:
                    value = self.visit(children[0])
                    with self.builder.if_then(value):
                        evaluate(children[1:])
                    builder.unreachable()

            evaluate(tree.children)
            assert builder.block.is_terminated, "you forgot a return statement at the end of a code block"
        else:
            ret(self.visit(tree))

    def visit(self, tree: TypeTree):
        value = getattr(self, tree.data)(tree)
        assert not self.builder.block.is_terminated, "dead code is not allowed"
        return value

    def visit_unsafe(self, tree: TypeTree):
        return getattr(self, tree.data)(tree)

    def assign(self, tree: TypeTree):
        tuple_assign(self.scope, self.builder, tree.children[0], self.visit(tree.children[1]))
        return ir.Constant(int1, True)

    def symbol(self, tree: TypeTree):
        return self.scope[tree.children[0].value]

    def func(self, tree: TypeTree):
        f = tree.ret
        assert isinstance(f, Unannotated)

        env_values = list(self.scope.values())
        env_types = list(v.type for v in env_values)
        env_names = list(self.scope.keys())

        module = self.builder.module
        env_size = environment_size(self.builder, env_values)
        env_ptr = self.builder.call(module.malloc, [env_size])
        write_environment(self.builder, env_ptr, env_values)

        func = ir.Function(module, f.func, str(next(module.func_count)))

        if f.checked:
            build_function(func, env_names, env_types, tree.children[:-1], tree.children[-1])
        else:
            print("ignoring function without usage")

        closure = ir.Constant(f, [func, ir.Undefined, ir.Undefined])
        closure = self.builder.insert_value(closure, env_size, 1)
        closure = self.builder.insert_value(closure, env_ptr, 2)
        return closure

    def func_call(self, tree: TypeTree):
        closure = self.visit(tree.children[0])
        args = [self.visit(child) for child in tree.children[1:]]

        assert isinstance(closure.type, Closure)

        func_ptr = self.builder.extract_value(closure, 0)
        env_size = self.builder.extract_value(closure, 1)
        env_ptr = self.builder.extract_value(closure, 2)

        return self.builder.call(func_ptr, [env_ptr, *args])

    def this_func(self, tree: TypeTree):
        return self.closure

    def returnn(self, tree: TypeTree):
        value = self.visit(tree.children[0])

        scope = set(self.scope.values()) - {value} - self.old_scope
        free_environment(self.builder, scope)

        self.ret(value)

    def code(self, tree: TypeTree):
        phi = []

        with options(self.builder, tree.ret, phi) as ret:
            LLVMScope(self.builder, self.scope.copy(), tree, ret, set(self.scope.values()), self.closure)

        return phi[0]

    def implication(self, tree: TypeTree):
        phi = []

        with options(self.builder, tree.ret, phi) as ret:
            value = self.visit(tree.children[0])
            with self.builder.if_then(value):
                ret(self.visit_unsafe(tree.children[1]))
            ret(make_bool(1))

        return phi[0]

    def logic_and(self, tree: TypeTree):
        phi = []

        with options(self.builder, tree.ret, phi) as ret:
            value = self.visit(tree.children[0])
            with self.builder.if_then(value):
                ret(self.visit_unsafe(tree.children[1]))
            ret(make_bool(0))

        return phi[0]

    def logic_or(self, tree: TypeTree):
        phi = []

        with options(self.builder, tree.ret, phi) as ret:
            value = self.visit(tree.children[0])
            with self.builder.if_then(value):
                ret(make_bool(1))
            ret(self.visit_unsafe(tree.children[1]))

        return phi[0]

    def logic_not(self, tree: TypeTree):
        value = self.visit_children(tree)[0]
        return self.builder.not_(value)

    def collect_tuple(self, tree: TypeTree):
        value = ir.Constant(tree.ret, ir.Undefined)
        for i, v in enumerate(self.visit_children(tree)):
            value = self.builder.insert_value(value, v, i)
        return value
        
    # Integers
    def integer(self, tree: TypeTree):
        return ir.Constant(tree.ret, tree.children[0].value)

    def add_int(self, tree: TypeTree):
        # TODO Use overflow bit to raise runtime error
        # self.builder.extract_value(self.builder.sadd_with_overflow(values[0], values[1]), 0)
        values = self.visit_children(tree)
        return self.builder.add(values[0], values[1])

    def sub_int(self, tree: TypeTree):
        # TODO Use overflow bit to raise runtime error
        # self.builder.extract_value(self.builder.ssub_with_overflow(values[0], values[1]), 0)
        values = self.visit_children(tree)
        return self.builder.sub(values[0], values[1])

    def mod_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.srem(values[0], values[1])

    def div_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.sdiv(values[0], values[1])

    def mul_int(self, tree: TypeTree):
        # TODO Use overflow bit to raise runtime error
        # self.builder.extract_value(self.builder.smul_with_overflow(values[0], values[1]), 0)
        values = self.visit_children(tree)
        return self.builder.extract_value(self.builder.smul_with_overflow(values[0], values[1]), 0)

    def negate_int(self, tree: TypeTree):
        value = self.visit_children(tree)[0]
        return self.builder.neg(value)

    def convert_int(self, tree: TypeTree):
        value = self.visit_children(tree)[0]
        return self.builder.sitofp(value, flt32)

    def equals_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.icmp_signed("==", values[0], values[1])

    def not_equals_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.icmp_signed("!=", values[0], values[1])

    def greater_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.icmp_signed(">", values[0], values[1])

    def less_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.icmp_signed("<", values[0], values[1])

    def greater_equals_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.icmp_signed(">=", values[0], values[1])

    def less_equals_int(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.icmp_signed("<=", values[0], values[1])

    # Floating point numbers
    def floating(self, tree: TypeTree):
        return ir.Constant(tree.ret, float(tree.children[0].value))

    def add_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fadd(values[0], values[1])

    def sub_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fsub(values[0], values[1])

    def mod_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.frem(values[0], values[1])

    def div_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fdiv(values[0], values[1])

    def mul_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fmul(values[0], values[1])

    def negate_flt(self, tree: TypeTree):
        value = self.visit_children(tree)[0]
        return self.builder.fsub(make_flt(0), value)

    # FLOAT TO INT DISABLED
    # def convert_flt(self, tree: TypeTree):
    #     value = self.visit_children(tree)[0]
    #     return self.builder.fptosi(value, int32)

    def equals_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fcmp_ordered("==", values[0], values[1])

    def not_equals_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fcmp_ordered("!=", values[0], values[1])

    def greater_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fcmp_ordered(">", values[0], values[1])

    def less_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fcmp_ordered("<", values[0], values[1])

    def greater_equals_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fcmp_ordered(">=", values[0], values[1])

    def less_equals_flt(self, tree: TypeTree):
        values = self.visit_children(tree)
        return self.builder.fcmp_ordered("<=", values[0], values[1])

    def __default__(self, tree: TypeTree):
        raise NotImplementedError("llvm", tree.data)


def build_function(func: ir.Function, env_names, env_types, arg_names, code):
    block = func.append_basic_block("entry")
    builder = ir.IRBuilder(block)

    env = func.args[0]
    env_values = read_environment(builder, env, env_types)
    args = dict(zip(env_names, env_values))
    for a, t in zip(arg_names, func.args[1:]):
        tuple_assign(args, builder, a, t)

    this_env_size = environment_size(builder, env_values)
    closure = ir.Constant(Closure(func.type), [func, ir.Undefined, ir.Undefined])
    closure = builder.insert_value(closure, this_env_size, 1)
    closure = builder.insert_value(closure, env, 2)

    LLVMScope(builder, args, code, builder.ret, set(), closure)
