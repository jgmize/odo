"""
A blaze backend that generates Q code
"""

from __future__ import absolute_import, print_function, division

import numbers
import datetime

import pandas as pd

from toolz.compatibility import zip
from toolz import map, first, second

from blaze import resource, compute

from blaze.dispatch import dispatch

from blaze.compute.core import compute
from blaze.expr import Symbol, Projection, Selection, Field
from blaze.expr import BinOp, UnaryOp, Expr, Reduction, By, Join, Head, Sort
from blaze.expr import Slice, Distinct, Summary, nelements
from blaze.expr import DateTime, Millisecond, Microsecond
from blaze.expr.datetime import Minute

from datashape.predicates import isrecord

from .. import q
from .qtable import QTable


qdatetimes = {
    'day': 'dd',
    'month': 'mm',
    'hour': 'hh',
    'second': 'ss',
}


def get(x):
    """Get a q atom from a single element list or return the list.

    Parameters
    ----------
    x : q.Expr
        A Q expression

    Returns
    -------
    r: q.Expr

    Examples
    --------
    >>> s = q.List(q.Atom('='), q.Symbol('t.name'), q.Symbol('Alice'))
    >>> s
    (=; `t.name; `Alice)
    >>> get(s)
    (=; `t.name; `Alice)
    >>> s = q.List(q.Symbol('t.name'))
    >>> get(s)
    `t.name
    """
    if len(x) == 1:
        return x[0]
    return x


def desubs(expr, t):
    """Remove a particular table `t` from an expression.

    TODO
    ----
    Is looking at the name of the table sufficient?

    Examples
    --------
    >>> import blaze as bz
    >>> s = q.Symbol('t.name')
    >>> desubs(s, 't')
    `name
    >>> s = q.List(q.Atom('first'), q.Symbol('t.name'))
    >>> s
    (first; `t.name)
    >>> desubs(s, 't')
    (first; `name)
    """
    # ignore the question mark needed for select, that's why we use *args[1:]
    result_type = {q.select: lambda *args: q.select(*args[1:])}
    result = list(_desubs(expr, t))
    return get(result_type.get(type(expr), q.List)(*result))


def compute_atom(atom, symbol):
    s = getattr(atom, 'str', atom.s)
    split = s.split('.', 1)
    if '.' in s and first(split) == symbol:
        return type(atom)(second(split))
    return atom


def _desubs(expr, t):
    if isinstance(expr, q.Atom):
        yield compute_atom(expr, t)
    elif isinstance(expr, (basestring, numbers.Number, q.Bool)):
        yield expr
    else:
        for sube in expr:
            if isinstance(sube, q.Atom):
                yield compute_atom(sube, t)
            elif isinstance(sube, q.List):
                yield q.List(*(desubs(s, t) for s in sube))
            elif isinstance(sube, q.Dict):
                yield q.Dict([(desubs(k, t), desubs(v, t))
                              for k, v in sube.items()])
            else:  # isinstance(sube, (basestring, numbers.Number, q.Bool)):
                yield sube


@dispatch(q.Atom, datetime.datetime)
def into(atom, d, **kwargs):
    # if we have a date only do the proper q conversion
    if pd.Timestamp(d) == pd.Timestamp(d.date()):
        return into(atom, d.date())
    return q.Atom(d.strftime('%Y.%m.%dD%H:%M:%S.%f000'))


@dispatch(q.Atom, datetime.date)
def into(atom, d, **kwargs):
    return q.Atom(d.strftime('%Y.%m.%d'))


@dispatch(Projection, q.Expr)
def compute_up(expr, data, **kwargs):
    fields = list(map(q.Symbol, expr.fields))
    return q.select(data, aggregates=q.Dict(list(zip(fields, fields))))


@dispatch(BinOp, q.Expr, q.Expr)
def compute_up(expr, lhs, rhs, **kwargs):
    op = q.binops[expr.symbol]
    return op(lhs, rhs)


