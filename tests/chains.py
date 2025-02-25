from contextlib import contextmanager
from unittest import TestCase, skipIf
import gast as ast
import beniget
import io
import sys


@contextmanager
def captured_output():
    if sys.version_info.major >= 3:
        new_out, new_err = io.StringIO(), io.StringIO()
    else:
        new_out, new_err = io.BytesIO(), io.BytesIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = new_out, new_err
        yield sys.stdout, sys.stderr
    finally:
        sys.stdout, sys.stderr = old_out, old_err


class TestDefUseChains(TestCase):
    def checkChains(self, code, ref):
        class StrictDefUseChains(beniget.DefUseChains):
            def unbound_identifier(self, name, node):
                raise RuntimeError(
                    "W: unbound identifier '{}' at {}:{}".format(
                        name, node.lineno, node.col_offset
                    )
                )

        node = ast.parse(code)
        c = StrictDefUseChains()
        c.visit(node)
        self.assertEqual(c.dump_chains(node), ref)

    def test_simple_expression(self):
        code = "a = 1; a + 2"
        self.checkChains(code, ["a -> (a -> (BinOp -> ()))"])

    def test_expression_chain(self):
        code = "a = 1; (- a + 2) > 0"
        self.checkChains(code, ["a -> (a -> (UnaryOp -> (BinOp -> (Compare -> ()))))"])

    def test_ifexp_chain(self):
        code = "a = 1; a + 1 if a else - a"
        self.checkChains(
            code,
            [
                "a -> ("
                "a -> (IfExp -> ()), "
                "a -> (BinOp -> (IfExp -> ())), "
                "a -> (UnaryOp -> (IfExp -> ()))"
                ")"
            ],
        )

    def test_type_destructuring_tuple(self):
        code = "a, b = range(2); a"
        self.checkChains(code, ["a -> (a -> ())", "b -> ()"])

    def test_type_destructuring_list(self):
        code = "[a, b] = range(2); a"
        self.checkChains(code, ["a -> (a -> ())", "b -> ()"])

    def test_type_destructuring_for(self):
        code = "for a, b in ((1,2), (3,4)): a"
        self.checkChains(code, ["a -> (a -> ())", "b -> ()"])

    def test_assign_in_loop(self):
        code = "a = 2\nwhile 1: a = 1\na"
        self.checkChains(code, ["a -> (a -> ())", "a -> (a -> ())"])

    def test_reassign_in_loop(self):
        code = "m = 1\nfor i in [1, 2]:\n m = m + 1"
        self.checkChains(
            code, ["m -> (m -> (BinOp -> ()))", "i -> ()", "m -> (m -> (BinOp -> ()))"]
        )

    def test_continue_in_loop(self):
        code = "for i in [1, 2]:\n if i: m = 1; continue\n m = 1\nm"
        self.checkChains(
            code, ['i -> (i -> ())', 'm -> (m -> ())', 'm -> (m -> ())']
        )

    def test_break_in_loop(self):
        code = "for i in [1, 2]:\n if i: m = 1; continue\n m = 1\nm"
        self.checkChains(
            code, ['i -> (i -> ())', 'm -> (m -> ())', 'm -> (m -> ())']
        )

    def test_augassign(self):
        code = "a = 1; a += 2; a"
        self.checkChains(code, ['a -> (a -> (a -> ()))'])

    def test_read_global_from_branch(self):
        code = "if 1: a = 1\ndef foo():\n def bar(): global a; return a"
        self.checkChains(code, ['a -> (a -> ())',
                                'foo -> ()'])


    def test_expanded_augassign(self):
        code = "a = 1; a = a + 2"
        self.checkChains(code, ["a -> (a -> (BinOp -> ()))", "a -> ()"])

    def test_augassign_in_loop(self):
        code = "a = 1\nfor i in [1]:\n a += 2\na"
        self.checkChains(code, ['a -> (a -> ((#1), a -> ()), a -> ())',
                                'i -> ()'])

    def test_assign_in_while_in_conditional(self):
        code = """
G = 1
while 1:
    if 1:
        G = 1
    G"""
        self.checkChains(code, ['G -> (G -> ())',
                                'G -> (G -> ())'])

    def test_assign_in_loop_in_conditional(self):
        code = """
G = 1
for _ in [1]:
    if 1:
        G = 1
    G"""
        self.checkChains(code, ['G -> (G -> ())',
                                '_ -> ()',
                                'G -> (G -> ())'])

    def test_simple_print(self):
        code = "a = 1; print(a)"
        if sys.version_info.major >= 3:
            self.checkChains(code, ["a -> (a -> (Call -> ()))"])
        else:
            self.checkChains(code, ["a -> (a -> ())"])

    def test_simple_redefinition(self):
        code = "a = 1; a + 2; a = 3; +a"
        self.checkChains(
            code, ["a -> (a -> (BinOp -> ()))", "a -> (a -> (UnaryOp -> ()))"]
        )

    def test_simple_for(self):
        code = "for i in [1,2,3]: j = i"
        self.checkChains(code, ["i -> (i -> ())", "j -> ()"])

    def test_simple_for_orelse(self):
        code = "for i in [1,2,3]: pass\nelse: i = 4\ni"
        self.checkChains(
            code,
            [
                # assign in loop iteration
                "i -> (i -> ())",
                # assign in orelse
                "i -> (i -> ())",
            ],
        )

    def test_for_break(self):
        code = "i = 8\nfor i in [1,2]:\n break\n i = 3\ni"
        self.checkChains(
            code,
            ['i -> (i -> ())',
             'i -> (i -> ())',
             'i -> ()'])

    def test_for_pass(self):
        code = "i = 8\nfor i in []:\n pass\ni"
        self.checkChains(
            code,
            ['i -> (i -> ())',
             'i -> (i -> ())'])

    def test_complex_for_orelse(self):
        code = "I = J = 0\nfor i in [1,2]:\n if i < 3: I = i\nelse:\n if 1: J = I\nJ"
        self.checkChains(
            code,
            ['I -> (I -> ())',
             'J -> (J -> ())',
             'i -> (i -> (Compare -> ()), i -> ())',
             'I -> (I -> ())',
             'J -> (J -> ())']
        )

    def test_simple_while(self):
        code = "i = 2\nwhile i: i = i - 1\ni"
        self.checkChains(
            code,
            [
                # first assign, out of loop
                "i -> (i -> (), i -> (BinOp -> ()), i -> ())",
                # second assign, in loop
                "i -> (i -> (), i -> (BinOp -> ()), i -> ())",
            ],
        )

    def test_while_break(self):
        code = "i = 8\nwhile 1:\n break\n i = 3\ni"
        self.checkChains(
            code,
            ['i -> (i -> ())',
             'i -> ()'])

    def test_while_cond_break(self):
        code = "i = 8\nwhile 1:\n if i: i=1;break\ni"
        self.checkChains(
            code,
            ['i -> (i -> (), i -> ())', 'i -> (i -> ())'])

    def test_nested_while(self):
        code = '''
done = 1
while done:

    while done:
        if 1:
            done = 1
            break

        if 1:
            break'''


        self.checkChains(
            code,
            ['done -> (done -> (), done -> ())',
             'done -> (done -> (), done -> ())']
            )

    def test_while_cond_continue(self):
        code = "i = 8\nwhile 1:\n if i: i=1;continue\ni"
        self.checkChains(
            code,
            ['i -> (i -> (), i -> ())', 'i -> (i -> (), i -> ())'])

    def test_complex_while_orelse(self):
        code = "I = J = i = 0\nwhile i:\n if i < 3: I = i\nelse:\n if 1: J = I\nJ"
        self.checkChains(
            code,
            [
                "I -> (I -> ())",
                "J -> (J -> ())",
                "i -> (i -> (), i -> (Compare -> ()), i -> ())",
                "J -> (J -> ())",
                "I -> (I -> ())",
            ],
        )

    def test_while_orelse_break(self):
        code = "I = 0\nwhile I:\n if 1: I = 1; break\nelse: I"
        self.checkChains(
            code,
            ['I -> (I -> (), I -> ())',
             'I -> ()'],
        )

    def test_while_nested_break(self):
        code = "i = 8\nwhile i:\n if i: break\n i = 3\ni"
        self.checkChains(
            code,
            ['i -> (i -> (), i -> (), i -> ())',
             'i -> (i -> (), i -> (), i -> ())'])

    def test_if_true_branch(self):
        code = "if 1: i = 0\ni"
        self.checkChains(code, ["i -> (i -> ())"])

    def test_if_false_branch(self):
        code = "if 1: pass\nelse: i = 0\ni"
        self.checkChains(code, ["i -> (i -> ())"])

    def test_if_both_branch(self):
        code = "if 1: i = 1\nelse: i = 0\ni"
        self.checkChains(code, ["i -> (i -> ())"] * 2)

    def test_if_in_loop(self):
        code = "for _ in [0, 1]:\n if _: i = 1\n else: j = i\ni"
        self.checkChains(code, ["_ -> (_ -> ())", "i -> (i -> (), i -> ())", "j -> ()"])

    def test_with_handler(self):
        code = 'with open("/dev/null") as x: pass\nx'
        self.checkChains(code, ["x -> (x -> ())"])

    def test_simple_try(self):
        code = 'try: e = open("/dev/null")\nexcept Exception: pass\ne'
        self.checkChains(code, ["e -> (e -> ())"])

    def test_simple_except(self):
        code = "try: pass\nexcept Exception as e: pass\ne"
        self.checkChains(code, ["e -> (e -> ())"])

    def test_simple_try_except(self):
        code = 'try: f = open("")\nexcept Exception as e: pass\ne;f'
        self.checkChains(code, ["f -> (f -> ())", "e -> (e -> ())"])

    def test_redef_try_except(self):
        code = 'try: f = open("")\nexcept Exception as f: pass\nf'
        self.checkChains(code, ["f -> (f -> ())", "f -> (f -> ())"])

    def test_simple_import(self):
        code = "import x; x"
        self.checkChains(code, ["x -> (x -> ())"])

    def test_simple_import_as(self):
        code = "import x as y; y()"
        self.checkChains(code, ["y -> (y -> (Call -> ()))"])

    def test_multiple_import_as(self):
        code = "import x as y, z; y"
        self.checkChains(code, ["y -> (y -> ())", "z -> ()"])

    def test_import_from(self):
        code = "from  y import x; x"
        self.checkChains(code, ["x -> (x -> ())"])

    def test_import_from_as(self):
        code = "from  y import x as z; z"
        self.checkChains(code, ["z -> (z -> ())"])

    def test_multiple_import_from_as(self):
        code = "from  y import x as z, w; z"
        self.checkChains(code, ["z -> (z -> ())", "w -> ()"])

    def test_method_function_conflict(self):
        code = "def foo():pass\nclass C:\n def foo(self): foo()"
        self.checkChains(code, ["foo -> (foo -> (Call -> ()))", "C -> ()"])

    def test_nested_if(self):
        code = "f = 1\nif 1:\n if 1:pass\n else: f=1\nelse: f = 1\nf"
        self.checkChains(code, ["f -> (f -> ())", "f -> (f -> ())", "f -> (f -> ())"])

    def test_nested_if_else(self):
        code = "f = 1\nif 1: f = 1\nelse:\n if 1:pass\n else: f=1\nf"
        self.checkChains(code, ["f -> (f -> ())", "f -> (f -> ())", "f -> (f -> ())"])

    def test_try_except(self):
        code = "f = 1\ntry: \n len(); f = 2\nexcept: pass\nf"
        self.checkChains(code, ["f -> (f -> ())", "f -> (f -> ())"])

    def test_attr(self):
        code = "import numpy as bar\ndef foo():\n return bar.zeros(2)"
        self.checkChains(
            code, ["bar -> (bar -> (Attribute -> (Call -> ())))", "foo -> ()"]
        )

    def test_class_decorator(self):
        code = "from some import decorator\n@decorator\nclass C:pass"
        self.checkChains(code, ["decorator -> (decorator -> (C -> ()))", "C -> ()"])

    def test_class_base(self):
        code = "class A:pass\nclass B(A):pass"
        self.checkChains(code, ["A -> (A -> (B -> ()))", "B -> ()"])

    @skipIf(sys.version_info.major < 3, "Python 3 syntax")
    def test_def_used_in_self_default(self):
        code = "def foo(x:foo): return foo"
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        self.assertEqual(c.dump_chains(node), ["foo -> (foo -> ())"])

    def test_unbound_class_variable(self):
        code = '''
def middle():
    x = 1
    class mytype(str):
        x = x+1 # <- this triggers NameError: name 'x' is not defined
    return x
        '''
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        self.assertEqual(c.dump_chains(node.body[0]), ['x -> (x -> ())', 'mytype -> ()'])

    def test_unbound_class_variable2(self):
        code = '''class A:\n  a = 10\n  def f(self):\n    return a # a is not defined'''
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        self.assertEqual(c.dump_chains(node.body[0]), ['a -> ()', 'f -> ()'])

    def test_unbound_class_variable3(self):
        code = '''class A:\n  a = 10\n  class I:\n    b = a + 1 # a is not defined'''
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        self.assertEqual(c.dump_chains(node.body[0]), ['a -> ()', 'I -> ()'])

    def test_unbound_class_variable4(self):
        code = '''class A:\n  a = 10\n  f = lambda: a # a is not defined'''
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        self.assertEqual(c.dump_chains(node.body[0]), ['a -> ()', 'f -> ()'])

    def test_unbound_class_variable5(self):
        code = '''class A:\n  a = 10\n  b = [a for _ in range(10)]  # a is not defined'''
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        if sys.version_info.major >= 3:
            self.assertEqual(c.dump_chains(node.body[0]), ['a -> ()', 'b -> ()'])
        else:
            self.assertEqual(c.dump_chains(node.body[0]),
                             ['a -> (a -> (ListComp -> ()))', '_ -> ()', 'b -> ()'])

    @skipIf(sys.version_info.major < 3, "Python 3 syntax")
    def test_functiondef_returns(self):
        code = "x = 1\ndef foo() -> x: pass"
        self.checkChains(code, ['x -> (x -> ())', 'foo -> ()'])

    @skipIf(sys.version_info.major < 3, "Python 3 syntax")
    def test_arg_annotation(self):
        code = "type_ = int\ndef foo(bar: type_): pass"
        self.checkChains(code, ["type_ -> (type_ -> ())", "foo -> ()"])


    @skipIf(sys.version_info.major < 3, "Python 3 syntax")
    def test_annotation_inner_class(self):

        code = '''
def outer():
    def middle():
        class mytype(str):
            def count(self) -> mytype: # this should trigger unbound identifier
                def c(x) -> mytype(): # this one shouldn't
                    ...
        '''
        c = beniget.DefUseChains()
        node = ast.parse(code)
        c.visit(node)
        self.assertEqual(c.dump_chains(node.body[0].body[0]), ['mytype -> (mytype -> (Call -> ()))'])

    def check_message(self, code, expected_messages, filename=None):
        node = ast.parse(code)
        c = beniget.DefUseChains(filename)
        with captured_output() as (out, err):
            c.visit(node)

        if not out.getvalue():
            produced_messages = []
        else:
            produced_messages = out.getvalue().strip().split("\n")

        self.assertEqual(len(expected_messages), len(produced_messages),
                         produced_messages)
        for expected, produced in zip(expected_messages, produced_messages):
            self.assertIn(expected, produced,
                          "actual message does not contains expected message")

    def test_unbound_identifier_message_format(self):
        code = "foo(1)\nbar(2)"
        self.check_message(code, ["<unknown>:1", "<unknown>:2"])
        self.check_message(code, ["foo.py:1", "foo.py:2"], filename="foo.py")

    def test_unbound_class_variable_reference_message_format(self):
        code = "class A:\n a = 10\n def f(self): return a # a is undef"
        self.check_message(code, ["unbound identifier 'a' at <unknown>:3"])

    def test_no_unbound_local_identifier_in_comp(self):
        code = "a = []; b = [1 for i in a]"
        self.check_message(code, [])

    def test_maybe_unbound_identifier_message_format(self):
        code = "x = 1\ndef foo(): y = x; x = 2"
        self.check_message(code,
                           ["unbound identifier 'x' at <unknown>:2"])

    def test_unbound_local_identifier_in_func(self):
        code = "def A():\n x = 1\n class B: x = x"
        self.check_message(code,
                           ["unbound identifier 'x' at <unknown>:3"])

    @skipIf(sys.version_info < (3, 0), 'Python 3 syntax')
    def test_unbound_local_identifier_in_method(self):
        code = "class A:pass\nclass B:\n def A(self) -> A:pass"
        self.check_message(code, [])

    @skipIf(sys.version_info < (3, 0), 'Python 3 syntax')
    def test_unbound_local_identifier_nonlocal(self):
        code = "def A():\n x = 1\n class B: nonlocal x; x = x"
        self.check_message(code, [])


    def test_assign_uses_class_level_name(self):
        code = '''
visit_Name = object
class Visitor:
    def visit_Name(self, node):pass
    visit_Attribute = visit_Name
'''
        node = ast.parse(code)
        c = beniget.DefUseChains()
        c.visit(node)
        self.assertEqual(c.dump_chains(node), ['visit_Name -> ()',
                                               'Visitor -> ()'])
        self.assertEqual(c.dump_chains(node.body[-1]),
                         ['visit_Name -> (visit_Name -> ())',
                          'visit_Attribute -> ()'])


    def test_base_class_uses_class_level_same_name(self):
            code = '''
class Attr(object):pass
class Visitor:
    class Attr(Attr):pass
    '''
            node = ast.parse(code)
            c = beniget.DefUseChains()
            c.visit(node)
            self.assertEqual(c.dump_chains(node),
                             ['Attr -> (Attr -> (Attr -> ()))',
                              'Visitor -> ()'])
            self.assertEqual(c.dump_chains(node.body[-1]), ['Attr -> ()'])


    @skipIf(sys.version_info < (3, 0), 'Python 3 syntax')
    def test_annotation_uses_class_level_name(self):
        code = '''
Thing = object
def f():...
class Visitor:
    Thing = bytes
    def f(): return f()
    def visit_Name(self, node:Thing, fn:f):...
'''
        node = ast.parse(code)
        c = beniget.DefUseChains()
        c.visit(node)
        self.assertEqual(c.dump_chains(node),
                         ['Thing -> ()',
                          'f -> (f -> (Call -> ()))',
                          'Visitor -> ()'])
        self.assertEqual(c.dump_chains(node.body[-1]),
                         ['Thing -> (Thing -> ())',
                          'f -> (f -> ())',
                          'visit_Name -> ()'])

    def test_assign_uses_class_level_same_name(self):
        code = '''
def visit_Attribute(self, node):pass
class Visitor:
    visit_Attribute = visit_Attribute
'''
        node = ast.parse(code)
        c = beniget.DefUseChains()
        c.visit(node)
        self.assertEqual(c.dump_chains(node),
            ['visit_Attribute -> (visit_Attribute -> ())',
             'Visitor -> ()'])
        self.assertEqual(c.dump_chains(node.body[-1]),
                         ['visit_Attribute -> ()'])

    def test_unbound_local_identifier_in_augassign(self):
        code = "def A():\n x = 1\n class B: x += 1"
        self.check_message(code,
                           ["unbound identifier 'x' at <unknown>:3"])

    def test_star_import_with_conditional_redef(self):
        code = '''
from math import *

if 1:
    def pop():
        cos()
cos = pop()'''
        self.checkChains(code, [
            '* -> (cos -> (Call -> ()))',
            'pop -> (pop -> (Call -> ()))',
            'cos -> (cos -> (Call -> ()))'
        ])

    @skipIf(sys.version_info < (3, 8), 'Python 3.8 syntax')
    def test_named_expr_simple(self):
        code = '''
if (x := 1):
    y = x + 1'''
        self.checkChains(
            code, ['x -> (x -> (BinOp -> ()))', 'y -> ()']
        )

    @skipIf(sys.version_info < (3, 8), 'Python 3.8 syntax')
    def test_named_expr_complex(self):
        code = '''
if (x := (y := 1) + 1):
    z = x + y'''
        self.checkChains(
            code, ['y -> (y -> (BinOp -> ()))', 'x -> (x -> (BinOp -> ()))', 'z -> ()']
        )

    @skipIf(sys.version_info < (3, 8), 'Python 3.8 syntax')
    def test_named_expr_with_rename(self):
        code = '''
a = 1
if (a := a + a):
    pass'''
        self.checkChains(
            code, ['a -> (a -> (BinOp -> (NamedExpr -> ())), a -> (BinOp -> (NamedExpr -> ())))', 'a -> ()']
        )


class TestUseDefChains(TestCase):
    def checkChains(self, code, ref):
        class StrictDefUseChains(beniget.DefUseChains):
            def unbound_identifier(self, name, node):
                raise RuntimeError(
                    "W: unbound identifier '{}' at {}:{}".format(
                        name, node.lineno, node.col_offset
                    )
                )

        node = ast.parse(code)
        c = StrictDefUseChains()
        c.visit(node)
        cc = beniget.UseDefChains(c)

        self.assertEqual(str(cc), ref)

    def test_simple_expression(self):
        code = "a = 1; a"
        self.checkChains(code, "a <- {a}, a <- {}")

    def test_call(self):
        code = "from foo import bar; bar(1, 2)"
        self.checkChains(code, "Call <- {Constant, Constant, bar}, bar <- {bar}")
