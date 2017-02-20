import sys
import traceback
import textwrap
import warnings
import types
from contextlib import contextmanager

import attr

from .._util import run_sync_coro

__all__ = ["MultiError", "format_exception_multi"]

################################################################
# MultiError
################################################################

def _filter_impl(handler, root_exc):
    # We have a tree of MultiError's, like:
    #
    #  MultiError([
    #      ValueError,
    #      MultiError([
    #          KeyError,
    #          ValueError,
    #      ]),
    #  ])
    #
    # or similar.
    #
    # We want to
    # 1) apply the filter to each of the leaf exceptions -- each leaf
    #    might stay the same, be replaced (with the original exception
    #    potentially sticking around as __context__ or __cause__), or
    #    disappear altogether.
    # 2) simplify the resulting tree -- remove empty nodes, and replace
    #    singleton MultiError's with their contents, e.g.:
    #        MultiError([KeyError]) -> KeyError
    #    (This can happen recursively, e.g. if the two ValueErrors above
    #    get caught then we'll just be left with a bare KeyError.)
    # 3) preserve sensible tracebacks
    #
    # It's the tracebacks that are most confusing. As a MultiError
    # propagates through the stack, it accumulates traceback frames, but
    # the exceptions inside it don't. Semantically, the traceback for a
    # leaf exception is the concatenation the tracebacks of all the
    # exceptions you see when traversing the exception tree from the root
    # to that leaf. Our correctness invariant is that this concatenated
    # traceback should be the same before and after.
    #
    # The easy way to do that would be to, at the beginning of this
    # function, "push" all tracebacks down to the leafs, so all the
    # MultiErrors have __traceback__=None, and all the leafs have complete
    # tracebacks. But whenever possible, we'd actually prefer to keep
    # tracebacks as high up in the tree as possible, because this lets us
    # keep only a single copy of the common parts of these exception's
    # tracebacks. This is cheaper (in memory + time -- tracebacks are
    # unpleasantly quadratic-ish to work with, and this might matter if
    # you have thousands of exceptions, which can happen e.g. after
    # cancelling a large task pool, and no-one will ever look at their
    # tracebacks!), and more importantly, factoring out redundant parts of
    # the tracebacks makes them more readable if/when users do see them.
    #
    # So instead our strategy is:
    # - first go through and construct the new tree, preserving any
    #   unchanged subtrees
    # - then go through the original tree (!) and push tracebacks down
    #   until either we hit a leaf, or we hit a subtree which was
    #   preserved in the new tree.

    # This used to also support async handler functions. But that runs into:
    #   https://bugs.python.org/issue29600
    # which is difficult to fix on our end.

    # XX Problems:
    # - handler adds a stack frame, might add several
    #   potential solutions:
    #   - push down all stack frames in the naive way
    #   - somehow stop the handler from doing that
    #     - do type checking before calling handler (but this doesn't help
    #       for common cancel_scope case)
    #     - pass exception object into the handler normally, without this
    #       yield cleverness
    #     - just unconditionally throw out the new stack frames
    #       - maybe synthetically adding them to the root MultiError?
    #
    # - it's impossible to throw an exception out of 'with' without
    #   exception chaining kicking in
    #   ...I guess we could use ctypes to clear exc_info (only if exc
    #   passed into __exit__ matches sys.exc_info() -- we *do* want to
    #   chain if the *whole block* is in an except:)
    #   (just PyErr_SetExcInfo(0, 0, 0))
    #   ...which it always will if we have an exception at all

    # Filters a subtree, ignoring tracebacks, while keeping a record of
    # which MultiErrors were preserved unchanged
    def filter_tree(exc, preserved):
        if isinstance(exc, MultiError):
            new_exceptions = []
            changed = False
            for child_exc in exc.exceptions:
                new_child_exc = filter_tree(child_exc, preserved)
                if new_child_exc is not child_exc:
                    changed = True
                if new_child_exc is not None:
                    new_exceptions.append(new_child_exc)
            if not new_exceptions:
                return None
            elif changed:
                return MultiError(new_exceptions)
            else:
                preserved.add(exc)
                return exc
        else:
            return handler(exc)

    def push_tb_down(tb, exc, preserved):
        if exc in preserved:
            return
        new_tb = concat_tb(tb, exc.__traceback__)
        if isinstance(exc, MultiError):
            for child_exc in exc.exceptions:
                push_tb_down(new_tb, child_exc, preserved)
            exc.__traceback__ = None
        else:
            exc.__traceback__ = new_tb

    preserved = set()
    new_root_exc = filter_tree(root_exc, preserved)
    push_tb_down(None, root_exc, preserved)
    return new_root_exc


