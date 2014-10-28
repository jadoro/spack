##############################################################################
# Copyright (c) 2013, Lawrence Livermore National Security, LLC.
# Produced at the Lawrence Livermore National Laboratory.
#
# This file is part of Spack.
# Written by Todd Gamblin, tgamblin@llnl.gov, All rights reserved.
# LLNL-CODE-647188
#
# For details, see https://scalability-llnl.github.io/spack
# Please also see the LICENSE file for our notice and the LGPL.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License (as published by
# the Free Software Foundation) version 2.1 dated February 1999.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the IMPLIED WARRANTY OF
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the terms and
# conditions of the GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
##############################################################################
"""
Fetch strategies are used to download source code into a staging area
in order to build it.  They need to define the following methods:

    * fetch()
        This should attempt to download/check out source from somewhere.
    * check()
        Apply a checksum to the downloaded source code, e.g. for an archive.
        May not do anything if the fetch method was safe to begin with.
    * expand()
        Expand (e.g., an archive) downloaded file to source.
    * reset()
        Restore original state of downloaded code.  Used by clean commands.
        This may just remove the expanded source and re-expand an archive,
        or it may run something like git reset --hard.
    * archive()
        Archive a source directory, e.g. for creating a mirror.
"""
import os
import re
import shutil
from functools import wraps
import llnl.util.tty as tty

import spack
import spack.error
import spack.util.crypto as crypto
from spack.util.executable import *
from spack.util.string import *
from spack.version import Version, ver
from spack.util.compression import decompressor_for, extension

"""List of all fetch strategies, created by FetchStrategy metaclass."""
all_strategies = []

def _needs_stage(fun):
    """Many methods on fetch strategies require a stage to be set
       using set_stage().  This decorator adds a check for self.stage."""
    @wraps(fun)
    def wrapper(self, *args, **kwargs):
        if not self.stage:
            raise NoStageError(fun)
        return fun(self, *args, **kwargs)
    return wrapper


class FetchStrategy(object):
    """Superclass of all fetch strategies."""
    enabled = False             # Non-abstract subclasses should be enabled.
    required_attributes = None  # Attributes required in version() args.

    class __metaclass__(type):
        """This metaclass registers all fetch strategies in a list."""
        def __init__(cls, name, bases, dict):
            type.__init__(cls, name, bases, dict)
            if cls.enabled: all_strategies.append(cls)


    def __init__(self):
        # The stage is initialized late, so that fetch strategies can be constructed
        # at package construction time.  This is where things will be fetched.
        self.stage = None


    def set_stage(self, stage):
        """This is called by Stage before any of the fetching
           methods are called on the stage."""
        self.stage = stage


    # Subclasses need to implement these methods
    def fetch(self):   pass  # Return True on success, False on fail.
    def check(self):   pass  # Do checksum.
    def expand(self):  pass  # Expand archive.
    def reset(self):   pass  # Revert to freshly downloaded state.

    def archive(self, destination): pass  # Used to create tarball for mirror.

    def __str__(self):       # Should be human readable URL.
        return "FetchStrategy.__str___"

    # This method is used to match fetch strategies to version()
    # arguments in packages.
    @classmethod
    def matches(cls, args):
        return any(k in args for k in cls.required_attributes)


