#!/usr/bin/env python2
# Copyright (c) 2016-present, Facebook, Inc.
# All Rights Reserved.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

"""
Mercurial extension for supporting eden client checkouts.

This overrides the dirstate to check with the eden daemon for modifications,
instead of doing a normal scan of the filesystem.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys

from mercurial import (
    commands, context, error, extensions, hg, localrepo, pathutil, node,
    scmutil, util
)
from mercurial import dirstate as dirstatemod
from mercurial import merge as mergemod
from mercurial.i18n import _
from mercurial import match as matchmod
import mercurial.demandimport

'''
In general, there are two appraoches we could take to implement subcommands like
`hg add` in Eden:
1. Make sure that edendirstate implements the full dirstate API such that we
   use the default implementation of `hg add` and it has no idea that it is
   talking to edendirstate.
2. Reimplement `hg add` completely.

In general, #1 is a better approach because it is helpful for other built-in
commands in Hg that also talk to the dirstate. However, it appears that `hg add`
calls `dirstate.walk()`, which is a real pain to implement, and honestly,
something we probably don't want to implement. We can make more progress by
redefining `hg add` in Eden so it does a simple Thrift call to update the
overlay.
'''
from . import (
    overrides,
)

# Disable demandimport while importing thrift files.
#
# The thrift modules try importing modules which may or may not exist, and they
# handle the ImportError generated if the modules aren't present.  demandimport
# breaks this behavior by making it appear like the modules were successfully
# loaded, and only throwing ImportError later when you actually try to use
# them.
with mercurial.demandimport.deactivated():
    try:
        # The native thrift code requires a new enough version of python
        # where struct.pack() accepts format strings as unicode.
        if sys.version_info < (2, 7, 6):
            raise Exception('python version is too old to use '
                            'the native thrift client')

        # Look for the native thrift client relative to our local file.
        #
        # Our file should be "hgext3rd/eden/__init__.py", inside a directory
        # that also contains the other thrift modules required to talk to eden.
        archive_root = os.path.normpath(os.path.join(__file__, '../../..'))
        sys.path.insert(0, archive_root)

        import eden.thrift as eden_thrift_module
        import facebook.eden.ttypes as eden_ttypes
        _thrift_client_type = 'native'
    except Exception:
        # If we fail to import eden.thrift, fall back to using the
        # LameThriftClient module for now.  At the moment we build the
        # eden.thrift modules with fairly recent versions of gcc and glibc, but
        # mercurial is often invoked with the system version of python, which
        # cannot import modules compiled against newer glibc versions.
        #
        # Eventually this fallback should be removed once we make sure
        # mercurial is always deployed to use our newer python builds.  For now
        # it is in place to ease development.
        from . import LameThriftClient as eden_thrift_module
        eden_ttypes = eden_thrift_module
        _thrift_client_type = 'lame'

create_thrift_client = eden_thrift_module.create_thrift_client
StatusCode = eden_ttypes.StatusCode
ConflictType = eden_ttypes.ConflictType

_requirement = 'eden'
_repoclass = localrepo.localrepository
_repoclass._basesupported.add(_requirement)


def extsetup(ui):
    # Wrap the localrepo.dirstate() function.
    #
    # The original dirstate attribute is a filecache object, and needs slightly
    # special handling to wrap properly.
    #
    # (The fsmonitor and sqldirstate extensions both already wrap it, and each
    # has slightly different mechanisms for doing so.  Here we wrap it more
    # like sqldirstate does.  Ideally code for wrapping filecache objects
    # should just get put into core mercurial.)
    orig = localrepo.localrepository.dirstate
    # For some reason, localrepository.invalidatedirstate() does not call
    # dirstate.invalidate() by default, so we must wrap it.
    extensions.wrapfunction(localrepo.localrepository, 'invalidatedirstate',
                            invalidatedirstate)
    extensions.wrapfunction(context.committablectx, 'markcommitted',
                            mark_committed)
    extensions.wrapfunction(mergemod, 'update', merge_update)
    extensions.wrapfunction(hg, '_showstats', update_showstats)
    extensions.wrapfunction(orig, 'func', wrapdirstate)
    extensions.wrapfunction(matchmod.match, '__init__', wrap_match_init)
    extensions.wrapcommand(commands.table, 'add', overrides.add)
    extensions.wrapcommand(commands.table, 'remove', overrides.remove)
    orig.paths = ()

    if _thrift_client_type != 'native':
        ui.warn(_('unable to import native thrift client for eden; '
                  'falling back to pyremote invocation\n'))


def invalidatedirstate(orig, self):
    if _requirement in self.requirements:
        self.dirstate.invalidate()
    else:
        # In Eden, we do not want the original behavior of
        # localrepository.invalidatedirstate because it operates on the private
        # _filecache property of dirstate, which is not a field we provide in
        # edendirstate.
        orig(self)


def mark_committed(orig, self, node):
    '''Perform post-commit cleanup necessary after committing this ctx (self).

    Specifically, self is a commitablectx from context.py.
    '''
    if _requirement in self._repo.requirements:
        # When markcommitted() is called from localrepo.py, it is in the middle
        # of a transaction. The commit data for the specified `node` will not be
        # written to .hg until the transaction completes. Because our
        # server-side logic relies on being able to read the commit data out of
        # .hg, we schedule it as an addpostclose callback on the current
        # transaction rather than execute it directly here.
        def callback(tr):
            dirstate = self._repo.dirstate
            dirstate.beginparentchange()
            dirstate.setparents(node)
            dirstate.endparentchange()

        self._repo.currenttransaction().addpostclose('commit', callback)
    else:
        orig(self, node)


# This function replaces the update() function in mercurial's mercurial.merge
# module.   It's signature must match the original mercurial.merge.update()
# function.
def merge_update(orig, repo, node, branchmerge, force, ancestor=None,
                 mergeancestor=False, labels=None, matcher=None,
                 mergeforce=False, updatecheck=None):
    assert node is not None

    if not util.safehasattr(repo.dirstate, 'eden_client'):
        # This is not an eden repository
        useeden = False
    if matcher is not None and not matcher.always():
        # We don't support doing a partial update through eden yet.
        useeden = False
    elif branchmerge or ancestor is not None:
        useeden = False
    else:
        # TODO: We probably also need to set useeden = False if there are
        # subrepositories.  (Personally I might vote for just not supporting
        # subrepos in eden.)
        useeden = True

    if not useeden:
        repo.ui.debug("falling back to non-eden update code path")
        return orig(repo, node, branchmerge, force, ancestor=ancestor,
                    mergeancestor=mergeancestor, labels=labels, matcher=matcher,
                    mergeforce=mergeforce)

    with repo.wlock():
        wctx = repo[None]
        parents = wctx.parents()

        p1ctx = parents[0]
        destctx = repo[node]
        deststr = str(destctx)

        if not force:
            # Make sure there isn't an outstanding merge or unresolved files.
            if len(parents) > 1:
                raise error.Abort(_("outstanding uncommitted merge"))
            ms = mergemod.mergestate.read(repo)
            if list(ms.unresolved()):
                raise error.Abort(_("outstanding merge conflicts"))

            # The vanilla merge code disallows updating between two unrelated
            # branches if the working directory is dirty.  I don't really see a
            # good reason to disallow this; it should be treated the same as if
            # we committed the changes, checked out the other branch then tried
            # to graft the changes here.

        # Invoke the preupdate hook
        repo.hook('preupdate', throw=True, parent1=deststr, parent2='')
        # note that we're in the middle of an update
        repo.vfs.write('updatestate', destctx.hex())

        # Ask eden to perform the checkout
        if force or p1ctx != destctx:
            conflicts = repo.dirstate.eden_client.checkout(
                destctx.node(), force=force)
        else:
            conflicts = None

        # Handle any conflicts
        # The stats returned are numbers of files affected:
        #   (updated, merged, removed, unresolved)
        # The updated and removed file counts will always be 0 in our case.
        if conflicts and not force:
            stats = _handleupdateconflicts(repo, wctx, p1ctx, destctx, labels,
                                           conflicts)
        else:
            stats = 0, 0, 0, 0

        # Clear the update state
        util.unlink(repo.vfs.join('updatestate'))

    # Invoke the update hook
    repo.hook('update', parent1=deststr, parent2='', error=stats[3])

    return stats


def update_showstats(orig, repo, stats, quietempty=False):
    # We hide the updated and removed counts, because they are not accurate
    # with eden.  One of the primary goals of eden is that the entire working
    # directory does not need to be accessed or traversed on update operations.
    (updated, merged, removed, unresolved) = stats
    if merged or unresolved:
        repo.ui.status(_('%d files merged, %d files unresolved\n') %
                       (merged, unresolved))
    elif not quietempty:
        repo.ui.status(_('update complete\n'))


def _handleupdateconflicts(repo, wctx, src, dest, labels, conflicts):
    # When resolving conflicts during an update operation, the working
    # directory (wctx) is one side of the merge, the destination commit (dest)
    # is the other side of the merge, and the source commit (src) is treated as
    # the common ancestor.
    #
    # This is what we want with respect to the graph topology.  If we are
    # updating from commit A (src) to B (dest), and the real ancestor is C, we
    # effectively treat the update operation as reverting all commits from A to
    # C, then applying the commits from C to B.  We are then trying to re-apply
    # the local changes in the working directory (against A) to the new
    # location B.  Using A as the common ancestor in this operation is the
    # desired behavior.

    # Build a list of actions to pass to mergemod.applyupdates()
    actions = dict((m, []) for m in 'a am f g cd dc r dm dg m e k'.split())
    numerrors = 0
    for conflict in conflicts:
        # The action tuple is:
        # - path_in_1, path_in_2, path_in_ancestor, move, ancestor_node

        if conflict.type == ConflictType.ERROR:
            # We don't record this as a conflict for now.
            # We will report the error, but the file will show modified in
            # the working directory status after the update returns.
            repo.ui.write_err(_('error updating %s: %s\n') %
                              (conflict.path, conflict.message))
            numerrors += 1
            continue
        elif conflict.type == ConflictType.MODIFIED_REMOVED:
            action_type = 'cd'
            action = (conflict.path, None, conflict.path, False, src.node())
            prompt = "prompt changed/deleted"
        elif conflict.type == ConflictType.UNTRACKED_ADDED:
            action_type = 'c'
            action = (dest.manifest().flags(conflict.path),)
            prompt = "remote created"
        elif conflict.type == ConflictType.REMOVED_MODIFIED:
            action_type = 'dc'
            action = (None, conflict.path, conflict.path, False, src.node())
            prompt = "prompt deleted/changed"
        elif conflict.type == ConflictType.MISSING_REMOVED:
            # Nothing to do here really.  The file was already removed
            # locally in the working directory before, and it was removed
            # in the new commit.
            continue
        elif conflict.type == ConflictType.MODIFIED:
            action_type = 'm'
            action = (conflict.path, conflict.path, conflict.path,
                      False, src.node())
            prompt = "versions differ"
        else:
            raise Exception('unknown conflict type received from eden: '
                            '%r, %r, %r' % (conflict.type, conflict.path,
                                            conflict.message))

        actions[action_type].append((conflict.path, action, prompt))

    # Call applyupdates
    stats = mergemod.applyupdates(repo, actions, wctx, dest,
                                  overwrite=False, labels=labels)

    # Add the error count to the number of unresolved files.
    # This ensures we exit unsuccessfully if there were any errors
    return (stats[0], stats[1], stats[2], stats[3] + numerrors)


def reposetup(ui, repo):
    # TODO: We probably need some basic sanity checking here:
    # - is this an eden client?
    # - are any conflicting extensions enabled?
    pass


def wrapdirstate(orig, repo):
    # Only override when actually inside an eden client directory.
    if _requirement not in repo.requirements:
        return orig(repo)

    # For now we intentionally do not derive from the original dirstate class.
    #
    # We want to make sure that we never accidentally fall back to the base
    # dirstate functionality; anything we do should be tailored for eden.

    # have the edendirstate class implementation more complete.
    return edendirstate(repo, repo.ui, repo.root)


class EdenMatchInfo(object):
    ''' Holds high fidelity information about a matching operation '''
    def __init__(self, root, cwd, exact, patterns, includes, excludes):
        self._root = root
        self._cwd = cwd
        self._includes = includes + patterns
        self._excludes = excludes
        self._exact = exact

    def make_glob_list(self):
        ''' run through the list of includes and transform it into
            a list of glob expressions. '''
        globs = []
        for kind, pat, raw in self._includes:
            if kind == 'glob':
                globs.append(pat)
                continue
            if kind in ('relpath', 'path'):
                globs.append(pat + '/**/*')
                continue
            if kind == 'relglob':
                globs.append('**/' + pat)
                continue

            raise NotImplementedError(
                'match pattern %r is not supported by Eden' % (kind, pat, raw))
        return globs


def wrap_match_init(orig, match, root, cwd, patterns, include=None, exclude=None,
                    default='glob', exact=False, auditor=None, ctx=None,
                    listsubrepos=False, warn=None, badfn=None):
    ''' Wrapper around matcher.match.__init__
        The goal is to capture higher fidelity information about the matcher
        being created than we would otherwise be able to extract from the
        object once it has been created.

        arguments:
        root - the canonical root of the tree you're matching against
        cwd - the current working directory, if relevant
        patterns - patterns to find
        include - patterns to include (unless they are excluded)
        exclude - patterns to exclude (even if they are included)
        default - if a pattern in patterns has no explicit type, assume this one
        exact - patterns are actually filenames (include/exclude still apply)
        warn - optional function used for printing warnings
        badfn - optional bad() callback for this matcher instead of the default

        a pattern is one of:
        'glob:<glob>' - a glob relative to cwd
        're:<regexp>' - a regular expression
        'path:<path>' - a path relative to repository root, which is matched
                        recursively
        'rootfilesin:<path>' - a path relative to repository root, which is
                        matched non-recursively (will not match subdirectories)
        'relglob:<glob>' - an unrooted glob (*.c matches C files in all dirs)
        'relpath:<path>' - a path relative to cwd
        'relre:<regexp>' - a regexp that needn't match the start of a name
        'set:<fileset>' - a fileset expression
        'include:<path>' - a file of patterns to read and include
        'subinclude:<path>' - a file of patterns to match against files under
                              the same directory
        '<something>' - a pattern of the specified default type
    '''

    res = orig(match, root, cwd, patterns, include, exclude, default,
               exact, auditor, ctx, listsubrepos, warn, badfn)

    info = EdenMatchInfo(root, cwd, exact,
                         match._normalize(patterns or [],
                                          default, root, cwd, auditor),
                         match._normalize(include or [],
                                          'glob', root, cwd, auditor),
                         match._normalize(exclude or [],
                                          'glob', root, cwd, auditor))

    match._eden_match_info = info

    return res


class ClientStatus(object):
    def __init__(self):
        self.modified = []
        self.added = []
        self.removed = []
        self.deleted = []
        self.unknown = []
        self.ignored = []
        self.clean = []


class EdenThriftClient(object):
    def __init__(self, repo):
        self._root = repo.root
        self._client = create_thrift_client(mounted_path=self._root)
        # TODO: It would be nicer to use a context manager to make sure we
        # close the client appropriately.
        self._client.open()

    def getParentCommits(self):
        '''
        Returns a tuple containing the IDs of the working directory's parent
        commits.

        The first element of the tuple is always a 20-byte binary value
        containing the commit ID.

        The second element of the tuple is None if there is only one parent,
        or the second parent ID as a 20-byte binary value.
        '''
        parents = self._client.getParentCommits(self._root)
        return (parents.parent1, parents.parent2)

    def setHgParents(self, p1, p2):
        if p2 == node.nullid:
            p2 = None

        parents = eden_ttypes.WorkingDirectoryParents(parent1=p1, parent2=p2)
        self._client.resetParentCommits(self._root, parents)

    def getStatus(self, list_ignored):
        status = ClientStatus()
        thrift_hg_status = self._client.scmGetStatus(self._root, list_ignored)
        for path, code in thrift_hg_status.entries.iteritems():
            if code == StatusCode.MODIFIED:
                status.modified.append(path)
            elif code == StatusCode.ADDED:
                status.added.append(path)
            elif code == StatusCode.REMOVED:
                status.removed.append(path)
            elif code == StatusCode.MISSING:
                status.deleted.append(path)
            elif code == StatusCode.NOT_TRACKED:
                status.unknown.append(path)
            elif code == StatusCode.IGNORED:
                status.ignored.append(path)
            elif code == StatusCode.CLEAN:
                status.clean.append(path)
            else:
                raise Exception('Unexpected status code: %s' % code)
        return status

    def add(self, paths):
        '''paths must be a normalized paths relative to the repo root.

        Note that each path in paths may refer to a file or a directory.

        Returns a possibly empty list of errors to present to the user.
        '''
        return self._client.scmAdd(self._root, paths)

    def remove(self, paths, force):
        '''paths must be a normalized paths relative to the repo root.

        Note that each path in paths may refer to a file or a directory.

        Returns a possibly empty list of errors to present to the user.
        '''
        return self._client.scmRemove(self._root, paths, force)

    def checkout(self, node, force):
        return self._client.checkOutRevision(self._root, node, force)

    def glob(self, globs):
        return self._client.glob(self._root, globs)

    def getFileInformation(self, files):
        return self._client.getFileInformation(self._root, files)


class statobject(object):
    ''' this is a stat-like object to represent information from eden.'''
    __slots__ = ('st_mode', 'st_size', 'st_mtime')

    def __init__(self, mode=None, size=None, mtime=None):
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime

class edendirstate(object):
    '''
    edendirstate replaces mercurial's normal dirstate class.

    edendirstate generally avoids performing normal filesystem operations for
    computing the working directory state, and instead communicates directly to
    eden instead to ask for the status of the working copy.

    edendirstate currently does not derive from the normal dirstate class
    primarily just to ensure that we do not ever accidentally fall back to the
    default dirstate behavior.
    '''
    def __init__(self, repo, ui, root):
        self._repo = repo
        self.eden_client = EdenThriftClient(repo)
        self._ui = ui
        self._root = root
        self._rootdir = pathutil.normasprefix(root)
        # self._parents is a cache of the current parent node IDs.
        # This is a tuple of 2 20-byte binary commit IDs, or None when unset.
        self._parents = None

        # Store a vanilla dirstate object, so we can re-use some of its
        # functionality in a handful of cases.  Primarily this is just for cwd
        # and path computation.
        self._normaldirstate = dirstatemod.dirstate(
            opener=None, ui=self._ui, root=self._root, validate=None)

        self._parentwriters = 0

    def thrift_scm_add(self, paths):
        '''paths must be a normalized paths relative to the repo root.

        Note that each path in paths may refer to a file or a directory.

        Returns a possibly empty list of errors to present to the user.
        '''
        return self.eden_client.add(paths)

    def thrift_scm_remove(self, paths, force):
        '''paths must be normalized paths relative to the repo root.

        Note that each path in paths may refer to a file or a directory.

        Returns a possibly empty list of errors to present to the user.
        '''
        return self.eden_client.remove(paths, force)

    def beginparentchange(self):
        self._parentwriters += 1

    def endparentchange(self):
        if self._parentwriters <= 0:
            raise ValueError("cannot call dirstate.endparentchange without "
                             "calling dirstate.beginparentchange")
        self._parentwriters -= 1

    def pendingparentchange(self):
        return self._parentwriters > 0

    def dirs(self):
        raise NotImplementedError('edendirstate.dirs()')

    def _ignore(self):
        # Even though this function starts with an underscore, it is directly
        # called from other parts of the mercurial code.
        raise NotImplementedError('edendirstate._ignore()')

    def _checklink(self):
        """
        check whether the given path is on a symlink-capable filesystem
        """
        # Even though this function starts with an underscore, it is directly
        # called from other parts of the mercurial code.
        return True

    def _checkexec(self):
        """
        Check whether the given path is on a filesystem with UNIX-like
        exec flags.
        """
        # Even though this function starts with an underscore, it is called
        # from other extensions and other parts of the mercurial code.
        return True

    def _join(self, f):
        # Use the same simple concatenation strategy as mercurial's
        # normal dirstate code.
        return self._rootdir + f

    def flagfunc(self, buildfallback):
        return self._flagfunc

    def _flagfunc(self, path):
        try:
            st = os.lstat(self._join(path))
            if util.statislink(st):
                return 'l'
            if util.statisexec(st):
                return 'x'
        except OSError:
            pass
        return ''

    def getcwd(self):
        # Use the vanilla mercurial dirstate.getcwd() implementation
        return self._normaldirstate.getcwd()

    def pathto(self, f, cwd=None):
        # Use the vanilla mercurial dirstate.pathto() implementation
        return self._normaldirstate.pathto(f, cwd)

    def __getitem__(self, key):
        # FIXME
        return '?'

    def __contains__(self, key):
        # FIXME
        return False

    def __iter__(self):
        # FIXME
        if False:
            yield None
        return

    def iteritems(self):
        raise NotImplementedError('edendirstate.iteritems()')

    def _getparents(self):
        if self._parents is None:
            p1, p2 = self.eden_client.getParentCommits()
            if p2 is None:
                p2 = node.nullid
            self._parents = (p1, p2)

    def parents(self):
        self._getparents()
        return list(self._parents)

    def p1(self):
        self._getparents()
        return self._parents[0]

    def p2(self):
        self._getparents()
        return self._parents[1]

    def branch(self):
        return 'default'

    def setparents(self, p1, p2=node.nullid):
        """Set dirstate parents to p1 and p2."""
        if self._parentwriters == 0:
            raise ValueError("cannot set dirstate parent without "
                             "calling dirstate.beginparentchange")

        self.eden_client.setHgParents(p1, p2)

    def setbranch(self, branch):
        raise NotImplementedError('edendirstate.setbranch()')

    def _opendirstatefile(self):
        # TODO: used by the journal extension
        raise NotImplementedError('edendirstate._opendirstatefile()')

    def invalidate(self):
        '''Clears local state such that it is forced to be recomputed the next
        time it is accessed.

        This method is invoked when the lock is acquired via
        localrepository.wlock(). In wlock(),
        localrepository.invalidatedirstate() is called when the lock is
        acquired, which calls dirstate.invalidate() (surprisingly, this is only
        because we have redefined localrepository.invalidatedirstate() to do so
        in extsetup(ui)).

        This method is also invoked when the lock is released if
        self.pendingparentchange() is True.
        '''
        self._parents = None

    def copy(self, source, dest):
        """Mark dest as a copy of source. Unmark dest if source is None."""
        raise NotImplementedError('edendirstate.copy()')

    def copied(self, file):
        # TODO(mbolin): Once we update edendirstate to properly store copy
        # information, we will have to return True if there are any
        # copies/renames.
        return False

    def copies(self):
        # TODO(mbolin): Once we update edendirstate to properly store copy
        # information, we will have to include it in the dict returned by this
        # method.
        return {}

    def normal(self, f):
        raise NotImplementedError('edendirstate.normal(%s)' % f)

    def normallookup(self, f):
        raise NotImplementedError('edendirstate.normallookup(%s)' % f)

    def otherparent(self, f):
        """Mark as coming from the other parent, always dirty."""
        raise NotImplementedError('edendirstate.otherparent()')

    def add(self, f):
        """Mark a file added."""
        raise NotImplementedError(
            'Unexpected call to edendirstate.add(). ' +
            'All calls to add() are expected to go through the CLI.')

    def remove(self, f):
        """Mark a file removed."""
        raise NotImplementedError(
            'Unexpected call to edendirstate.remove(). ' +
            'All calls to remove() are expected to go through the CLI.')

    def merge(self, f):
        """Mark a file merged."""
        raise NotImplementedError('edendirstate.merge()')

    def drop(self, f):
        """Drop a file from the dirstate"""
        raise NotImplementedError('edendirstate.drop()')

    def normalize(self, path, isknown=False, ignoremissing=False):
        """normalize the case of a pathname when on a casefolding filesystem"""
        # TODO: Should eden always be case-sensitive?
        return path

    def clear(self):
        raise NotImplementedError('edendirstate.clear()')

    def rebuild(self, parent, allfiles, changedfiles=None):
        # We don't ever need to rebuild file status with eden, all we need to
        # do is reset the parent commit of the working directory.
        #
        # TODO: It would be nicer if we could update the higher-level code so
        # it doesn't even bother computing allfiles and changedfiles.
        self.eden_client.setHgParents(parent, node.nullid)

    def write(self, tr):
        # TODO: write the data if it is dirty
        return

    def _dirignore(self, f):
        # Not used by core mercurial code; only internally by dirstate.walk
        # and by the hgview application
        raise NotImplementedError('edendirstate._dirignore()')

    def _ignorefileandline(self, f):
        # Only used by the "debugignore" command
        raise NotImplementedError('edendirstate._ignorefileandline()')

    def _eden_walk_helper(self, match, deleted, unknown, ignored):
        ''' Extract the matching information we collected from the
            match constructor and try to turn it into a list of
            glob expressions.  If we don't have enough information
            for this, make_glob_list() will raise an exception '''
        if not util.safehasattr(match, '_eden_match_info'):
            raise NotImplementedError(
                'match object is not eden compatible' + \
                '(_eden_match_info is missing)')
        info = match._eden_match_info
        globs = info.make_glob_list()

        # Expand the glob into a set of candidate files
        globbed_files = self.eden_client.glob(globs)

        # Run the results through the matcher object; this processes
        # any excludes that might be part of the matcher
        matched_files = [f for f in globbed_files if match(f)]

        if matched_files and (deleted or (not unknown) or (not ignored)):
            # !unknown as parameter means that we need to exclude
            # any files with an unknown status.
            # !ignored -> exclude any ignored files.
            # To get ignored files in the status list, we need to pass
            # True when !ignored is passed in to us.
            status = self.eden_client.getStatus(not ignored)
            elide = set()
            if not unknown:
                elide.update(status.unknown)
            if not ignored:
                elide.update(status.ignored)
            if deleted:
                elide.update(status.removed)
                elide.update(status.deleted)

            matched_files = [f for f in matched_files if f not in elide]

        return matched_files

    def walk(self, match, subrepos, unknown, ignored, full=True):
        '''
        Walk recursively through the directory tree, finding all files
        matched by match.

        If full is False, maybe skip some known-clean files.

        Return a dict mapping filename to stat-like object
        '''

        matched_files = self._eden_walk_helper(match,
                                               deleted=True,
                                               unknown=unknown,
                                               ignored=ignored)

        # Now we need to build a stat-like-object for each of these results
        file_info = self.eden_client.getFileInformation(matched_files)

        results = {}
        for index, info in enumerate(file_info):
            file_name = matched_files[index]
            if info.getType() == eden_ttypes.FileInformationOrError.INFO:
                finfo = info.get_info()
                results[file_name] = statobject(mode=finfo.mode,
                                                size=finfo.size,
                                                mtime=finfo.mtime)
            else:
                # Indicates that we knew of the file, but that is it
                # not present on disk; it has been removed.
                results[file_name] = None

        return results

    def status(self, match, subrepos, ignored, clean, unknown):
        # We should never have any files we are unsure about
        unsure = []

        edenstatus = self.eden_client.getStatus(ignored)

        status = scmutil.status(edenstatus.modified,
                                edenstatus.added,
                                edenstatus.removed,
                                edenstatus.deleted,
                                edenstatus.unknown,
                                edenstatus.ignored,
                                edenstatus.clean)
        return (unsure, status)

    def matches(self, match):
        return self._eden_walk_helper(match,
                                      deleted=False,
                                      unknown=False,
                                      ignored=False)

    def savebackup(self, tr, suffix='', prefix=''):
        '''
        Saves the current dirstate, using prefix/suffix to namespace the storage
        where the current dirstate is persisted.
        One of prefix or suffix must be set.

        The complement to this method is self.restorebackup(tr, suffix, prefix).

        Args:
            tr (transaction?): such as `repo.currenttransaction()` or None.
            suffix (str): If persisted to a file, suffix of file to use.
            prefix (str): If persisted to a file, prefix of file to use.
        '''
        assert len(suffix) > 0 or len(prefix) > 0
        # TODO(mbolin): Create a snapshot for the current dirstate and persist
        # it to a safe place.
        pass

    def restorebackup(self, tr, suffix='', prefix=''):
        '''
        Restores the saved dirstate, using prefix/suffix to namespace the
        storage where the dirstate was persisted.
        One of prefix or suffix must be set.

        The complement to this method is self.savebackup(tr, suffix, prefix).

        Args:
            tr (transaction?): such as `repo.currenttransaction()` or None.
            suffix (str): If persisted to a file, suffix of file to use.
            prefix (str): If persisted to a file, prefix of file to use.
        '''
        assert len(suffix) > 0 or len(prefix) > 0
        # TODO(mbolin): Restore the snapshot written by savebackup().
        pass

    def clearbackup(self, tr, suffix='', prefix=''):
        raise NotImplementedError('edendirstate.clearbackup()')
