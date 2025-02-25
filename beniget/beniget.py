from collections import defaultdict, OrderedDict
from contextlib import contextmanager
import sys

import gast as ast

# TODO: remove me when python 2 is not supported anymore
class _ordered_set(object):
    def __init__(self, elements=None):
        self.values = OrderedDict.fromkeys(elements or [])

    def add(self, value):
        self.values[value] = None

    def update(self, values):
        self.values.update((k, None) for k in values)

    def __iter__(self):
        return iter(self.values.keys())

    def __contains__(self, value):
        return value in self.values

    def __add__(self, other):
        out = self.values.copy()
        out.update(other.values)
        return out

    def __len__(self):
        return len(self.values)

if sys.version_info >= (3,6):
    from .ordered_set import ordered_set
else:
    # python < 3,6 we fall back on older version of the ordered_set
    ordered_set = _ordered_set

class Ancestors(ast.NodeVisitor):
    """
    Build the ancestor tree, that associates a node to the list of node visited
    from the root node (the Module) to the current node

    >>> import gast as ast
    >>> code = 'def foo(x): return x + 1'
    >>> module = ast.parse(code)

    >>> from beniget import Ancestors
    >>> ancestors = Ancestors()
    >>> ancestors.visit(module)

    >>> binop = module.body[0].body[0].value
    >>> for n in ancestors.parents(binop):
    ...    print(type(n))
    <class 'gast.gast.Module'>
    <class 'gast.gast.FunctionDef'>
    <class 'gast.gast.Return'>
    """

    def __init__(self):
        self._parents = dict()
        self._current = list()

    def generic_visit(self, node):
        self._parents[node] = list(self._current)
        self._current.append(node)
        super(Ancestors, self).generic_visit(node)
        self._current.pop()

    def parent(self, node):
        return self._parents[node][-1]

    def parents(self, node):
        return self._parents[node]

    def parentInstance(self, node, cls):
        for n in reversed(self._parents[node]):
            if isinstance(n, cls):
                return n
        raise ValueError("{} has no parent of type {}".format(node, cls))

    def parentFunction(self, node):
        return self.parentInstance(node, (ast.FunctionDef,
                                          ast.AsyncFunctionDef))

    def parentStmt(self, node):
        return self.parentInstance(node, ast.stmt)


