# -*- coding: utf-8 -*-
"""
    celery.canvas
    ~~~~~~~~~~~~~

    Composing task workflows.

    Documentation for these functions are in :mod:`celery`.
    You should not import from this module directly.

"""
from __future__ import absolute_import

from operator import itemgetter
from itertools import chain as _chain

from kombu.utils import fxrange, kwdict, reprcall

from celery import current_app
from celery.local import Proxy
from celery.utils import cached_property, uuid
from celery.utils.compat import chain_from_iterable
from celery.utils.functional import (
    maybe_list, is_list, regen,
    chunks as _chunks,
)
from celery.utils.text import truncate

Chord = Proxy(lambda: current_app.tasks['celery.chord'])


class _getitem_property(object):

    def __init__(self, key):
        self.key = key

    def __get__(self, obj, type=None):
        if obj is None:
            return type
        return obj.get(self.key)

    def __set__(self, obj, value):
        obj[self.key] = value


class Signature(dict):
    """Class that wraps the arguments and execution options
    for a single task invocation.

    Used as the parts in a :class:`group` or to safely
    pass tasks around as callbacks.

    :param task: Either a task class/instance, or the name of a task.
    :keyword args: Positional arguments to apply.
    :keyword kwargs: Keyword arguments to apply.
    :keyword options: Additional options to :meth:`Task.apply_async`.

    Note that if the first argument is a :class:`dict`, the other
    arguments will be ignored and the values in the dict will be used
    instead.

        >>> s = subtask('tasks.add', args=(2, 2))
        >>> subtask(s)
        {'task': 'tasks.add', args=(2, 2), kwargs={}, options={}}

    """
    TYPES = {}
    _type = None

    @classmethod
    def register_type(cls, subclass, name=None):
        cls.TYPES[name or subclass.__name__] = subclass
        return subclass

    @classmethod
    def from_dict(self, d):
        typ = d.get('subtask_type')
        if typ:
            return self.TYPES[typ].from_dict(d)
        return Signature(d)

    def __init__(self, task=None, args=None, kwargs=None, options=None,
                type=None, subtask_type=None, immutable=False, **ex):
        init = dict.__init__

        if isinstance(task, dict):
            return init(self, task)  # works like dict(d)

        # Also supports using task class/instance instead of string name.
        try:
            task_name = task.name
        except AttributeError:
            task_name = task
        else:
            self._type = task

        init(self, task=task_name, args=tuple(args or ()),
                                   kwargs=kwargs or {},
                                   options=dict(options or {}, **ex),
                                   subtask_type=subtask_type,
                                   immutable=immutable)

    def __call__(self, *partial_args, **partial_kwargs):
        return self.apply_async(partial_args, partial_kwargs)
    delay = __call__

    def apply(self, args=(), kwargs={}, **options):
        """Apply this task locally."""
        # For callbacks: extra args are prepended to the stored args.
        args, kwargs, options = self._merge(args, kwargs, options)
        return self.type.apply(args, kwargs, **options)

    def _merge(self, args=(), kwargs={}, options={}):
        if self.immutable:
            return self.args, self.kwargs, dict(self.options, **options)
        return (tuple(args) + tuple(self.args) if args else self.args,
                dict(self.kwargs, **kwargs) if kwargs else self.kwargs,
                dict(self.options, **options) if options else self.options)

    def clone(self, args=(), kwargs={}, **options):
        args, kwargs, options = self._merge(args, kwargs, options)
        s = Signature.from_dict({'task': self.task, 'args': args,
                                 'kwargs': kwargs, 'options': options,
                                 'subtask_type': self.subtask_type,
                                 'immutable': self.immutable})
        s._type = self._type
        return s
    partial = clone

    def replace(self, args=None, kwargs=None, options=None):
        s = self.clone()
        if args is not None:
            s.args = args
        if kwargs is not None:
            s.kwargs = kwargs
        if options is not None:
            s.options = options
        return s

    def set(self, immutable=None, **options):
        if immutable is not None:
            self.immutable = immutable
        self.options.update(options)
        return self

    def apply_async(self, args=(), kwargs={}, **options):
        # For callbacks: extra args are prepended to the stored args.
        args, kwargs, options = self._merge(args, kwargs, options)
        return self.type.apply_async(args, kwargs, **options)

    def append_to_list_option(self, key, value):
        items = self.options.setdefault(key, [])
        if value not in items:
            items.append(value)
        return value

    def link(self, callback):
        return self.append_to_list_option('link', callback)

    def link_error(self, errback):
        return self.append_to_list_option('link_error', errback)

    def flatten_links(self):
        return list(chain_from_iterable(_chain([[self]],
                (link.flatten_links()
                    for link in maybe_list(self.options.get('link')) or []))))

    def __or__(self, other):
        if isinstance(other, chain):
            return chain(*self.tasks + other.tasks)
        elif isinstance(other, Signature):
            if isinstance(self, chain):
                return chain(*self.tasks + (other, ))
            return chain(self, other)
        return NotImplemented

    def __invert__(self):
        return self.apply_async().get()

    def __reduce__(self):
        # for serialization, the task type is lazily loaded,
        # and not stored in the dict itself.
        return subtask, (dict(self), )

    def reprcall(self, *args, **kwargs):
        args, kwargs, _ = self._merge(args, kwargs, {})
        return reprcall(self['task'], args, kwargs)

    def __repr__(self):
        return self.reprcall()

    @cached_property
    def type(self):
        return self._type or current_app.tasks[self['task']]
    task = _getitem_property('task')
    args = _getitem_property('args')
    kwargs = _getitem_property('kwargs')
    options = _getitem_property('options')
    subtask_type = _getitem_property('subtask_type')
    immutable = _getitem_property('immutable')


