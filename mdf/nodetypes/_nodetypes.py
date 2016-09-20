import inspect
import types
import sys
import cython

from ..nodes import (
    MDFNode,
    MDFEvalNode,
    MDFIterator,
    MDFIteratorFactory,
    _isgeneratorfunction,
    _is_member_of,
    _get_func_name
)
from ..ctx_pickle import _unpickle_custom_node, _pickle_custom_node

_python_version = cython.declare(int, sys.version_info[0])


@cython.cfunc
def dict_iteritems(d):
    if _python_version > 2:
        return iter(d.items())
    return d.iteritems()


class MDFCustomNodeIteratorFactory(MDFIteratorFactory):

    def __init__(self, custom_node):
        self.custom_node = custom_node

    @property
    def func(self):
        return self.custom_node._custom_iterator_func

    @property
    def func_doc(self):
        func_doc = getattr(self.func, "func_doc", None)
        if func_doc is None:
            func_doc = getattr(self.func, "__doc__", None)
        return func_doc

    @property
    def node_type_func(self):
        return self.custom_node._custom_iterator_node_type_func

    def __call__(self):
        return MDFCustomNodeIterator(self.custom_node)


class MDFCustomNodeIterator(MDFIterator):

    def __init__(self, custom_node):
        self.custom_node = custom_node
        self.func = custom_node._custom_iterator_func
        self.node_type_func = self.custom_node._custom_iterator_node_type_func

        self.value_generator = None
        self.is_generator = _isgeneratorfunction(self.func)

        self.node_type_is_generator = _isgeneratorfunction(self.node_type_func)
        self.node_type_generator = None

    def __reduce__(self):
        return (
            _unpickle_custom_node_iterator,
            _pickle_custom_node_iterator(self),
            None,
            None,
            None
        )

    def __iter__(self):
        return self

    def next(self):
        if self.custom_node._call_with_no_value:
            value = None
        else:
            if self.is_generator:
                if not self.value_generator:
                    self.value_generator = self.func()
                value = next(self.value_generator)
            else:
                value = self.func()

        if self.node_type_is_generator:
            if not self.node_type_generator:
                # create the new node type generator and return
                kwargs = self.custom_node._get_kwargs()
                self.node_type_generator = self.node_type_func(value, **kwargs)
                return next(self.node_type_generator)

            return self.node_type_generator.send(value)

        # node type is plain function
        kwargs = self.custom_node._get_kwargs()
        return self.custom_node._custom_iterator_node_type_func(value, **kwargs)



def _unpickle_custom_node_iterator(custom_node, value_generator, node_type_generator):
    self = cython.declare(MDFCustomNodeIterator)
    self = MDFCustomNodeIterator(custom_node)
    self.value_generator = value_generator
    self.node_type_generator = node_type_generator
    return self


def _pickle_custom_node_iterator(self_):
    self = cython.declare(MDFCustomNodeIterator)
    self = self_
    return (
        self.custom_node,
        self.value_generator,
        self.node_type_generator,
    )


