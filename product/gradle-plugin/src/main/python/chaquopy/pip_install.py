#!/usr/bin/env python3

"""Copyright (c) 2020 Chaquo Ltd. All rights reserved."""

# Keep valid Python 2 syntax so we can produce an error message.
from __future__ import absolute_import, division, print_function

# Do this as early as possible to minimize the chance of something else going wrong and causing
# a less comprehensible error message.
from .util import check_build_python
check_build_python()

import argparse
from collections import namedtuple
import email.parser
from glob import glob
import hashlib
import logging.config
import os
from os.path import abspath, dirname, exists, isdir, join
import re
import subprocess
import sys

from pip._internal.utils.misc import rmtree
from pip._vendor.distlib.database import InstalledDistribution
from pip._vendor.retrying import retry
from wheel.util import urlsafe_b64encode  # Not the same as the version in base64.

from .util import CommandError


ABI_API_LEVELS = {
    "arm64-v8a": 23,
    "x86": 23,
}


logger = logging.getLogger(__name__)

class PipInstall(object):

    def main(self):
        # This matches pip's own logging setup in pip/basecommand.py.
        self.parse_args()
        verbose = ("-v" in self.pip_options) or ("--verbose" in self.pip_options)
        config_logging(verbose)
        if verbose:
            os.environ["DISTUTILS_DEBUG"] = "1"

        try:
            os.mkdir(join(self.target, "common"))
            abi_trees = {}

            # Install the first ABI.
            abi = self.android_abis[0]
            req_infos, abi_trees[abi] = self.pip_install(abi, self.reqs)
            self.move_pure([ri.tree for ri in req_infos if ri.is_pure], abi, abi_trees[abi])

            # Create minimal .dist-info directories so pkg_resources will work (see importer.py).
            for ri in req_infos:
                dist_info_dir = join(self.target, "common", "{}-{}.dist-info".format(
                    normalize_name_wheel(ri.dist.name), ri.dist.version))
                os.mkdir(dist_info_dir)

            # Install native requirements for the other ABIs.
            native_reqs = ["{}=={}".format(ri.dist.name, ri.dist.version)
                           for ri in req_infos if not ri.is_pure]
            self.pip_options.append("--no-deps")
            for abi in self.android_abis[1:]:
                _, abi_trees[abi] = self.pip_install(abi, native_reqs)
            self.merge_common(abi_trees)
            logger.debug("Finished")

        except CommandError as e:
            logger.error(str(e))
            sys.exit(1)

    # pip makes no attempt to check for multiple packages providing the same filename, so the
    # version we end up with will be the one that pip installed last. We therefore treat a
    # duplicate filename as being owned by the package whose RECORD matches it. If more than
    # one package matches, priority is given to non-pure packages, so that all ABI trees will
    # end up with their own copies of the file. Beyond that, it doesn't matter which ReqInfo
    # the file ends up in.
    #
    # In any case, if all the ABI trees end up with identical copies of the file, then
    # merge_common will merge them. There is one awkward case: if a non-pure package has a file
    # overwritten by a different version from a pure package, the file will be moved to common
    # by move_pure, and will therefore only exist in the ABI trees of the second and subsequent
    # ABIs. This shouldn't cause runtime inconsistency between ABIs, because the common tree
    # still comes first in the runtime sys.path.
    def pip_install(self, abi, reqs):
        logger.info("Installing for " + abi)
        abi_dir = join(self.target, abi)
        os.mkdir(abi_dir)
        if not reqs:
            return [], {}

        try:
            # Warning: `pip install --target` is very simple-minded: see
            # https://github.com/pypa/pip/issues/4625#issuecomment-375977073. Also, we've
            # altered its behaviour somewhat for performance: see commands/install.py.
            cmdline = ([sys.executable,
                       "-S",  # Avoid interference from system/user site-packages
                              # (this is not inherited by subprocesses).
                        "-m", "pip", "install",
                        "--target", abi_dir,
                        "--platform", self.platform_tag(abi)] +
                       self.pip_options + reqs)
            logger.debug("Running {}".format(cmdline))
            subprocess.check_call(cmdline)
        except subprocess.CalledProcessError as e:
            raise CommandError("Exit status {}".format(e.returncode))

        logger.debug("Reading dist-info")
        req_infos = []
        abi_tree = {}
        for dist_info_dir in sorted(glob(join(abi_dir, "*.dist-info"))):
            try:
                dist = InstalledDistribution(dist_info_dir)
                wheel_info = email.parser.Parser().parse(open(join(dist_info_dir, "WHEEL")))
                is_pure = (wheel_info.get("Root-Is-Purelib", "false") == "true")
                req_tree = {}
                for path, hash_str, size_str in dist.list_installed_files():
                    path_abs = abspath(join(abi_dir, path))
                    if not path_abs.startswith(abi_dir):
                        # pip's gone and installed something outside of the target directory.
                        raise ValueError("invalid path in RECORD: '{}'".format(path))
                    if not path_abs.startswith(dist_info_dir):
                        value = (hash_str, int(size_str))
                        try:
                            tree_add_path(abi_tree, path, value)
                        except PathExistsError as e:  # Duplicate filename: see note above.
                            if file_matches_record(join(abi_dir, path), *value) and \
                               ((e.existing_value != value) or (not is_pure)):
                                for ri in req_infos:
                                    tree_remove_path(ri.tree, path, ignore_missing=True)
                                tree_add_path(abi_tree, path, value, force=True)
                            else:
                                continue
                        tree_add_path(req_tree, path, value)

                req_infos.append(ReqInfo(dist, req_tree, is_pure))
                rmtree(dist_info_dir)

            except Exception:
                logger.error("Failed to process " + dist_info_dir)
                raise

        return req_infos, abi_tree

    def move_pure(self, pure_trees, abi, abi_tree):
        logger.debug("Moving pure requirements")
        for req_tree in pure_trees:
            for path in common_paths(abi_tree, req_tree):
                self.move_to_common(abi, path)
                tree_remove_path(abi_tree, path)

    def merge_common(self, abi_trees):
        logger.debug("Merging ABIs")
        for path in common_paths(*abi_trees.values()):
            self.move_to_common(self.android_abis[0], path)
            for abi in self.android_abis[1:]:
                abi_path = join(self.target, abi, path)
                if isdir(abi_path):
                    rmtree(abi_path)
                else:
                    os.remove(abi_path)
                try:
                    os.removedirs(dirname(abi_path))
                except OSError:
                    pass  # Directory is not empty.

        # If an ABI directory ended up empty, os.removedirs or os.renames will have deleted it.
        for abi in self.android_abis:
            abi_dir = join(self.target, abi)
            if not exists(abi_dir):
                os.mkdir(abi_dir)

    def move_to_common(self, abi, filename):
        abi_filename, common_filename = [join(self.target, subdir, filename)
                                         for subdir in [abi, "common"]]
        if exists(common_filename):
            if isdir(common_filename):
                for sub_name in os.listdir(abi_filename):
                    self.move_to_common(abi, join(filename, sub_name))
            else:
                raise ValueError("File already exists: '{}'".format(common_filename))
        else:
            try:
                renames(abi_filename, common_filename)
            except OSError:
                # Depending on the OS and Python version, the exception message may not contain any
                # filenames.
                logger.error("Failed to rename '{}' to '{}'".format(abi_filename, common_filename))
                raise

    def platform_tag(self, abi):
        return "android_{}_{}".format(ABI_API_LEVELS[abi], re.sub(r"[-.]", "_", abi))

    def parse_args(self):
        class ReqFileAppend(argparse.Action):
            def __call__(self, parser, namespace, value, option_string=None):
                getattr(namespace, self.dest).extend(["-r", value])

        ap = argparse.ArgumentParser()
        ap.add_argument("--target", metavar="DIR", type=abspath, required=True)
        ap.add_argument("--android-abis", metavar="ABI", nargs="+", required=True)

        # Passing the requirements this way ensures their order is maintained on the pip install
        # command line, which may be significant because of pip's simple-minded dependency
        # resolution (https://github.com/pypa/pip/issues/988).
        ap.set_defaults(reqs=[])
        ap.add_argument("--req", metavar="SPEC_OR_WHEEL", dest="reqs", action="append")
        ap.add_argument("--req-file", metavar="FILE", dest="reqs", action=ReqFileAppend)

        ap.add_argument("pip_options", nargs="*")
        ap.parse_args(namespace=self)


