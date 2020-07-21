from contextlib import contextmanager

from llvmlite import ir

from tree import TypeTree, volpe_assert, VolpeError
from volpe_types import int64, flt64, char, unwrap


def math(self, tree: TypeTree):
    values = self.visit_children(tree)
    t = tree.return_type
    if unwrap(t) == int64:
        return getattr(self, tree.data + "_int")(values)
    if unwrap(t) == flt64:
        return getattr(self, tree.data + "_flt")(values)
    raise VolpeError("math operations only work for integers and floats", tree)


def unary_math(self, tree: TypeTree):
    values = self.visit_children(tree)
    t = unwrap(tree.return_type)
    if t == int64:
        return getattr(self, tree.data + "_int")(values)
    if t == flt64:
        return getattr(self, tree.data + "_flt")(values)
    raise VolpeError("unary math operations only work for integers and floats", tree)


def comp(self, tree: TypeTree):
    values = self.visit_children(tree)
    t = unwrap(tree.children[0].return_type)
    if t == int64 or t == char:
        return getattr(self, tree.data + "_int")(values)
    if t == flt64:
        return getattr(self, tree.data + "_flt")(values)
    raise VolpeError("comparisons only work for integers, floats, and chars", tree)


@contextmanager
def options(b: ir.IRBuilder, t: ir.Type) -> ir.Value:
    new_block = b.function.append_basic_block("block")
    with b.goto_block(new_block):
        phi_node = b.phi(t)

    def ret(value):
        if not b.block.is_terminated:
            phi_node.add_incoming(value, b.block)
            b.branch(new_block)

    yield ret, phi_node

    b.position_at_end(new_block)


@contextmanager
def build_func(func: ir.Function):
    block = func.append_basic_block("entry")
    builder = ir.IRBuilder(block)

    yield builder, func.args


def mutate_array(self, tree):
    if tree.data == "symbol":
        name = tree.children[0].value

        def fun(value):
            self.local_scope[name] = value

        return self.get_scope(name), fun

    volpe_assert(tree.data == "list_index", "can only index arrays", tree)
    array_value, inner_fun = mutate_array(self, tree.children[0])
    i = self.visit(tree.children[1])

    def fun(value):
        inner_fun(self.builder.insert_element(array_value, value, i))

    return self.builder.extract_element(array_value, i), fun


def assign(self, tree: TypeTree, value):
    if tree.data == "object":
        for i, child in enumerate(tree.children):
            assign(self, child, self.builder.extract_value(value, i))

    elif tree.data == "list":
        for i, child in enumerate(tree.children):
            assign(self, child, self.builder.extract_element(value, i))

    elif tree.data == "list_index":
        array_value, fun = mutate_array(self, tree.children[0])
        fun(self.builder.insert_element(array_value, value, self.visit(tree.children[1])))

    else:
        assert tree.data == "symbol"  # no message?
        name = tree.children[0].value
        self.local_scope[name] = value


def build_or_get_function(self, tree):
    closure, args = tree.children[0].return_type, tree.children[1].return_type
    inst = closure.tree.instances[args]
    if not hasattr(inst, "func"):
        arg_type = inst.children[0].return_type
        ret_type = inst.children[1].return_type

        module = self.builder.module
        func_name = str(next(module.func_count))
        func_type = ir.FunctionType(unwrap(ret_type), [unwrap(closure), unwrap(arg_type)])
        inst.func = ir.Function(module, func_type, func_name)

        with build_func(inst.func) as (b, args):
            b: ir.IRBuilder
            with options(b, args[1].type) as (rec, phi):
                rec(args[1])

            new_values = [b.extract_value(args[0], i) for i in range(len(closure.env))]
            env_scope = dict(zip(closure.env.keys(), new_values))
            env_scope["@"] = args[0]

            def scope(name):
                return env_scope[name]

            self.__class__(b, inst.children[1], scope, b.ret, rec, (inst.children[0], phi))

    return inst.func
