# Copyright (C) 2019-2021 Intel Corporation
#
# SPDX-License-Identifier: MIT

import argparse
import logging as log
import os
import os.path as osp
import shutil

from datumaro.components.project import Environment
from datumaro.util import error_rollback

from ..util import CliException, MultilineFormatter, add_subparser
from ..util.project import load_project, \
    generate_next_name, generate_next_file_name


def build_add_parser(parser_ctor=argparse.ArgumentParser):
    builtins = sorted(Environment().launchers)

    parser = parser_ctor(help="Add model to project",
        description="""
            Registers an executable model into a project. A model requires
            a launcher to be executed. Each launcher has its own options, which
            are passed after '--' separator, pass '-- -h' for more info.
            |n
            List of builtin launchers: %s
        """ % ', '.join(builtins),
        formatter_class=MultilineFormatter)

    parser.add_argument('_positionals', nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS) # workaround for -- eaten by positionals
    parser.add_argument('url', nargs='?', help="URL to the model data")
    parser.add_argument('-n', '--name', default=None,
        help="Name of the model to be added (default: generate automatically)")
    parser.add_argument('-l', '--launcher', required=True,
        help="Model launcher")
    parser.add_argument('--no-check', action='store_true',
        help="Skip model availability checking")
    parser.add_argument('-p', '--project', dest='project_dir', default='.',
        help="Directory of the project to operate on (default: current dir)")
    parser.add_argument('extra_args', nargs=argparse.REMAINDER, default=None,
        help="Additional arguments for converter (pass '-- -h' for help)")
    parser.set_defaults(command=add_command)

    return parser

@error_rollback('on_error', implicit=True)
def add_command(args):
    has_sep = '--' in args._positionals
    if has_sep:
        pos = args._positionals.index('--')
    else:
        pos = 1
    args.url = (args._positionals[:pos] or [''])[0]
    args.extra_args = args._positionals[pos + has_sep:]

    project = load_project(args.project_dir)

    name = args.name
    if name:
        if name in project.config.models:
            raise CliException("Model '%s' already exists" % name)
    else:
        name = generate_next_name(list(project.models),
            'model', sep='-', default=0)

    try:
        launcher = project.env.launchers[args.launcher]
    except KeyError:
        raise CliException("Launcher '%s' is not found" % args.launcher)

    cli_plugin = getattr(launcher, 'cli_plugin', launcher)
    model_args = {}
    if args.extra_args:
        model_args = cli_plugin.parse_cmdline(args.extra_args)

    if args.url and args.copy:
        raise CliException("Can't specify both 'url' and 'copy' args, "
            "'copy' is only applicable for local paths.")
    elif args.copy:
        log.info("Copying model data")

        model_dir = project.models.model_dir(name)
        os.makedirs(model_dir, exist_ok=False)
        on_error.do(shutil.rmtree, model_dir, ignore_errors=True)

        try:
            cli_plugin.copy_model(model_dir, model_args)
        except (AttributeError, NotImplementedError):
            log.error("Can't copy: copying is not available for '%s' models. "
                "The model will be used as a local-only.",
                args.launcher)
            model_dir = ''
    else:
        model_dir = args.url

    project.models.add(name, {
        'url': model_dir,
        'launcher': args.launcher,
        'options': model_args,
    })
    on_error.do(project.models.remove, name, force=True, keep_data=False,
        ignore_errors=True)

    if not args.no_check:
        log.info("Checking the model...")
        project.models.make_executable_model(name)

    project.save()

    log.info("Model '%s' with launcher '%s' has been added to project",
        name, args.launcher)

    return 0

def build_remove_parser(parser_ctor=argparse.ArgumentParser):
    parser = parser_ctor(help="Remove model from project",
        description="Remove a model from a project")

    parser.add_argument('name',
        help="Name of the model to be removed")
    parser.add_argument('-p', '--project', dest='project_dir', default='.',
        help="Directory of the project to operate on (default: current dir)")
    parser.set_defaults(command=remove_command)

    return parser

def remove_command(args):
    project = load_project(args.project_dir)

    project.remove_model(args.name)
    project.save()

    return 0

def build_run_parser(parser_ctor=argparse.ArgumentParser):
    parser = parser_ctor(help="Launches model inference",
        description="Launches model inference on a project target.")

    parser.add_argument('target', nargs='?', default='project',
        help="Project target to launch inference on (default: project)")
    parser.add_argument('-o', '--output-dir', dest='dst_dir',
        help="Directory to save output (default: auto-generated)")
    parser.add_argument('-m', '--model', dest='model_name', required=True,
        help="Model to apply to the project")
    parser.add_argument('-p', '--project', dest='project_dir', default='.',
        help="Directory of the project to operate on (default: current dir)")
    parser.add_argument('--overwrite', action='store_true',
        help="Overwrite output dorectory if exists")
    parser.set_defaults(command=run_command)

    return parser

def run_command(args):
    project = load_project(args.project_dir)

    dst_dir = args.dst_dir
    if dst_dir:
        if not args.overwrite and osp.isdir(dst_dir) and os.listdir(dst_dir):
            raise CliException("Directory '%s' already exists "
                "(pass --overwrite overwrite)" % dst_dir)
    else:
        dst_dir = generate_next_file_name('%s-inference' % \
            project.config.project_name)

    project.make_dataset(args.target).run_model(
        save_dir=osp.abspath(dst_dir),
        model=args.model_name)

    log.info("Inference results have been saved to '%s'" % dst_dir)

    return 0

def build_info_parser(parser_ctor=argparse.ArgumentParser):
    parser = parser_ctor()

    parser.add_argument('-n', '--name',
        help="Model name")
    parser.add_argument('-v', '--verbose', action='store_true',
        help="Show details")
    parser.add_argument('-p', '--project', dest='project_dir', default='.',
        help="Directory of the project to operate on (default: current dir)")
    parser.set_defaults(command=info_command)

    return parser

def info_command(args):
    project = load_project(args.project_dir)

    if args.name:
        model = project.get_model(args.name)
        print(model)
    else:
        for name, conf in project.config.models.items():
            print(name)
            if args.verbose:
                print(dict(conf))

def build_parser(parser_ctor=argparse.ArgumentParser):
    parser = parser_ctor()

    subparsers = parser.add_subparsers()
    add_subparser(subparsers, 'add', build_add_parser)
    add_subparser(subparsers, 'remove', build_remove_parser)
    add_subparser(subparsers, 'run', build_run_parser)
    add_subparser(subparsers, 'info', build_info_parser)

    return parser
