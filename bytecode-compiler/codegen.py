"""
Byte-compile an almost-reasonable subset of Python.
"""

import ast, collections, dis, types
from functools import reduce
from assembler import op, assemble
from scoper import top_scope

def byte_compile(source, f_globals, loud=0):
    t = ast.parse(source)
    t = Expander().visit(t)
    top_level = top_scope(t, source, loud)
    return types.FunctionType(CodeGen(top_level).compile(t), f_globals)

class Expander(ast.NodeTransformer):
    def visit_Assert(self, t):
        return ast.If(ast.UnaryOp(ast.Not(), t.test),
                      [ast.Raise(ast.Call(ast.Name('AssertionError', ast.Load()),
                                          [] if t.msg is None else [t.msg],
                                          [], None, None),
                                 None)],
                      [])

class CodeGen(ast.NodeVisitor):

    def __init__(self, scope):
        self.scope = scope
        self.constants = make_table()
        self.names     = make_table()
        self.varnames  = make_table()

    def compile_class(self, t):
        assembly = [self.load('__name__'), self.store('__module__'),
                    self.load_const(t.name), self.store('__qualname__'), # XXX
                    self(t.body), self.load_const(None), op.RETURN_VALUE]
        return self.make_code(assembly, 0)

    def compile_function(self, t):
        stmt0 = t.body[0]
        if not (isinstance(stmt0, ast.Expr) and isinstance(stmt0.value, ast.Str)):
            self.load_const(None) # The doc comment starts the constant table.
        for arg in t.args.args:
            self.varnames[arg.arg] # argh, naming
        return self.compile(t.body, len(t.args.args))

    def compile(self, t, argcount=0):
        assembly = [self(t), self.load_const(None), op.RETURN_VALUE]
        return self.make_code(assembly, argcount)

    def make_code(self, assembly, argcount):
        kwonlyargcount = 0
        nlocals = len(self.varnames)
        bytecode, stacksize = assemble(assembly)
        if 1: print('stacksize =', stacksize)
        flags = 64  # XXX I don't understand the flags
        flags |= 2 if nlocals else 0  # this is just a guess
        filename = '<stdin>'
        name = 'the_name'
        firstlineno = 1
        lnotab = b''
        return types.CodeType(argcount, kwonlyargcount, nlocals, stacksize, flags,
                              bytecode,
                              tuple(const for const,_ in collect(self.constants)),
                              collect(self.names),
                              collect(self.varnames),
                              filename, name, firstlineno, lnotab,
                              freevars=(), cellvars=())

    def __call__(self, t):
        assert isinstance(t, list) or isinstance(t, ast.AST)
        return list(map(self, t)) if isinstance(t, list) else self.visit(t)

    def load_const(self, constant):
        return op.LOAD_CONST(self.constants[constant, type(constant)])

    def generic_visit(self, t):
        assert False, t

    def visit_Module(self, t):
        return self(t.body)

    def visit_Import(self, t):
        return self(t.names)

    def visit_alias(self, t):
        return [self.load_const(0),
                self.load_const(None), # XXX not for 'importfrom'
                op.IMPORT_NAME(self.names[t.name]),
                self.store(t.asname or t.name.split('.')[0])]
        
    def visit_ClassDef(self, t):
        assert not t.decorator_list
        code = CodeGen(self.scope.get_child(t)).compile_class(t)
        return [op.LOAD_BUILD_CLASS, self.make_closure(code, t.name), 
                                     self.load_const(t.name),
                                     self(t.bases),
                op.CALL_FUNCTION(2 + len(t.bases)),
                self.store(t.name)]

    def visit_FunctionDef(self, t):
        assert not t.decorator_list
        code = CodeGen(self.scope.get_child(t)).compile_function(t)
        return [self.make_closure(code, t.name), self.store(t.name)]

    def make_closure(self, code, name):
        return [self.load_const(code), self.load_const(name), op.MAKE_FUNCTION(0)] # XXX 0?

    def visit_Return(self, t):
        return [self(t.value) if t.value else self.load_const(None),
                op.RETURN_VALUE]

    def visit_If(self, t):
        return {0: [self(t.test), op.POP_JUMP_IF_FALSE(1),
                    self(t.body), op.JUMP_FORWARD(2)],
                1: [self(t.orelse)],
                2: []}

    def visit_While(self, t):
        return {0: [op.SETUP_LOOP(3)],
                1: [self(t.test), op.POP_JUMP_IF_FALSE(2),
                    self(t.body), op.JUMP_ABSOLUTE(1)],
                2: [op.POP_BLOCK],
                3: []}

    def visit_For(self, t):
        return {0: [op.SETUP_LOOP(3), self(t.iter), op.GET_ITER],
                1: [op.FOR_ITER(2), self.store(t.target.id),
                    self(t.body), op.JUMP_ABSOLUTE(1)],
                2: [op.POP_BLOCK],
                3: []}

    def visit_Raise(self, t):
        return [self(t.exc), op.RAISE_VARARGS(1)]

    def visit_Expr(self, t):
        return [self(t.value), op.POP_TOP]

    def visit_Assign(self, t):
        assert 1 == len(t.targets)  # XXX is >1 meant for like a = b = c?
        return [self(t.value), self(t.targets)]

    def visit_Call(self, t):
        return [self(t.func), self(t.args), op.CALL_FUNCTION(len(t.args))]

    def visit_List(self, t):
        return self.visit_sequence(t, op.BUILD_LIST)

    def visit_Tuple(self, t):
        return self.visit_sequence(t, op.BUILD_TUPLE)

    def visit_sequence(self, t, build_op):
        if   isinstance(t.ctx, ast.Load):
            return [self(t.elts), build_op(len(t.elts))]
        elif isinstance(t.ctx, ast.Store):
            # XXX make sure there are no stars in elts
            return [op.UNPACK_SEQUENCE(len(t.elts)), self(t.elts)]
        else:
            assert False

    def visit_Dict(self, t):
        return [op.BUILD_MAP(len(t.keys)),
                [[self(v), self(k), op.STORE_MAP]
                 for k, v in zip(t.keys, t.values)]]

    def visit_UnaryOp(self, t):
        return [self(t.operand), self.ops1[type(t.op)]]
    ops1 = {ast.UAdd: op.UNARY_POSITIVE,  ast.Invert: op.UNARY_INVERT,
            ast.USub: op.UNARY_NEGATIVE,  ast.Not:    op.UNARY_NOT}

    def visit_BinOp(self, t):
        return [self(t.left), self(t.right), self.ops2[type(t.op)]]
    ops2 = {ast.Pow:    op.BINARY_POWER,  ast.Add:  op.BINARY_ADD,
            ast.LShift: op.BINARY_LSHIFT, ast.Sub:  op.BINARY_SUBTRACT,
            ast.RShift: op.BINARY_RSHIFT, ast.Mult: op.BINARY_MULTIPLY,
            ast.BitOr:  op.BINARY_OR,     ast.Mod:  op.BINARY_MODULO,
            ast.BitAnd: op.BINARY_AND,    ast.Div:  op.BINARY_TRUE_DIVIDE,
            ast.BitXor: op.BINARY_XOR,    ast.FloorDiv: op.BINARY_FLOOR_DIVIDE}

    def visit_Compare(self, t):
        assert 1 == len(t.ops)
        return [self(t.left), self(t.comparators[0]),
                op.COMPARE_OP(dis.cmp_op.index(self.ops_cmp[type(t.ops[0])]))]
    ops_cmp = {ast.Eq: '==', ast.NotEq: '!=', ast.Is: 'is', ast.IsNot: 'is not',
               ast.Lt: '<',  ast.LtE:   '<=', ast.In: 'in', ast.NotIn: 'not in',
               ast.Gt: '>',  ast.GtE:   '>='}

    def visit_BoolOp(self, t):
        op_jump = self.ops_bool[type(t.op)]
        def compound(left, right):
            return {0: [left, op_jump(1), right],
                    1: []}
        return reduce(compound, map(self, t.values))
    ops_bool = {ast.And: op.JUMP_IF_FALSE_OR_POP,
                ast.Or:  op.JUMP_IF_TRUE_OR_POP}

    def visit_Pass(self, t):
        return []

    def visit_Break(self, t):
        return op.BREAK_LOOP

    def visit_Num(self, t):
        return self.load_const(t.n)

    def visit_Str(self, t):
        return self.load_const(t.s)

    visit_Bytes = visit_Str

    def visit_Attribute(self, t):
        if   isinstance(t.ctx, ast.Load):
            return [self(t.value), op.LOAD_ATTR(self.names[t.attr])]
        elif isinstance(t.ctx, ast.Store):
            return [self(t.value), op.STORE_ATTR(self.names[t.attr])]
        else:
            assert False

    def visit_Subscript(self, t):
        if isinstance(t.slice, ast.Index):
            if   isinstance(t.ctx, ast.Load):  sub_op = op.BINARY_SUBSCR
            elif isinstance(t.ctx, ast.Store): sub_op = op.STORE_SUBSCR
            else: assert False
            return [self(t.value), self(t.slice.value), sub_op]
        else:
            assert False

    def visit_NameConstant(self, t):
        return self.load_const(t.value)

    def visit_Name(self, t):
        if   isinstance(t.ctx, ast.Load):  return self.load(t.id)
        elif isinstance(t.ctx, ast.Store): return self.store(t.id)
        else: assert False

    def load(self, name):
        level = self.scope.scope(name)
        if   level == 'fast':   return op.LOAD_FAST(self.varnames[name])
        elif level == 'global': return op.LOAD_GLOBAL(self.names[name])
        elif level == 'name':   return op.LOAD_NAME(self.names[name])
        else: assert False

    def store(self, name):
        level = self.scope.scope(name)
        if   level == 'fast':   return op.STORE_FAST(self.varnames[name])
        elif level == 'global': return op.STORE_GLOBAL(self.names[name])
        elif level == 'name':   return op.STORE_NAME(self.names[name])
        else: assert False

def make_table():
    table = collections.defaultdict(lambda: len(table))
    return table

def collect(table):
    return tuple(sorted(table, key=table.get))