def qify(x):
    """Deal with putting q symbols in the AST.

    Examples
    --------
    >>> from blaze import Symbol
    >>> s = Symbol('s', 'var * {amount: float64, name: string}')
    >>> expr = s.name == 'Alice'
    >>> result = qify(expr.rhs)
    >>> result
    (,:[`Alice])
    >>> qify(1)
    1
    >>> qify('2014-01-02')
    2014.01.02
    >>> qify(pd.Timestamp('2014-01-02'))
    2014.01.02
    """
    assert not isinstance(x, Expr), 'input cannot be a blaze expression'
    if isinstance(x, basestring):
        try:
            return into(q.Atom, pd.Timestamp(x))
        except ValueError:
            return q.List(q.Symbol(x))
    elif isinstance(x, (datetime.date, datetime.datetime)):
        return into(q.Atom, x)
    else:
        return x


@dispatch(BinOp, q.Expr)
def compute_up(expr, data, **kwargs):
    op = q.binops[expr.symbol]
    if isinstance(expr.lhs, Expr):
        lhs, rhs = data, qify(expr.rhs)
    else:
        lhs, rhs = qify(expr.lhs), data
    return op(lhs, rhs)


@dispatch(Reduction, q.Expr)
def compute_up(expr, data, **kwargs):
    if expr.axis != (0,):
        raise ValueError("Axis keyword arugment on reductions not supported")
    return q.unops[expr.symbol](data)