# Normally I'm a big fan of (a)contextmanager, but in this case I found it
# easier to use the raw context manager protocol, because it makes it a lot
# easier to reason about how we're mutating the traceback as we go. (End
# result: if the exception gets modified, then the 'raise' here makes this
# frame show up in the traceback; otherwise, we leave no trace.)
@attr.s(frozen=True)
class MultiErrorCatch:
    _handler = attr.ib()

    def __enter__(self):
        pass

    def __exit__(self, etype, exc, tb):
        if exc is not None:
            filtered_exc = MultiError.filter(self._handler, exc)
            if filtered_exc is exc:
                # Let the interpreter re-raise it
                return False
            if filtered_exc is None:
                # Swallow the exception
                return True
            if (filtered_exc.__cause__ is None
                  and filtered_exc.__context__ is None):
                # We can't stop Python from setting __context__, but we can
                # hide it
                filtered_exc.__suppress_context__ = True
            raise filtered_exc

class MultiError(BaseException):
    def __new__(cls, exceptions):
        if len(exceptions) == 1:
            return exceptions[0]
        else:
            self = BaseException.__new__(cls)
            self.exceptions = exceptions
            return self

    def __str__(self):
        def format_child(exc):
            #return "{}: {}".format(exc.__class__.__name__, exc)
            return repr(exc)
        return ", ".join(format_child(exc) for exc in self.exceptions)

    def __repr__(self):
        return "<MultiError: {}>".format(self)

    @classmethod
    def filter(cls, handler, root_exc):
        return _filter_impl(handler, root_exc)

    @classmethod
    def catch(cls, handler):
        return MultiErrorCatch(handler)


################################################################
# concat_tb
################################################################

# We need to compute a new traceback that is the concatenation of two existing
# tracebacks. This requires copying the entries in 'head' and then pointing
# the final tb_next to 'tail'.
#
# NB: 'tail' might be None, which requires some special handling in the ctypes
# version.
#
# The complication here is that Python doesn't actually support copying or
# modifying traceback objects, so we have to get creative...
#
# On CPython, we use ctypes. On PyPy, we use "transparent proxies".
#
# Jinja2 is a useful source of inspiration:
#   https://github.com/pallets/jinja/blob/master/jinja2/debug.py

try:
    import tputil
except ImportError:
    have_tproxy = False
else:
    have_tproxy = True

if have_tproxy:
    # http://doc.pypy.org/en/latest/objspace-proxies.html
    def copy_tb(base_tb, tb_next):
        def controller(operation):
            if operation.opname in ["__getattribute__", "__getattr__"]:
                if operation.args[0] == "tb_next":
                    return tb_next
            return operation.delegate()
        return tputil.make_proxy(controller, type(base_tb), base_tb)
else:
    # ctypes it is
    import ctypes
    # How to handle refcounting? I don't want to use ctypes.py_object because
    # I don't understand or trust it, and I don't want to use
    # ctypes.pythonapi.Py_{Inc,Dec}Ref because we might clash with user code
    # that also tries to use them but with different types. So private _ctypes
    # APIs it is!
    import _ctypes

    class CTraceback(ctypes.Structure):
        _fields_ = [
            ("PyObject_HEAD", ctypes.c_byte * object().__sizeof__()),
            ("tb_next", ctypes.c_void_p),
            ("tb_frame", ctypes.c_void_p),
            ("tb_lasti", ctypes.c_int),
            ("tb_lineno", ctypes.c_int),
        ]

    def copy_tb(base_tb, tb_next):
        # TracebackType has no public constructor, so allocate one the hard way
        try:
            raise ValueError
        except ValueError as exc:
            new_tb = exc.__traceback__
        c_new_tb = CTraceback.from_address(id(new_tb))

        # At the C level, tb_next either pointer to the next traceback or is
        # NULL. c_void_p and the .tb_next accessor both convert NULL to None,
        # but we shouldn't DECREF None just because we assigned to a NULL
        # pointer! Here we know that our new traceback has only 1 frame in it,
        # so we can assume the tb_next field is NULL.
        assert c_new_tb.tb_next is None
        # If tb_next is None, then we want to set c_new_tb.tb_next to NULL,
        # which it already is, so we're done. Otherwise, we have to actually
        # do some work:
        if tb_next is not None:
            _ctypes.Py_INCREF(tb_next)
            c_new_tb.tb_next = id(tb_next)

        assert c_new_tb.tb_frame is not None
        _ctypes.Py_INCREF(base_tb.tb_frame)
        old_tb_frame = new_tb.tb_frame
        c_new_tb.tb_frame = id(base_tb.tb_frame)
        _ctypes.Py_DECREF(old_tb_frame)

        c_new_tb.tb_lasti = base_tb.tb_lasti
        c_new_tb.tb_lineno = base_tb.tb_lineno

        return new_tb