ReqInfo = namedtuple("ReqInfo", ["dist", "tree", "is_pure"])


def tree_add_path(tree, path, value, force=False):
    dir_name, base_name = os.path.split(path)
    subtree = tree
    if dir_name:
        for name in re.split(r"[\\/]", dir_name):
            subtree = subtree.setdefault(name, {})
            assert isinstance(subtree, dict), path  # If `name` exists, it must be a directory.
    if not force:
        existing_value = subtree.get(base_name)
        if existing_value is not None:
            raise PathExistsError(path, existing_value)
    subtree[base_name] = value


def tree_remove_path(tree, path, ignore_missing=False):
    dir_name, base_name = os.path.split(path)
    subtree = tree
    try:
        if dir_name:
            for name in re.split(r"[\\/]", dir_name):
                subtree = subtree[name]
        del subtree[base_name]
    except KeyError:
        if not ignore_missing:
            raise ValueError("Path not found: " + path)


# Returns a list of paths which are recursively identical in all trees.
def common_paths(*trees):
    def process_subtrees(subtrees, prefix):
        for name in subtrees[0]:
            values = [t.get(name) for t in subtrees]
            if all(values[0] == v for v in values[1:]):
                result.append(join(prefix, name))
            elif all(isinstance(v, dict) for v in values):
                process_subtrees(values, join(prefix, name))

    result = []
    process_subtrees(trees, "")
    return result


