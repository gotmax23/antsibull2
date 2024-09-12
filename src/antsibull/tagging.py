# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or
# https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2022 Maxwell G <gotmax@e.email>

"""
Validate that collections tag their releases in their respective git repositories
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncGenerator, Collection
from typing import TYPE_CHECKING, TextIO, TypedDict

import asyncio_pool  # type: ignore[import]
from antsibull_core import app_context
from antsibull_core.dependency_files import DepsFile
from antsibull_core.logging import log
from antsibull_core.schemas.collection_meta import (
    CollectionMetadata,
    CollectionsMetadata,
)
from antsibull_fileutils.yaml import load_yaml_file, store_yaml_file

if TYPE_CHECKING:
    from typing_extensions import NotRequired

TAG_MATCHER: re.Pattern[str] = re.compile(r"^.*refs/tags/(.*)$")
TAG_VERSION_REGEX: re.Pattern[str] = re.compile(r"^v?(.*)$")
mlog = log.fields(mod=__name__)


def validate_tags_file_command() -> int:
    """
    Ensure that collection versions in an Ansible release are tagged in
    collections' respective git repositories. This validates the tag file
    generated by the 'validate-tags' subcommand.
    """
    app_ctx = app_context.app_ctx.get()
    ignores = _get_ignores(app_ctx.extra["ignore"], app_ctx.extra["ignores_file"])
    tag_data = load_yaml_file(app_ctx.extra["tags_file"])
    return _print_validation_errors(
        tag_data,
        ignores,
        app_ctx.extra["error_on_useless_ignores"],
    )


def validate_tags_command() -> int:
    """
    Ensure that collection versions in an Ansible release are tagged in
    collections' respective git repositories.
    This retrieves a list of tags from the repository of each collection,
    optionally saves the retrieved tag data to a file,
    and then verifies the data.
    """
    app_ctx = app_context.app_ctx.get()
    ignores = _get_ignores(app_ctx.extra["ignore"], app_ctx.extra["ignores_file"])
    tag_data = asyncio.run(
        get_collections_tags(app_ctx.extra["data_dir"], app_ctx.extra["deps_file"])
    )
    if app_ctx.extra["output"]:
        store_yaml_file(app_ctx.extra["output"], tag_data)
    return _print_validation_errors(
        tag_data,
        ignores,
        app_ctx.extra["error_on_useless_ignores"],
    )


def _print_validation_errors(
    tag_data: dict[str, CollectionTagData],
    ignores: Collection[str] = (),
    error_on_useless_ignores: bool = True,
) -> int:
    """
    This takes the tag_data and prints any validation errors to stderr.
    """
    errors = validate_tags(tag_data, ignores, error_on_useless_ignores)
    if not errors:
        return 0
    for error in errors:
        print(error, file=sys.stderr)
    return 1


def _get_ignores(ignores: Collection[str], ignore_fp: TextIO | None) -> set[str]:
    ignores = set(ignores)
    if ignore_fp:
        ignores.update(
            line.strip()
            for line in ignore_fp
            if line.strip() and not line.startswith("#")
        )
    return ignores


##############
# Library code
##############


def validate_tags(
    tag_data: dict[str, CollectionTagData],
    ignores: Collection[str] = (),
    error_on_useless_ignores: bool = True,
) -> list[str]:
    """
    Validate that each collection in tag_data has a repository and a tag
    associated with it. Return a list of validation errors.

    :param tag_data: A tag data dictionary as returned by `get_collections_tags`
    :param ignores: A list of collection names for which to ignore errors
    :param error_on_useless_ignores: Whether to error for useless ignores
    """
    errors = []
    ignore_set = set(ignores)
    for name, data in tag_data.items():
        version = data["version"]
        if name in ignore_set:
            ignore_set.remove(name)
            if data["repository"] and data["tag"] and error_on_useless_ignores:
                errors.append(
                    f"useless ignore {name!r}: {name} {version} is properly tagged"
                )
        elif not data["repository"]:
            errors.append(
                f"{name}'s repository is not specified at all in collection-meta.yaml"
            )
        elif not data["tag"]:
            errors.append(f'{name} {version} is not tagged in {data["repository"]}')
    if ignore_set and error_on_useless_ignores:
        for name in ignore_set:
            errors.append(
                f"invalid ignore {name!r}: {name} does not match any collection"
            )
    return errors


class CollectionTagData(TypedDict):
    version: str
    repository: str | None
    tag: str | None
    collection_directory: NotRequired[str]


async def get_collections_tags(
    data_dir: str, deps_filename: str
) -> dict[str, CollectionTagData]:
    """
    Iterate over the collections in a CollectionsMetadata file,
    retrieve their tags, and return a dictionary
    of collection names mapped to dictionaries containing
    each collection's 'version' and 'repository' from a DepsFile
    and the 'tag' that matches.
    """
    lib_ctx = app_context.lib_ctx.get()

    deps_filename = os.path.join(data_dir, deps_filename)
    deps_data = DepsFile(deps_filename).parse()
    meta_data = CollectionsMetadata.load_from(data_dir)

    async with asyncio_pool.AioPool(size=lib_ctx.thread_max) as pool:
        collection_tags = {}
        for name, data in meta_data.collections.items():
            collection_tags[name] = pool.spawn_n(
                _get_collection_tags(deps_data.deps[name], data, name)
            )
        collection_tags = {name: await data for name, data in collection_tags.items()}
        return collection_tags


async def _get_collection_tags(
    version: str, meta_data: CollectionMetadata, name: str
) -> CollectionTagData:
    flog = mlog.fields(func="_get_collection_tags")
    repository = meta_data.repository
    data: CollectionTagData = {
        "version": version,
        "repository": repository,
        "tag": None,
    }
    if meta_data.collection_directory:
        data["collection_directory"] = meta_data.collection_directory
    if not repository:
        flog.debug("'repository' is None. Exitting...")
        return data
    tag_version_regex: re.Pattern[str] | None = None
    if meta_data.tag_version_regex:
        try:
            tag_version_regex = re.compile(meta_data.tag_version_regex)
        except re.error as err:
            flog.fields(err=err, collection=name).error(
                f"{tag_version_regex} is an invalid regex"
            )
            return data
    async for tag in _get_tags(repository):
        if _normalize_tag(tag, tag_version_regex) == version:
            data["tag"] = tag
            break
    return data


async def _get_tags(repository) -> AsyncGenerator[str, None]:
    flog = mlog.fields(func="_get_tags")
    args = (
        "git",
        "ls-remote",
        "--refs",
        "--tags",
        repository,
    )
    flog.debug(f"Running {args}")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # This makes it so git doesn't ask for a password when a repository
        # is inaccessible.
        env={"GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await proc.communicate()
    flog.fields(stderr=stderr, returncode=proc.returncode).debug("Ran git ls-remote")
    if proc.returncode != 0:
        flog.error(f"Failed to fetch tags for {repository}")
        return
    tags: list[str] = stdout.decode("utf-8").splitlines()
    if not tags:
        flog.warning(f"{repository} does not have any tags")
        return
    for tag in tags:
        match = TAG_MATCHER.match(tag)
        if match:
            yield match.group(1)
        else:
            flog.debug(f"git ls-remote output line skipped: {tag}")


def _normalize_tag(tag: str, regex: re.Pattern[str] | None) -> str | None:
    regex = regex or TAG_VERSION_REGEX
    if match := regex.match(tag):
        return match.group(1)
    return tag