class MDFCustomNode(MDFEvalNode):
    """
    subclass of MDFEvalNode that forms the base for all over custom
    node types.
    """
    # override this in a subclass if any kwargs should be passed as nodes
    # instead of being evaluated
    nodetype_node_kwargs = set()
    
    # override this is the function being wrapped by the subclass can't
    # be inspected (eg is a cython function) and it takes some keyword
    # arguments.
    nodetype_kwargs = None

    # if set to True on the subclass the first parameter passed to the node
    # type function will always be none and the underlying node won't be
    # evaluated.
    call_with_no_value = False

    def __init__(self,
                    func,
                    node_type_func=None,
                    name=None,
                    short_name=None,
                    fqname=None,
                    cls=None,
                    category=None,
                    filter=None,
                    base_node=None, # set if created via MDFCustomNodeMethod
                    base_node_method_name=None,
                    nodetype_func_kwargs={}):
        if isinstance(func, MDFCustomNodeIteratorFactory):
            node_type_func = func.node_type_func
            func = func.func
        self._base_node = base_node
        self._base_node_method_name = base_node_method_name
        self._node_type_func = node_type_func
        self._cn_func = self._validate_func(func)
        self._category = category
        self._kwargs = dict(nodetype_func_kwargs)
        self._kwnodes = dict([(k, v) for (k, v) in dict_iteritems(nodetype_func_kwargs) if isinstance(v, MDFNode)])
        self._kwfuncs = {} # reserved for functions added via decorators

        # if 'filter_node_value' is in the node type generator args we pass in the value of the filter
        kwargs = self._get_nodetype_func_kwargs(False)
        self._call_with_filter_node = "filter_node" in kwargs
        self._call_with_filter = "filter_node_value" in kwargs
        self._call_with_self = "owner_node" in kwargs
        self._call_with_no_value = self.call_with_no_value

        eval_func = self._cn_eval_func
        if _isgeneratorfunction(node_type_func) or _isgeneratorfunction(func):
            eval_func = MDFCustomNodeIteratorFactory(self)

        MDFEvalNode.__init__(self,
                             eval_func,
                             name=name or self._get_func_name(func),
                             short_name=short_name,
                             fqname=fqname,
                             cls=cls,
                             category=category,
                             filter=filter)

        # set func_doc from the inner function's docstring
        self.func_doc = getattr(func, "func_doc", None)
        if self.func_doc is None:
            self.func_doc = getattr(func, "__doc__", None)

    def __reduce__(self):
        """support for pickling"""
        kwargs = dict(self._kwargs)

        # add filter and category to the kwargs
        filter = self.get_filter()
        if filter is not None:
            kwargs["filter"] = filter
        if self._category is not None:
            kwargs["category"] = self._category

        return (
            _unpickle_custom_node,
            _pickle_custom_node(self, self._base_node, self._base_node_method_name, kwargs),
            None,
            None,
            None,
        )

    def _get_nodetype_func_kwargs(self, remove_special=True):
        """return a list of named arguments for the node type function"""
        kwargs = self.nodetype_kwargs

        if kwargs is None:
            # try and get them from the func/iterator object
            node_type_func = self._node_type_func
            argspec = None
            try:
                kwargs = node_type_func._init_kwargs_
            except AttributeError:
                init_kwargs = None

        if kwargs is None:
            # try and get them from the func/iterator object
            if isinstance(node_type_func, types.TypeType):
                node_type_func = node_type_func.__init__
            try:
                argspec = inspect.getargspec(node_type_func).args
            except TypeError:
                return []
            kwargs = argspec[1:]

        # remove 'special' kwargs
        if remove_special:
            for special in ("filter_node", "filter_node_value", "owner_node"):
                if special in kwargs:
                    kwargs = list(kwargs)
                    kwargs.remove(special)

        return kwargs

    def __getattr__(self, attr):
        # give the superclass a go first
        try:
            # super doesn't work well with cython
            return MDFEvalNode.__getattr__(self, attr)
        except AttributeError:
            pass

        # return a decorator function for setting kwargs for the inner function
        # if attr is in the argspec for the node function
        if attr.startswith("_") or self._node_type_func is None:
            raise AttributeError(attr)

        kwargs = self._get_nodetype_func_kwargs()
        if attr not in kwargs:
            raise AttributeError(attr)

        def _make_decorator(attr):
            # the decorator takes either a value, function or node
            # do you can do things like:
            #
            # @mynodetype
            # def func():
            #    ...
            #
            # @func.some_kwarg
            # def kwarg_value():
            #    ...
            #
            #
            # If the decorator is called as a method the previously registered
            # target function is evaluated and the result is returned.
            # If the target function is a node the kwarg 'ctx' can be used
            # to provide a context to evaluate it in (otherwise the current
            # context will be used.
            #
            # e.g.:
            #
            #   func.some_kwarg()  # calls kwarg_value()
            #
            def _decorator(*args, **kwargs):
                if not args:
                    # if there are no args evaluate and return the value
                    func = self._kwargs.get(attr)
                    if func is None:
                        return None

                    if isinstance(func, MDFNode):
                        ctx = kwargs.get("ctx")
                        if ctx is not None:
                            return ctx[func]
                    return func()

                # set the function as the node's kwarg
                func, = args
                self._kwargs[attr] = func
                if isinstance(func, MDFNode):
                    self._kwnodes[attr] = func
                elif isinstance(func, types.FunctionType):
                    self._kwfuncs[attr] = func

                return func

            return _decorator

        return _make_decorator(attr)

    @property
    def node_type(self):
        """returns the name of the node type of this node"""
        try:
            return _get_func_name(self._node_type_func)
        except AttributeError:
            return "customnode"

    @property
    def func(self):
        return self._cn_func


    @property
    def base_node(self):
        """node this custom node was derived from if created via a method call."""
        return self._base_node

    #
    # Properties for use with MDFCustomNodeIterator
    #
    # The iterator uses these instead of being constructed with them
    # as it needs to be pickleable, and so by keeping a reference
    # to the node that can be unpickled more easily than a function.
    #
    @property
    def _custom_iterator_node_type_func(self):
        return self._node_type_func

    @property
    def _custom_iterator_func(self):
        return self._cn_func

    def _set_func(self, func):
        # set the underlying MDFEvalNode func
        if _isgeneratorfunction(func) or _isgeneratorfunction(self._node_type_func):
            MDFEvalNode._set_func(self, MDFCustomNodeIteratorFactory(self))
        else:
            MDFEvalNode._set_func(self, self._cn_eval_func)

        # update the docstring
        self.func_doc = getattr(func, "func_doc", None)
        if self.func_doc is None:
            self.func_doc = getattr(func, "__doc__", None)

        # set the func used by this class
        self._cn_func = func

    def _bind(self, other_evalnode, owner):
        other = cython.declare(MDFCustomNode)
        other = other_evalnode
        MDFEvalNode._bind(self, other, owner)
        self._node_type_func = other._node_type_func
        func = self._bind_function(other._cn_func, owner)
        self._set_func(func)

        # copy the kwargs
        self._kwargs = dict(other._kwargs)
        
        # bind the eval nodes (won't do anything if they're already bound)
        self._kwnodes = {}
        for k, node in dict_iteritems(other._kwnodes):
            if isinstance(node, MDFEvalNode) and _is_member_of(owner, node):
                self._kwnodes[k] = node.__get__(None, owner)
            else:
                self._kwnodes[k] = node

        # bind the functions in case they're classmethods
        self._kwfuncs = {}
        for k, func in dict_iteritems(other._kwfuncs):
            if _is_member_of(owner, func):
                self._kwfuncs[k] = self._bind_function(func, owner)

        # set the docstring for the bound node to the same as the unbound one
        self.func_doc = other.func_doc

    def _get_kwargs(self):
        kwargs = cython.declare(dict)
        kwargs = self._kwargs
        if self._kwnodes or self._kwfuncs:
            kwargs = dict(kwargs)

            node = cython.declare(MDFNode)
            for key, node in dict_iteritems(self._kwnodes):
                if key not in self.nodetype_node_kwargs:
                    kwargs[key] = node()
                else:
                    kwargs[key] = node

            for key, value in dict_iteritems(self._kwfuncs):
                kwargs[key] = value()

        # if the filter value should be passed in as a kwarg
        # add it in now (defaulting to True)
        if self._call_with_filter:
            filter_node = self.get_filter()
            filter_node_value = True
            if filter_node is not None:
                filter_node_value = filter_node()
            kwargs["filter_node_value"] = filter_node_value

        if self._call_with_filter_node:
            kwargs["filter_node"] = self.get_filter()

        if self._call_with_self:
            kwargs["owner_node"] = self

        return kwargs

    def _cn_eval_func(self):
        # get the inner node value
        if self._call_with_no_value:
            value = None
        else:
            value = self._cn_func()

        # and call the node type function
        kwargs = cython.declare(dict)
        kwargs = self._get_kwargs()
        return self._node_type_func(value, **kwargs)