@dispatch(UnaryOp, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.unops[expr.symbol](data)


@dispatch(Field, q.Expr)
def compute_up(expr, data, **kwargs):
    sym = q.Symbol(expr._name)

    try:
        return data[expr._name]
    except TypeError:
        return q.slice(data, sym)


@dispatch(Selection, q.Expr)
def compute_up(expr, data, **kwargs):
    # template: ?[selectable, predicate or list of predicates, by, aggregations]
    predicate = compute(expr.predicate, {expr._child: data})
    return q.select(data, constraints=q.List(q.List(predicate)))


@dispatch(DateTime, q.Expr)
def compute_up(expr, data, **kwargs):
    attr = expr.attr
    attr = qdatetimes.get(attr, attr)
    return data[attr]


@dispatch(Microsecond, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.floor(q.div(q.mod(q.long(data), 1000000000), 1000))


@dispatch(Millisecond, q.Expr)
def compute_up(expr, data, **kwargs):
    return compute(expr._child.microsecond // 1000, data)


@dispatch(Minute, q.Expr)
def compute_up(expr, data, **kwargs):
    # q has mm for time types and mm for datetime and date types, this makes -1
    # amount of sense, so we bypass that and compute it our damn selves using
    # (`long$expr.minute) mod 60
    return q.mod(q.long(data[expr.attr]), 60)


@dispatch(Join, q.Expr, q.Expr)
def compute_up(expr, lhs, rhs, **kwargs):
    if expr.how != 'inner':
        raise NotImplementedError('only inner joins supported')
    if expr._on_left != expr._on_right:
        raise NotImplementedError('can only join on same named columns')
    return q.List('ej', q.symlist(expr._on_left), lhs, rhs)


@dispatch(Sort, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.sort(data, expr._key, expr.ascending)


@dispatch(Summary, q.Expr)
def compute_up(expr, data, **kwargs):
    ops = [compute(op, {expr._child: data}) for op in expr.values]
    aggregates = q.Dict(list(zip(map(q.Symbol, expr.names), ops)))
    return desubs(q.select(data, aggregates=aggregates), expr._leaves()[0])


@dispatch(By, q.Expr)
def compute_up(expr, data, **kwargs):
    if isinstance(data, q.select):  # we are combining multiple selects
        child = data.child
        constraints = data.constraints
    else:
        child = data
        constraints = q.List()
    grouper = compute(expr.grouper, child)
    grouper = q.Dict([(q.Symbol(expr.grouper._name), grouper)])
    aggregates = compute(expr.apply, child).aggregates
    select = q.select(child, q.List(constraints), grouper, aggregates)
    return desubs(select, child.s)


def nrows(expr, data, **kwargs):
    return compute(expr._child.nrows, data)


@dispatch(nelements, q.Expr)
def compute_down(expr, data, **kwargs):
    if expr.axis != (0,):
        raise ValueError("axis == 1 not supported on record types")
    if getattr(data, 'fields', ()) and not isinstance(data, q.select):
        return q.count(q.Symbol(data.s))
    return q.count(data)


@dispatch(Head, q.Expr)
def compute_up(expr, data, **kwargs):
    n = expr.n

    # q repeats if the N of take is larger than the number of rows, so we
    # need to get the min of the number of rows and the requested N from the
    # Head expression

    # & in q is min for 2 arguments
    final_index = q.and_(n, nrows(expr, data, **kwargs))

    if data.is_partitioned:
        return q.partake(data, q.til(final_index))
    return q.take(final_index, data)


@dispatch(numbers.Integral, q.Expr, q.Expr)
def compute_slice(index, child, nrows, dshape=None):
    if index < 0:
        index = q.add(index, nrows)

    qexpr = q.List(child, index)

    if not isrecord(dshape):
        return qexpr
    return q.List(',:', qexpr)


@dispatch(slice, q.Expr, q.Expr)
def compute_slice(index, child, nrows, dshape=None):
    start = index.start or 0
    stop = index.stop or nrows

    if start < 0:
        start = q.add(start, nrows)

    if stop < 0:
        stop = q.add(stop, nrows)

    return q.List('@', child, q.add(start, q.til(q.sub(stop, start))))


@dispatch(Slice, q.Expr)
def compute_up(expr, data, **kwargs):
    """Slice expressions from Python to Q.

    Notes
    -----
    ``sublist`` is actually defined in K land so we have to jump through hoops
    to actually evaluate it properly.

    In Q::

        r: X sublist Y
        3 sublist 1 2 3 4 5 = 1 2 3
        1 3 sublist 1 2 3 4 5 = 2 3 4
        x = [1, 2, 3, 4, 5]
        Y[2:5] == 2 3 sublist Y
        Y[a:b] == a (b - a) sublist Y
    """
    assert len(expr.index) == 1, 'only single slice allowed'
    index, = expr.index
    rowcount = nrows(expr, data, **kwargs)
    return compute_slice(index, data, rowcount, dshape=expr.dshape)


@dispatch(Distinct, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.distinct(data)


@dispatch(Expr, QTable, QTable)
def compute_down(expr, lhs, rhs, **kwargs):
    # TODO: this is an anti-pattern
    # we should probably evaluate on the Q database
    lhs_leaf = expr._leaves()[0]
    rhs_leaf = expr._leaves()[1]
    new_lhs_leaf = Symbol(lhs.tablename, lhs_leaf.dshape)
    new_rhs_leaf = Symbol(rhs.tablename, rhs_leaf.dshape)
    new_expr = expr._subs({lhs_leaf: new_lhs_leaf, rhs_leaf: new_rhs_leaf})
    scope = {new_lhs_leaf: lhs._qsymbol, new_rhs_leaf: rhs._qsymbol}
    result_expr = compute(new_expr, scope)  # Return q.Expr, not data
    result = lhs.eval(result_expr)
    return result


@dispatch(Field, QTable)
def compute_down(expr, data, **kwargs):
    leaf = expr._leaves()[0]
    new_leaf = Symbol(data.tablename, leaf.dshape)
    new_expr = expr._subs({leaf: new_leaf})
    data_leaf = data._qsymbol

    if data_leaf.is_partitioned or data_leaf.is_splayed:
        result_expr = compute(new_expr._child[[new_expr._name]],
                              {new_leaf: data_leaf})
    else:
        # Return q.Expr, not data
        result_expr = compute(new_expr, {new_leaf: data_leaf})

    return data.eval(result_expr).squeeze()


@dispatch(Expr, QTable)
def compute_down(expr, data, **kwargs):
    leaf = expr._leaves()[0]
    new_leaf = Symbol(data.tablename, leaf.dshape)
    new_expr = expr._subs({leaf: new_leaf})
    data_leaf = data._qsymbol

    result_expr = compute(new_expr,
                          {new_leaf: data_leaf})  # Return q.Expr, not data
    result = data.eval(result_expr)
    if isinstance(result, pd.Series):
        result.name = expr._name
    return result


@resource.register('kdb://.+', priority=13)
def resource_kdb(uri, tablename, **kwargs):
    return QTable(uri, tablename=tablename, **kwargs)


@dispatch(pd.DataFrame, QTable)
def into(_, t, **kwargs):
    return t.eval(t.tablename)