def concat_tb(head, tail):
    # We have to use an iterative algorithm here, because in the worst case
    # this might be a RecursionError stack that is by definition too deep to
    # process by recursion!
    head_tbs = []
    pointer = head
    while pointer is not None:
        head_tbs.append(pointer)
        pointer = pointer.tb_next
    current_head = tail
    for head_tb in reversed(head_tbs):
        current_head = copy_tb(head_tb, tb_next=current_head)
    return current_head

################################################################
# MultiError traceback formatting
################################################################

# format_exception's semantics for limit= are odd: they apply separately to
# each traceback. I'm not sure how much sense this makes, but we copy it
# anyway.
def format_exception_multi(etype, value, tb, *, limit=None, chain=True):
    "Like traceback.format_exception, but supports MultiErrors."
    return _format_exception_multi(set(), etype, value, tb, limit, chain)

def _format_exception_multi(seen, etype, value, tb, limit, chain):
    if id(value) in seen:
        return ["<previously printed exception {!r}>\n".format(value)]
    seen.add(id(value))

    chunks = []
    if chain:
        if value.__cause__ is not None:
            v = value.__cause__
            chunks += _format_exception_multi(
                seen, type(v), v, v.__traceback__, limit=limit, chain=True)
            chunks += [
                "\nThe above exception was the direct cause of the "
                "following exception:\n\n",
            ]
        elif value.__context__ is not None and not value.__suppress_context__:
            v = value.__context__
            chunks += _format_exception_multi(
                seen, type(v), v, v.__traceback__, limit=limit, chain=True)
            chunks += [
                "\nDuring handling of the above exception, another "
                "exception occurred:\n\n",
            ]

    chunks += traceback.format_exception(
        etype, value, tb, limit=limit, chain=False)

    if isinstance(value, MultiError):
        for i, exc in enumerate(value.exceptions):
            chunks += [
                "\nDetails of embedded exception {}:\n\n".format(i + 1),
            ]
            sub_chunks = _format_exception_multi(
                seen, type(exc), exc, exc.__traceback__, limit=limit, chain=chain)
            for chunk in sub_chunks:
                chunks.append(textwrap.indent(chunk, " " * 2))

    return chunks

def excepthook_multi(etype, value, tb):
    for chunk in format_exception_multi(etype, value, tb):
        sys.stderr.write(chunk)

IPython_handler_installed = False
warning_given = False
if "IPython" in sys.modules:
    import IPython
    ip = IPython.get_ipython()
    if ip is not None:
        if ip.custom_exceptions != ():
            warnings.warn(
                "IPython detected, but you already have a custom exception "
                "handler installed.\n"
                "I'll skip installing trio's custom handler, but this means "
                "MultiErrors will not show full tracebacks.")
            warning_given = True
        else:
            def show_multi_traceback(self, etype, value, tb, tb_offset=None):
                # XX it would be better to integrate with IPython's fancy
                # exception formatting stuff (and not ignore tb_offset)
                multi_excepthook(etype, value, tb)
            ip.set_custom_exc((MultiError,), show_multi_traceback)
            IPython_handler_installed = True

if sys.excepthook is sys.__excepthook__:
    sys.excepthook = excepthook_multi
else:
    if not IPython_handler_installed and not warning_given:
        warnings.warn(
            "You seem to already have a custom sys.excepthook handler\n"
            "installed. I'll skip installing trio's custom handler, but this\n"
            "means MultiErrors will not show full tracebacks.")