class MDFCustomNodeMethod(object):
    """
    Callable object that is added to MDFNode's set of attributes
    to work as an additional method for calling node types
    directly from a node rather than explicitly creating
    derived nodes.
    
    eg: instead of:

    @delaynode(periods=10)
    def my_delayed_node():
        return my_node()
        

    it is possible to simply call:
    
    my_node.delay(periods=10)

    """
    # this class behaves like a method, but types.MethodType isn't subclass-able

    def __init__(self,
                 node_type_func,
                 node_cls,
                 method_name,
                 node=None,
                 call=True): # if call is True __call__ will call the derived node
        self._node_type_func = node_type_func
        self._node_cls = node_cls
        self._method_name = method_name
        self._node = node
        self._call = call
        self._derived_nodes = node._derived_nodes if node else {}

    def __get__(self, instance, cls=None):
        if instance and self._node is instance:
            return self

        return MDFCustomNodeMethod(self._node_type_func,
                                   self._node_cls,
                                   self._method_name,
                                   node=instance,
                                   call=self._call)

    def __repr__(self):
        # try to get the args directly from the iterator (if it is one)
        args = self._node_cls.nodetype_kwargs
        if args is None:
            try:
                args = self._node_type_func._init_kwargs_
            except AttributeError:
                args = None

        if args is None:
            # otherwise try and use inspect to get the args, but this won't
            # work for cythoned functions
            try:
                if isinstance(self._node_type_func, types.TypeType):
                    args = inspect.getargspec(self._node_type_func.__init__).args[2:]
                else:
                    args = inspect.getargspec(self._node_type_func).args[1:]
            except TypeError:
                args = ["..."]

        args = ", ".join(args)

        if self._node:
            return "<MDFCustomNodeMethod %s.%s(%s)>" % (self._node.name,
                                                         self._method_name,
                                                         args)
        return "<unbound MDFCustomNodeMethod %s(%s)>" % (self._method_name, args)                                                         

    def __call__(self,
                    name=None,
                    short_name=None,
                    filter=None,
                    category=None,
                    **kwargs):
        # get the derived node and call it
        derived_node = self._get_derived_node(name=name,
                                              short_name=short_name,
                                              filter=filter,
                                              category=category,
                                              nodetype_func_kwargs=kwargs)
        if self._call:
            return derived_node()
        return derived_node

    def _get_derived_node(self,
                            name=None,
                            short_name=None,
                            filter=None,
                            category=None,
                            nodetype_func_kwargs={}):
        """
        return a new or cached node made from the base node with
        the node type func applied
        """
        # find the derived node for these arguments
        derived_node_key = cython.declare(tuple)
        derived_node = cython.declare(MDFEvalNode)

        derived_node_key = (self._node_type_func,
                            self._node_cls,
                            filter,
                            category,
                            frozenset(dict_iteritems(nodetype_func_kwargs)))
        try:
            derived_node = self._derived_nodes[derived_node_key]
        except KeyError:
            # use name and short_name if present. If name is present but short_name
            # isn't, use name for short_name too.
            if short_name is None and name is not None:
                short_name = name

            # get all kwargs for the node func and the others passed to this func
            # to build the node name
            if name is None:
                kwargs = dict(nodetype_func_kwargs)
                if filter is not None:
                    kwargs["filter"] = filter

                kwarg_strs = [None] * len(kwargs)
                short_kwarg_strs = [None] * len(kwargs)
                for i, (k, v) in enumerate(sorted(kwargs.items())):
                    vs = v
                    if isinstance(v, MDFNode):
                        vs = v.short_name
                        v = v.name
                    kwarg_strs[i] = "%s=%s" % (k, v)
                    short_kwarg_strs[i] = "%s=%s" % (k, vs)
    
                args = ", ".join(kwarg_strs)
                name = "%s.%s(%s)" % (self._node.name, self._method_name, args)

                if short_name is None:
                    short_args = ", ".join(short_kwarg_strs)
                    short_name = "%s.%s(%s)" % (self._node.short_name,
                                                self._method_name,
                                                short_args)

            derived_node = self._node_cls(self._node.__call__,
                                          self._node_type_func,
                                          name=name,
                                          short_name=short_name,
                                          fqname=name,
                                          category=category,
                                          base_node=self._node,
                                          base_node_method_name=self._method_name,
                                          filter=filter,
                                          nodetype_func_kwargs=nodetype_func_kwargs)

            # update the docstring
            derived_node.func_doc = "\n".join(("*Derived Node* ::", "",
                                                "    " + short_name, "",
                                                derived_node.func_doc or "")).strip()

            self._derived_nodes[derived_node_key] = derived_node

        return derived_node