class Def(object):
    """
    Model a definition, either named or unnamed, and its users.
    """

    __slots__ = "node", "_users"

    def __init__(self, node):
        self.node = node
        self._users = ordered_set()

    def add_user(self, node):
        assert isinstance(node, Def)
        self._users.add(node)

    def name(self):
        """
        If the node associated to this Def has a name, returns this name.
        Otherwise returns its type
        """
        if isinstance(self.node, (ast.ClassDef,
                                  ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
            return self.node.name
        elif isinstance(self.node, ast.Name):
            return self.node.id
        elif isinstance(self.node, ast.alias):
            base = self.node.name.split(".", 1)[0]
            return self.node.asname or base
        elif isinstance(self.node, tuple):
            return self.node[1]
        else:
            return type(self.node).__name__

    def users(self):
        """
        The list of ast entity that holds a reference to this node
        """
        return self._users

    def __repr__(self):
        return self._repr({})

    def _repr(self, nodes):
        if self in nodes:
            return "(#{})".format(nodes[self])
        else:
            nodes[self] = len(nodes)
            return "{} -> ({})".format(
                self.node, ", ".join(u._repr(nodes.copy())
                                     for u in self._users)
            )

    def __str__(self):
        return self._str({})

    def _str(self, nodes):
        if self in nodes:
            return "(#{})".format(nodes[self])
        else:
            nodes[self] = len(nodes)
            return "{} -> ({})".format(
                self.name(), ", ".join(u._str(nodes.copy())
                                       for u in self._users)
            )


if sys.version_info.major == 2:
    BuiltinsSrc = __builtins__
else:
    import builtins
    BuiltinsSrc = builtins.__dict__

Builtins = {k: v for k, v in BuiltinsSrc.items()}

Builtins["__file__"] = __file__

DeclarationStep, DefinitionStep = object(), object()


class CollectLocals(ast.NodeVisitor):
    def __init__(self):
        self.Locals = set()
        self.NonLocals = set()

    def visit_FunctionDef(self, node):
        self.Locals.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    visit_ClassDef = visit_FunctionDef

    def visit_Nonlocal(self, node):
        self.NonLocals.update(name for name in node.names)

    visit_Global = visit_Nonlocal

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store) and node.id not in self.NonLocals:
            self.Locals.add(node.id)

    def skip(self, _):
        pass

    if sys.version_info.major >= 3:
        visit_SetComp = visit_DictComp = visit_ListComp = skip
        visit_GeneratorExp = skip

    visit_Lambda = skip

    def visit_Import(self, node):
        for alias in node.names:
            base = alias.name.split(".", 1)[0]
            self.Locals.add(alias.asname or base)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.Locals.add(alias.asname or alias.name)


def collect_locals(node):
    '''
    Compute the set of identifiers local to a given node.

    This is meant to emulate a call to locals()
    '''
    visitor = CollectLocals()
    visitor.generic_visit(node)
    return visitor.Locals


class DefUseChains(ast.NodeVisitor):
    """
    Module visitor that gathers two kinds of informations:
        - locals: Dict[node, List[Def]], a mapping between a node and the list
          of variable defined in this node,
        - chains: Dict[node, Def], a mapping between nodes and their chains.

    >>> import gast as ast
    >>> module = ast.parse("from b import c, d; c()")
    >>> duc = DefUseChains()
    >>> duc.visit(module)
    >>> for head in duc.locals[module]:
    ...     print("{}: {}".format(head.name(), len(head.users())))
    c: 1
    d: 0
    >>> alias_def = duc.chains[module.body[0].names[0]]
    >>> print(alias_def)
    c -> (c -> (Call -> ()))
    """

    def __init__(self, filename=None):
        """
            - filename: str, included in error messages if specified
        """
        self.chains = {}
        self.locals = defaultdict(list)

        self.filename = filename

        # deep copy of builtins, to remain reentrant
        self._builtins = {k: Def(v) for k, v in Builtins.items()}

        # function body are not executed when the function definition is met
        # this holds a list of the functions met during body processing
        self._defered = []

        # stack of mapping between an id and Names
        self._definitions = []

        # stack of scope depth
        self._scope_depths = []

        # stack of variable defined with the global keywords
        self._globals = []

        # stack of local identifiers, used to detect 'read before assign'
        self._precomputed_locals = []

        # stack of variable that were undefined when we met them, but that may
        # be defined in another path of the control flow (esp. in loop)
        self._undefs = []

        # stack of nodes starting a scope: class, module, function...
        self._scopes = []

        self._breaks = []
        self._continues = []

        # dead code levels, it's non null for code that cannot be executed
        self._deadcode = 0

    #
    ## helpers
    #
    def dump_definitions(self, node, ignore_builtins=True):
        if isinstance(node, ast.Module) and not ignore_builtins:
            builtins = {d for d in self._builtins.values()}
            return sorted(d.name()
                          for d in self.locals[node] if d not in builtins)
        else:
            return sorted(d.name() for d in self.locals[node])

    def dump_chains(self, node):
        chains = []
        for d in self.locals[node]:
            chains.append(str(d))
        return chains

    def location(self, node):
        if hasattr(node, "lineno"):
            filename = "{}:".format(
                "<unknown>" if self.filename is None else self.filename
            )
            return " at {}{}:{}".format(filename,
                                            node.lineno,
                                            node.col_offset)
        else:
            return ""

    def unbound_identifier(self, name, node):
        location = self.location(node)
        print("W: unbound identifier '{}'{}".format(name, location))

    def invalid_name_lookup(self, name, scope, precomputed_locals, local_defs):
        # We may hit the situation where we refer to a local variable which is
        # not bound yet. This is a runtime error in Python, so we try to detec
        # it statically.

        # not a local variable => fine
        if name not in precomputed_locals:
            return

        # It's meant to be a local, but can we resolve it by a local lookup?
        islocal = any((name in defs or '*' in defs) for defs in local_defs)

        # At class scope, it's ok to refer to a global even if we also have a
        # local definition for that variable. Stated other wise
        #
        # >>> a = 1
        # >>> def foo(): a = a
        # >>> foo() # fails, a is a local referenced before being assigned
        # >>> class bar: a = a
        # >>> bar() # ok, and `bar.a is a`
        if isinstance(scope, ast.ClassDef):
            top_level_definitions = self._definitions[0:-self._scope_depths[0]]
            isglobal = any((name in top_lvl_def or '*' in top_lvl_def)
                           for top_lvl_def in top_level_definitions)
            return not islocal and not isglobal
        else:
            return not islocal

    def defs(self, node, quiet=False):
        '''
        Performs an actual lookup of node's id in current context, returning
        the list of def linked to that use.
        '''
        name = node.id
        stars = []

        # If the `global` keyword has been used, honor it
        if any(name in _globals for _globals in self._globals):
            looked_up_definitions = self._definitions[0:-self._scope_depths[0]]
        else:
            # List of definitions to check. This includes all non-class
            # definitions *and* the last definition. Class definitions are not
            # included because they require fully qualified access.
            looked_up_definitions = []

            scopes_iter = iter(reversed(self._scopes))
            depths_iter = iter(reversed(self._scope_depths))
            precomputed_locals_iter = iter(reversed(self._precomputed_locals))

            # Keep the last scope because we could be in class scope, in which
            # case we don't need fully qualified access.
            lvl = depth = next(depths_iter)
            precomputed_locals = next(precomputed_locals_iter)
            base_scope = next(scopes_iter)
            defs = self._definitions[depth:]
            if not self.invalid_name_lookup(name, base_scope, precomputed_locals, defs):
                looked_up_definitions.extend(reversed(defs))

                # Iterate over scopes, filtering out class scopes.
                for scope, depth, precomputed_locals in zip(scopes_iter,
                                                            depths_iter,
                                                            precomputed_locals_iter):
                    if not isinstance(scope, ast.ClassDef):
                        defs = self._definitions[lvl + depth: lvl]
                        if self.invalid_name_lookup(name, base_scope, precomputed_locals, defs):
                            looked_up_definitions.clear()
                            break
                        looked_up_definitions.extend(reversed(defs))
                    lvl += depth

        for defs in looked_up_definitions:
            if name in defs:
                return defs[name] if not stars else stars + list(defs[name])
            if "*" in defs:
                stars.extend(defs["*"])

        d = self.chains.setdefault(node, Def(node))

        if self._undefs:
            self._undefs[-1][name].append((d, stars))

        if stars:
            return stars + [d]
        else:
            if not self._undefs and not quiet:
                self.unbound_identifier(name, node)
            return [d]

    def process_body(self, stmts):
        deadcode = False
        for stmt in stmts:
            if isinstance(stmt, (ast.Break, ast.Continue, ast.Raise)):
                if not deadcode:
                    deadcode = True
                    self._deadcode += 1
            self.visit(stmt)
        if deadcode:
            self._deadcode -= 1

    def process_undefs(self):
        for undef_name, _undefs in self._undefs[-1].items():
            if undef_name in self._definitions[-1]:
                for newdef in self._definitions[-1][undef_name]:
                    for undef, _ in _undefs:
                        for user in undef.users():
                            newdef.add_user(user)
            else:
                for undef, stars in _undefs:
                    if not stars:
                        self.unbound_identifier(undef_name, undef.node)
        self._undefs.pop()


    @contextmanager
    def ScopeContext(self, node):
        self._scopes.append(node)
        self._scope_depths.append(-1)
        self._definitions.append(defaultdict(ordered_set))
        self._globals.append(set())
        self._precomputed_locals.append(collect_locals(node))
        yield
        self._precomputed_locals.pop()
        self._globals.pop()
        self._definitions.pop()
        self._scope_depths.pop()
        self._scopes.pop()

    if sys.version_info.major >= 3:
        CompScopeContext = ScopeContext
    else:
        @contextmanager
        def CompScopeContext(self, node):
            yield

    @contextmanager
    def DefinitionContext(self, definitions):
        self._definitions.append(definitions)
        self._scope_depths[-1] -= 1
        yield self._definitions[-1]
        self._scope_depths[-1] += 1
        self._definitions.pop()

    @contextmanager
    def SwitchScopeContext(self, defs, scopes, scope_depths, precomputed_locals):
        scope_depths, self._scope_depths = self._scope_depths, scope_depths
        scopes, self._scopes = self._scopes, scopes
        defs, self._definitions = self._definitions, defs
        precomputed_locals, self._precomputed_locals = self._precomputed_locals, precomputed_locals
        yield
        self._definitions = defs
        self._scopes = scopes
        self._scope_depths = scope_depths
        self._precomputed_locals = precomputed_locals


    # stmt
    def visit_Module(self, node):
        self.module = node
        with self.ScopeContext(node):

            self._definitions[-1].update(
                {k: ordered_set((v,)) for k, v in self._builtins.items()}
            )

            self.process_body(node.body)

            # handle function bodies
            for fnode, defs, scopes, scope_depths, precomputed_locals in self._defered:
                visitor = getattr(self,
                                  "visit_{}".format(type(fnode).__name__))
                with self.SwitchScopeContext(defs, scopes, scope_depths,
                                             precomputed_locals):
                    visitor(fnode, step=DefinitionStep)

            # various sanity checks
            if __debug__:
                overloaded_builtins = set()
                for d in self.locals[node]:
                    name = d.name()
                    if name in self._builtins:
                        overloaded_builtins.add(name)
                    assert name in self._definitions[0], (name, d.node)

                nb_defs = len(self._definitions[0])
                nb_bltns = len(self._builtins)
                nb_overloaded_bltns = len(overloaded_builtins)
                nb_heads = len({d.name() for d in self.locals[node]})
                assert nb_defs == nb_heads + nb_bltns - nb_overloaded_bltns

        assert not self._definitions
        assert not self._scopes
        assert not self._scope_depths
        assert not self._precomputed_locals

    def set_definition(self, name, dnode_or_dnodes):
        if self._deadcode:
            return
        if isinstance(dnode_or_dnodes, Def):
            self._definitions[-1][name] = ordered_set((dnode_or_dnodes,))
        else:
            self._definitions[-1][name] = ordered_set(dnode_or_dnodes)

    @staticmethod
    def add_to_definition(definition, name, dnode_or_dnodes):
        if isinstance(dnode_or_dnodes, Def):
            definition[name].add(dnode_or_dnodes)
        else:
            definition[name].update(dnode_or_dnodes)

    def extend_definition(self, name, dnode_or_dnodes):
        if self._deadcode:
            return
        DefUseChains.add_to_definition(self._definitions[-1], name,
                                       dnode_or_dnodes)

    def extend_global(self, name, dnode_or_dnodes):
        if self._deadcode:
            return
        DefUseChains.add_to_definition(self._definitions[0], name,
                                       dnode_or_dnodes)

    def set_or_extend_global(self, name, dnode):
        if self._deadcode:
            return
        if name not in self._definitions[0]:
            self.locals[self.module].append(dnode)
        DefUseChains.add_to_definition(self._definitions[0], name, dnode)

    def visit_annotation(self, node):
        annotation = getattr(node, 'annotation', None)
        if annotation:
            self.visit(annotation)

    def visit_skip_annotation(self, node):
        if isinstance(node, ast.Name):
            self.visit_Name(node, skip_annotation=True)
        else:
            self.visit(node)

    def visit_FunctionDef(self, node, step=DeclarationStep):
        if step is DeclarationStep:
            dnode = self.chains.setdefault(node, Def(node))
            self.locals[self._scopes[-1]].append(dnode)
            for arg in node.args.args:
                self.visit_annotation(arg)
            for arg in node.args.posonlyargs:
                self.visit_annotation(arg)
            if node.args.vararg:
                self.visit_annotation(node.args.vararg)
            for arg in node.args.kwonlyargs:
                self.visit_annotation(arg)
            if node.args.kwarg:
                self.visit_annotation(node.args.kwarg)

            for kw_default in filter(None, node.args.kw_defaults):
                self.visit(kw_default).add_user(dnode)
            for default in node.args.defaults:
                self.visit(default).add_user(dnode)
            for decorator in node.decorator_list:
                self.visit(decorator)

            if node.returns:
                self.visit(node.returns)

            self.set_definition(node.name, dnode)

            self._defered.append((node,
                                  list(self._definitions),
                                  list(self._scopes),
                                  list(self._scope_depths),
                                  list(self._precomputed_locals)))
        elif step is DefinitionStep:
            with self.ScopeContext(node):
                for arg in node.args.args:
                    self.visit_skip_annotation(arg)
                for arg in node.args.posonlyargs:
                    self.visit_skip_annotation(arg)
                if node.args.vararg:
                    self.visit_skip_annotation(node.args.vararg)
                for arg in node.args.kwonlyargs:
                    self.visit_skip_annotation(arg)
                if node.args.kwarg:
                    self.visit_skip_annotation(node.args.kwarg)
                self.process_body(node.body)
        else:
            raise NotImplementedError()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.locals[self._scopes[-1]].append(dnode)

        for base in node.bases:
            self.visit(base).add_user(dnode)
        for keyword in node.keywords:
            self.visit(keyword.value).add_user(dnode)
        for decorator in node.decorator_list:
            self.visit(decorator).add_user(dnode)

        with self.ScopeContext(node):
            self.set_definition("__class__", Def("__class__"))
            self.process_body(node.body)

        self.set_definition(node.name, dnode)


    def visit_Return(self, node):
        if node.value:
            self.visit(node.value)

    def visit_Break(self, _):
        for k, v in self._definitions[-1].items():
            DefUseChains.add_to_definition(self._breaks[-1], k, v)
        self._definitions[-1].clear()

    def visit_Continue(self, _):
        for k, v in self._definitions[-1].items():
            DefUseChains.add_to_definition(self._continues[-1], k, v)
        self._definitions[-1].clear()

    def visit_Delete(self, node):
        for target in node.targets:
            self.visit(target)

    def visit_Assign(self, node):
        # link is implicit through ctx
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)

    def visit_AnnAssign(self, node):
        if node.value:
            dvalue = self.visit(node.value)
        dannotation = self.visit(node.annotation)
        dtarget = self.visit(node.target)
        dtarget.add_user(dannotation)
        if node.value:
            dvalue.add_user(dtarget)

    def visit_AugAssign(self, node):
        dvalue = self.visit(node.value)
        if isinstance(node.target, ast.Name):
            ctx, node.target.ctx = node.target.ctx, ast.Load()
            dtarget = self.visit(node.target)
            dvalue.add_user(dtarget)
            node.target.ctx = ctx
            if any(node.target.id in _globals for _globals in self._globals):
                self.extend_global(node.target.id, dtarget)
            else:
                loaded_from = [d.name() for d in self.defs(node.target,
                                                           quiet=True)]
                self.set_definition(node.target.id, dtarget)
                # If we augassign from a value that comes from '*', let's use
                # this node as the definition point.
                if '*' in loaded_from:
                    self.locals[self._scopes[-1]].append(dtarget)
        else:
            self.visit(node.target).add_user(dvalue)

    def visit_Print(self, node):
        if node.dest:
            self.visit(node.dest)
        for value in node.values:
            self.visit(value)

    def visit_For(self, node):
        self.visit(node.iter)

        self._breaks.append(defaultdict(ordered_set))
        self._continues.append(defaultdict(ordered_set))

        self._undefs.append(defaultdict(list))
        with self.DefinitionContext(self._definitions[-1].copy()) as body_defs:
            self.visit(node.target)
            self.process_body(node.body)
            self.process_undefs()

            continue_defs = self._continues.pop()
            for d, u in continue_defs.items():
                self.extend_definition(d, u)
            self._continues.append(defaultdict(ordered_set))

            # extra round to ``emulate'' looping
            self.visit(node.target)
            self.process_body(node.body)

            # process else clause in case of late break
            with self.DefinitionContext(defaultdict(ordered_set)) as orelse_defs:
                self.process_body(node.orelse)

            break_defs = self._breaks.pop()
            continue_defs = self._continues.pop()


        for d, u in orelse_defs.items():
            self.extend_definition(d, u)

        for d, u in continue_defs.items():
            self.extend_definition(d, u)

        for d, u in break_defs.items():
            self.extend_definition(d, u)

        for d, u in body_defs.items():
            self.extend_definition(d, u)

    visit_AsyncFor = visit_For

    def visit_While(self, node):

        with self.DefinitionContext(self._definitions[-1].copy()):
            self._undefs.append(defaultdict(list))
            self._breaks.append(defaultdict(ordered_set))
            self._continues.append(defaultdict(ordered_set))

            self.process_body(node.orelse)

        with self.DefinitionContext(self._definitions[-1].copy()) as body_defs:

            self.visit(node.test)
            self.process_body(node.body)

            self.process_undefs()

            continue_defs = self._continues.pop()
            for d, u in continue_defs.items():
                self.extend_definition(d, u)
            self._continues.append(defaultdict(ordered_set))

            # extra round to simulate loop
            self.visit(node.test)
            self.process_body(node.body)

            # the false branch of the eval
            self.visit(node.test)

            with self.DefinitionContext(self._definitions[-1].copy()) as orelse_defs:
                self.process_body(node.orelse)

        break_defs = self._breaks.pop()
        continue_defs = self._continues.pop()

        for d, u in continue_defs.items():
            self.extend_definition(d, u)

        for d, u in break_defs.items():
            self.extend_definition(d, u)

        for d, u in orelse_defs.items():
            self.extend_definition(d, u)

        for d, u in body_defs.items():
            self.extend_definition(d, u)

    def visit_If(self, node):
        self.visit(node.test)

        # putting a copy of current level to handle nested conditions
        with self.DefinitionContext(self._definitions[-1].copy()) as body_defs:
            self.process_body(node.body)

        with self.DefinitionContext(self._definitions[-1].copy()) as orelse_defs:
            self.process_body(node.orelse)

        for d in body_defs:
            if d in orelse_defs:
                self.set_definition(d, body_defs[d] + orelse_defs[d])
            else:
                self.extend_definition(d, body_defs[d])

        for d in orelse_defs:
            if d in body_defs:
                pass  # already done in the previous loop
            else:
                self.extend_definition(d, orelse_defs[d])

    def visit_With(self, node):
        for withitem in node.items:
            self.visit(withitem)
        self.process_body(node.body)

    visit_AsyncWith = visit_With

    def visit_Raise(self, node):
        if node.exc:
            self.visit(node.exc)
        if node.cause:
            self.visit(node.cause)

    def visit_Try(self, node):
        with self.DefinitionContext(self._definitions[-1].copy()) as failsafe_defs:
            self.process_body(node.body)
            self.process_body(node.orelse)

        # handle the fact that definitions may have fail
        for d in failsafe_defs:
            self.extend_definition(d, failsafe_defs[d])

        for excepthandler in node.handlers:
            with self.DefinitionContext(defaultdict(ordered_set)) as handler_def:
                self.visit(excepthandler)

            for hd in handler_def:
                self.extend_definition(hd, handler_def[hd])

        self.process_body(node.finalbody)

    def visit_Assert(self, node):
        self.visit(node.test)
        if node.msg:
            self.visit(node.msg)

    def visit_Import(self, node):
        for alias in node.names:
            dalias = self.chains.setdefault(alias, Def(alias))
            base = alias.name.split(".", 1)[0]
            self.set_definition(alias.asname or base, dalias)
            self.locals[self._scopes[-1]].append(dalias)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            dalias = self.chains.setdefault(alias, Def(alias))
            self.set_definition(alias.asname or alias.name, dalias)
            self.locals[self._scopes[-1]].append(dalias)

    def visit_Exec(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.body)

        if node.globals:
            self.visit(node.globals)
        else:
            # any global may be used by this exec!
            for defs in self._definitions[0].values():
                for d in defs:
                    d.add_user(dnode)

        if node.locals:
            self.visit(node.locals)
        else:
            # any local may be used by this exec!
            visible_locals = set()
            for _definitions in reversed(self._definitions[1:]):
                for dname, defs in _definitions.items():
                    if dname not in visible_locals:
                        visible_locals.add(dname)
                        for d in defs:
                            d.add_user(dnode)

        self.extend_definition("*", dnode)

    def visit_Global(self, node):
        for name in node.names:
            self._globals[-1].add(name)

    def visit_Nonlocal(self, node):
        for name in node.names:
            for d in reversed(self._definitions[:-1]):
                if name not in d:
                    continue
                else:
                    # this rightfully creates aliasing
                    self.set_definition(name, d[name])
                    break
            else:
                self.unbound_identifier(name, node)

    def visit_Expr(self, node):
        self.generic_visit(node)

    # expr
    def visit_BoolOp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for value in node.values:
            self.visit(value).add_user(dnode)
        return dnode

    def visit_BinOp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.left).add_user(dnode)
        self.visit(node.right).add_user(dnode)
        return dnode

    def visit_UnaryOp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.operand).add_user(dnode)
        return dnode

    def visit_Lambda(self, node, step=DeclarationStep):
        if step is DeclarationStep:
            dnode = self.chains.setdefault(node, Def(node))
            return dnode
        elif step is DefinitionStep:
            dnode = self.chains[node]
            with self.ScopeContext(node):
                self.visit(node.args)
                self.visit(node.body).add_user(dnode)
            return dnode
        else:
            raise NotImplementedError()

    def visit_IfExp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.test).add_user(dnode)
        self.visit(node.body).add_user(dnode)
        self.visit(node.orelse).add_user(dnode)
        return dnode

    def visit_Dict(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for key in filter(None, node.keys):
            self.visit(key).add_user(dnode)
        for value in node.values:
            self.visit(value).add_user(dnode)
        return dnode

    def visit_Set(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for elt in node.elts:
            self.visit(elt).add_user(dnode)
        return dnode

    def visit_ListComp(self, node):
        dnode = self.chains.setdefault(node, Def(node))

        with self.CompScopeContext(node):
            for comprehension in node.generators:
                self.visit(comprehension).add_user(dnode)
            self.visit(node.elt).add_user(dnode)

        return dnode

    visit_SetComp = visit_ListComp

    def visit_DictComp(self, node):
        dnode = self.chains.setdefault(node, Def(node))

        with self.CompScopeContext(node):
            for comprehension in node.generators:
                self.visit(comprehension).add_user(dnode)
            self.visit(node.key).add_user(dnode)
            self.visit(node.value).add_user(dnode)

        return dnode

    visit_GeneratorExp = visit_ListComp

    def visit_Await(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        return dnode

    def visit_Yield(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.value:
            self.visit(node.value).add_user(dnode)
        return dnode

    visit_YieldFrom = visit_Await

    def visit_Compare(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.left).add_user(dnode)
        for expr in node.comparators:
            self.visit(expr).add_user(dnode)
        return dnode

    def visit_Call(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.func).add_user(dnode)
        for arg in node.args:
            self.visit(arg).add_user(dnode)
        for kw in node.keywords:
            self.visit(kw.value).add_user(dnode)
        return dnode

    visit_Repr = visit_Await

    def visit_Constant(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        return dnode

    def visit_FormattedValue(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        if node.format_spec:
            self.visit(node.format_spec).add_user(dnode)
        return dnode

    def visit_JoinedStr(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for value in node.values:
            self.visit(value).add_user(dnode)
        return dnode

    visit_Attribute = visit_Await

    def visit_Subscript(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        self.visit(node.slice).add_user(dnode)
        return dnode

    visit_Starred = visit_Await

    def visit_NamedExpr(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        self.visit(node.target)
        return dnode

    def is_in_current_scope(self, name):
        return any(name in defs
                   for defs in self._definitions[self._scope_depths[-1]:])

    def visit_Name(self, node, skip_annotation=False):
        if isinstance(node.ctx, (ast.Param, ast.Store)):
            dnode = self.chains.setdefault(node, Def(node))
            if any(node.id in _globals for _globals in self._globals):
                self.set_or_extend_global(node.id, dnode)
            else:
                self.set_definition(node.id, dnode)
                if dnode not in self.locals[self._scopes[-1]]:
                    self.locals[self._scopes[-1]].append(dnode)

            if node.annotation is not None and not skip_annotation:
                self.visit(node.annotation)

        elif isinstance(node.ctx, (ast.Load, ast.Del)):
            node_in_chains = node in self.chains
            if node_in_chains:
                dnode = self.chains[node]
            else:
                dnode = Def(node)
            for d in self.defs(node):
                d.add_user(dnode)
            if not node_in_chains:
                self.chains[node] = dnode
            # currently ignore the effect of a del
        else:
            raise NotImplementedError()
        return dnode

    def visit_Destructured(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        tmp_store = ast.Store()
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                tmp_store, elt.ctx = elt.ctx, tmp_store
                self.visit(elt)
                tmp_store, elt.ctx = elt.ctx, tmp_store
            elif isinstance(elt, ast.Subscript):
                self.visit(elt)
            elif isinstance(elt, (ast.List, ast.Tuple)):
                self.visit_Destructured(elt)
        return dnode

    def visit_List(self, node):
        if isinstance(node.ctx, ast.Load):
            dnode = self.chains.setdefault(node, Def(node))
            for elt in node.elts:
                self.visit(elt).add_user(dnode)
            return dnode
        # unfortunately, destructured node are marked as Load,
        # only the parent List/Tuple is marked as Store
        elif isinstance(node.ctx, ast.Store):
            return self.visit_Destructured(node)

    visit_Tuple = visit_List

    # slice

    def visit_Slice(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.lower:
            self.visit(node.lower).add_user(dnode)
        if node.upper:
            self.visit(node.upper).add_user(dnode)
        if node.step:
            self.visit(node.step).add_user(dnode)
        return dnode

    # misc

    def visit_comprehension(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.iter).add_user(dnode)
        self.visit(node.target)
        for if_ in node.ifs:
            self.visit(if_).add_user(dnode)
        return dnode

    def visit_excepthandler(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.type:
            self.visit(node.type).add_user(dnode)
        if node.name:
            self.visit(node.name).add_user(dnode)
        self.process_body(node.body)
        return dnode

    def visit_arguments(self, node):
        for arg in node.args:
            self.visit(arg)

        for arg in node.posonlyargs:
            self.visit(arg)

        if node.vararg:
            self.visit(node.vararg)

        for arg in node.kwonlyargs:
            self.visit(arg)
        if node.kwarg:
            self.visit(node.kwarg)

    def visit_withitem(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.context_expr).add_user(dnode)
        if node.optional_vars:
            self.visit(node.optional_vars)
        return dnode


class UseDefChains(object):
    """
    DefUseChains adaptor that builds a mapping between each user
    and the Def that defines this user:
        - chains: Dict[node, List[Def]], a mapping between nodes and the Defs
          that define it.
    """

    def __init__(self, defuses):
        self.chains = {}
        for chain in defuses.chains.values():
            if isinstance(chain.node, ast.Name):
                self.chains.setdefault(chain.node, [])
            for use in chain.users():
                self.chains.setdefault(use.node, []).append(chain)

        for chain in defuses._builtins.values():
            for use in chain.users():
                self.chains.setdefault(use.node, []).append(chain)

    def __str__(self):
        out = []
        for k, uses in self.chains.items():
            kname = Def(k).name()
            kstr = "{} <- {{{}}}".format(
                kname, ", ".join(sorted(use.name() for use in uses))
            )
            out.append((kname, kstr))
        out.sort()
        return ", ".join(s for k, s in out)


if __name__ == "__main__":
    import sys

    class Beniget(ast.NodeVisitor):
        def __init__(self, filename, module):
            super(Beniget, self).__init__()

            self.filename = filename or "<stdin>"

            self.ancestors = Ancestors()
            self.ancestors.visit(module)

            self.defuses = DefUseChains(self.filename)
            self.defuses.visit(module)

            self.visit(module)

        def check_unused(self, node, skipped_types=()):
            for local_def in self.defuses.locals[node]:
                if not local_def.users():
                    if local_def.name() == "_":
                        continue  # typical naming by-pass
                    if isinstance(local_def.node, skipped_types):
                        continue

                    location = local_def.node
                    while not hasattr(location, "lineno"):
                        location = self.ancestors.parent(location)

                    if isinstance(location, ast.ImportFrom):
                        if location.module == "__future__":
                            continue

                    print(
                        "W: '{}' is defined but not used at {}:{}:{}".format(
                            local_def.name(),
                            self.filename,
                            location.lineno,
                            location.col_offset,
                        )
                    )

        def visit_Module(self, node):
            self.generic_visit(node)
            if self.filename.endswith("__init__.py"):
                return
            self.check_unused(
                node, skipped_types=(ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Name)
            )

        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            self.check_unused(node)

    paths = sys.argv[1:] or (None,)

    for path in paths:
        with open(path) if path else sys.stdin as target:
            module = ast.parse(target.read())
            Beniget(path, module)