class URLFetchStrategy(FetchStrategy):
    """FetchStrategy that pulls source code from a URL for an archive,
       checks the archive against a checksum,and decompresses the archive.
    """
    enabled = True
    required_attributes = ['url']

    def __init__(self, url=None, digest=None, **kwargs):
        super(URLFetchStrategy, self).__init__()

        # If URL or digest are provided in the kwargs, then prefer
        # those values.
        self.url = kwargs.get('url', None)
        if not self.url: self.url = url

        self.digest = kwargs.get('md5', None)
        if not self.digest: self.digest = digest

        if not self.url:
            raise ValueError("URLFetchStrategy requires a url for fetching.")

    @_needs_stage
    def fetch(self):
        self.stage.chdir()

        if self.archive_file:
            tty.msg("Already downloaded %s." % self.archive_file)
            return

        tty.msg("Trying to fetch from %s" % self.url)

        # Run curl but grab the mime type from the http headers
        headers = spack.curl('-#',        # status bar
                             '-O',        # save file to disk
                             '-f',        # fail on >400 errors
                             '-D', '-',   # print out HTML headers
                             '-L', self.url,
                             return_output=True, fail_on_error=False)

        if spack.curl.returncode != 0:
            # clean up archive on failure.
            if self.archive_file:
                os.remove(self.archive_file)

            if spack.curl.returncode == 22:
                # This is a 404.  Curl will print the error.
                raise FailedDownloadError(url)

            if spack.curl.returncode == 60:
                # This is a certificate error.  Suggest spack -k
                raise FailedDownloadError(
                    self.url,
                    "Curl was unable to fetch due to invalid certificate. "
                    "This is either an attack, or your cluster's SSL configuration "
                    "is bad.  If you believe your SSL configuration is bad, you "
                    "can try running spack -k, which will not check SSL certificates."
                    "Use this at your own risk.")

        # Check if we somehow got an HTML file rather than the archive we
        # asked for.  We only look at the last content type, to handle
        # redirects properly.
        content_types = re.findall(r'Content-Type:[^\r\n]+', headers)
        if content_types and 'text/html' in content_types[-1]:
            tty.warn("The contents of " + self.archive_file + " look like HTML.",
                     "The checksum will likely be bad.  If it is, you can use",
                     "'spack clean --dist' to remove the bad archive, then fix",
                     "your internet gateway issue and install again.")

        if not self.archive_file:
            raise FailedDownloadError(self.url)


    @property
    def archive_file(self):
        """Path to the source archive within this stage directory."""
        return self.stage.archive_file

    @_needs_stage
    def expand(self):
        tty.msg("Staging archive: %s" % self.archive_file)

        self.stage.chdir()
        if not self.archive_file:
            raise NoArchiveFileError("URLFetchStrategy couldn't find archive file",
                                      "Failed on expand() for URL %s" % self.url)

        decompress = decompressor_for(self.archive_file)
        decompress(self.archive_file)


    def archive(self, destination):
        """Just moves this archive to the destination."""
        if not self.archive_file:
            raise NoArchiveFileError("Cannot call archive() before fetching.")

        if not extension(destination) == extension(self.archive_file):
            raise ValueError("Cannot archive without matching extensions.")

        shutil.move(self.archive_file, destination)


    @_needs_stage
    def check(self):
        """Check the downloaded archive against a checksum digest.
           No-op if this stage checks code out of a repository."""
        if not self.digest:
            raise NoDigestError("Attempt to check URLFetchStrategy with no digest.")

        checker = crypto.Checker(self.digest)
        if not checker.check(self.archive_file):
            raise ChecksumError(
                "%s checksum failed for %s." % (checker.hash_name, self.archive_file),
                "Expected %s but got %s." % (self.digest, checker.sum))


    @_needs_stage
    def reset(self):
        """Removes the source path if it exists, then re-expands the archive."""
        if not self.archive_file:
            raise NoArchiveFileError("Tried to reset URLFetchStrategy before fetching",
                                     "Failed on reset() for URL %s" % self.url)
        if self.stage.source_path:
            shutil.rmtree(self.stage.source_path, ignore_errors=True)
        self.expand()


    def __repr__(self):
        url = self.url if self.url else "no url"
        return "URLFetchStrategy<%s>" % url


    def __str__(self):
        if self.url:
            return self.url
        else:
            return "[no url]"


class VCSFetchStrategy(FetchStrategy):
    def __init__(self, name, *rev_types, **kwargs):
        super(VCSFetchStrategy, self).__init__()
        self.name = name

        # Set a URL based on the type of fetch strategy.
        self.url = kwargs.get(name, None)
        if not self.url: raise ValueError(
            "%s requires %s argument." % (self.__class__, name))

        # Ensure that there's only one of the rev_types
        if sum(k in kwargs for k in rev_types) > 1:
            raise FetchStrategyError(
                "Supply only one of %s to fetch with %s." % (
                    comma_or(rev_types), name))

        # Set attributes for each rev type.
        for rt in rev_types:
            setattr(self, rt, kwargs.get(rt, None))


    @_needs_stage
    def check(self):
        tty.msg("No checksum needed when fetching with %s." % self.name)


    @_needs_stage
    def expand(self):
        tty.debug("Source fetched with %s is already expanded." % self.name)


    @_needs_stage
    def archive(self, destination, **kwargs):
        assert(extension(destination) == 'tar.gz')
        assert(self.stage.source_path.startswith(self.stage.path))

        tar = which('tar', required=True)

        patterns = kwargs.get('exclude', None)
        if patterns is not None:
            if isinstance(patterns, basestring):
                patterns = [patterns]
            for p in patterns:
                tar.add_default_arg('--exclude=%s' % p)

        self.stage.chdir()
        tar('-czf', destination, os.path.basename(self.stage.source_path))


    def __str__(self):
        return "VCS: %s" % self.url


    def __repr__(self):
        return "%s<%s>" % (self.__class__, self.url)