class MDFCustomNodeDecorator(object):
    """
    decorator that applies a custom node type to a function to
    create a node.
    """
    def __init__(self,
                 node_type_func,
                 node_type_cls,
                 name=None,
                 short_name=None,
                 filter=None,
                 category=None,
                 kwargs={}):
        """
        functor type object that can be used as a decorator to create an
        instance of 'node_type_cls' with 'node_type_func'
        """
        self.func = node_type_func
        self.__node_type_cls = node_type_cls
        self.__filter = filter
        self.__name = name
        self.__short_name = short_name
        self._category = category
        self._kwargs = dict(kwargs)

        # set the docs for this object to the same as the underlying function
        if hasattr(node_type_func, "__doc__"):
            self.__doc__ = node_type_func.__doc__

    def __call__(self,
                    _func=None,
                    name=None,
                    short_name=None,
                    filter=None,
                    category=None,
                    **kwargs):
        """
        If func is None return a copy of self with category, filter
        and kwargs bound to what's passed in.

        Otherwise if func is not None decorate func with the node type.
        """
        filter = filter or self.__filter
        category = category or self._category
        kwargs = kwargs or self._kwargs

        if _func is None:
            return MDFCustomNodeDecorator(self.func,
                                          self.__node_type_cls,
                                          name,
                                          short_name,
                                          filter,
                                          category,
                                          kwargs)

        node = self.__node_type_cls(_func,
                                    self.func,
                                    name=name,
                                    short_name=short_name,
                                    category=category,
                                    filter=filter,
                                    nodetype_func_kwargs=kwargs)
        return node