class chain(Signature):

    def __init__(self, *tasks, **options):
        tasks = tasks[0] if len(tasks) == 1 and is_list(tasks[0]) else tasks
        Signature.__init__(self,
            'celery.chain', (), {'tasks': tasks}, **options)
        self.tasks = tasks
        self.subtask_type = 'chain'

    def __call__(self, *args, **kwargs):
        return self.apply_async(args, kwargs)

    @classmethod
    def from_dict(self, d):
        return chain(*d['kwargs']['tasks'], **kwdict(d['options']))

    def __repr__(self):
        return ' | '.join(map(repr, self.tasks))
Signature.register_type(chain)


class _basemap(Signature):
    _task_name = None
    _unpack_args = itemgetter('task', 'it')

    def __init__(self, task, it, **options):
        Signature.__init__(self, self._task_name, (),
                {'task': task, 'it': regen(it)}, immutable=True, **options)

    def apply_async(self, args=(), kwargs={}, **opts):
        # need to evaluate generators
        task, it = self._unpack_args(self.kwargs)
        return self.type.apply_async((),
                {'task': task, 'it': list(it)}, **opts)

    @classmethod
    def from_dict(self, d):
        return chunks(*self._unpack_args(d['kwargs']), **d['options'])


class xmap(_basemap):
    _task_name = 'celery.map'

    def __repr__(self):
        task, it = self._unpack_args(self.kwargs)
        return '[%s(x) for x in %s]' % (task.task, truncate(repr(it), 100))
Signature.register_type(xmap)


class xstarmap(_basemap):
    _task_name = 'celery.starmap'

    def __repr__(self):
        task, it = self._unpack_args(self.kwargs)
        return '[%s(*x) for x in %s]' % (task.task, truncate(repr(it), 100))
Signature.register_type(xstarmap)


class chunks(Signature):
    _unpack_args = itemgetter('task', 'it', 'n')

    def __init__(self, task, it, n, **options):
        Signature.__init__(self, 'celery.chunks', (),
                {'task': task, 'it': regen(it), 'n': n},
                immutable=True, **options)

    @classmethod
    def from_dict(self, d):
        return chunks(*self._unpack_args(d['kwargs']), **d['options'])

    def apply_async(self, args=(), kwargs={}, **opts):
        return self.group().apply_async(args, kwargs, **opts)

    def __call__(self, **options):
        return self.group()(**options)

    def group(self):
        # need to evaluate generators
        task, it, n = self._unpack_args(self.kwargs)
        return group(xstarmap(task, part) for part in _chunks(iter(it), n))

    @classmethod
    def apply_chunks(cls, task, it, n):
        return cls(task, it, n)()
Signature.register_type(chunks)


def _maybe_group(tasks):
    if isinstance(tasks, group):
        tasks = list(tasks.tasks)
    else:
        tasks = regen(tasks if is_list(tasks) else tasks)
    return tasks


class group(Signature):

    def __init__(self, *tasks, **options):
        if len(tasks) == 1:
            tasks = _maybe_group(tasks[0])
        Signature.__init__(self,
            'celery.group', (), {'tasks': tasks}, **options)
        self.tasks, self.subtask_type = tasks, 'group'

    @classmethod
    def from_dict(self, d):
        return group(d['kwargs']['tasks'], **kwdict(d['options']))

    def __call__(self, *partial_args, **options):
        tasks, result, gid, args = self.type.prepare(options,
                    map(Signature.clone, self.tasks), partial_args)
        return self.type(tasks, result, gid, args)

    def skew(self, start=1.0, stop=None, step=1.0):
        _next_skew = fxrange(start, stop, step, repeatlast=True).next
        for task in self.tasks:
            task.set(countdown=_next_skew())
        return self

    def __iter__(self):
        return iter(self.tasks)

    def __repr__(self):
        return repr(self.tasks)
Signature.register_type(group)


class chord(Signature):
    Chord = Chord

    def __init__(self, header, body=None, **options):
        Signature.__init__(self, 'celery.chord', (),
                         {'header': _maybe_group(header),
                          'body': maybe_subtask(body)}, **options)
        self.subtask_type = 'chord'

    @classmethod
    def from_dict(self, d):
        kwargs = d['kwargs']
        return chord(kwargs['header'], kwargs.get('body'),
                     **kwdict(d['options']))

    def __call__(self, body=None, **options):
        _chord = self.Chord
        body = self.kwargs['body'] = body or self.kwargs['body']
        if _chord.app.conf.CELERY_ALWAYS_EAGER:
            return self.apply((), {}, **options)
        callback_id = body.options.setdefault('task_id', uuid())
        _chord(**self.kwargs)
        return _chord.AsyncResult(callback_id)

    def clone(self, *args, **kwargs):
        s = Signature.clone(self, *args, **kwargs)
        # need to make copy of body
        try:
            s.kwargs['body'] = s.kwargs['body'].clone()
        except (AttributeError, KeyError):
            pass
        return s

    def link(self, callback):
        self.body.link(callback)
        return callback

    def link_error(self, errback):
        self.body.link_error(errback)
        return errback

    def __repr__(self):
        if self.body:
            return self.body.reprcall(self.tasks)
        return '<chord without body: %r>' % (self.tasks, )

    @property
    def tasks(self):
        return self.kwargs['header']

    @property
    def body(self):
        return self.kwargs.get('body')
Signature.register_type(chord)


def subtask(varies, *args, **kwargs):
    if not (args or kwargs) and isinstance(varies, dict):
        if isinstance(varies, Signature):
            return varies.clone()
        return Signature.from_dict(varies)
    return Signature(varies, *args, **kwargs)


def maybe_subtask(d):
    if d is not None and isinstance(d, dict) and not isinstance(d, Signature):
        return subtask(d)
    return d