class GitFetchStrategy(VCSFetchStrategy):
    """Fetch strategy that gets source code from a git repository.
       Use like this in a package:

           version('name', git='https://github.com/project/repo.git')

       Optionally, you can provide a branch, or commit to check out, e.g.:

           version('1.1', git='https://github.com/project/repo.git', tag='v1.1')

       You can use these three optional attributes in addition to ``git``:

           * ``branch``: Particular branch to build from (default is master)
           * ``tag``: Particular tag to check out
           * ``commit``: Particular commit hash in the repo
    """
    enabled = True
    required_attributes = ('git',)

    def __init__(self, **kwargs):
        super(GitFetchStrategy, self).__init__(
            'git', 'tag', 'branch', 'commit', **kwargs)
        self._git = None

        # For git fetch branches and tags the same way.
        if not self.branch:
            self.branch = self.tag


    @property
    def git_version(self):
        git = which('git', required=True)
        vstring = git('--version', return_output=True).lstrip('git version ')
        return Version(vstring)


    @property
    def git(self):
        if not self._git:
            self._git = which('git', required=True)
        return self._git

    @_needs_stage
    def fetch(self):
        self.stage.chdir()

        if self.stage.source_path:
            tty.msg("Already fetched %s." % self.stage.source_path)
            return

        args = []
        if self.commit:
            args.append('at commit %s' % self.commit)
        elif self.tag:
            args.append('at tag %s' % self.tag)
        elif self.branch:
            args.append('on branch %s' % self.branch)
        tty.msg("Trying to clone git repository:", self.url, *args)

        if self.commit:
            # Need to do a regular clone and check out everything if
            # they asked for a particular commit.
            self.git('clone', self.url)
            self.stage.chdir_to_source()
            self.git('checkout', self.commit)

        else:
            # Can be more efficient if not checking out a specific commit.
            args = ['clone']

            # If we want a particular branch ask for it.
            if self.branch:
                args.extend(['--branch', self.branch])

            # Try to be efficient if we're using a new enough git.
            # This checks out only one branch's history
            if self.git_version > ver('1.7.10'):
                args.append('--single-branch')

            args.append(self.url)
            self.git(*args)
            self.stage.chdir_to_source()


    def archive(self, destination):
        super(GitFetchStrategy, self).archive(destination, exclude='.git')


    @_needs_stage
    def reset(self):
        self.stage.chdir_to_source()
        self.git('checkout', '.')
        self.git('clean', '-f')


    def __str__(self):
        return "[git] %s" % self.url


class SvnFetchStrategy(VCSFetchStrategy):
    """Fetch strategy that gets source code from a subversion repository.
       Use like this in a package:

           version('name', svn='http://www.example.com/svn/trunk')

       Optionally, you can provide a revision for the URL:

           version('name', svn='http://www.example.com/svn/trunk',
                   revision='1641')
    """
    enabled = True
    required_attributes = ['svn']

    def __init__(self, **kwargs):
        super(SvnFetchStrategy, self).__init__(
            'svn', 'revision', **kwargs)
        self._svn = None
        if self.revision is not None:
            self.revision = str(self.revision)


    @property
    def svn(self):
        if not self._svn:
            self._svn = which('svn', required=True)
        return self._svn


    @_needs_stage
    def fetch(self):
        self.stage.chdir()

        if self.stage.source_path:
            tty.msg("Already fetched %s." % self.stage.source_path)
            return

        tty.msg("Trying to check out svn repository: %s" % self.url)

        args = ['checkout', '--force']
        if self.revision:
            args += ['-r', self.revision]
        args.append(self.url)

        self.svn(*args)
        self.stage.chdir_to_source()


    def _remove_untracked_files(self):
        """Removes untracked files in an svn repository."""
        status = self.svn('status', '--no-ignore', return_output=True)
        self.svn('status', '--no-ignore')
        for line in status.split('\n'):
            if not re.match('^[I?]', line):
                continue
            path = line[8:].strip()
            if os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)


    def archive(self, destination):
        super(SvnFetchStrategy, self).archive(destination, exclude='.svn')


    @_needs_stage
    def reset(self):
        self.stage.chdir_to_source()
        self._remove_untracked_files()
        self.svn('revert', '.', '-R')


    def __str__(self):
        return "[svn] %s" % self.url