def nodetype(func=None, cls=MDFCustomNode, method=None):
    """
    decorator for creating a custom node type::

        #
        # create a new node type 'new_node_type'
        #
        @nodetype
        def new_node_type(value, fast, slow):
            return (value + fast) * slow

        #
        # use the new type to create a node
        #
        @new_node_type(fast=1, slow=10)
        def my_node():
            return some_value

        # ctx[my_node] returns new_node_type(value=my_node(), fast=1, slow=10)

    The node type function takes the value of the decorated node
    and any other keyword arguments that may be supplied when
    the node is created.

    The node type function may be a plain function, in which case
    it is simply called for every evaluation of the node, or it
    may be a co-routine in which case it is sent the new value
    for each iteration::

        @nodetype
        def nansumnode(value):
            accum = 0.
            while True:
                accum = np.nansum([value, accum])
                value = yield accum

        @nansumnode
        def my_nansum_node():
            return some_value

    The kwargs passed to the node decorator may be values (as shown above)
    or nodes which will be evaluated before the node type function is
    called.

    Nodes defined using the @nodetype decorator may be applied to 
    classmethods as well as functions and also support the standard
    node kwargs 'filter' and 'category'.

    Node types may also be used to add methods to the MDFNode class
    (See :ref:`nodetype_method_syntax`)::

        @nodetype(method="my_nodetype_method")
        def my_nodetype(value, scale=1):
            return value * scale
    
        @evalnode
        def x():
            return ...
    
        @my_nodetype(scale=10)
        def y():
            return x()

        # can be re-written as:
        y = x.my_nodetype_method(scale=10)
    """
    if func is None:
        return lambda func: nodetype(func, cls, method)

    # set a new method on MDFNode if required
    if method is not None:
        # this method gets the node and evaluates it
        method_func = MDFCustomNodeMethod(func, cls, method, call=True)
        MDFNode._additional_attrs_[method] = method_func

        # add another method to access the node
        getnode_func = MDFCustomNodeMethod(func, cls, method, call=False)
        MDFNode._additional_attrs_[method + "node"] = getnode_func

    return MDFCustomNodeDecorator(func, cls)