def file_matches_record(filename, hash_str, size):
    if os.stat(filename).st_size != size:
        return False
    hash_algo, hash_expected = hash_str.split("=")
    with open(filename, "rb") as f:
        hash_actual = (urlsafe_b64encode(hashlib.new(hash_algo, f.read()).digest())
                       .decode("ASCII"))
    return hash_actual == hash_expected


# Saw intermittent "Access is denied" errors on Windows (#5425), so use the same strategy as
# pip does for rmtree.
@retry(wait_fixed=50, stop_max_delay=3000)
def renames(src, dst):
    os.renames(src, dst)


def config_logging(verbose):
    STDERR_THRESHOLD = logging.ERROR
    class StdoutFilter(object):
        def filter(self, record):
            return record.levelno < STDERR_THRESHOLD

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "root": {
            "level": logging.NOTSET,
            "handlers": ["stdout", "stderr"]
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "level": logging.DEBUG if verbose else logging.INFO, "filters": ["stdout"],
                "stream": sys.stdout, "formatter": "fmt"
            },
            "stderr": {
                "class": "logging.StreamHandler",
                "level": STDERR_THRESHOLD,
                "stream": sys.stderr, "formatter": "fmt"
            }
        },
        "filters": {
            "stdout": {"()": StdoutFilter}
        },
        "formatters": {
            "fmt": {
                "format": ("%(asctime)s Chaquopy: %(message)s" if verbose
                           else "Chaquopy: %(message)s"),
                "datefmt": "%H:%M:%S"
            }
        },
    })


class PathExistsError(ValueError):
    def __init__(self, path, value):
        ValueError.__init__(self, "{} with value {}".format(path, value))
        self.path = path
        self.existing_value = value


# This is what bdist_wheel does both for wheel filenames and .dist-info directory names.
# NOTE: this is not entirely equivalent to the specifications in PEP 427 and PEP 376.
def normalize_name_wheel(name):
    return re.sub(r"[^A-Za-z0-9.]+", '_', name)


if __name__ == "__main__":
    PipInstall().main()