class HgFetchStrategy(VCSFetchStrategy):
    """Fetch strategy that gets source code from a Mercurial repository.
       Use like this in a package:

           version('name', hg='https://jay.grs.rwth-aachen.de/hg/lwm2')

       Optionally, you can provide a branch, or revision to check out, e.g.:

           version('torus', hg='https://jay.grs.rwth-aachen.de/hg/lwm2', branch='torus')

       You can use the optional 'revision' attribute to check out a
       branch, tag, or particular revision in hg.  To prevent
       non-reproducible builds, using a moving target like a branch is
       discouraged.

           * ``revision``: Particular revision, branch, or tag.
    """
    enabled = True
    required_attributes = ['hg']

    def __init__(self, **kwargs):
        super(HgFetchStrategy, self).__init__(
            'hg', 'revision', **kwargs)
        self._hg = None


    @property
    def hg(self):
        if not self._hg:
            self._hg = which('hg', required=True)
        return self._hg

    @_needs_stage
    def fetch(self):
        self.stage.chdir()

        if self.stage.source_path:
            tty.msg("Already fetched %s." % self.stage.source_path)
            return

        args = []
        if self.revision:
            args.append('at revision %s' % self.revision)
        tty.msg("Trying to clone Mercurial repository:", self.url, *args)

        args = ['clone', self.url]
        if self.revision:
            args += ['-r', self.revision]

        self.hg(*args)


    def archive(self, destination):
        super(HgFetchStrategy, self).archive(destination, exclude='.hg')


    @_needs_stage
    def reset(self):
        self.stage.chdir()

        source_path = self.stage.source_path
        scrubbed = "scrubbed-source-tmp"

        args = ['clone']
        if self.revision:
            args += ['-r', self.revision]
        args += [source_path, scrubbed]
        self.hg(*args)

        shutil.rmtree(source_path, ignore_errors=True)
        shutil.move(scrubbed, source_path)
        self.stage.chdir_to_source()


    def __str__(self):
        return "[hg] %s" % self.url


def from_url(url):
    """Given a URL, find an appropriate fetch strategy for it.
       Currently just gives you a URLFetchStrategy that uses curl.

       TODO: make this return appropriate fetch strategies for other
             types of URLs.
    """
    return URLFetchStrategy(url)


def args_are_for(args, fetcher):
    fetcher.matches(args)


def for_package_version(pkg, version):
    """Determine a fetch strategy based on the arguments supplied to
       version() in the package description."""
    # If it's not a known version, extrapolate one.
    if not version in pkg.versions:
        url = pkg.url_for_verison(version)
        if not url:
            raise InvalidArgsError(pkg, version)
        return URLFetchStrategy(url)

    # Grab a dict of args out of the package version dict
    args = pkg.versions[version]

    # Test all strategies against per-version arguments.
    for fetcher in all_strategies:
        if fetcher.matches(args):
            return fetcher(**args)

    # If nothing matched for a *specific* version, test all strategies
    # against
    for fetcher in all_strategies:
        attrs = dict((attr, getattr(pkg, attr, None))
                     for attr in fetcher.required_attributes)
        if 'url' in attrs:
            attrs['url'] = pkg.url_for_version(version)
        attrs.update(args)
        if fetcher.matches(attrs):
            return fetcher(**attrs)

    raise InvalidArgsError(pkg, version)


class FetchError(spack.error.SpackError):
    def __init__(self, msg, long_msg):
        super(FetchError, self).__init__(msg, long_msg)


class FailedDownloadError(FetchError):
    """Raised wen a download fails."""
    def __init__(self, url, msg=""):
        super(FailedDownloadError, self).__init__(
            "Failed to fetch file from URL: %s" % url, msg)
        self.url = url


class NoArchiveFileError(FetchError):
    def __init__(self, msg, long_msg):
        super(NoArchiveFileError, self).__init__(msg, long_msg)


class NoDigestError(FetchError):
    def __init__(self, msg, long_msg):
        super(NoDigestError, self).__init__(msg, long_msg)


class InvalidArgsError(FetchError):
    def __init__(self, pkg, version):
        msg = "Could not construct a fetch strategy for package %s at version %s"
        msg %= (pkg.name, version)
        super(InvalidArgsError, self).__init__(msg)


class ChecksumError(FetchError):
    """Raised when archive fails to checksum."""
    def __init__(self, message, long_msg=None):
        super(ChecksumError, self).__init__(message, long_msg)


class NoStageError(FetchError):
    """Raised when fetch operations are called before set_stage()."""
    def __init__(self, method):
        super(NoStageError, self).__init__(
            "Must call FetchStrategy.set_stage() before calling %s" % method.__name__)
